"""Prototype S4: exec approvals of the voice platform as Telegram cards pressed by the owner.

What it answers: can a ``kind: platform`` plugin living in the DEFAULT profile of a multiplexing
gateway (the future ``yandex_alice`` adapter), through the public plugin API only, show the owner a
Telegram card with "approve once" / "deny" buttons - sent by the default profile's bot - for an
exec approval raised in a turn routed to another profile; take ONLY the owner's press in its own
pattern-scoped handler and resolve exactly that request; and deny at once whenever the card cannot
be delivered (error, send deadline, approvals disabled)?

How: a REAL ``GatewayRunner`` with ``multiplex_profiles``: the default profile (the probe plugin
from ``plugin/``, platform ``yandex_alice``, a REAL Telegram adapter on Hermes' own Bot API
stand-in ``tests/fakes/platforms/telegram_standin``), ``p2`` - the route target of
``yandex_alice`` (``platform_toolsets.yandex_alice: [terminal]``, ``approvals.mode: manual``,
``timeout: 20``), ``p3`` - its own bot on a second stand-in. The agent is Hermes' scripted loopback
LLM (``tests/fakes/fake_llm_provider``): no model, keys or cost. The dangerous command is
``rm -r <run root>/victim/<scene>`` - the directory disappears only when the owner approved.

PROBE ONLY. ``gateway.run``, ``tests.*``, ``tools.approval.list_gateway_approvals`` and the runner's
private attributes are the harness, NOT the plugin API; the plugin itself imports only
``public-api.md`` modules. Run through ``run.sh`` (throwaway HOME / HERMES_HOME / lock dir).
``BookTests`` need no Hermes: ``cd prototypes/s4 && python -m unittest probe_s4 -k BookTests``.
Python >= 3.11.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
PLATFORM = "yandex_alice"
PLUGIN_NAME = "yandex-alice-s4"
P2, P3 = "p2", "p3"
OWNER_ID = "u-probe-owner"          # Alice user id of the owner (synthetic)
TG_OWNER, TG_STRANGER, TG_P3_USER = 111, 222, 333
TG_GROUP = -1001234
TG_WRONG_CHAT = 999999
# Synthetic, deliberately NOT in the Telegram token format (keeps secret scanners quiet).
TOKEN_DEFAULT = "100001:s4-default-bot"
TOKEN_P3 = "100003:s4-p3-bot"
P2_APPROVAL_TIMEOUT = 20
TURN_TIMEOUT = 90.0
SETTLE = 1.0
TOOLS_EAGER = "tools:\n  tool_search:\n    enabled: 'off'\n"
GROUPS: dict[str, dict[str, Any]] = {
    "A": dict(chat=TG_OWNER, owner=None),    # owner's private chat, no explicit owner id
    "B": dict(chat=TG_GROUP, owner=None),    # group chat without an owner id: approvals disabled
    "C": dict(chat=TG_GROUP, owner=TG_OWNER),
}


def _load_book():
    spec = importlib.util.spec_from_file_location("s4_approval_book", HERE / "plugin" / "approval_book.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


# --- the pure layer, no Hermes ---------------------------------------------------------------------


class BookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.b = _load_book()

    def book(self, chat: object = TG_OWNER, owner: object = None):
        n = iter(range(1, 1000))
        return self.b.ApprovalBook(chat, owner, clock=lambda: 0.0, new_token=lambda: f"tok{next(n)}",
                                   new_card_id=lambda: f"hya-card:{next(n)}")

    def open_card(self, book, chat="c1", choices=("once", "deny"), rid="r1"):
        return book.open("agent:p2:yandex_alice:dm:" + chat, rid, chat, choices, "card")

    def test_owner_from_private_chat(self) -> None:
        self.assertEqual(self.b.resolve_owner("111", ""), (111, 111))

    def test_group_without_owner_disables(self) -> None:
        self.assertEqual(self.b.resolve_owner("-1001234", None), (-1001234, None))
        self.assertFalse(self.book(TG_GROUP).enabled)

    def test_explicit_owner_wins(self) -> None:
        self.assertEqual(self.b.resolve_owner("-1001234", "111"), (-1001234, 111))
        self.assertEqual(self.b.resolve_owner("111", "222"), (111, 222))

    def test_garbage_disables(self) -> None:
        self.assertEqual(self.b.resolve_owner("", "111"), (None, None))
        self.assertEqual(self.b.resolve_owner("111", "x"), (111, None))
        self.assertFalse(self.book("chat").enabled)

    def test_no_request_id_no_card(self) -> None:
        book = self.book()
        self.assertIsNone(book.open("sk", None, "c1", ("once", "deny"), "t"))
        self.assertIsNone(book.open("sk", "", "c1", ("once", "deny"), "t"))
        self.assertIsNone(book.open("", "r1", "c1", ("once", "deny"), "t"))

    def test_press_checks_in_order(self) -> None:
        book = self.book()
        card = self.open_card(book)
        self.assertEqual(book.press("nope", "once", TG_OWNER, TG_OWNER)[0], self.b.STALE)
        self.assertEqual(book.press(card.token, "once", TG_OWNER, TG_WRONG_CHAT)[0], self.b.WRONG_CHAT)
        self.assertEqual(book.press(card.token, "once", TG_STRANGER, TG_OWNER)[0], self.b.NOT_OWNER)
        self.assertEqual(book.press(card.token, "once", TG_OWNER, TG_OWNER)[0], self.b.WAIT)
        book.mark_sent(card.card_id, 5)
        self.assertEqual(book.press(card.token, "always", TG_OWNER, TG_OWNER)[0], self.b.NOT_OFFERED)
        self.assertEqual(book.press(card.token, "once", TG_OWNER, TG_OWNER)[0], self.b.RESOLVE)
        self.assertEqual(book.press(card.token, "once", TG_OWNER, TG_OWNER)[0], self.b.STALE)

    def test_stranger_sees_no_state(self) -> None:
        book = self.book()
        card = self.open_card(book)
        book.fail(card.card_id, "deadline")
        self.assertEqual(book.press(card.token, "once", TG_STRANGER, TG_OWNER)[0], self.b.NOT_OWNER)

    def test_disabled_book_refuses_everyone(self) -> None:
        book = self.book(TG_GROUP)
        card = self.open_card(book)
        book.mark_sent(card.card_id, 5)
        self.assertEqual(book.press(card.token, "once", TG_OWNER, TG_GROUP)[0], self.b.NOT_OWNER)

    def test_deny_only_card(self) -> None:
        book = self.book()
        card = self.open_card(book, choices=("deny",))
        book.mark_sent(card.card_id, 5)
        self.assertEqual(book.press(card.token, "once", TG_OWNER, TG_OWNER)[0], self.b.NOT_OFFERED)

    def test_fail_and_sent_are_exclusive(self) -> None:
        book = self.book()
        a, c = self.open_card(book, "c1", rid="r1"), self.open_card(book, "c2", rid="r2")
        self.assertTrue(book.fail(a.card_id, "deadline"))
        self.assertFalse(book.mark_sent(a.card_id, 7))  # late delivery of a failed card
        self.assertTrue(book.mark_sent(c.card_id, 8))
        self.assertFalse(book.fail(c.card_id, "deadline"))

    def test_finish_gone_is_expired(self) -> None:
        book = self.book()
        card = self.open_card(book)
        book.mark_sent(card.card_id, 5)
        book.press(card.token, "once", TG_OWNER, TG_OWNER)
        self.assertEqual(book.finish(card.card_id, "once", 0).state, "expired")

    def test_awaiting_timeline(self) -> None:
        book = self.book()
        self.assertFalse(book.awaiting("c1"))
        a = self.open_card(book, rid="r1")
        self.assertTrue(book.awaiting("c1"))
        book.mark_sent(a.card_id, 5)
        b2 = self.open_card(book, rid="r2")
        book.press(a.token, "deny", TG_OWNER, TG_OWNER)
        book.finish(a.card_id, "deny", 1)
        self.assertTrue(book.awaiting("c1"))  # the second card still waits
        book.mark_sent(b2.card_id, 6)
        self.assertEqual([c.card_id for c in book.close_chat("c1")], [b2.card_id])
        self.assertFalse(book.awaiting("c1"))
        self.assertFalse(book.awaiting("c2"))

    def test_expire_only_open(self) -> None:
        book = self.book()
        card = self.open_card(book)
        self.assertIsNone(book.expire(card.card_id, "t"))
        book.mark_sent(card.card_id, 5)
        self.assertEqual(book.expire(card.card_id, "t").state, "expired")
        self.assertIsNone(book.expire(card.card_id, "t"))

    def test_render_escapes_and_cuts(self) -> None:
        text, whole = self.b.render_card("echo <b>&", "why <i>", False)
        self.assertTrue(whole)
        self.assertIn("echo &lt;b&gt;&amp;", text)
        self.assertIn("why &lt;i&gt;", text)
        text, whole = self.b.render_card("x" * 4000, "", True)
        self.assertFalse(whole)
        self.assertLess(len(text), 4096)
        self.assertEqual(self.b.offered_choices([("A", "once", ""), ("S", "session", ""), ("D", "deny", "")], False),
                         ("deny",))

    def test_offered_choices(self) -> None:
        rows = [("A", "once", ""), ("S", "session", ""), ("W", "always", ""), ("D", "deny", "")]
        self.assertEqual(self.b.offered_choices(rows, True), ("once", "deny"))
        self.assertEqual(self.b.offered_choices([("D", "deny", "")], True), ("deny",))
        self.assertEqual(self.b.offered_choices([], True), ("deny",))

    def test_callback_data_fits(self) -> None:
        import secrets
        data = self.b.callback_data("once", secrets.token_urlsafe(12))
        self.assertLessEqual(len(data.encode()), 64)
        self.assertEqual(self.b.parse_callback(data)[0], "once")
        self.assertIsNone(self.b.parse_callback("ea:once:1"))
        self.assertIsNone(self.b.parse_callback("hya:once:"))

    def test_full_command_block_rule(self) -> None:
        block = "```\n" + "x" * 3100 + "\n```"
        self.assertTrue(self.b.is_full_command_block(block, {"_interim_send": True}))
        self.assertFalse(self.b.is_full_command_block(block, {}))
        self.assertFalse(self.b.is_full_command_block("```\nls\n```", {"_interim_send": True}))
        self.assertFalse(self.b.is_full_command_block("Сейчас проверю." * 300, {"_interim_send": True}))


# --- harness (Hermes) ------------------------------------------------------------------------------


def _rss_mb() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def _guard_temp_home() -> Path:
    import pwd  # POSIX only

    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        raise SystemExit("refusing: HERMES_HOME is not set (run through run.sh)")
    home = Path(raw).resolve()
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if real_home == home or real_home in home.parents:
        raise SystemExit(f"refusing: HERMES_HOME {home} is inside the real home of this user")
    if not any(p.name.startswith("hya-s4-") for p in home.parents):
        raise SystemExit(f"refusing: HERMES_HOME {home} is not under a hya-s4-* temp dir")
    return home


def _text_of(content: Any) -> str:
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return str(content or "")


def _last_user_text(body: dict) -> str:
    for msg in reversed(body.get("messages") or []):
        if msg.get("role") == "user":
            return _text_of(msg.get("content"))
    return ""


def _is_card(params: dict) -> bool:
    return "hya:" in json.dumps(params.get("reply_markup"), ensure_ascii=False, default=str)


def _buttons(params: dict) -> list[dict]:
    markup = params.get("reply_markup")
    if isinstance(markup, str):
        try:
            markup = json.loads(markup)
        except ValueError:
            return []
    rows = (markup or {}).get("inline_keyboard") or [] if isinstance(markup, dict) else []
    return [dict(text=b.get("text"), data=b.get("callback_data")) for row in rows for b in row]


class Script:
    """FakeLLMServer responder keyed by a ``[token]`` in the latest user message."""

    def __init__(self, text_cls) -> None:
        self._text_cls = text_cls
        self.queues: dict[str, list] = {}
        self.seen: list[str] = []

    def add(self, token: str, *responses) -> None:
        self.queues.setdefault(token, []).extend(responses)

    def __call__(self, record: dict):
        text = _last_user_text(record.get("body") or {})
        for token, queue in self.queues.items():
            if f"[{token}]" in text:
                self.seen.append(token)
                return queue.pop(0) if queue else self._text_cls(f"default {token}")
        self.seen.append("?")
        return self._text_cls("unscripted")


def _standin_cls():
    from aiohttp import web
    from tests.fakes.platforms import telegram_standin as ts

    class S4Standin(ts.TelegramStandin):
        """Hermes' Bot API stand-in plus one fault it lacks: a card send that shows the card at once
        (it is in the chat and pressable) but holds the HTTP answer until ``release``."""

        def __init__(self, token: str) -> None:
            super().__init__(token)
            self.hold_cards = threading.Event()
            self.release = threading.Event()
            self.held: Optional[tuple[dict, dict]] = None

        async def _api(self, request):  # same dispatch as the base, with the hold in front
            method = request.match_info["method"]
            if request.match_info["token"] != self.token:
                return web.json_response({"ok": False, "error_code": 401, "description": "Unauthorized"},
                                         status=401)
            params = await self._params(request)
            if method == "getUpdates":
                result = await self._get_updates(params)
                self.record(method, params, result)
                return web.json_response({"ok": True, "result": result})
            fault = self.take_fault(method, params)
            if fault is not None:
                self.record(method, params, fault.body, faulted=True)
                return web.json_response(fault.body, status=fault.status)
            if method == "sendMessage" and self.hold_cards.is_set() and _is_card(params):
                msg = self._bot_message(params, text=ts._check_text(params))
                with self._lock:
                    self.held = (params, msg)
                while not self.release.is_set():
                    await asyncio.sleep(0.05)
                self.record(method, params, msg)
                return web.json_response({"ok": True, "result": msg})
            handler = getattr(self, f"_m_{method}", None)
            if handler is None:
                body = {"ok": False, "error_code": 404, "description": "Not Found"}
                self.record(method, params, body, faulted=True)
                return web.json_response(body, status=404)
            try:
                result = handler(params)
            except ts.BotApiError as exc:
                body = {"ok": False, "error_code": exc.code, "description": exc.description}
                self.record(method, params, body, faulted=True)
                return web.json_response(body, status=exc.code)
            self.record(method, params, result)
            return web.json_response({"ok": True, "result": result})

    return S4Standin


def _insert_into_agent(cfg: Path, lines: str) -> None:
    text = cfg.read_text(encoding="utf-8")
    if text.count("\nagent:\n") != 1:
        raise AssertionError(f"expected exactly one agent: section in {cfg}")
    cfg.write_text(text.replace("\nagent:\n", "\nagent:\n" + lines, 1), encoding="utf-8")


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _fakes():
    from tests.fakes import fake_llm_provider as f
    return f


class World:
    """One gateway (default + p2 + p3), one scripted LLM, two Bot API stand-ins, one group."""

    def __init__(self, group: str) -> None:
        self.group = group
        self.cfg = GROUPS[group]
        self.tg_chat = int(self.cfg["chat"])
        self.results: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self.harness: dict[str, Any] = {}
        self.rss_peak_mb = 0
        self.home = Path()
        self.validate: Optional[dict] = None

    # -- setup --

    def _write_homes(self, base_url: str) -> None:
        f = _fakes()
        home, p2, p3 = self.home, self.home / "profiles" / P2, self.home / "profiles" / P3
        t1, t2 = self.t1, self.t2
        f.write_hermes_home(home, base_url, extra_config=(
            "gateway:\n  multiplex_profiles: true\n  loop_watchdog: false\n  write_sessions_json: false\n"
            "  profile_routes:\n    - name: alice-probe\n"
            f"      platform: {PLATFORM}\n      profile: {P2}\n"
            f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n"
            f"platforms:\n  {PLATFORM}:\n    enabled: true\n"
            f"  telegram:\n    enabled: true\n    extra:\n      base_url: {t1.api_base}\n"
            f"      base_file_url: {t1.file_base}\n"
            "approvals:\n  mode: manual\n  timeout: 300\n" + TOOLS_EAGER))
        f.write_hermes_home(p2, base_url, extra_config=(
            f"platform_toolsets:\n  {PLATFORM}: [terminal]\n"
            f"approvals:\n  mode: manual\n  timeout: {P2_APPROVAL_TIMEOUT}\n"
            "display:\n  busy_input_mode: interrupt\n  tool_progress: all\n"
            "  interim_assistant_messages: true\n" + TOOLS_EAGER))
        f.write_hermes_home(p3, base_url, extra_config=(
            f"platforms:\n  telegram:\n    enabled: true\n    extra:\n      base_url: {t2.api_base}\n"
            f"      base_file_url: {t2.file_base}\n" + TOOLS_EAGER))
        for h in (home, p2, p3):
            _insert_into_agent(h / "config.yaml", "  auto_recovery_cycles: 0\n")
        owner_line = f"ALICE_TELEGRAM_OWNER_ID={self.cfg['owner']}\n" if self.cfg["owner"] is not None else ""
        _append(home / ".env", f"ALICE_OWNER_USER_ID={OWNER_ID}\nALICE_TELEGRAM_CHAT={self.tg_chat}\n"
                + owner_line + f"TELEGRAM_BOT_TOKEN={TOKEN_DEFAULT}\nTELEGRAM_ALLOWED_USERS={TG_OWNER}\n"
                "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1\n")
        _append(p3 / ".env", f"TELEGRAM_BOT_TOKEN={TOKEN_P3}\nTELEGRAM_ALLOWED_USERS={TG_P3_USER}\n"
                "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS=1\n")
        shutil.copytree(HERE / "plugin", home / "plugins" / PLUGIN_NAME,
                        ignore=shutil.ignore_patterns("__pycache__"))

    def _start_rss_watchdog(self) -> None:
        limit = int(os.environ.get("S4_RSS_LIMIT_MB") or 0)

        def loop() -> None:
            while True:
                rss = _rss_mb()
                self.rss_peak_mb = max(self.rss_peak_mb, rss)
                if limit and rss > limit:
                    print(f"\n!!! RSS {rss} MB > limit {limit} MB - probe aborted by the watchdog", flush=True)
                    os._exit(137)
                time.sleep(0.5)

        threading.Thread(target=loop, name="s4-rss-watchdog", daemon=True).start()

    # -- run --

    def run(self, scenarios) -> None:
        self.home = _guard_temp_home()
        self.root = self.home.parents[1]
        self.victims = self.root / "victim"
        self.victims.mkdir(parents=True, exist_ok=True)
        self._start_rss_watchdog()
        asyncio.run(self._main(scenarios))
        self._validate()

    async def _main(self, scenarios) -> None:
        from tests.fakes.fake_llm_provider import FakeLLMServer, Text

        cls = _standin_cls()
        self.t1, self.t2 = cls(TOKEN_DEFAULT), cls(TOKEN_P3)
        self.t1.start()
        self.t2.start()
        if self.tg_chat < 0:
            self.t1.chats[str(self.tg_chat)] = {"id": self.tg_chat, "type": "supergroup", "title": "Probe group"}
        self.script = Script(Text)
        self.llm = FakeLLMServer(self.script)
        self.llm.start()
        try:
            self._write_homes(self.llm.base_url)
            await self._run_gateway(scenarios)
        finally:
            self.t1.release.set()
            self.llm.stop()
            self.t1.stop()
            self.t2.stop()

    async def _run_gateway(self, scenarios) -> None:
        from gateway.config import Platform, load_gateway_config
        from gateway.run import GatewayRunner

        runner = GatewayRunner(load_gateway_config())
        if not await runner.start():
            raise AssertionError("GatewayRunner.start() returned False")
        try:
            self.alice = runner.adapters.get(Platform(PLATFORM))
            self.tg_default = runner.adapters.get(Platform("telegram"))
            p3map = (getattr(runner, "_profile_adapters", None) or {}).get(P3) or {}
            self.tg_p3 = p3map.get(Platform("telegram")) if isinstance(p3map, dict) else None
            if self.alice is None:
                raise AssertionError("no default-profile yandex_alice adapter")
            self.mod = sys.modules[type(self.alice).__module__]
            await self.wait(lambda: self.t1.calls_of("getUpdates"), "default bot polling", 60)
            p3_polling = True
            try:
                await self.wait(lambda: self.t2.calls_of("getUpdates"), "p3 bot polling", 20)
            except AssertionError:
                p3_polling = False
            self.harness.update(
                tg_default_id=id(self.tg_default) if self.tg_default else None,
                tg_p3_id=id(self.tg_p3) if self.tg_p3 else None, p3_polling=p3_polling,
                failed_platforms=sorted(str(k) for k in (getattr(runner, "_failed_platforms", None) or {})),
                plugin_home=self.mod.STATE["plugin_home"], default_home=str(self.home),
                p2_home=str(self.home / "profiles" / P2))
            for name, coro_fn in scenarios:
                try:
                    self.results[name] = await asyncio.wait_for(coro_fn(self), timeout=TURN_TIMEOUT * 2)
                except Exception as exc:  # recorded, asserted by the test of that scenario
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
                self.t1.hold_cards.clear()
                self.t1.release.set()
                await asyncio.sleep(SETTLE)
        finally:
            await runner.stop()

    def _validate(self) -> None:
        """``hermes plugins validate`` on the installed probe plugin: imports outside public-api.md."""
        try:
            from hermes_cli.plugin_validate import validate_plugin_dir

            report = validate_plugin_dir(self.home / "plugins" / PLUGIN_NAME, None)
            self.validate = {"ok": bool(report.ok), "warnings": [str(w) for w in report.warnings],
                             "checks": [[str(n), bool(ok), str(d or "")] for n, ok, d in report.checks]}
        except Exception as exc:  # noqa: BLE001 - reported, the test says it did not run
            self.validate = {"error": f"{type(exc).__name__}: {exc}"}

    # -- helpers --

    async def wait(self, pred, what: str, timeout: float = TURN_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = pred()
            if value:
                return value
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what} ({timeout:.0f}s)")

    def journal(self, op: Optional[str] = None, **match) -> list[dict]:
        return [r for r in list(self.mod.JOURNAL) if (op is None or r["op"] == op)
                and all(r.get(k) == v for k, v in match.items())]

    def make_victim(self, name: str) -> Path:
        path = self.victims / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "keep.txt").write_text("probe", encoding="utf-8")
        return path

    def card_calls(self) -> list:
        return [c for c in self.t1.calls_of("sendMessage") if _is_card(c.params)]

    async def start_turn(self, chat: str, mid: str, token: str) -> float:
        t0 = time.monotonic()
        await self.alice.probe_inject(f"probe [{token}] выполни", mid, chat, OWNER_ID)
        return t0

    async def wait_card(self, since: int) -> dict:
        def ready():
            calls = [c for c in self.card_calls()[since:] if not c.faulted]
            return calls[0] if calls else None

        call = await self.wait(ready, "an approval card")
        buttons = _buttons(call.params)
        return dict(message_id=(call.response or {}).get("message_id"), buttons=buttons,
                    data={b["data"].split(":")[1]: b["data"] for b in buttons},
                    chat_id=call.params.get("chat_id"), parse_mode=call.params.get("parse_mode"),
                    disable_notification=call.params.get("disable_notification"),
                    text_len=len(str(call.params.get("text") or "")), t=call.at)

    async def press(self, user: int, chat: int, message_id: Any, data: str) -> dict:
        t0 = time.monotonic()
        update = self.t1.callback(int(user), int(chat), int(message_id), data)
        qid = update["callback_query"]["id"]
        call = await self.wait(lambda: next((c for c in self.t1.calls_of("answerCallbackQuery")
                                             if str(c.params.get("callback_query_id")) == qid), None),
                               "the press answer", 40)
        return dict(text=call.params.get("text"), show_alert=call.params.get("show_alert"),
                    latency=round(call.at - t0, 3))

    async def wait_complete(self, mid: str, timeout: float = TURN_TIMEOUT) -> float:
        row = await self.wait(lambda: next(iter(self.journal("complete", mid=mid)), None), f"turn {mid}", timeout)
        return row["t"]

    def tool_results(self, token: str) -> list[str]:
        bodies = [r["body"] for r in list(self.llm.requests) if r.get("kind") == "main"
                  and f"[{token}]" in _last_user_text(r.get("body") or {})]
        if not bodies:
            return []
        return [_text_of(m.get("content")) for m in bodies[-1].get("messages") or [] if m.get("role") == "tool"]

    def finals(self, chat: str, mid: str) -> list[dict]:
        return [r for r in self.journal("send", chat=chat) if r["reply_to"] == mid and r["notify"] is True
                and not r["interim"]]

    def card_edits(self, message_id: Any) -> list[dict]:
        return [dict(text=str(c.params.get("text") or "")[-60:], has_markup="reply_markup" in c.params)
                for c in self.t1.calls_of("editMessageText") if str(c.params.get("message_id")) == str(message_id)]

    def events(self, chat: str) -> list[dict]:
        return [dict(t=round(e.t, 3), card=e.card_id, event=e.event, awaiting=e.awaiting)
                for e in list(self.alice.book.events) if e.alice_chat == chat]

    def session_key(self, chat: str) -> Optional[str]:
        rows = self.journal("prompt", chat=chat)
        return rows[-1]["session_key"] if rows else None

    def pending(self, chat: str) -> int:
        from tools.approval import list_gateway_approvals  # harness only, not public

        key = self.session_key(chat)
        return len(list_gateway_approvals(key)) if key else -1

    def dump(self) -> None:
        print(f"\n=== group {self.group}: cfg={self.cfg} rss_peak_mb={self.rss_peak_mb}")
        print(f"harness={json.dumps(self.harness, ensure_ascii=False, default=str)}")
        print(f"errors={self.errors}")
        for row in self.mod.JOURNAL:
            if row["op"] in ("wire", "connect", "prompt", "deny", "card_sent", "late", "press", "resolved",
                             "bot_pick", "edit", "complete", "edit_failed") or row.get("refused") \
                    or row.get("approval_prompt"):
                print(json.dumps({k: v for k, v in row.items() if k not in ("session_key",)},
                                 ensure_ascii=False, default=str))
        for name, res in self.results.items():
            print(f"--- {name}: {json.dumps(res, ensure_ascii=False, default=str)[:3000]}")
        print(f"validate={json.dumps(self.validate, ensure_ascii=False)[:2000]}")


# --- scenarios -------------------------------------------------------------------------------------


def _cmd(victim: Path, pad: int = 0, sep: str = " #") -> str:
    """``sep`` "; :" keeps a long command under Hermes' parser limit: separator-free commands over
    4096 chars are hardline-blocked (``approval_detection._command_parser_limit_exceeded``)."""
    return f"rm -r {victim}" + (f"{sep} " + "x" * pad if pad else "")


async def _turn_with_card(w: World, scene: str, *responses, pad: int = 0, sep: str = " #") -> dict:
    f = _fakes()
    victim = w.make_victim(scene)
    w.script.add(scene, f.ToolCall("terminal", {"command": _cmd(victim, pad, sep)}), *responses)
    since = len(w.card_calls())
    t0 = await w.start_turn("c-" + scene, "m-" + scene, scene)
    card = await w.wait_card(since)
    return dict(victim=victim, t0=t0, card=card, awaiting_at_card=w.alice.book.awaiting("c-" + scene))


def _summary(w: World, scene: str, ctx: dict, **extra) -> dict:
    chat, mid = "c-" + scene, "m-" + scene
    victim = ctx.get("victim")
    return dict(card=ctx.get("card"), awaiting_at_card=ctx.get("awaiting_at_card"),
                victim_exists=victim.exists() if victim else None, tool=w.tool_results(scene),
                finals=[r["content"] for r in w.finals(chat, mid)],
                approval_prompt_sends=[dict(reply_to=r["reply_to"], notify=r["notify"])
                                       for r in w.journal("send", chat=chat) if r["approval_prompt"]],
                refused_sends=[r["length"] for r in w.journal("send", chat=chat) if r["refused"]],
                prompts=[{k: r[k] for k in ("home", "command_len", "hermes_choices", "choices", "whole")}
                         for r in w.journal("prompt", chat=chat)],
                denies=[{k: r[k] for k in ("why", "count", "t")} for r in w.journal("deny")
                        if r["card"] in {e.card_id for e in w.alice.book.events if e.alice_chat == chat}],
                events=w.events(chat), complete_outcome=[r["outcome"] for r in w.journal("complete", chat=chat)],
                **extra)


async def sc01_allow(w: World) -> dict:
    f = _fakes()
    ctx = await _turn_with_card(w, "s01", f.Text("Готово: удалено."))
    ans = await w.press(TG_OWNER, w.tg_chat, ctx["card"]["message_id"], ctx["card"]["data"]["once"])
    awaiting_after_press = w.alice.book.awaiting("c-s01")
    await w.wait_complete("m-s01")
    non_card_edits = [r for r in w.journal("edit", chat="c-s01") if not r["card"]]
    return _summary(w, "s01", ctx, answer=ans, awaiting_after_press=awaiting_after_press,
                    card_edits=w.card_edits(ctx["card"]["message_id"]),
                    non_card_edits=len(non_card_edits),
                    interim_sends=len([r for r in w.journal("send", chat="c-s01") if r["interim"]]))


async def sc02_deny(w: World) -> dict:
    f = _fakes()
    ctx = await _turn_with_card(w, "s02", f.Text("Понял, не удаляю."))
    ans = await w.press(TG_OWNER, w.tg_chat, ctx["card"]["message_id"], ctx["card"]["data"]["deny"])
    await w.wait_complete("m-s02")
    return _summary(w, "s02", ctx, answer=ans, card_edits=w.card_edits(ctx["card"]["message_id"]))


async def sc03_stranger(w: World) -> dict:
    f = _fakes()
    ctx = await _turn_with_card(w, "s03", f.Text("Понял, не удаляю."))
    card = ctx["card"]
    stranger = await w.press(TG_STRANGER, w.tg_chat, card["message_id"], card["data"]["once"])
    wrong_chat = await w.press(TG_OWNER, TG_WRONG_CHAT, card["message_id"], card["data"]["once"])
    pending = w.pending("c-s03")
    edits_before_owner = len(w.card_edits(card["message_id"]))
    owner = await w.press(TG_OWNER, w.tg_chat, card["message_id"], card["data"]["deny"])
    await w.wait_complete("m-s03")
    return _summary(w, "s03", ctx, stranger=stranger, wrong_chat=wrong_chat, pending_after_refusals=pending,
                    edits_before_owner=edits_before_owner, owner=owner)


async def sc04_late(w: World) -> dict:
    s01, s02 = w.results.get("s01") or {}, w.results.get("s02") or {}
    if not s01.get("card") or not s02.get("card"):
        raise AssertionError("s01/s02 did not produce cards")
    again = await w.press(TG_OWNER, w.tg_chat, s01["card"]["message_id"], s01["card"]["data"]["once"])
    after_deny = await w.press(TG_OWNER, w.tg_chat, s02["card"]["message_id"], s02["card"]["data"]["once"])
    return dict(again=again, after_deny=after_deny, s02_victim_exists=(w.victims / "s02").exists())


async def sc05_timeout(w: World) -> dict:
    f = _fakes()
    ctx = await _turn_with_card(w, "s05", f.Text("Подтверждения нет, не удаляю."))
    t_done = await w.wait_complete("m-s05", timeout=P2_APPROVAL_TIMEOUT + 60)
    elapsed = round(t_done - ctx["card"]["t"], 1)
    edits = w.card_edits(ctx["card"]["message_id"])
    late = await w.press(TG_OWNER, w.tg_chat, ctx["card"]["message_id"], ctx["card"]["data"]["once"])
    hermes_edits = [{k: r[k] for k in ("card", "length", "head")} for r in w.journal("edit", chat="c-s05")]
    return _summary(w, "s05", ctx, wait_s=elapsed, card_edits=edits, hermes_edits=hermes_edits, late=late,
                    awaiting_end=w.alice.book.awaiting("c-s05"))


async def sc06a_rejected(w: World) -> dict:
    f = _fakes()
    fault = w.t1.fail("sendMessage", {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
                      status=400, match=_is_card)
    victim = w.make_victim("s06a")
    w.script.add("s06a", f.ToolCall("terminal", {"command": _cmd(victim)}), f.Text("Не получилось подтвердить."))
    await w.start_turn("c-s06a", "m-s06a", "s06a")
    await w.wait_complete("m-s06a")
    prompt = w.journal("prompt", chat="c-s06a")
    deny = [r for r in w.journal("deny") if r["why"] == "BadRequest"]
    gap = round(deny[-1]["t"] - prompt[-1]["t"], 3) if prompt and deny else None
    return _summary(w, "s06a", dict(victim=victim), fault_fired=fault.fired, prompt_to_deny_s=gap)


async def sc07_held(w: World) -> dict:
    """The card shows up in the chat at once, Telegram's answer is held past the 10 s deadline."""
    f = _fakes()
    w.t1.release.clear()
    w.t1.held = None
    w.t1.hold_cards.set()
    victim = w.make_victim("s07")
    w.script.add("s07", f.ToolCall("terminal", {"command": _cmd(victim)}), f.Text("Не получилось подтвердить."))
    await w.start_turn("c-s07", "m-s07", "s07")
    params, msg = await w.wait(lambda: w.t1.held, "the held card")
    data = {b["data"].split(":")[1]: b["data"] for b in _buttons(params)}
    during = await w.press(TG_OWNER, w.tg_chat, msg["message_id"], data["once"])
    await w.wait_complete("m-s07", timeout=60)
    w.t1.release.set()
    late = await w.wait(lambda: next(iter(w.journal("late")), None), "the late delivery", 30)
    after = await w.press(TG_OWNER, w.tg_chat, msg["message_id"], data["once"])
    w.t1.hold_cards.clear()
    prompt = w.journal("prompt", chat="c-s07")
    deny = [r for r in w.journal("deny") if r["why"] == "deadline"]
    return _summary(w, "s07", dict(victim=victim), during=during, after=after,
                    late={k: late[k] for k in ("delivered", "edited") if k in late},
                    prompt_to_deny_s=round(deny[-1]["t"] - prompt[-1]["t"], 2) if prompt and deny else None,
                    card_edits=w.card_edits(msg["message_id"]))


