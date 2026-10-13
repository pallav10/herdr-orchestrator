#!/usr/bin/env python3
"""Focused consumer-boundary coverage for collection, retirement, and monitoring."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from testkit import FakeHerdr, load_script, run_module


class HelperCoverageTest(unittest.TestCase):
    def test_collection_terminal_parsing_and_question_boundaries(self):
        module = load_script("collect-agent.py")
        raw = (
            "\x1b]0;worker title\x07╭── terminal ──╮\n"
            "│ $ omp run │\n│ Output │\n├────────────┤\n"
            " old context \n│ \x1b[32mPatched parser\x1b[0m │\n"
            "│ Preserved receipts │\nRan focused checks\n"
            "  Ship   this change?  \n⟨Wall: 2s⟩\n╰────────────╯\n"
        )
        self.assertEqual(
            module.strip_terminal_chrome(raw),
            "old context\nPatched parser\nPreserved receipts\nRan focused checks\nShip   this change?",
        )
        fake = FakeHerdr({("agent", "read"): raw})
        with mock.patch.object(module, "herdr_text", fake):
            summary = module.terminal_summary("worker")
        self.assertEqual(
            summary,
            "Patched parser Preserved receipts Ran focused checks Ship   this change?",
        )
        self.assertEqual(fake.calls, [
            ("agent", "read", "worker", "--source", "recent-unwrapped", "--lines", "120"),
        ])
        questions = [
            (summary, "Patched parser Preserved receipts Ran focused checks Ship this change?"),
            ("First question?\n  Use   branch B? \n", "Use branch B?"),
            ("Question? Answer follows.", None),
            ("\n  \t", None),
        ]
        for text, expected in questions:
            with self.subTest(text=text):
                self.assertEqual(module.trailing_question(text), expected)
        chrome = FakeHerdr({("agent", "read"): "│ Output │\n$ omp run\n───\n"})
        with mock.patch.object(module, "herdr_text", chrome):
            self.assertEqual(module.terminal_summary("worker"), "")

    def test_retirement_summary_selects_requested_agent(self):
        module = load_script("retire-agents.py")
        self.assertEqual(module.extract_collect_summary({"agents": [
            {"name": "other", "summary": "wrong summary"},
            {"name": "worker", "summary": "collected result"},
        ]}, "worker"), "collected result")
        self.assertEqual(module.extract_collect_summary({"agents": [{"name": "worker"}]}, "worker"), "")
        for payload, message in [
            ({"agents": {}}, "missing agents for worker"),
            ({"agents": [{"name": "other", "summary": "wrong"}]}, "did not include worker"),
        ]:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(RuntimeError, message):
                    module.extract_collect_summary(payload, "worker")

    def test_retirement_collects_all_candidates_before_closing(self):
        module = load_script("retire-agents.py")
        order = []
        routes = {}
        for name in ("worker-a", "worker-b"):
            routes[("agent", "get", name)] = {"result": {"agent": {
                "name": name, "workspace_id": "ws-" + name, "agent_status": "done",
            }}}
            routes[("workspace", "get", "ws-" + name)] = {"result": {"workspace": {
                "tokens": {"provenance": "orchestrator"},
            }}}
        routes[("workspace", "close")] = lambda *args: order.append(("close", args[2])) or {}
        fake = FakeHerdr(routes)

        def collect(argv, **kwargs):
            name = argv[-1]
            order.append(("collect", name))
            self.assertEqual(Path(argv[1]).name, "collect-agent.py")
            return subprocess.CompletedProcess(argv, 0, "diagnostic before JSON\n" + json.dumps({
                "agents": [{"name": "other", "summary": "not ours"},
                           {"name": name, "summary": "result " + name}],
            }) + "\n", "")

        with mock.patch.object(module, "herdr", fake), mock.patch.object(module.ioutil, "run_cmd", side_effect=collect):
            code, out, err = run_module(module, ["retire-agents.py", "worker-a", "worker-b", "--collect"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"retired": [
            {"name": "worker-a", "workspace": "ws-worker-a", "summary": "result worker-a"},
            {"name": "worker-b", "workspace": "ws-worker-b", "summary": "result worker-b"},
        ], "refused": []})
        self.assertEqual(order, [
            ("collect", "worker-a"), ("collect", "worker-b"),
            ("close", "ws-worker-a"), ("close", "ws-worker-b"),
        ])

    def test_retirement_collection_errors_never_close_workspaces(self):
        # A later failed collection must not close an earlier successful candidate.
        failures = [
            (7, "", "collector unavailable"),
            (4, "", ""),
            (0, "not JSON\n", ""),
            (0, '{"agents": []}\n', ""),
            (0, '{}\n', ""),
        ]
        for returncode, stdout, stderr in failures:
            with self.subTest(returncode=returncode, stdout=stdout, stderr=stderr):
                module = load_script("retire-agents.py")
                routes = {}
                for name in ("worker-a", "worker-b"):
                    routes[("agent", "get", name)] = {"result": {"agent": {
                        "name": name, "workspace_id": "ws-" + name, "agent_status": "done",
                    }}}
                    routes[("workspace", "get", "ws-" + name)] = {"result": {"workspace": {
                        "tokens": {"provenance": "orchestrator"},
                    }}}
                routes[("workspace", "close")] = {}
                fake = FakeHerdr(routes)
                collected = []

                def collect(argv, **kwargs):
                    collected.append(argv[-1])
                    if argv[-1] == "worker-a":
                        return subprocess.CompletedProcess(argv, 0, json.dumps({"agents": [
                            {"name": "worker-a", "summary": "safe result"},
                        ]}), "")
                    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

                with mock.patch.object(module, "herdr", fake), mock.patch.object(module.ioutil, "run_cmd", side_effect=collect):
                    code, out, err = run_module(module, ["retire-agents.py", "worker-a", "worker-b", "--collect"])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                if stderr:
                    self.assertIn(stderr, err)
                self.assertEqual(collected, ["worker-a", "worker-b"])
                self.assertEqual([call for call in fake.calls if call[:2] == ("workspace", "close")], [])

                def successful_collect(argv, **kwargs):
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"agents": [
                        {"name": argv[-1], "summary": "result " + argv[-1]},
                    ]}), "")

                with mock.patch.object(module, "herdr", fake), mock.patch.object(module.ioutil, "run_cmd", side_effect=successful_collect):
                    code, out, err = run_module(module, ["retire-agents.py", "worker-a", "worker-b", "--collect"])
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(json.loads(out), {"retired": [
                    {"name": "worker-a", "workspace": "ws-worker-a", "summary": "result worker-a"},
                    {"name": "worker-b", "workspace": "ws-worker-b", "summary": "result worker-b"},
                ], "refused": []})
                self.assertEqual([call for call in fake.calls if call[:2] == ("workspace", "close")], [
                    ("workspace", "close", "ws-worker-a"),
                    ("workspace", "close", "ws-worker-b"),
                ])

    def test_monitor_inventory_validates_states_and_sessions(self):
        module = load_script("monitor-agents.py")
        fake = FakeHerdr({("agent", "list"): {"result": {"agents": [
            {"name": "worker-a", "agent_status": "working", "agent_session": {"value": "/isolated/a.jsonl"}},
            {"name": "worker-b", "agent_status": "blocked", "agent_session": {"value": 42}},
            {"name": "worker-c", "agent_status": "done"},
            {"agent_status": "working"},
        ]}}})
        with mock.patch.object(module, "herdr", fake):
            self.assertEqual(module.agent_states(), {
                "worker-a": {"status": "working", "session": "/isolated/a.jsonl"},
                "worker-b": {"status": "blocked", "session": None},
                "worker-c": {"status": "done", "session": None},
            })
        for agents in ({}, [None], [{"name": "worker"}], [{"name": "worker", "agent_status": 4}]):
            with self.subTest(agents=agents):
                fake = FakeHerdr({("agent", "list"): {"result": {"agents": agents}}})
                with mock.patch.object(module, "herdr", fake):
                    with self.assertRaisesRegex(RuntimeError, "herdr agent list returned unexpected output"):
                        module.agent_states()

    def test_monitor_notification_failure_is_reported_and_best_effort(self):
        module = load_script("monitor-agents.py")
        failures = [
            module.ioutil.RunError("pane gone"),
            OSError("transport unavailable"),
            subprocess.TimeoutExpired("herdr", 1),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                fake = FakeHerdr({("pane", "run"): failure})
                out = io.StringIO()
                with mock.patch.object(module, "herdr", fake), contextlib.redirect_stdout(out):
                    module.notify("controller", "batch-a", "worker blocked")
                    fake.routes[("pane", "run")] = {}
                    module.notify("controller", "batch-a", "worker done")
                    module.notify(None, "batch-a", "must not send")
                self.assertEqual(fake.calls, [
                    ("pane", "run", "controller", "[monitor batch-a] worker blocked"),
                    ("pane", "run", "controller", "[monitor batch-a] worker done"),
                ])
                rows = [json.loads(line) for line in out.getvalue().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["notify_failed"], "controller")
                self.assertEqual(rows[0]["detail"], f"{type(failure).__name__}: {failure}"[:200])

    def test_role_cache_miss_hit_mtime_refresh_and_corruption(self):
        module = load_script("spawn-agent.py")
        with tempfile.TemporaryDirectory(prefix="herdr-helper-home-") as tmp:
            home = Path(tmp)
            config = home / ".omp/agent/config.yml"
            cache = home / ".omp/run/herdr-cache/model-roles.json"
            config.parent.mkdir(parents=True)
            config.write_text("modelRoles: isolated\n", encoding="utf-8")
            initial_mtime = 1_700_000_000_000_000_000
            os.utime(config, ns=(initial_mtime, initial_mtime))
            roles = {"slow": "provider/first:high", "smol": "provider/small"}

            def omp(argv, **kwargs):
                self.assertEqual(argv, ["omp", "config", "get", "modelRoles", "--json"])
                return subprocess.CompletedProcess(argv, 0, json.dumps({"value": roles}), "")

            with mock.patch.object(module.Path, "home", return_value=home), mock.patch.object(module.ioutil, "run_cmd", side_effect=omp) as runner:
                self.assertEqual(module.resolve_role("slow"), "provider/first:high")
                self.assertEqual(runner.call_count, 1)
                saved = cache.read_bytes()
                self.assertEqual(json.loads(saved), {"config_mtime_ns": config.stat().st_mtime_ns, "roles": roles})
                roles = {"slow": "provider/second:low", "smol": "provider/new-small"}
                self.assertEqual(module.resolve_role("slow"), "provider/first:high")
                self.assertEqual(module.resolve_role("smol"), "provider/small")
                self.assertEqual(runner.call_count, 1)
                self.assertEqual(cache.read_bytes(), saved)
                changed_mtime = initial_mtime + 2_000_000_000
                os.utime(config, ns=(changed_mtime, changed_mtime))
                self.assertEqual(module.resolve_role("slow"), "provider/second:low")
                self.assertEqual(runner.call_count, 2)
                self.assertEqual(json.loads(cache.read_text(encoding="utf-8")), {
                    "config_mtime_ns": config.stat().st_mtime_ns, "roles": roles,
                })
                for corrupt in ("{truncated", json.dumps({
                    "config_mtime_ns": config.stat().st_mtime_ns, "roles": {"slow": 42},
                })):
                    cache.write_text(corrupt, encoding="utf-8")
                    roles = {"slow": "provider/recovered"}
                    before = runner.call_count
                    self.assertEqual(module.resolve_role("slow"), "provider/recovered")
                    self.assertEqual(runner.call_count, before + 1)
                    self.assertEqual(json.loads(cache.read_text(encoding="utf-8")), {
                        "config_mtime_ns": config.stat().st_mtime_ns, "roles": roles,
                    })


if __name__ == "__main__":
    unittest.main()
