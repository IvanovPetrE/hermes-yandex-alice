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


@dataclass
class Op:
    t: float
    op: str   # start | complete | send | ask
    chat: str
    mid: Optional[str] = None
    content: str = ""
    reply_to: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class Script:
    """Ответ скриптованного агента выбирается по токену `[tNN]` в последней реплике пользователя."""

    def __init__(self, text_cls) -> None:
        self._text_cls = text_cls
        self.queues: dict[str, list] = {}
        self.fallback: dict[str, Any] = {}
        self.seen: list[str] = []

    def add(self, token: str, *responses, fallback=None) -> None:
        self.queues.setdefault(token, []).extend(responses)
        if fallback is not None:
            self.fallback[token] = fallback

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
        for token, queue in self.queues.items():
            if f"[{token}]" in text:
                self.seen.append(token)
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
    wrap("on_processing_start", lambda b: journal.append(Op(now(), "start", str(b["event"].source.chat_id),
                                                            mid=b["event"].message_id)))
    wrap("on_processing_complete", lambda b: journal.append(Op(now(), "complete", str(b["event"].source.chat_id),
                                                               mid=b["event"].message_id)))


class World:
    """Один шлюз Hermes с нашим плагином и скриптованным агентом на группу (процесс на группу)."""

    def __init__(self, config_extra: str = "") -> None:
        self.config_extra = config_extra
        self.journal: list = []
        self.errors: dict = {}
        self.voice: dict = {}   # сценарий -> ответы Алисы подряд
        self.state: dict = {}
        self._n = 0

    def run(self, scenarios) -> None:
        asyncio.run(self._run(scenarios))

    async def _run(self, scenarios) -> None:
        home = temp_home()
        from tests.fakes.fake_llm_provider import FakeLLMServer, Text, write_hermes_home

        self.script = Script(Text)
        llm = FakeLLMServer(self.script)
        llm.start()
        try:
            self.port, self.secret = free_port(), secrets.token_hex(32)
            write_hermes_home(home, llm.base_url, extra_config=(
                "plugins:\n  enabled:\n    - yandex-alice\n"
                f"platforms:\n  {PLATFORM}:\n    enabled: true\n    extra:\n      port: {self.port}\n" + self.config_extra))
            cfg = home / "config.yaml"
            text = cfg.read_text(encoding="utf-8")
            if text.count("\nagent:\n") != 1:
                raise AssertionError("expected exactly one agent: section")
            cfg.write_text(text.replace("\nagent:\n", "\nagent:\n  auto_recovery_cycles: 0\n", 1), encoding="utf-8")
            with open(home / ".env", "a", encoding="utf-8") as env:   # до импорта gateway.run: он читает .env при загрузке
                env.write(f"ALICE_PATH_SECRET={self.secret}\nALICE_SKILL_ID={SKILL}\nALICE_OWNER_USER_ID={OWNER}\n")
            install_plugin(home)
            await self._gateway(scenarios)
        finally:
            llm.stop()

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

    async def say(self, text: str, *, confirm: bool = False) -> str:
        import aiohttp

        self._n += 1
        request: dict = {"original_utterance": text, "command": text, "type": "SimpleUtterance"}
        if confirm:
            request["nlu"] = {"intents": {"YANDEX.CONFIRM": {}}}
        body = {"request": request, "version": "1.0",
                "session": {"session_id": "compat-session", "message_id": self._n, "skill_id": SKILL, "new": False,
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