async def _sc08(w: World, scene: str, pad: int, sep: str = " #") -> dict:
    f = _fakes()
    ctx = await _turn_with_card(w, scene, f.Text("Понял, не удаляю."), pad=pad, sep=sep)
    card = ctx["card"]
    token = card["data"]["deny"].split(":")[2]
    forged = await w.press(TG_OWNER, w.tg_chat, card["message_id"], f"hya:once:{token}")
    owner = await w.press(TG_OWNER, w.tg_chat, card["message_id"], card["data"]["deny"])
    await w.wait_complete("m-" + scene)
    return _summary(w, scene, ctx, forged=forged, owner=owner)


async def sc08a_long(w: World) -> dict:
    return await _sc08(w, "s08a", 3200)


async def sc08b_longer(w: World) -> dict:
    return await _sc08(w, "s08b", 4200, sep="; :")


async def sc08c_parser_limit(w: World) -> dict:
    """Separator-free and over 4096 chars: Hermes blocks it outright, no approval is raised."""
    f = _fakes()
    victim = w.make_victim("s08c")
    w.script.add("s08c", f.ToolCall("terminal", {"command": _cmd(victim, 4200)}), f.Text("Не выполнено."))
    since = len(w.card_calls())
    await w.start_turn("c-s08c", "m-s08c", "s08c")
    await w.wait_complete("m-s08c")
    return _summary(w, "s08c", dict(victim=victim), cards=len(w.card_calls()) - since)


