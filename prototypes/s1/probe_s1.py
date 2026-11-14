"""Prototype S1: turn finality and answer-to-turn binding for a plugin platform.

What it answers: can a ``kind: platform`` plugin (the future ``yandex_alice`` adapter) tell, for
every gateway turn, (a) which ``send()`` is the turn's final answer and which are interim/status
sends, and (b) when the turn is over and with what outcome - for plain text, tool calls, an empty
answer, a provider error, a busy session and two conversations in parallel.

How: a REAL ``GatewayRunner`` runs in this process with one stub platform adapter registered the
way ``ctx.register_platform`` registers it (``PlatformEntry(source="plugin", display_tier=
"minimal")``). The agent is Hermes' own scripted loopback LLM (``tests/fakes/fake_llm_provider``):
no real model, keys or cost. The stub journals every adapter call; ``TurnTracker`` applies the
classification rule the adapter is meant to use and the tests pin what was observed.

PROBE ONLY. ``gateway.run`` and ``tests.*`` are NOT the public plugin API - they are the test
harness that starts a gateway. The plugin itself (``yandex-alice/``) must never import them.

Run through ``run.sh`` only (throwaway HOME / HERMES_HOME / lock dir, see README.md).
Python >= 3.11.
"""

from __future__ import annotations

import asyncio
import json
import pwd
import os
import resource
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PLATFORM = "yandex_alice"
OWNER_ID = "u-probe-owner"  # synthetic; run.sh exports ALICE_OWNER_USER_ID with the same value
OWNER_ENV = "ALICE_OWNER_USER_ID"
TURN_TIMEOUT = 90.0
SETTLE = 1.5  # seconds to keep listening after a turn closed (late sends)
SYSTEM_PREFIXES = ("⚡", "⏳", "⏩", "💾")  # LINE's fallback markers

GROUP_CONFIG = {
    # Group A: streaming on globally, busy_input_mode interrupt, no operator display overrides.
    "A": "display:\n  busy_input_mode: interrupt\nstreaming:\n  enabled: true\n",
    # Group B: operator overrides that beat the platform's display tier.
    "B": ("display:\n  busy_input_mode: queue\n  tool_progress: all\n"
          "  interim_assistant_messages: true\nstreaming:\n  enabled: true\n"),
}
# Keys added to the ``agent:`` section write_hermes_home() already writes (a second top-level
# ``agent:`` would be a duplicate YAML key). The provider-recovery ladder (default 5 cycles,
# 18-70 s apart) is off in A, so a failing provider ends the turn; B keeps one cycle to show it.
GROUP_AGENT = {"A": "  auto_recovery_cycles: 0\n", "B": "  auto_recovery_cycles: 1\n"}


# --- journal + classification rule (stdlib only) -------------------------------------------------


@dataclass
class Op:
    t: float
    op: str  # start | complete | send | edit | delete | typing
    chat_id: str
    message_id: Optional[str] = None  # event message_id for start/complete
    content: str = ""
    reply_to: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    outcome: Optional[str] = None

    def as_dict(self) -> dict:
        meta = {k: v for k, v in self.metadata.items() if k != "hermes_profile"}
        return {"t": round(self.t, 3), "op": self.op, "chat": self.chat_id, "mid": self.message_id,
                "reply_to": self.reply_to, "outcome": self.outcome, "meta": meta,
                "content": self.content[:80]}


def is_interim_hint(op: Op) -> bool:
    return bool(op.metadata.get("_interim_send")) or op.content.startswith(SYSTEM_PREFIXES)


@dataclass
class TurnResult:
    message_id: str
    chat_id: str
    outcome: Optional[str] = None
    final: Optional[str] = None
    final_meta: dict = field(default_factory=dict)
    t_start: float = 0.0
    t_final: Optional[float] = None
    t_complete: Optional[float] = None

    @property
    def verdict(self) -> str:
        """What the adapter would conclude for this turn when the turn closes."""
        if self.outcome is None:
            return "open"
        if self.final is not None:
            return "final"
        return "empty" if self.outcome == "success" else self.outcome


