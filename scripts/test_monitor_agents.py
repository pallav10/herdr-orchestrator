#!/usr/bin/env python3
"""Monitor-agents poll shape, liveness, adaptive cadence, and retry regressions."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from testkit import FakeHerdr, load_script, run_module

FLAGS = ["--interval", "0", "--grace", "0", "--timeout", "1000"]




class FakeClock:
    def __init__(self, now: float):
        self.now = now
        self.sleeps = []

    def time(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


def scripted(seq):
    remaining = list(seq)

    def inventory(*_args):
        if not remaining:
            raise AssertionError("agent inventory exhausted")
        response = remaining.pop(0)
        if isinstance(response, BaseException):
            raise response
        return {"result": {"agents": [
            {"name": name, "agent_status": info["status"],
             "agent_session": {"value": info.get("session")}}
            for name, info in response.items()
        ]}}

    return inventory


def parse_lines(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def poll_lines(events: list[dict]) -> list[dict]:
    return [e for e in events if "pending" in e]


def plant_session(tmp: Path, *, session_mtime: float, trans_mtime: float) -> tuple[Path, Path]:
    session = tmp / "agent.jsonl"
    session.write_text("{}\n", encoding="utf-8")
    stem = tmp / "agent"
    stem.mkdir()
    trans = stem / "sub.jsonl"
    trans.write_text("{}\n", encoding="utf-8")
    os.utime(session, (session_mtime, session_mtime))
    os.utime(trans, (trans_mtime, trans_mtime))
    return session, trans


class TestMonitorAgents(unittest.TestCase):
    def _patch(self, module, seq, *, now: float):
        recorded = []
        module.time = FakeClock(now)
        module.herdr = FakeHerdr({("agent", "list"): scripted(seq)})
        module.notify = lambda *a: recorded.append(a)
        return recorded

    def test_last_activity_uses_session_and_subagent_transcripts(self):
        module = load_script("monitor-agents.py")
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            session, trans = plant_session(tmp, session_mtime=1_700_000_000, trans_mtime=1_700_000_050)
            self.assertEqual(module.last_activity(str(session)), trans.stat().st_mtime)
            self.assertIsNone(module.last_activity(None))
            self.assertIsNone(module.last_activity(str(tmp / "missing.jsonl")))

    def test_stale_line_emitted_once_per_edge_and_notifies(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            session, trans = plant_session(tmp, session_mtime=now - 200, trans_mtime=now - 180)
            info = {"w": {"status": "working", "session": str(session)}}
            idle = {"w": {"status": "idle", "session": str(session)}}
            module = load_script("monitor-agents.py")
            recorded = self._patch(module, [info, info, info, idle], now=now)
            code, out, err = run_module(module, [
                "monitor-agents.py", "w", *FLAGS, "--stale", "60", "--notify-pane", "w1:p1",
            ])
            self.assertEqual(code, 0, err)
            events = parse_lines(out)
            stale = [e for e in events if "STALE" in e]
            self.assertEqual(len(stale), 1)
            self.assertGreaterEqual(stale[0]["idle_for"], 60)
            self.assertEqual(stale[0]["session"], str(session))
            stale_notifies = [r for r in recorded if "STALE" in r[2]]
            self.assertEqual(len(stale_notifies), 1)
            self.assertIn("STALE", stale_notifies[0][2])

            os.utime(session, (now - 200, now - 200))
            os.utime(trans, (now - 180, now - 180))
            calls = {"n": 0}

            def agent_states():
                calls["n"] += 1
                if calls["n"] == 2:
                    os.utime(trans, (now, now))
                status = "idle" if calls["n"] >= 3 else "working"
                return {"w": {"status": status, "session": str(session)}}

            module = load_script("monitor-agents.py")
            recorded = []
            module.time = FakeClock(now)
            module.agent_states = agent_states
            module.notify = lambda *a: recorded.append(a)
            code, out, err = run_module(module, [
                "monitor-agents.py", "w", *FLAGS, "--stale", "60", "--notify-pane", "w1:p1",
            ])
            self.assertEqual(code, 0, err)
            active = [e for e in parse_lines(out) if "ACTIVE" in e]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["ACTIVE"], "w")

    def test_default_suppresses_identical_polls_and_every_poll_restores_them(self):
        now = 2_000_000_000.0
        working = {"w": {"status": "working", "session": None}}
        idle = {"w": {"status": "idle", "session": None}}
        module = load_script("monitor-agents.py")
        self._patch(module, [working, working, working, idle], now=now)
        code, out, err = run_module(module, [
            "monitor-agents.py", "w", *FLAGS,
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(poll_lines(parse_lines(out))), 2)

        module = load_script("monitor-agents.py")
        self._patch(module, [working, working, working, idle], now=now)
        code, out, err = run_module(module, ["monitor-agents.py", "w", *FLAGS, "--every-poll"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(poll_lines(parse_lines(out))), 4)


    def test_change_only_prints_terminal_snapshot_after_grace(self):
        now = 2_000_000_000.0
        idle = {"w": {"status": "idle", "session": None}}
        module = load_script("monitor-agents.py")
        clock = FakeClock(now)

        def sleep(_secs=None):
            clock.now = now + 10

        clock.sleep = sleep
        module.time = clock
        module.herdr = FakeHerdr({("agent", "list"): scripted([idle, idle])})
        module.notify = lambda *a: None
        code, out, err = run_module(module, [
            "monitor-agents.py", "w", "--interval", "0", "--grace", "5",
            "--timeout", "1000",
        ])
        self.assertEqual(code, 0, err)
        polls = poll_lines(parse_lines(out))
        self.assertEqual(len(polls), 2)
        self.assertEqual(polls[0]["pending"], ["w"])
        self.assertEqual(polls[-1]["pending"], [])

    def test_adaptive_interval_doubles_caps_and_resets_on_state_change(self):
        now = 2_000_000_000.0
        working = {"w": {"status": "working", "session": None}}
        starting = {"w": {"status": "starting", "session": None}}
        idle = {"w": {"status": "idle", "session": None}}
        module = load_script("monitor-agents.py")
        self._patch(module, [working, working, working, working, starting, idle], now=now)
        code, out, err = run_module(module, [
            "monitor-agents.py", "w", "--interval", "8", "--grace", "0",
        ])
        self.assertEqual(code, 0, err)
        self.assertEqual(module.time.sleeps, [2, 4, 8, 8, 2])
        polls = poll_lines(parse_lines(out))
        self.assertEqual([row["t"] - now for row in polls], [0, 22, 24])
        self.assertEqual([row["states"]["w"] for row in polls], ["working", "starting", "idle"])

    def test_two_transient_failures_then_success_reset_retry_allowance(self):
        now = 2_000_000_000.0
        module = load_script("monitor-agents.py")
        failure = module.ioutil.RunError("transient agent list transport failure")
        failure.code = "connection_reset"
        working = {"w": {"status": "working", "session": None}}
        idle = {"w": {"status": "idle", "session": None}}
        self._patch(module, [failure, failure, working, failure, failure, idle], now=now)
        code, out, err = run_module(module, ["monitor-agents.py", "w", "--grace", "0"])
        self.assertEqual(code, 0, err)
        polls = poll_lines(parse_lines(out))
        self.assertEqual([row["t"] - now for row in polls], [4, 10])
        self.assertEqual(polls[-1]["pending"], [])

    def test_poll_line_shape_backward_compatible(self):
        now = 2_000_000_000.0
        working = {"w": {"status": "working", "session": None}}
        idle = {"w": {"status": "idle", "session": None}}
        module = load_script("monitor-agents.py")
        self._patch(module, [working, idle], now=now)
        code, out, err = run_module(module, ["monitor-agents.py", "w", *FLAGS])
        self.assertEqual(code, 0, err)
        polls = poll_lines(parse_lines(out))
        self.assertGreaterEqual(len(polls), 1)
        for row in polls:
            self.assertEqual(set(row), {"t", "states", "pending", "last_activity", "idle_for"})
            self.assertIsInstance(row["states"]["w"], str)


if __name__ == "__main__":
    unittest.main()