async def sc09_forged(w: World) -> dict:
    s01 = w.results.get("s01") or {}
    mid = (s01.get("card") or {}).get("message_id") or 1
    return dict(answer=await w.press(TG_OWNER, w.tg_chat, mid, "hya:once:" + "Z" * 16))


async def sc10_two(w: World) -> dict:
    f = _fakes()
    v1, v2 = w.make_victim("s10-1"), w.make_victim("s10-2")
    w.script.add("s10", f.ToolCall("terminal", {"command": _cmd(v1)}), f.ToolCall("terminal", {"command": _cmd(v2)}),
                 f.Text("Оба удалены."))
    since = len(w.card_calls())
    await w.start_turn("c-s10", "m-s10", "s10")
    c1 = await w.wait_card(since)
    a1 = w.alice.book.awaiting("c-s10")
    await w.press(TG_OWNER, w.tg_chat, c1["message_id"], c1["data"]["once"])
    c2 = await w.wait_card(since + 1)
    a2 = w.alice.book.awaiting("c-s10")
    await w.press(TG_OWNER, w.tg_chat, c2["message_id"], c2["data"]["once"])
    await w.wait_complete("m-s10")
    return _summary(w, "s10", {}, cards=[c1["message_id"], c2["message_id"]], awaiting=[a1, a2],
                    victims_exist=[v1.exists(), v2.exists()], awaiting_end=w.alice.book.awaiting("c-s10"))


