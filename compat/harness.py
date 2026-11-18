"""Обвязка тестов совместимости (этап 6, срез «б») — только для `compat/`, в `yandex-alice/` не переносится.

Здесь разрешена тестовая обвязка Hermes (`gateway.run`, `tests.fakes.*`), которой нет в `public-api.md`: она поднимает
настоящий шлюз. Верхний уровень модуля Hermes не импортирует — импорты внутри функций, чтобы модуль грузился и без Hermes.
Запуск — только через `run.sh` (временные HOME/HERMES_HOME, `env -i`).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import secrets
import shutil
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = ROOT / "yandex-alice"
PLUGIN_NAME = "yandex-alice"


def temp_home() -> Path:
    """HERMES_HOME прогона; отказ, если он не во временном каталоге `hya-compat-*` (живой дом трогать нельзя)."""
    home = Path(os.environ["HERMES_HOME"]).resolve()
    if not any(p.name.startswith("hya-compat-") for p in home.parents):
        raise SystemExit(f"refusing: HERMES_HOME {home} is not under a hya-compat-* temp dir")
    return home


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def install_plugin(home: Path) -> Path:
    """Копия плагина в `<home>/plugins/yandex-alice` — как ставит пользователь."""
    dest = home / "plugins" / PLUGIN_NAME
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(PLUGIN_SRC, dest, ignore=shutil.ignore_patterns("__pycache__"))
    return dest


# --- шлюз: журнал, скриптованный агент, реплики Алисы -----------------------------------------------------------------

PLATFORM = "yandex_alice"
SKILL, OWNER = "compat-skill", "compat-owner"
# Ответы «ещё думаю» — литералы, не константы плагина: тест фиксирует то, что слышит владелец.
WAITING = ("Вопрос принят. Спросите ответ позже или готовы подождать?", "Ещё не готово. Подождёте?",
           "Всё ещё работаю. Подождёте?", "Пока нет. Ждём дальше?")
TURN_TIMEOUT = 90.0
BIG_TOKENS = 125_000   # > 0.85 × окна 128k поддельной модели: заставляет Hermes сжимать беседу


@dataclass
class Op:
    t: float
    op: str   # start | complete | send | ask
    chat: str
    mid: Optional[str] = None
    content: str = ""
    reply_to: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    pid: int = 0   # процесс шлюза (журнал дочерних шлюзов общий на дом)


RESUME_MARK = "[System note: The previous turn was interrupted"   # заметка автопродолжения Hermes (run.py)


class Script:
    """Ответ скриптованного агента выбирается по токену `[tNN]` в последней реплике пользователя; заметка
    автопродолжения (`RESUME_MARK`) — отдельный ответ `resume`. Токен можно «подержать» до события (`hold`)."""

    def __init__(self, text_cls) -> None:
        self._text_cls = text_cls
        self.queues: dict[str, list] = {}
        self.fallback: dict[str, Any] = {}
        self.seen: list[str] = []
        self.holds: dict[str, Any] = {}
        self.resume_notes: list[str] = []   # последние реплики пользователя с заметкой автопродолжения
        self.resume_hold = None             # threading.Event: подержать ответ на заметку
        self.resume_text = "Ответ после перезапуска."

    def add(self, token: str, *responses, fallback=None) -> None:
        self.queues.setdefault(token, []).extend(responses)
        if fallback is not None:
            self.fallback[token] = fallback

    def hold(self, token: str):
        """Событие, пока не взведено, держит ответ на токен (поток поддельного LLM; 300 с — предел страховки)."""
        import threading

        self.holds[token] = threading.Event()
        return self.holds[token]

    @staticmethod
    def _text_of(content) -> str:
        if isinstance(content, list):
            return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
        return str(content or "")

    @staticmethod
    def _last_user_text(body: dict) -> str:
        for msg in reversed(body.get("messages") or []):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
            return str(content or "")
        return ""

    def __call__(self, record: dict):
        text = self._last_user_text(record.get("body") or {})
        if RESUME_MARK in text:   # раньше токенов: после SIGKILL заметка идёт в одной реплике с исходным вопросом
            self.seen.append("resume")
            self.resume_notes.append(text)
            if self.resume_hold is not None:
                self.resume_hold.wait(300)
            return self._text_cls(self.resume_text)
        for token, queue in self.queues.items():
            if f"[{token}]" in text:
                self.seen.append(token)
                if token in self.holds:
                    self.holds[token].wait(300)
                if queue:
                    return queue.pop(0)
                return self.fallback.get(token) or self._text_cls(f"default {token}")
        self.seen.append("?")
        return self._text_cls("unscripted")


def instrument(adapter, journal: list) -> None:
    """Журнал вызовов шлюза к адаптеру; вызывает оригинал, поведение адаптера не меняется."""
    def wrap(name: str, record) -> None:
        orig = getattr(adapter, name)
        sig = inspect.signature(orig)

        async def wrapper(*a, **k):
            record(sig.bind(*a, **k).arguments)
            return await orig(*a, **k)
        setattr(adapter, name, wrapper)

    now = time.monotonic
    wrap("send", lambda b: journal.append(Op(now(), "send", str(b["chat_id"]), content=b.get("content") or "",
                                             reply_to=None if b.get("reply_to") is None else str(b["reply_to"]),
                                             metadata=dict(b.get("metadata") or {}))))
    wrap("handle_message", lambda b: journal.append(Op(now(), "ask", str(b["event"].source.chat_id),
                                                       mid=b["event"].message_id, content=b["event"].text)))
    wrap("on_processing_start", lambda b: journal.append(Op(
        now(), "start", str(b["event"].source.chat_id), mid=b["event"].message_id,
        metadata={"internal": bool(getattr(b["event"], "internal", False))})))
    wrap("on_processing_complete", lambda b: journal.append(Op(now(), "complete", str(b["event"].source.chat_id),
                                                               mid=b["event"].message_id)))


class World:
    """Один шлюз Hermes с нашим плагином и скриптованным агентом на группу (процесс на группу)."""

    def __init__(self, config_extra: str = "", route_profile: Optional[str] = None,
                 target_extra: str = "", platform_lines: str = "", home: Optional[Path] = None,
                 port: Optional[int] = None) -> None:
        """`route_profile` — имя профиля-цели маршрута `yandex_alice → профиль` (раскладка A2: плагин по симлинку и в
        цели); `target_extra` — добавка в config цели. Без профиля голос идёт в профиле по умолчанию.
        `platform_lines` — строки в `platforms.yandex_alice` (отступ 4); `home`/`port` — свой дом и порт сцены."""
        self.config_extra = config_extra
        self.route_profile = route_profile
        self.target_extra = target_extra
        self.platform_lines = platform_lines
        self.home_override = home
        self.port_override = port
        self.target_home: Optional[Path] = None
        self.journal: list = []
        self.errors: dict = {}
        self.voice: dict = {}   # сценарий -> ответы Алисы подряд
        self.state: dict = {}
        self._n = 0

    def run(self, scenarios) -> None:
        asyncio.run(self._run(scenarios))

    async def _run(self, scenarios) -> None:
        from tests.fakes.fake_llm_provider import FakeLLMServer, Text

        self.script = Script(Text)
        llm = FakeLLMServer(self.script, prompt_tokens_fn=self._tokens)
        llm.start()
        try:
            self.prepare(llm.base_url)
            await self._gateway(scenarios)
        finally:
            llm.stop()

    @property
    def home(self) -> Path:
        return self.home_override or temp_home()

    def prepare(self, llm_url: str) -> None:
        """Дом шлюза: config, `.env`, плагин (и профиль-цель с симлинком, если есть маршрут)."""
        from tests.fakes.fake_llm_provider import write_hermes_home

        home = self.home
        self.port, self.secret = self.port_override or free_port(), secrets.token_hex(32)
        routing = ""
        if self.route_profile:
            routing = ("gateway:\n  multiplex_profiles: true\n  loop_watchdog: false\n  profile_routes:\n"
                       f"    - name: alice-compat\n      platform: {PLATFORM}\n      profile: {self.route_profile}\n")
        write_hermes_home(home, llm_url, extra_config=(
            routing + "plugins:\n  enabled:\n    - yandex-alice\n"
            f"platforms:\n  {PLATFORM}:\n    enabled: true\n{self.platform_lines}"
            f"    extra:\n      port: {self.port}\n" + self.config_extra))
        self._patch_agent(home / "config.yaml")
        with open(home / ".env", "a", encoding="utf-8") as env:   # до импорта gateway.run: он читает .env при загрузке
            env.write(f"ALICE_PATH_SECRET={self.secret}\nALICE_SKILL_ID={SKILL}\nALICE_OWNER_USER_ID={OWNER}\n")
        plugin_dir = install_plugin(home)
        if self.route_profile:   # раскладка A2: профиль-цель получает тот же плагин по симлинку
            target = self.target_home = home / "profiles" / self.route_profile
            write_hermes_home(target, llm_url, extra_config=(
                "plugins:\n  enabled:\n    - yandex-alice\n" + self.target_extra))
            self._patch_agent(target / "config.yaml")
            (target / "plugins").mkdir(exist_ok=True)
            (target / "plugins" / PLUGIN_NAME).symlink_to(plugin_dir, target_is_directory=True)

    big = False   # True: запросы с токеном `[z…]` сообщают >0.85 окна 128k и толкают Hermes к сжатию

    def _tokens(self, body: dict) -> int:
        text = "\n".join(Script._text_of(m.get("content")) for m in body.get("messages") or [])
        return BIG_TOKENS if self.big and "[z" in text else 100

    def target_db_view(self) -> dict:
        """Что `state.db` профиля-цели (иначе — дома) хранит о голосовых сессиях: счётчик Hermes и число строк."""
        import sqlite3

        path = (self.target_home or self.home) / "state.db"
        if not path.exists():
            return {"state_db": "missing"}
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            sessions = []
            for sid, count, parent in con.execute("SELECT id, message_count, parent_session_id FROM sessions "
                                                  "WHERE source = ?", (PLATFORM,)):
                rows = con.execute("SELECT COUNT(*) FROM messages WHERE session_id = ?", (sid,)).fetchone()[0]
                sessions.append({"message_count": count, "rows": rows, "parent": parent})
            return {"sessions": sessions}
        finally:
            con.close()

    @staticmethod
    def _patch_agent(cfg: Path) -> None:
        text = cfg.read_text(encoding="utf-8")
        if text.count("\nagent:\n") != 1:
            raise AssertionError(f"expected exactly one agent: section in {cfg}")
        cfg.write_text(text.replace("\nagent:\n", "\nagent:\n  auto_recovery_cycles: 0\n", 1), encoding="utf-8")

    async def _gateway(self, scenarios) -> None:
        from gateway.config import Platform, load_gateway_config
        from gateway.run import GatewayRunner

        runner = GatewayRunner(load_gateway_config())
        if not await runner.start():
            raise AssertionError("GatewayRunner.start() returned False")
        try:
            self.adapter = runner.adapters.get(Platform(PLATFORM))
            if self.adapter is None:
                raise AssertionError("адаптер yandex_alice не поднят шлюзом")
            connected = self.adapter.is_connected   # в v0.23.2 свойство; метод тоже допускаем
            self.state["connected"] = bool(connected() if callable(connected) else connected)
            self.state["adapter_module"] = type(self.adapter).__module__
            instrument(self.adapter, self.journal)
            for name, fn in scenarios:
                try:
                    await asyncio.wait_for(fn(self), timeout=TURN_TIMEOUT * 2)
                except Exception as exc:   # записывается, проверяется тестом сценария
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(1.0)
        finally:
            await runner.stop()

    # --- Алиса ----------------------------------------------------------------------------------------------------

    async def say(self, text: str, *, confirm: bool = False, intents: tuple = (), new: bool = False,
                  session: str = "compat-session") -> str:
        import aiohttp

        self._n += 1
        request: dict = {"original_utterance": text, "command": text, "type": "SimpleUtterance"}
        names = (("YANDEX.CONFIRM",) if confirm else ()) + tuple(intents)
        if names:
            request["nlu"] = {"intents": {name: {} for name in names}}
        body = {"request": request, "version": "1.0",
                "session": {"session_id": session, "message_id": self._n, "skill_id": SKILL, "new": new,
                            "user": {"user_id": OWNER}}}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://127.0.0.1:{self.port}/{self.secret}",
                                    data=json.dumps(body, ensure_ascii=False).encode()) as resp:
                if resp.status != 200:
                    raise AssertionError(f"HTTP {resp.status}")
                data = await resp.json()
        if data["response"]["end_session"]:
            raise AssertionError("беседа закрыта: end_session=true")
        return data["response"]["text"]

    async def wait_seen(self, token: str, timeout: float = 30.0) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if token in self.script.seen:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"агент не получил запрос [{token}]")

    async def ask(self, name: str, text: str) -> str:
        """Вопрос и «да» на каждое «ещё думаю», пока не прозвучит что-то иное; все реплики — в `voice[name]`."""
        said = self.voice.setdefault(name, [])
        reply = await self.say(text)
        said.append(reply)
        for _ in range(20):
            if reply not in WAITING:
                return reply
            reply = await self.say("да", confirm=True)
            said.append(reply)
        raise AssertionError(f"{name}: ответа нет за 20 раундов")

    # --- журнал ---------------------------------------------------------------------------------------------------

    def turns(self) -> dict:
        """Ход по `message_id` старта: индексы start/complete и итоги (send с `reply_to` хода и notify=True)."""
        out: dict = {}
        for i, op in enumerate(self.journal):
            if op.op == "start":
                out.setdefault(op.mid, {"chat": op.chat, "start": i, "complete": None, "finals": []})
            elif op.op == "complete" and op.mid in out:
                out[op.mid]["complete"] = i
        for i, op in enumerate(self.journal):
            if (op.op == "send" and op.reply_to in out and op.metadata.get("notify") is True
                    and not op.metadata.get("_interim_send")):
                out[op.reply_to]["finals"].append(i)
        return out
