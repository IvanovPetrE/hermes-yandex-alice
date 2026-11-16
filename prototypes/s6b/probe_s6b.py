"""Prototype S6b: where does the ``pre_tool_call`` hook of a voice turn run - in the gateway, or in a worker?

Question (``docs/PLAN.md``, stage 1, S6 supplement): S6 ran the agent inside the gateway process; a real
gateway can run a turn in a managed worker (``agent/managed_worker.py``). If the voice turn ran there, would
the hook of the target profile's plugin fire and see ``ALICE_TELEGRAM_CHAT``? By the code of Hermes v0.23.1
(``gateway/session_managed_worker.py: managed_policy``, ``gateway/session_policy.py: policy_for_source``) only
``Platform.LOCAL`` sessions can be managed, so a ``yandex_alice`` turn has no worker at all. S6b checks that
in a running gateway, with ``gateway.managed_workers: true`` set in BOTH profiles to give a worker every chance:

* W0 - ``managed_policy`` is ``None`` for every live session (``yandex_alice`` ones included) and no process
  started by the gateway runs ``agent.managed_worker`` (a ``subprocess.Popen`` spy);
* W1 - the spy works: it records a process the harness itself starts, and the cron executors if they come
  through ``Popen`` (informational);
* W2 - the hook fires (group B) in the gateway's own pid, sees the chat and rewrites ``deliver``;
* W3 - the job made by voice reaches Telegram (S6 H3 again, with ``HERMES_EXEC_ASK`` instead of
  ``HERMES_GATEWAY_SESSION``: the variable ``start_gateway()`` sets);
* W4 - group C (no chat in the p2 ``.env``): the hook does not see it (S6 H4 again).

It reuses the S6 harness (``prototypes/s6/probe_s6.py``: the real ``GatewayRunner``, multiplex default + p2,
scripted LLM, Bot API stand-in) and only repoints it at ``prototypes/s6b/plugin``. PROBE ONLY: ``gateway.*``,
``tests.*`` and the runner's attributes are the harness, not the plugin API. Run through ``run.sh``. Pure-layer
tests need no Hermes: ``cd prototypes/s6b && python -m unittest probe_s6b -k GuardTests -k ForwardTests``.
Python >= 3.11.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
S6 = HERE.parent / "s6"
sys.path.insert(0, str(S6))

import probe_s6 as base  # noqa: E402  (the S6 harness; repointed below)
from probe_s6 import ForwardTests, GuardTests  # noqa: E402,F401  (pure layer, run against the s6b plugin copy)

base.HERE = HERE  # World copies, validates and loads the plugin from HERE / "plugin"


def _guard_temp_home() -> Path:
    """The S6 guard (not the real home, not unset) for a ``hya-s6b-*`` temp dir instead of ``hya-s6-*``."""
    import pwd  # POSIX only

    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        raise SystemExit("refusing: HERMES_HOME is not set (run through run.sh)")
    home = Path(raw).resolve()
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if real_home == home or real_home in home.parents:
        raise SystemExit(f"refusing: HERMES_HOME {home} is inside the real home of this user")
    if not any(p.name.startswith("hya-s6b-") for p in home.parents):
        raise SystemExit(f"refusing: HERMES_HOME {home} is not under a hya-s6b-* temp dir")
    return home


base._guard_temp_home = _guard_temp_home

MANAGED = "  managed_workers: true\n"


def _enable_managed_workers(cfg: Path) -> bool:
    """Put ``gateway.managed_workers: true`` into the home's config.yaml (a new section if there is none)."""
    text = cfg.read_text(encoding="utf-8")
    if text.count("\ngateway:\n") > 1:
        raise AssertionError(f"more than one gateway: section in {cfg}")
    if "\ngateway:\n" in text:
        cfg.write_text(text.replace("\ngateway:\n", "\ngateway:\n" + MANAGED, 1), encoding="utf-8")
    else:
        cfg.write_text(text.rstrip("\n") + "\ngateway:\n" + MANAGED, encoding="utf-8")
    return True


def _arg_shape(arg) -> str:
    """argv without payload: flags and dotted module names are kept, anything else (paths, ids) is masked."""
    s = str(arg)
    if s.startswith("-") or (s.replace(".", "").replace("_", "").isalnum() and not s.isdigit() and len(s) < 60):
        return s
    return "<arg>"