async def sc11_busy_dm(w: World) -> dict:
    """A slow turn runs in the owner's private chat with the default bot when the card is pressed."""
    f = _fakes()
    w.script.add("s11dm", f.Text("Долгий ответ в личке. " * 4, chunk_chars=2, delay_per_chunk=0.15))
    w.t1.dm(TG_OWNER, "probe [s11dm] долго")
    await w.wait(lambda: "s11dm" in w.script.seen, "the DM turn to reach the LLM", 60)
    ctx = await _turn_with_card(w, "s11", f.Text("Готово: удалено."))
    t_press = time.monotonic()
    ans = await w.press(TG_OWNER, w.tg_chat, ctx["card"]["message_id"], ctx["card"]["data"]["once"])
    await w.wait_complete("m-s11")

    def dm_final():
        return next((c for c in w.t1.calls_of("sendMessage") if str(c.params.get("chat_id")) == str(TG_OWNER)
                     and "Долгий ответ" in str(c.params.get("text") or "") and not c.faulted), None)

    final = await w.wait(dm_final, "the DM final", 60)
    return _summary(w, "s11", ctx, answer=ans, dm_final_after_press_s=round(final.at - t_press, 2),
                    press_before_dm_final=(t_press + ans["latency"]) < final.at)


async def scB_disabled(w: World) -> dict:
    f = _fakes()
    victim = w.make_victim("b06")
    w.script.add("b06", f.ToolCall("terminal", {"command": _cmd(victim)}), f.Text("Не получилось подтвердить."))
    before = len(w.t1.calls_of("sendMessage"))
    await w.start_turn("c-b06", "m-b06", "b06")
    await w.wait_complete("m-b06")
    return _summary(w, "b06", dict(victim=victim), bot_sends=len(w.t1.calls_of("sendMessage")) - before,
                    connect=[{k: r[k] for k in ("enabled", "chat_set", "owner_set")} for r in w.journal("connect")])