class TurnTracker:
    """The candidate adapter rule.

    A turn opens at ``on_processing_start(event)`` and closes at ``on_processing_complete``.
    The final answer is a send to the turn's chat with ``reply_to == event.message_id`` and
    ``metadata["notify"]``, carrying no interim hint (``_interim_send`` / LINE prefixes).
    Anything else during an open turn is ``during``; with no open turn in the chat - ``outside``
    (a classification label only; what the adapter does with it is decided later).
    """

    def __init__(self) -> None:
        self.turns: dict[str, TurnResult] = {}
        self.sends: list[tuple[str, Op]] = []  # (class, op)

    def feed(self, ops: list[Op]) -> "TurnTracker":
        for op in ops:
            if op.op == "start":
                self.turns[op.message_id] = TurnResult(op.message_id, op.chat_id, t_start=op.t)
            elif op.op == "complete":
                turn = self.turns.setdefault(op.message_id, TurnResult(op.message_id, op.chat_id))
                turn.outcome, turn.t_complete = op.outcome, op.t
            elif op.op == "send":
                self.sends.append((self._classify(op), op))
        return self

    def _open(self, chat_id: str) -> list[TurnResult]:
        return [t for t in self.turns.values() if t.chat_id == chat_id and t.outcome is None]

    def _classify(self, op: Op) -> str:
        open_turns = self._open(op.chat_id)
        if is_interim_hint(op):
            return "interim" if open_turns else "outside"
        for turn in open_turns:
            if op.reply_to == turn.message_id and op.metadata.get("notify"):
                if turn.final is None:
                    turn.final, turn.final_meta, turn.t_final = op.content, dict(op.metadata), op.t
                    return "final"
                return "final-again"
        return "during" if open_turns else "outside"

    def classes(self, chat_id: str, *, skip_home_notice: bool = True) -> list[str]:
        """Send classes in one chat; the per-chat "📬 No home channel" notice is left out."""
        return [c for c, op in self.sends if op.chat_id == chat_id
                and not (skip_home_notice and is_home_notice(op))]

    def home_notices(self, chat_id: str) -> list[str]:
        return [c for c, op in self.sends if op.chat_id == chat_id and is_home_notice(op)]


def is_home_notice(op: Op) -> bool:
    return op.content.startswith("📬")


# --- scripted agent --------------------------------------------------------------------------


class Script:
    """Responder for FakeLLMServer keyed by a token in the latest user message, so concurrent
    turns get their own responses regardless of request arrival order."""

    def __init__(self, text_cls) -> None:
        self._text_cls = text_cls
        self.queues: dict[str, list] = {}
        self.fallback: dict[str, Any] = {}  # answer once the queue is used up (e.g. "always fail")
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


# --- the gateway world -----------------------------------------------------------------------


def _guard_temp_home() -> Path:
    """Refuse to run against a real Hermes home."""
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home()).resolve()
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if real_home == home or real_home in home.parents:
        raise SystemExit(f"refusing: HERMES_HOME {home} is inside the real home of this user")
    if not any(p.name.startswith("hya-s1-") for p in home.parents):
        raise SystemExit(f"refusing: HERMES_HOME {home} is not under a hya-s1-* temp dir")
    return home