class SpyWorld(base.World):
    """The S6 world with managed workers switched on in both profiles and a spy on process starts."""

    def __init__(self, group: str) -> None:
        super().__init__(group)
        self.spawns: list[dict] = []
        self.gateway_pid = os.getpid()

    def _write_homes(self, base_url: str) -> None:
        super()._write_homes(base_url)
        for h in (self.home, self.p2):
            _enable_managed_workers(h / "config.yaml")

    async def _run_gateway(self, scenarios) -> None:
        real = subprocess.Popen.__init__
        spawns = self.spawns

        def spy(popen, args, *a, **kw):
            try:
                argv = list(args) if isinstance(args, (list, tuple)) else [args]
                spawns.append(dict(by=os.getpid(), exe=Path(str(argv[0])).name,
                                   argv=[_arg_shape(x) for x in argv[1:6]]))
            except Exception:  # noqa: BLE001 - the spy never breaks a start
                pass
            return real(popen, args, *a, **kw)

        subprocess.Popen.__init__ = spy
        try:
            await super()._run_gateway(scenarios)
        finally:
            subprocess.Popen.__init__ = real

    def file_journal(self, op: str | None = None) -> list[dict]:
        rows = []
        for where, home in (("default", self.home), (base.P2, self.p2)):
            path = home / "s6b-journal.jsonl"
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                row["where"] = where
                rows.append(row)
        rows.sort(key=lambda r: r["t"])
        return [r for r in rows if op is None or r["op"] == op]

    def managed_check(self) -> dict:
        """W0: ``managed_policy`` and the platform of every live session of every authority."""
        from gateway.session_authorities import all_authorities
        from gateway.session_managed_worker import managed_policy
        from gateway.session_policy import policy_for_source

        out = []
        authorities = all_authorities(self.runner)
        for authority in authorities:
            for sid, live in list(getattr(authority, "sessions", {}).items()):
                platform = getattr(getattr(live, "source", None), "platform", None)
                try:
                    verdict = managed_policy(authority, SimpleNamespace(session_id=sid))
                    result = None if verdict is None else type(verdict).__name__
                except Exception as exc:  # noqa: BLE001 - recorded, not hidden
                    result = f"raised {type(exc).__name__}: {str(exc)[:80]}"
                out.append(dict(platform=str(getattr(platform, "value", platform)), managed_policy=result))
        # The decision itself, on the source of a voice turn: what managed_policy asks first.
        source = self.alice.build_source(chat_id="chat-w0", chat_name="chat-w0", chat_type="dm",
                                         user_id=base.OWNER_ID, user_name="probe")
        try:
            verdict = policy_for_source(self.runner, source)
            by_source = None if verdict is None else type(verdict).__name__
        except Exception as exc:  # noqa: BLE001
            by_source = f"raised {type(exc).__name__}: {str(exc)[:80]}"
        return dict(authorities=len(authorities), sessions=out, policy_for_voice_source=by_source,
                    source_platform=str(getattr(source.platform, "value", source.platform)))


async def sc_w0(w: SpyWorld) -> dict:
    """A voice turn that creates a job without ``deliver`` - the hook's place, its pid, the managed verdicts."""
    f = base._fakes()
    t0 = base.time.time()
    w.script.add("w0", base._create("w0", schedule="in 30d"), f.Text("Поставил w0"))
    res = await w.turn("chat-w0", "m-w0", "w0")
    probe = subprocess.Popen([sys.executable, "-c", "pass"])  # W1 control: the spy must see this one
    probe.wait()
    return dict(turn=res, managed=w.managed_check(), job=w.jobs().get("w0"),
                gateway_pid=w.gateway_pid, tool_results=w.tool_results("w0"),
                tools=base._tool_names((w.bodies("w0") or [{}])[-1]),
                hooks=[r for r in w.file_journal("hook") if r["t"] >= t0],
                env=dict(exec_ask=os.environ.get("HERMES_EXEC_ASK"),
                         gateway_session=os.environ.get("HERMES_GATEWAY_SESSION")))