async def scC_allow(w: World) -> dict:
    f = _fakes()
    ctx = await _turn_with_card(w, "c01", f.Text("Готово: удалено."))
    ans = await w.press(TG_OWNER, TG_GROUP, ctx["card"]["message_id"], ctx["card"]["data"]["once"])
    await w.wait_complete("m-c01")
    return _summary(w, "c01", ctx, answer=ans)


async def scC_stranger(w: World) -> dict:
    f = _fakes()
    ctx = await _turn_with_card(w, "c03", f.Text("Понял, не удаляю."))
    card = ctx["card"]
    stranger = await w.press(TG_STRANGER, TG_GROUP, card["message_id"], card["data"]["once"])
    pending = w.pending("c-c03")
    owner = await w.press(TG_OWNER, TG_GROUP, card["message_id"], card["data"]["deny"])
    await w.wait_complete("m-c03")
    return _summary(w, "c03", ctx, stranger=stranger, pending_after_refusal=pending, owner=owner)


SCENARIOS_A = [("s01", sc01_allow), ("s02", sc02_deny), ("s03", sc03_stranger), ("s04", sc04_late),
               ("s05", sc05_timeout), ("s06a", sc06a_rejected), ("s07", sc07_held), ("s08a", sc08a_long),
               ("s08b", sc08b_longer), ("s08c", sc08c_parser_limit), ("s09", sc09_forged), ("s10", sc10_two), ("s11", sc11_busy_dm)]