def _build_adapter_class():
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.config import Platform

    class StubAliceAdapter(BasePlatformAdapter):
        """No network, no port: journals every call the gateway makes."""

        SUPPORTS_MESSAGE_EDITING = False

        def __init__(self, config, journal: list[Op]) -> None:
            super().__init__(config, Platform(PLATFORM))
            self.journal = journal
            self._n = 0

        def _rec(self, **kw) -> None:
            self.journal.append(Op(t=time.monotonic(), **kw))

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            self._running = True
            self._mark_connected()
            return True

        async def disconnect(self) -> None:
            self._running = False
            self._mark_disconnected()

        async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
            return {"name": f"chat-{chat_id}", "type": "dm", "chat_id": chat_id}

        async def send_typing(self, chat_id: str, metadata=None) -> None:
            self._rec(op="typing", chat_id=str(chat_id))

        async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                       metadata: Optional[dict[str, Any]] = None) -> SendResult:
            self._n += 1
            self._rec(op="send", chat_id=str(chat_id), content=content or "",
                      reply_to=None if reply_to is None else str(reply_to), metadata=dict(metadata or {}))
            return SendResult(success=True, message_id=f"out-{self._n}")

        async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                               finalize: bool = False) -> SendResult:
            self._rec(op="edit", chat_id=str(chat_id), content=content or "", reply_to=message_id,
                      metadata={"finalize": finalize})
            return SendResult(success=False, error="Not supported")

        async def delete_message(self, chat_id: str, message_id: str) -> bool:
            self._rec(op="delete", chat_id=str(chat_id), reply_to=message_id)
            return False

        async def on_processing_start(self, event) -> None:
            self._rec(op="start", chat_id=str(event.source.chat_id), message_id=event.message_id)

        async def on_processing_complete(self, event, outcome) -> None:
            self._rec(op="complete", chat_id=str(event.source.chat_id), message_id=event.message_id,
                      outcome=getattr(outcome, "value", str(outcome)))

        async def inject(self, text: str, message_id: str, chat_id: str) -> None:
            from gateway.platforms.event import MessageEvent, MessageType
            source = self.build_source(chat_id=chat_id, chat_name=f"chat-{chat_id}", chat_type="dm",
                                       user_id=OWNER_ID, user_name="owner")
            await self.handle_message(MessageEvent(text=text, message_type=MessageType.TEXT,
                                                   source=source, message_id=message_id))

    return StubAliceAdapter