class _S6bGroup(base._Group):
    @classmethod
    def setUpClass(cls) -> None:
        cls.w = SpyWorld(cls.GROUP)
        try:
            cls.w.run(cls.SCENARIOS)
        finally:
            cls.w.dump()
            print(f"spawns={json.dumps(cls.w.spawns, ensure_ascii=False)}")
            print(f"file_journal_ops={sorted({r['op'] for r in cls.w.file_journal()})}")

    def test_validate(self) -> None:
        v = self.w.validate or {}
        print(f"validate={json.dumps(v, ensure_ascii=False)}")
        self.assertNotIn("error", v, v)
        self.assertTrue(v.get("ok"), v)  # provides_hooks is declared: no "undeclared hooks registered"

    def test_W0_no_worker_for_a_voice_turn(self) -> None:
        r = self.res("w0")
        managed = r["managed"]
        self.assertEqual(managed["source_platform"], base.PLATFORM, managed)
        self.assertIsNone(managed["policy_for_voice_source"], managed)
        self.assertEqual([s for s in managed["sessions"] if s["managed_policy"] is not None], [], managed)
        workers = [s for s in self.w.spawns if "agent.managed_worker" in s["argv"]]
        self.assertEqual(workers, [], self.w.spawns)
        print(f"W0 managed={managed} env={r['env']}")

    def test_W1_spy_sees_a_start(self) -> None:
        mine = [s for s in self.w.spawns if s["argv"][:1] == ["-c"]]
        self.assertTrue(mine, self.w.spawns)
        print(f"W1 spawns={len(self.w.spawns)} exes={sorted({s['exe'] for s in self.w.spawns})}")


class GroupB(_S6bGroup):
    GROUP = "B"
    SCENARIOS = [("w0", sc_w0), ("h2", base.sc_h2), ("h3", base.sc_h3)]

    def test_W2_hook_runs_in_the_gateway_and_sees_the_chat(self) -> None:
        r = self.res("w0")
        hooks = r["hooks"]
        self.assertTrue(hooks, "the hook did not fire")
        self.assertEqual({h["pid"] for h in hooks}, {r["gateway_pid"]}, hooks)
        self.assertTrue(all(h["chat_set"] for h in hooks), hooks)
        self.assertIn("deliver", hooks[0]["merged"], hooks)
        self.assertEqual((r["job"] or {}).get("deliver"), base.TARGET, r["job"])
        self.assertEqual(r["env"]["gateway_session"], None, r["env"])
        self.assertEqual(r["env"]["exec_ask"], "1", r["env"])
        print(f"W2 hooks={hooks} job={r['job']}")

    def test_W3_voice_job_reaches_telegram(self) -> None:
        r = self.res("h3")
        self.assertEqual((r["jobs"].get("h3") or {}).get("deliver"), base.TARGET, r["jobs"])
        hits = [s for s in r["tg"] if s["chat"] == str(base.TG_OWNER) and "Напоминание h3" in s["text"]]
        self.assertTrue(hits, r)
        print(f"W3 deliveries={json.dumps(r['deliveries'], ensure_ascii=False, default=str)[:1500]}")

    def test_H2_hook_rewrites_only_alice(self) -> None:
        print(f"H2 {json.dumps(self.res('h2'), ensure_ascii=False, default=str)[:5000]}")


class GroupC(_S6bGroup):
    GROUP = "C"
    SCENARIOS = [("w0", sc_w0), ("h4", base.sc_h4)]

    def test_W4_the_hook_does_not_see_the_chat_without_it_in_p2(self) -> None:
        r = self.res("w0")
        hooks = r["hooks"]
        self.assertTrue(hooks, "the hook did not fire")
        self.assertEqual({h["pid"] for h in hooks}, {r["gateway_pid"]}, hooks)
        self.assertFalse(any(h["chat_set"] for h in hooks), hooks)
        self.assertEqual((r["job"] or {}).get("deliver"), "origin", r["job"])
        print(f"W4 hooks={hooks} job={r['job']}")


class CopyTests(unittest.TestCase):
    """The pure modules of the s6b plugin are byte copies of S6's: the S6 verdict about them still holds."""

    def test_pure_modules_are_copies(self) -> None:
        for name in ("cron_guard.py", "recovery.py"):
            self.assertEqual((HERE / "plugin" / name).read_bytes(), (S6 / "plugin" / name).read_bytes(), name)

    def test_manifest_declares_the_hook(self) -> None:
        self.assertIn("pre_tool_call", (HERE / "plugin" / "plugin.yaml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