SCENARIOS_B = [("b06", scB_disabled)]
SCENARIOS_C = [("c01", scC_allow), ("c03", scC_stranger)]

TOAST_OWNER_ONLY = "Отвечать может только владелец"
TOAST_STALE = "Запрос неактуален"
TOAST_WAIT = "Подождите секунду и нажмите снова"


class _GroupBase(unittest.TestCase):
    GROUP = ""
    SCENARIOS: list = []
    world: World

    @classmethod
    def setUpClass(cls) -> None:
        cls.world = World(cls.GROUP)
        cls.world.run(cls.SCENARIOS)
        cls.world.dump()

    def res(self, name: str) -> dict:
        self.assertNotIn(name, self.world.errors, self.world.errors.get(name))
        return self.world.results[name]

    def assert_denied_by_plugin(self, r: dict) -> None:
        tool = " ".join(r["tool"])
        self.assertIn("denied by user", tool)
        self.assertIn(self.world.mod.DENY_REASON, tool)
        self.assertTrue(r["victim_exists"])
        # The text /approve fallback reaches the voice adapter, but never as the turn's final.
        self.assertTrue(all(not (p["notify"] is True) for p in r["approval_prompt_sends"]), r)
        self.assertEqual(r["finals"], ["Не получилось подтвердить."])

    def assert_owner_allowed(self, r: dict) -> None:
        self.assertEqual(r["answer"]["text"], "✅ Разрешено один раз")
        self.assertFalse(r["victim_exists"])
        self.assertEqual(r["finals"], ["Готово: удалено."])
        self.assertEqual(r["card"]["parse_mode"], "HTML")
        self.assertIn(r["card"]["disable_notification"], (None, False, "false"))

    def test_00_rss(self) -> None:
        print(f"rss_peak_mb={self.world.rss_peak_mb}")
        self.assertGreater(self.world.rss_peak_mb, 0)