class World:
    """One gateway + scripted LLM for one config group (one process per group)."""

    def __init__(self, group: str) -> None:
        self.group = group
        self.journal: list[Op] = []
        self.results: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        self.effective: dict[str, Any] = {}

    async def wait_complete(self, mid: str, timeout: float = TURN_TIMEOUT) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(o.op == "complete" and o.message_id == mid for o in self.journal):
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"turn {mid} did not complete in {timeout:.0f}s")

    async def wait_seen(self, token: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if token in self.script.seen:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"agent never asked the LLM for [{token}]")

    def slice(self, chats: set[str]) -> list[Op]:
        return [o for o in self.journal if o.chat_id in chats]

    async def turn(self, name: str, chat: str, mid: str, token: str) -> None:
        await self.adapter.inject(f"probe [{token}] вопрос", mid, chat)
        await self.wait_complete(mid)

    async def run(self, scenarios) -> None:
        home = _guard_temp_home()
        from tests.fakes.fake_llm_provider import FakeLLMServer, Text, write_hermes_home

        self.script = Script(Text)
        llm = FakeLLMServer(self.script)
        llm.start()
        try:
            write_hermes_home(home, llm.base_url, extra_config=GROUP_CONFIG[self.group])
            cfg = home / "config.yaml"
            text = cfg.read_text(encoding="utf-8")
            if text.count("\nagent:\n") != 1:
                raise AssertionError("expected exactly one agent: section in the probe config")
            cfg.write_text(text.replace("\nagent:\n", "\nagent:\n" + GROUP_AGENT[self.group], 1),
                           encoding="utf-8")
            await self._run_gateway(scenarios)
        finally:
            llm.stop()

    async def _run_gateway(self, scenarios) -> None:
        from gateway.config import GatewayConfig, Platform, PlatformConfig, StreamingConfig
        from gateway.platform_registry import PlatformEntry, platform_registry
        from gateway.display_config import resolve_display_setting
        from gateway.run import GatewayRunner, _load_gateway_config

        cls = _build_adapter_class()
        holder: dict[str, Any] = {}

        def factory(pconfig):
            holder["adapter"] = cls(pconfig, self.journal)
            return holder["adapter"]

        platform_registry.register(PlatformEntry(
            name=PLATFORM, label="Yandex Alice (S1 probe)", adapter_factory=factory,
            check_fn=lambda: True, source="plugin", display_tier="minimal",
            allowed_users_env=OWNER_ENV))
        config = GatewayConfig(
            platforms={Platform(PLATFORM): PlatformConfig(enabled=True, token="probe-token")},
            streaming=StreamingConfig(enabled=True, transport="edit", edit_interval=0.05,
                                      buffer_threshold=24, cursor=""),
            loop_watchdog=False, write_sessions_json=False, multiplex_profiles=False,
        )
        runner = GatewayRunner(config)
        if not await runner.start():
            raise AssertionError("GatewayRunner.start() returned False")
        try:
            self.adapter = holder.get("adapter") or runner.adapters[Platform(PLATFORM)]
            user_cfg = _load_gateway_config()
            for key in ("tool_progress", "interim_assistant_messages", "streaming"):
                self.effective[key] = resolve_display_setting(user_cfg, PLATFORM, key)
            self.effective["streaming_global"] = bool(config.streaming.globally_enabled)
            self.effective["busy_input_mode"] = getattr(runner, "_busy_input_mode", None)
            for name, coro_fn in scenarios:
                try:
                    await asyncio.wait_for(coro_fn(self), timeout=TURN_TIMEOUT * 2)
                except Exception as exc:  # recorded, asserted by the test of that scenario
                    self.errors[name] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(SETTLE)
        finally:
            await runner.stop()
            self.effective["maxrss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024

    def dump(self, name: str, chats: set[str]) -> None:
        print(f"\n--- {self.group}/{name} ---")
        t0 = next((o.t for o in self.journal if o.chat_id in chats), 0.0)
        for o in self.slice(chats):
            if o.op == "typing":
                continue
            d = o.as_dict()
            d["t"] = round(o.t - t0, 3)
            print(json.dumps(d, ensure_ascii=False))


# --- scenarios -------------------------------------------------------------------------------

LONG = ("Длинный ответ для проверки стриминга. " * 12).strip()


def _hermes_types():
    from tests.fakes import fake_llm_provider as f
    return f


async def sc_text(w: World) -> None:
    w.script.add("t01", _hermes_types().Text("Ответ первый."))
    await w.turn("text", "c01", "m01", "t01")


async def sc_tool(w: World, chat="c02", mid="m02", token="t02") -> None:
    f = _hermes_types()
    w.script.add(token, f.ToolCall("terminal", {"command": "echo hi"}, text="Сейчас проверю."),
                 f.Text("Готово: hi."))
    await w.turn("tool", chat, mid, token)


async def sc_long(w: World) -> None:
    w.script.add("t03", _hermes_types().Text(LONG, chunk_chars=16, delay_per_chunk=0.03))
    await w.turn("long", "c03", "m03", "t03")


async def sc_empty(w: World) -> None:
    # The model answers empty every time (Hermes retries an empty answer).
    w.script.add("t04", fallback=_hermes_types().Text(""))
    await w.turn("empty", "c04", "m04", "t04")


async def sc_error(w: World, chat="c05", mid="m05", token="t05") -> None:
    # The provider fails every time.
    w.script.add(token, fallback=_hermes_types().Error(500, "scripted failure"))
    await w.turn("error", chat, mid, token)


async def sc_busy(w: World, chat="c06", token="t06") -> None:
    """Second phrase into the same chat while the first turn is still running.

    The second event may never get hooks of its own (interrupt redirects it into the running
    turn), so only the first turn is awaited; late sends are caught by SETTLE."""
    f = _hermes_types()
    w.script.add(f"{token}a", f.Hang(5.0), f.Text("Ответ на первую."))
    w.script.add(f"{token}b", f.Text("Ответ на вторую."))
    await w.adapter.inject(f"probe [{token}a] первая", f"{chat}-m1", chat)
    await w.wait_seen(f"{token}a")
    await asyncio.sleep(0.5)
    await w.adapter.inject(f"probe [{token}b] вторая", f"{chat}-m2", chat)
    await w.wait_complete(f"{chat}-m1")
    deadline = time.monotonic() + 30.0  # queue mode: the follow-up closes later
    while time.monotonic() < deadline and not w.script.seen.count(f"{token}b"):
        await asyncio.sleep(0.05)
    await asyncio.sleep(3.0)


async def sc_parallel(w: World) -> None:
    """Two conversations (old and new chat_id) in flight at once."""
    f = _hermes_types()
    w.script.add("t07a", f.Hang(3.0), f.Text("Ответ A."))
    w.script.add("t07b", f.Text("Ответ B."))
    await w.adapter.inject("probe [t07a] долгий", "m07a", "c07a")
    await w.wait_seen("t07a")
    await w.adapter.inject("probe [t07b] быстрый", "m07b", "c07b")
    await w.wait_complete("m07b")
    await w.wait_complete("m07a")


SCENARIOS_A = [("text", sc_text), ("tool", sc_tool), ("long", sc_long), ("empty", sc_empty),
               ("error", sc_error), ("busy", sc_busy), ("parallel", sc_parallel)]
SCENARIOS_B = [("tool", lambda w: sc_tool(w, "c08", "m08", "t08")),
               ("busy", lambda w: sc_busy(w, "c09", "t09")),
               ("error", lambda w: sc_error(w, "c10", "m10", "t10"))]


# --- tests -----------------------------------------------------------------------------------


class _GroupBase(unittest.TestCase):
    GROUP = ""
    SCENARIOS: list = []
    world: World

    @classmethod
    def setUpClass(cls) -> None:
        cls.world = World(cls.GROUP)
        asyncio.run(cls.world.run(cls.SCENARIOS))
        print(f"\n=== group {cls.GROUP}: effective={cls.world.effective} errors={cls.world.errors}")

    def scenario(self, name: str, chats: set[str]) -> TurnTracker:
        self.world.dump(name, chats)
        self.assertNotIn(name, self.world.errors, self.world.errors.get(name))
        return TurnTracker().feed(self.world.slice(chats))

    def assert_single_final(self, tr: TurnTracker, mid: str, text: str) -> TurnResult:
        turn = tr.turns[mid]
        self.assertEqual(turn.verdict, "final")
        self.assertEqual(turn.final, text)
        self.assertNotIn("_interim_send", turn.final_meta)
        self.assertTrue(turn.t_final <= turn.t_complete, "final must precede the complete hook")
        return turn

    def assert_no_edits(self, chats: set[str]) -> None:
        self.assertEqual([o for o in self.world.slice(chats) if o.op in ("edit", "delete")], [])


class GroupA(_GroupBase):
    """Display tier ``minimal``, streaming on globally, busy_input_mode interrupt."""

    GROUP = "A"
    SCENARIOS = SCENARIOS_A

    def test_00_effective_config(self) -> None:
        e = self.world.effective
        self.assertEqual(e["tool_progress"], "off")
        self.assertFalse(e["interim_assistant_messages"])
        self.assertFalse(e["streaming"])
        self.assertTrue(e["streaming_global"])
        self.assertEqual(e["busy_input_mode"], "interrupt")

    def test_01_text(self) -> None:
        tr = self.scenario("text", {"c01"})
        self.assert_single_final(tr, "m01", "Ответ первый.")
        self.assertEqual(tr.classes("c01"), ["final"])
        # Unmarked, no reply_to, no notify: LINE's "first unmarked send" would voice it.
        self.assertEqual(tr.home_notices("c01"), ["during"])
        self.assert_no_edits({"c01"})

    def test_02_tool_minimal(self) -> None:
        tr = self.scenario("tool", {"c02"})
        self.assert_single_final(tr, "m02", "Готово: hi.")
        self.assertEqual(tr.classes("c02"), ["final"])

    def test_03_long_no_streaming(self) -> None:
        tr = self.scenario("long", {"c03"})
        self.assert_single_final(tr, "m03", LONG)
        self.assert_no_edits({"c03"})

    def test_04_empty(self) -> None:
        """An always-empty model: unmarked "⚠️ … retrying" / "❌ …" notices, then a gateway-made
        error text arrives as an ordinary final (reply_to + notify) and the hook says SUCCESS."""
        tr = self.scenario("empty", {"c04"})
        turn = tr.turns["m04"]
        self.assertEqual((turn.outcome, turn.verdict), ("success", "final"))
        self.assertTrue(turn.final.startswith("⚠️ No reply"), turn.final)
        self.assertEqual(tr.classes("c04"), ["during"] * 4 + ["final"])

    def test_05_provider_error(self) -> None:
        """A failing provider (recovery ladder off): the error text is an ordinary final too."""
        tr = self.scenario("error", {"c05"})
        turn = tr.turns["m05"]
        self.assertEqual((turn.outcome, turn.verdict), ("success", "final"))
        self.assertIn("server error", turn.final)
        self.assertEqual(tr.classes("c05"), ["during", "final"])

    def test_06_busy_interrupt(self) -> None:
        """Pinned exception: the redirected second phrase gets no hooks of its own and the final
        answer is anchored on it, so the rule closes the opening turn as "empty" - the plugin must
        not hand Hermes a phrase while a turn of that conversation is open (decision R2)."""
        tr = self.scenario("busy", {"c06"})
        self.assertNotIn("c06-m2", tr.turns, "redirected phrase got its own start/complete")
        self.assertEqual(tr.turns["c06-m1"].outcome, "success")
        self.assertEqual(tr.turns["c06-m1"].verdict, "empty")
        finals = [op for _, op in tr.sends if op.metadata.get("notify")]
        self.assertEqual([(op.reply_to, op.content) for op in finals], [("c06-m2", "Ответ на вторую.")])

    def test_07_parallel_chats(self) -> None:
        tr = self.scenario("parallel", {"c07a", "c07b"})
        self.assert_single_final(tr, "m07a", "Ответ A.")
        self.assert_single_final(tr, "m07b", "Ответ B.")
        self.assertEqual(tr.classes("c07a"), ["final"])
        self.assertEqual(tr.classes("c07b"), ["final"])


class GroupB(_GroupBase):
    """Operator overrides: tool_progress all, interim_assistant_messages true, queue."""

    GROUP = "B"
    SCENARIOS = SCENARIOS_B

    def test_00_effective_config(self) -> None:
        e = self.world.effective
        self.assertEqual(e["tool_progress"], "all")
        self.assertTrue(e["interim_assistant_messages"])
        self.assertEqual(e["busy_input_mode"], "queue")

    def test_08_tool_with_operator_overrides(self) -> None:
        """Commentary carries _interim_send; tool progress ("💻 Running …") carries NO marker and
        no reply_to - only reply_to + notify single out the final."""
        tr = self.scenario("tool", {"c08"})
        self.assert_single_final(tr, "m08", "Готово: hi.")
        self.assertEqual(tr.classes("c08"), ["interim", "during", "final"])
        progress = [op for c, op in tr.sends if c == "during" and not is_home_notice(op)]
        self.assertEqual([op.metadata for op in progress], [{}])

    def test_09_busy_queue(self) -> None:
        """Pinned exception: the queued follow-up gets its own hooks, but its complete fires BEFORE
        its answer, and the answer is anchored on the OPENING event - the rule reads it as a second
        final of the opener and the follow-up as empty. Hence decision R2 (gate in the plugin)."""
        tr = self.scenario("busy", {"c09"})
        opener, follow = tr.turns["c09-m1"], tr.turns["c09-m2"]
        self.assertEqual(opener.final, "Ответ на первую.")
        self.assertEqual(tr.classes("c09"), ["final", "final-again"])
        self.assertEqual(follow.verdict, "empty")
        again = next(op for c, op in tr.sends if c == "final-again")
        self.assertEqual((again.reply_to, again.content), ("c09-m1", "Ответ на вторую."))
        self.assertLess(follow.t_complete, again.t)

    def test_10_provider_error_with_recovery_cycle(self) -> None:
        """Recovery-ladder notices start with ⏳ but carry no _interim_send (prefix-only hint)."""
        tr = self.scenario("error", {"c10"})
        turn = tr.turns["m10"]
        self.assertEqual((turn.outcome, turn.verdict), ("success", "final"))
        self.assertIn("server error", turn.final)
        self.assertEqual(tr.classes("c10"), ["interim", "interim", "during", "final"])
        ladder = [op for c, op in tr.sends if c == "interim"]
        self.assertTrue(all(op.content.startswith("⏳") and not op.metadata for op in ladder))


if __name__ == "__main__":
    unittest.main(verbosity=2)
