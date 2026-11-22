#!/usr/bin/env python3
"""Tests for deterministic `script` executor (`run-script.py`) and wave dispatcher (`dispatch-wave.py`)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cli_common
import graph_model
import ioutil
import ledger
import scheduler
from testkit import HOLDER, load_script, new_run, run_module, seed_repo

run_script = load_script("run-script.py")
dispatch_wave = load_script("dispatch-wave.py")


def _auth_args(prog: str, run: Path, epoch: int = 1) -> list[str]:
    return [
        prog,
        "--run", str(run),
        "--session", HOLDER["session"],
        "--holder-workspace", HOLDER["workspace"],
        "--holder-pane", HOLDER["pane"],
        "--epoch", str(epoch),
    ]
class ScriptExecutorValidationTest(unittest.TestCase):
    def test_valid_script_nodes_list_and_str_command(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "S1": {
                    "id": "S1",
                    "executor": "script",
                    "command": [sys.executable, "-c", "print('ok')"],
                    "timeout_sec": 30,
                    "cwd": "/tmp",
                    "outputs": [{"name": "summary", "type": "summary"}],
                },
                "S2": {
                    "id": "S2",
                    "executor": "script",
                    "command": "echo hello",
                    "depends_on": ["S1"],
                },
            },
        }
        self.assertIsNone(graph_model.validate_graph(graph))

    def test_script_node_validation_rejects_bad_command_timeout_cwd(self):
        cases = [
            ("missing command", {"id": "S", "executor": "script"}),
            ("empty str", {"id": "S", "executor": "script", "command": "   "}),
            ("empty list", {"id": "S", "executor": "script", "command": []}),
            ("blank list item", {"id": "S", "executor": "script", "command": ["echo", ""]}),
            ("bad timeout 0", {"id": "S", "executor": "script", "command": ["true"], "timeout_sec": 0}),
            ("bool timeout", {"id": "S", "executor": "script", "command": ["true"], "timeout_sec": True}),
            ("blank cwd", {"id": "S", "executor": "script", "command": ["true"], "cwd": "  "}),
        ]
        for label, node in cases:
            with self.subTest(label=label):
                with self.assertRaises(ioutil.PreconditionError):
                    graph_model.validate_graph({"schema_version": 1, "nodes": {"S": node}})


class RunScriptExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = seed_repo(self.root / "repo")

    def tearDown(self):
        self.tmp.cleanup()

    def test_passing_script_records_hashed_receipt_and_summary_output(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "CHECK": {
                    "id": "CHECK",
                    "executor": "script",
                    "command": [sys.executable, "-c", "import sys; print('hello stdout'); print('warn stderr', file=sys.stderr)"],
                    "outputs": [{"name": "summary", "type": "summary"}],
                }
            },
        }
        run = new_run(self.root, graph, repo=self.repo)
        code, out, err = run_module(run_script, [*_auth_args("run-script.py", run), "--node", "CHECK"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out.strip())
        self.assertEqual(payload["node"], "CHECK")
        self.assertEqual(payload["attempt"], 1)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["state"], "succeeded")

        receipt_path = Path(payload["receipt"]["path"])
        self.assertTrue(receipt_path.is_file())
        self.assertEqual(ioutil.sha256_file(receipt_path), payload["receipt"]["hash"])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["exit_code"], 0)
        self.assertIn("hello stdout", receipt["stdout"])
        self.assertIn("warn stderr", receipt["stderr"])
        self.assertEqual(receipt["cwd"], str(self.repo.resolve()))

        state = ledger.load_state(run)
        node = state["nodes"]["CHECK"]
        self.assertEqual(node["state"], "succeeded")
        self.assertEqual(len(node["artifacts"]), 1)
        summary_art = node["artifacts"][0]
        self.assertEqual(summary_art["name"], "summary")
        self.assertEqual(ioutil.sha256_file(summary_art["path"]), summary_art["hash"])

    def test_failing_script_records_failed_and_hashed_receipt(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "FAIL": {
                    "id": "FAIL",
                    "executor": "script",
                    "command": [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(7)"],
                }
            },
        }
        run = new_run(self.root, graph, repo=self.repo)
        code, out, err = run_module(run_script, [*_auth_args("run-script.py", run), "--node", "FAIL"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out.strip())
        self.assertEqual(payload["exit_code"], 7)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(ioutil.sha256_file(payload["receipt"]["path"]), payload["receipt"]["hash"])

        node = ledger.load_state(run)["nodes"]["FAIL"]
        self.assertEqual(node["state"], "failed")
        self.assertIn("7", node.get("reason", ""))

    def test_timeout_script_records_failed_with_timeout_reason(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "SLOW": {
                    "id": "SLOW",
                    "executor": "script",
                    "command": [sys.executable, "-c", "import time; time.sleep(5)"],
                    "timeout_sec": 1,
                }
            },
        }
        run = new_run(self.root, graph, repo=self.repo)
        code, out, err = run_module(run_script, [*_auth_args("run-script.py", run), "--node", "SLOW"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out.strip())
        self.assertIsNone(payload["exit_code"])
        self.assertEqual(payload["state"], "failed")
        receipt = json.loads(Path(payload["receipt"]["path"]).read_text(encoding="utf-8"))
        self.assertTrue(receipt["timed_out"])

        node = ledger.load_state(run)["nodes"]["SLOW"]
        self.assertEqual(node["state"], "failed")
        self.assertIn("timeout", node.get("reason", "").lower())

    def test_large_output_is_bounded_to_8kib(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "LOUD": {
                    "id": "LOUD",
                    "executor": "script",
                    "command": [sys.executable, "-c", "print('A' * 20000)"],
                }
            },
        }
        run = new_run(self.root, graph, repo=self.repo)
        code, out, err = run_module(run_script, [*_auth_args("run-script.py", run), "--node", "LOUD"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out.strip())
        receipt = json.loads(Path(payload["receipt"]["path"]).read_text(encoding="utf-8"))
        self.assertLessEqual(len(receipt["stdout"].encode("utf-8")), 8 * 1024)
        self.assertIn("...[truncated]...", receipt["stdout"])

    def test_stale_epoch_refuses_with_exit_3(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "S": {"id": "S", "executor": "script", "command": [sys.executable, "-c", "print(1)"]},
            },
        }
        run = new_run(self.root, graph, repo=self.repo)
        code, _out, err = run_module(run_script, [*_auth_args("run-script.py", run, epoch=99), "--node", "S"])
        self.assertEqual(code, 3)
        self.assertIn("epoch", err)


class DispatchWaveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = seed_repo(self.root / "repo")
        self.prompts = self.root / "prompts"
        self.prompts.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _ranking_graph(self) -> dict:
        # Ready roots:
        # - A_LEAF: depth=0, fanout=0 (lexicographically first, lowest impact)
        # - M_FANOUT: depth=1, fanout=3 (M1, M2, M3 depend on it)
        # - Z_DEEP: depth=3, fanout=3 (Z1 -> Z2 -> Z3)
        # - B_MID: depth=1, fanout=1 (B1 depends on it)
        # Expected order: Z_DEEP (3,3), M_FANOUT (1,3), B_MID (1,1), A_LEAF (0,0)
        return {
            "schema_version": 1,
            "nodes": {
                "A_LEAF": {"id": "A_LEAF", "executor": "herdr-agent"},
                "B_MID": {"id": "B_MID", "executor": "herdr-agent"},
                "B1": {"id": "B1", "executor": "herdr-agent", "depends_on": ["B_MID"]},
                "M_FANOUT": {"id": "M_FANOUT", "executor": "script", "command": [sys.executable, "-c", "print('m')"]},
                "M1": {"id": "M1", "executor": "herdr-agent", "depends_on": ["M_FANOUT"]},
                "M2": {"id": "M2", "executor": "herdr-agent", "depends_on": ["M_FANOUT"]},
                "M3": {"id": "M3", "executor": "herdr-agent", "depends_on": ["M_FANOUT"]},
                "Z_DEEP": {"id": "Z_DEEP", "executor": "herdr-agent"},
                "Z1": {"id": "Z1", "executor": "herdr-agent", "depends_on": ["Z_DEEP"]},
                "Z2": {"id": "Z2", "executor": "herdr-agent", "depends_on": ["Z1"]},
                "Z3": {"id": "Z3", "executor": "herdr-agent", "depends_on": ["Z2"]},
            },
        }

    def test_dry_run_ranks_by_critical_path_and_fanout_without_mutating(self):
        graph = self._ranking_graph()
        run = new_run(self.root, graph, repo=self.repo)
        for name in ("A_LEAF", "B_MID", "Z_DEEP"):
            (self.prompts / f"{name}.md").write_text(f"Prompt for {name}\n", encoding="utf-8")

        events_before = (run / "events.jsonl").read_text(encoding="utf-8")
        code, out, err = run_module(
            dispatch_wave,
            [*_auth_args("dispatch-wave.py", run), "--prompts-dir", str(self.prompts), "--dry-run"],
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out.strip())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(
            [item["node"] for item in payload["planned"]],
            ["Z_DEEP", "M_FANOUT", "B_MID", "A_LEAF"],
        )
        self.assertEqual(payload["planned"][0]["critical_path_depth"], 3)
        self.assertEqual(payload["planned"][0]["downstream_fanout"], 3)
        self.assertEqual(payload["planned"][1]["critical_path_depth"], 1)
        self.assertEqual(payload["planned"][1]["downstream_fanout"], 3)
        # No mutation occurred during --dry-run
        self.assertEqual((run / "events.jsonl").read_text(encoding="utf-8"), events_before)

    def test_max_parallel_counts_already_running_nodes(self):
        graph = self._ranking_graph()
        run = new_run(self.root, graph, repo=self.repo)
        for name in ("A_LEAF", "B_MID", "Z_DEEP"):
            (self.prompts / f"{name}.md").write_text(f"Prompt for {name}\n", encoding="utf-8")

        # Mark A_LEAF as already running.
        scheduler.reconcile(run, holder=HOLDER)
        ledger.append_event(run, {"type": "dispatch_intent", "node": "A_LEAF", "attempt": 1, "epoch": 1}, holder=HOLDER)
        ledger.append_event(
            run,
            {
                "type": "dispatched",
                "node": "A_LEAF",
                "attempt": 1,
                "epoch": 1,
                "model": {k: None for k in graph_model.MODEL_KEYS},
            },
            holder=HOLDER,
        )

        # With max_parallel=2 and 1 already running, only 1 slot remains (Z_DEEP).
        code, out, err = run_module(
            dispatch_wave,
            [*_auth_args("dispatch-wave.py", run), "--prompts-dir", str(self.prompts), "--max-parallel", "2", "--dry-run"],
        )
        self.assertEqual(code, 0, err)
        payload = json.loads(out.strip())
        self.assertEqual(payload["running"], 1)
        self.assertEqual([item["node"] for item in payload["planned"]], ["Z_DEEP"])
        self.assertEqual([item["node"] for item in payload["skipped"]], ["M_FANOUT", "B_MID"])
        self.assertTrue(all(item["reason"] == "max_parallel" for item in payload["skipped"]))

    def test_live_wave_dispatches_herdr_and_script_and_builds_monitor_cmd(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "S_RUN": {
                    "id": "S_RUN",
                    "executor": "script",
                    "command": [sys.executable, "-c", "print('script-ok')"],
                },
                "H_AGENT": {"id": "H_AGENT", "executor": "herdr-agent"},
                "DOWN": {"id": "DOWN", "executor": "herdr-agent", "depends_on": ["H_AGENT"]},
            },
        }
        run = new_run(self.root, graph, repo=self.repo)
        (self.prompts / "H_AGENT.txt").write_text("Do agent work\n", encoding="utf-8")

        calls: list[tuple[list[str], str]] = []

        def fake_run_child(argv: list[str], child_name: str, **_kwargs) -> dict:
            calls.append((argv, child_name))
            if child_name == "dispatch-agent.py":
                return {
                    "node": "H_AGENT",
                    "attempt": 1,
                    "worker_id": "fo-0123456789abcdef01234567",
                    "state": "running",
                }
            # Let run-script.py execute for real via run_module
            sub_code, sub_out, sub_err = run_module(run_script, argv[1:])
            self.assertEqual(sub_code, 0, sub_err)
            return json.loads(sub_out.strip())

        with mock.patch.object(cli_common, "run_child", side_effect=fake_run_child):
            code, out, err = run_module(
                dispatch_wave,
                [*_auth_args("dispatch-wave.py", run), "--prompts-dir", str(self.prompts)],
            )
        self.assertEqual(code, 0, err)
        payload = json.loads(out.strip())
        # H_AGENT has depth=1, fanout=1; S_RUN has depth=0, fanout=0 -> H_AGENT first.
        self.assertEqual([item["node"] for item in payload["dispatched"]], ["H_AGENT", "S_RUN"])
        self.assertEqual(payload["skipped"], [])
        self.assertEqual(calls[0][1], "dispatch-agent.py")
        self.assertIn(str((self.prompts / "H_AGENT.txt").resolve()), calls[0][0])
        self.assertEqual(calls[1][1], "run-script.py")
        self.assertIn("monitor-agents.py", payload["monitor_cmd"][1])
        self.assertIn("fo-0123456789abcdef01234567", payload["monitor_cmd"])
        self.assertEqual(ledger.load_state(run)["nodes"]["S_RUN"]["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