class GroupA(_GroupBase):
    """Owner's private chat, no ALICE_TELEGRAM_OWNER_ID: the owner is the chat id."""

    GROUP = "A"
    SCENARIOS = SCENARIOS_A

    def test_01_factory_only_default_bot(self) -> None:
        h = self.world.harness
        wires = self.world.journal("wire")
        accepted = [r for r in wires if r["accepted"]]
        self.assertTrue(accepted, wires)
        self.assertTrue(all(r["adapter_id"] == h["tg_default_id"] for r in accepted), (wires, h))
        self.assertFalse(any(r["adapter_id"] == h["tg_p3_id"] for r in accepted), (wires, h))
        self.assertTrue(all(r["home"] == h["plugin_home"] for r in accepted))
        self.assertFalse([c for c in self.world.t2.calls_of("sendMessage") if _is_card(c.params)])
        print(f"wires={wires} harness={h}")

    def test_02_prompt_scope_and_request_ids(self) -> None:
        prompts = self.world.journal("prompt")
        self.assertTrue(prompts)
        self.assertTrue(all(p["request_id"] for p in prompts))
        self.assertTrue(all(p["session_key"].startswith(f"agent:{P2}:{PLATFORM}:") for p in prompts), prompts)
        print("prompt homes:", sorted({p["home"] for p in prompts}))

    def test_10_allow_once(self) -> None:
        r = self.res("s01")
        self.assert_owner_allowed(r)
        self.assertEqual([b["data"].split(":")[1] for b in r["card"]["buttons"]], ["once", "deny"])
        self.assertTrue(all(len(b["data"].encode()) <= 64 for b in r["card"]["buttons"]))
        self.assertTrue(r["awaiting_at_card"])
        self.assertFalse(r["awaiting_after_press"])
        self.assertTrue({"once", "deny"} <= set(r["prompts"][0]["hermes_choices"]), r["prompts"])
        self.assertEqual(r["prompts"][0]["choices"], ["once", "deny"])  # session/always cut by the plugin
        self.assertTrue(r["card_edits"] and not r["card_edits"][-1]["has_markup"])
        self.assertIn("Разрешено", r["card_edits"][-1]["text"])
        self.assertEqual(r["refused_sends"], [])  # commentary / progress interim sends are not refused
        self.assertFalse(r["events"][-1]["awaiting"])

    def test_11_deny(self) -> None:
        r = self.res("s02")
        self.assertEqual(r["answer"]["text"], "❌ Отклонено")
        self.assertIn("denied by user", " ".join(r["tool"]))
        self.assertTrue(r["victim_exists"])
        self.assertEqual(r["finals"], ["Понял, не удаляю."])

    def test_12_stranger_and_wrong_chat(self) -> None:
        r = self.res("s03")
        for ans in (r["stranger"], r["wrong_chat"]):
            self.assertEqual(ans["text"], TOAST_OWNER_ONLY)
            self.assertTrue(ans["show_alert"])
        self.assertEqual(r["pending_after_refusals"], 1)
        self.assertEqual(r["edits_before_owner"], 0)
        self.assertEqual(r["owner"]["text"], "❌ Отклонено")
        self.assertTrue(r["victim_exists"])

    def test_13_late_press(self) -> None:
        r = self.res("s04")
        self.assertEqual(r["again"]["text"], TOAST_STALE)
        self.assertEqual(r["after_deny"]["text"], TOAST_STALE)
        self.assertTrue(r["s02_victim_exists"])

    def test_14_timeout(self) -> None:
        r = self.res("s05")
        self.assertIn("timed out", " ".join(r["tool"]))
        self.assertTrue(P2_APPROVAL_TIMEOUT - 3 <= r["wait_s"] <= P2_APPROVAL_TIMEOUT + 30, r["wait_s"])
        self.assertTrue(any(e["card"] for e in r["hermes_edits"]), r["hermes_edits"])
        self.assertTrue(r["card_edits"] and not r["card_edits"][-1]["has_markup"])
        self.assertEqual(r["late"]["text"], TOAST_STALE)
        self.assertTrue(r["victim_exists"])
        self.assertFalse(r["awaiting_end"])

    def test_15_card_rejected(self) -> None:
        r = self.res("s06a")
        self.assertEqual(r["fault_fired"], 1)
        self.assert_denied_by_plugin(r)
        self.assertIsNotNone(r["prompt_to_deny_s"])
        self.assertLess(r["prompt_to_deny_s"], 2.0)

    def test_16_held_send(self) -> None:
        r = self.res("s07")
        self.assertEqual(r["during"]["text"], TOAST_WAIT)
        self.assert_denied_by_plugin(r)
        self.assertTrue(9.0 <= r["prompt_to_deny_s"] <= 13.0, r["prompt_to_deny_s"])
        self.assertEqual(r["late"], {"delivered": True, "edited": True})
        self.assertEqual(r["after"]["text"], TOAST_STALE)
        self.assertTrue(r["card_edits"] and not r["card_edits"][-1]["has_markup"])

    def test_17_long_commands_deny_only(self) -> None:
        for scene in ("s08a", "s08b"):
            r = self.res(scene)
            self.assertEqual(r["prompts"][0]["hermes_choices"], ["deny"], r["prompts"])
            self.assertEqual([b["data"].split(":")[1] for b in r["card"]["buttons"]], ["deny"])
            self.assertEqual(len(r["refused_sends"]), 1, r)
            self.assertLess(r["card"]["text_len"], 4096)
            self.assertEqual(r["forged"]["text"], "Этот выбор недоступен")
            self.assertEqual(r["owner"]["text"], "❌ Отклонено")
            self.assertTrue(r["victim_exists"])

    def test_17c_parser_limit_hardline(self) -> None:
        r = self.res("s08c")
        self.assertEqual(r["cards"], 0)
        self.assertEqual(r["prompts"], [])
        self.assertIn("BLOCKED", " ".join(r["tool"]))
        self.assertTrue(r["victim_exists"])
        self.assertEqual(r["finals"], ["Не выполнено."])

    def test_18_forged_token(self) -> None:
        self.assertEqual(self.res("s09")["answer"]["text"], TOAST_STALE)

    def test_19_two_cards(self) -> None:
        r = self.res("s10")
        self.assertEqual(r["awaiting"], [True, True])
        self.assertEqual(r["victims_exist"], [False, False])
        self.assertFalse(r["awaiting_end"])
        self.assertEqual(r["finals"], ["Оба удалены."])

    def test_20_busy_dm(self) -> None:
        r = self.res("s11")
        self.assert_owner_allowed(r)
        print(f"busy DM: press latency {r['answer']['latency']} s, "
              f"press before DM final: {r['press_before_dm_final']}, DM final +{r['dm_final_after_press_s']} s")

    def test_30_validate(self) -> None:
        v = self.world.validate or {}
        print(f"validate={json.dumps(v, ensure_ascii=False)}")
        self.assertNotIn("error", v, v)
        self.assertFalse([w for w in v["warnings"] if "import" in w.lower()], v["warnings"])


class GroupB(_GroupBase):
    """Group chat without ALICE_TELEGRAM_OWNER_ID: approvals disabled, every request denied at once."""

    GROUP = "B"
    SCENARIOS = SCENARIOS_B

    def test_01_disabled(self) -> None:
        r = self.res("b06")
        self.assertEqual(r["connect"], [{"enabled": False, "chat_set": True, "owner_set": False}])
        self.assertEqual(r["bot_sends"], 0)
        self.assert_denied_by_plugin(r)
        self.assertEqual([d["why"] for d in r["denies"]], ["disabled"])


class GroupC(_GroupBase):
    """Group chat with ALICE_TELEGRAM_OWNER_ID: the owner presses in the group, members cannot."""

    GROUP = "C"
    SCENARIOS = SCENARIOS_C

    def test_01_owner_allows_in_group(self) -> None:
        r = self.res("c01")
        self.assert_owner_allowed(r)
        self.assertEqual(str(r["card"]["chat_id"]), str(TG_GROUP))

    def test_02_member_refused(self) -> None:
        r = self.res("c03")
        self.assertEqual(r["stranger"]["text"], TOAST_OWNER_ONLY)
        self.assertTrue(r["stranger"]["show_alert"])
        self.assertEqual(r["pending_after_refusal"], 1)
        self.assertEqual(r["owner"]["text"], "❌ Отклонено")
        self.assertTrue(r["victim_exists"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
