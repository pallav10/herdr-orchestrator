#!/usr/bin/env python3
"""Unit tests for inspect-run.py and doctor.py."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import context_usage
import ioutil
import ledger
from testkit import HOLDER, commit_file, load_script, new_run, plant_run_files, run_module, seed_repo, write_events

inspect_mod = load_script("inspect-run.py")
doctor_mod = load_script("doctor.py")


class InspectRunTests(unittest.TestCase):
    def _sample_graph(self) -> dict:
        return {
            "schema_version": 1,
            "nodes": {
                "A": {
                    "id": "A",
                    "executor": "herdr-agent",
                    "worktree": "a",
                    "owns": ["src/a/**"],
                    "outputs": [{"name": "vcs", "type": "vcs"}],
                },
                "B": {
                    "id": "B",
                    "executor": "herdr-agent",
                    "depends_on": ["A"],
                    "integrates": True,
                    "worktree": True,
                    "owns": ["**"],
                    "outputs": [
                        {"name": "vcs", "type": "vcs"},
                        {"name": "integration", "type": "integration"},
                    ],
                },
                "C": {
                    "id": "C",
                    "executor": "task",
                    "outputs": [{"name": "summary", "type": "summary"}],
                },
                "D": {
                    "id": "D",
                    "executor": "herdr-agent",
                    "outputs": [{"name": "summary", "type": "summary"}],
                },
            }
        }

    def test_json_and_text_modes_durations_lock_and_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = seed_repo(tmp / "repo")
            run = new_run(tmp / "docs", self._sample_graph(), repo=repo)
            run_doc = ledger.load_run(run)

            # Seed events directly so we control exact timestamps and node states cleanly.
            a_art = run / "artifacts" / "A" / "1" / "vcs.json"
            a_art.parent.mkdir(parents=True, exist_ok=True)
            a_art.write_text('{"result":{"form":"commit"}}\n', encoding="utf-8")
            (a_art.parent / "dispatch-identity.json").write_text(
                json.dumps({"worker_id": "fo-" + "a" * 24, "worktree_path": "/tmp/wt-a"}) + "\n",
                encoding="utf-8",
            )

            # B has an integration receipt ready for promotion while running.
            b_dir = run / "artifacts" / "B" / "1"
            b_dir.mkdir(parents=True, exist_ok=True)
            (b_dir / "integration.json").write_text('{"result":"ok"}\n', encoding="utf-8")

            events = ledger.replay(run)["events"]
            events.extend([
                {
                    "type": "dispatched",
                    "node": "A",
                    "attempt": 1,
                    "epoch": 1,
                    "t": "2026-09-04T01:05:00.000000Z",
                    "handle": {
                        "workspace": "ws-a",
                        "pane": "pane-a",
                        "agent": "agt-a",
                        "worker_id": "fo-" + "a" * 24,
                        "worktree_path": "/tmp/wt-a",
                    },
                },
                {
                    "type": "succeeded",
                    "node": "A",
                    "attempt": 1,
                    "epoch": 1,
                    "t": "2026-09-04T01:05:12.500000Z",
                    "artifacts": [{"name": "vcs", "path": str(a_art), "hash": "sha256:00"}],
                },
                {
                    "type": "dispatched",
                    "node": "B",
                    "attempt": 1,
                    "epoch": 1,
                    "t": "2026-09-04T01:06:00.000000Z",
                    "handle": {"workspace": "ws-b", "pane": "pane-b", "agent": "agt-b"},
                },
                {
                    "type": "ready",
                    "node": "C",
                    "attempt": 0,
                    "epoch": 1,
                    "t": "2026-09-04T01:06:01.000000Z",
                },
                {
                    "type": "dispatched",
                    "node": "D",
                    "attempt": 1,
                    "epoch": 1,
                    "t": "2026-09-04T01:06:02.000000Z",
                    "handle": {"workspace": "ws-d", "pane": "pane-d", "agent": "agt-d"},
                },
                {
                    "type": "blocked",
                    "node": "D",
                    "attempt": 1,
                    "epoch": 1,
                    "t": "2026-09-04T01:06:05.000000Z",
                    "question": "Which API version?",
                },
            ])
            write_events(run, events)

            # Write an unverified lock owner sidecar.
            ledger.write_lock_owner(
                run / "run.lock.owner",
                {"pid": 4242, "script": "dispatch-agent.py", "node": "B", "since": "2026-09-04T01:06:00Z"},
            )
            state_before = (run / "state.json").read_bytes()

            code, out, err = run_module(inspect_mod, ["inspect-run.py", "--run", str(run)])
            self.assertEqual(code, 0, err)
            self.assertEqual(err, "")
            report = json.loads(out)

            # Read-only guarantee: caches untouched.
            self.assertEqual((run / "state.json").read_bytes(), state_before)

            self.assertEqual(report["run"]["id"], run_doc["id"])
            self.assertEqual(report["run"]["job"], "Job")
            self.assertEqual(report["run"]["epoch"], 1)
            self.assertEqual(report["run"]["lease"], "active")
            self.assertEqual(report["run"]["holder"], HOLDER)
            self.assertFalse(report["run"]["truncated_tail"])

            node_a = report["nodes"]["A"]
            self.assertEqual(node_a["state"], "succeeded")
            self.assertEqual(node_a["attempt"], 1)
            self.assertEqual(node_a["duration_sec"], 12.5)
            self.assertEqual(node_a["worker_id"], "fo-" + "a" * 24)
            self.assertEqual(node_a["worktree"], "/tmp/wt-a")
            self.assertEqual(node_a["artifact_count"], 1)

            self.assertEqual(report["nodes"]["D"]["question"], "Which API version?")

            self.assertTrue(report["lock"]["present"])
            self.assertTrue(report["lock"]["unverified"])
            self.assertIn("pid=4242", report["lock"]["summary"])
            self.assertIn("unverified", report["lock"]["summary"])

            by_node = {(a["node"], a["action"]) for a in report["next_actions"]}
            self.assertIn(("A", "retire"), by_node)
            self.assertIn(("B", "promote"), by_node)
            self.assertIn(("C", "dispatch"), by_node)
            self.assertIn(("D", "unblock"), by_node)

            # Text output mode
            code_t, out_t, err_t = run_module(inspect_mod, ["inspect-run.py", "--run", str(run), "--format", "text"])
            self.assertEqual(code_t, 0, err_t)
            self.assertIn("NODE", out_t)
            self.assertIn("12.500", out_t)
            self.assertIn("Next actions:", out_t)
            self.assertIn("[promote] B:", out_t)
            self.assertIn("Lock:", out_t)

            # Single-node filter
            code_n, out_n, err_n = run_module(inspect_mod, ["inspect-run.py", "--run", str(run), "--node", "A"])
            self.assertEqual(code_n, 0, err_n)
            filtered = json.loads(out_n)
            self.assertEqual(list(filtered["nodes"].keys()), ["A"])

            # Unknown node -> exit 2 (UsageError)
            code_bad, _, err_bad = run_module(inspect_mod, ["inspect-run.py", "--run", str(run), "--node", "NOPE"])
            self.assertEqual(code_bad, 2)
            self.assertIn("unknown node", err_bad)

    def test_truncated_tail_and_draining_lease_next_actions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run"
            plant_run_files(
                run,
                nodes={"A": {"executor": "herdr-agent", "outputs": []}},
            )
            write_events(
                run,
                [
                    {"type": "session_run_bound", "epoch": 1, **HOLDER},
                    {"type": "lease_drain_started", "epoch": 1, **HOLDER},
                    {"type": "dispatched", "node": "A", "attempt": 1, "epoch": 1, "t": "2026-09-04T01:00:00Z"},
                ],
            )
            with open(run / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write('{"type":"succeeded","truncated')

            code, out, err = run_module(inspect_mod, ["inspect-run.py", "--run", str(run)])
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertTrue(report["run"]["truncated_tail"])
            self.assertEqual(report["run"]["lease"], "draining")
            actions = {a["action"] for a in report["next_actions"]}
            self.assertIn("repair", actions)
            self.assertIn("handoff", actions)
            self.assertIn("retry_or_replan", actions)


class DoctorTests(unittest.TestCase):
    def _checks_map(self, payload: dict) -> dict[str, dict]:
        return {item["name"]: item for item in payload["checks"]}

    def test_gitignore_pass_and_fail_with_skip_herdr(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = seed_repo(Path(td) / "repo")

            # Missing .gitignore rules -> fail (exit 3)
            code, out, err = run_module(doctor_mod, ["doctor.py", "--repo", str(repo), "--skip-herdr"])
            self.assertEqual(code, 3, err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            checks = self._checks_map(payload)
            self.assertEqual(checks["python"]["status"], "ok")
            self.assertEqual(checks["fcntl"]["status"], "ok")
            self.assertEqual(checks["git_repo"]["status"], "ok")
            self.assertEqual(checks["git_worktree"]["status"], "ok")
            self.assertEqual(checks["gitignore"]["status"], "fail")
            self.assertIn("/docs/runs/", checks["gitignore"]["detail"])
            self.assertIn("/docs/sessions/", checks["gitignore"]["detail"])

            # Add ignore rules and commit -> pass (exit 0)
            commit_file(repo, ".gitignore", "/docs/runs/\n/docs/sessions/\n", "ignore runtime docs")
            code2, out2, err2 = run_module(doctor_mod, ["doctor.py", "--repo", str(repo), "--skip-herdr"])
            self.assertEqual(code2, 0, err2)
            payload2 = json.loads(out2)
            self.assertTrue(payload2["ok"])
            checks2 = self._checks_map(payload2)
            self.assertEqual(checks2["gitignore"]["status"], "ok")
            self.assertEqual(checks2["git_clean"]["status"], "ok")

    def test_missing_herdr_env_and_mocked_protocol_check(self) -> None:
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        with (
            mock.patch.dict(os.environ, clean_env, clear=True),
            mock.patch.object(doctor_mod.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            mock.patch.object(
                doctor_mod.herdr_cli,
                "herdr",
                return_value={"result": {"protocol_version": 20}},
            ) as mock_herdr,
        ):
            code, out, err = run_module(doctor_mod, ["doctor.py"])
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertTrue(payload["ok"])
            checks = self._checks_map(payload)
            self.assertEqual(checks["herdr_env"]["status"], "warn")
            self.assertIn("HERDR_ENV", checks["herdr_env"]["detail"])
            self.assertEqual(checks["herdr_protocol"]["status"], "ok")
            self.assertIn("20", checks["herdr_protocol"]["detail"])
            mock_herdr.assert_called_once_with("api", "schema", "--json")

    def test_herdr_protocol_failure_exits_3(self) -> None:
        env = {
            **os.environ,
            "HERDR_ENV": "1",
            "HERDR_WORKSPACE_ID": "ws-1",
            "HERDR_PANE_ID": "pane-1",
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(doctor_mod.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}"),
            mock.patch.object(
                doctor_mod.herdr_cli,
                "herdr",
                side_effect=ioutil.RunError("herdr api schema --json failed: server_not_running"),
            ),
        ):
            code, out, err = run_module(doctor_mod, ["doctor.py"])
            self.assertEqual(code, 3, err)
            payload = json.loads(out)
            self.assertFalse(payload["ok"])
            checks = self._checks_map(payload)
            self.assertEqual(checks["herdr_env"]["status"], "ok")
            self.assertEqual(checks["herdr_protocol"]["status"], "fail")
            self.assertIn("server_not_running", checks["herdr_protocol"]["detail"])

    def test_context_extension_fresh_and_stale_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            env = {
                **os.environ,
                "HOME": str(fake_home),
                "HERDR_ENV": "1",
                "HERDR_WORKSPACE_ID": "ws-ctx",
                "HERDR_PANE_ID": "pane-ctx",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                snap = context_usage.context_snapshot_path("ws-ctx", "pane-ctx")
                snap.parent.mkdir(parents=True, exist_ok=True)

                # Fresh snapshot -> ok
                snap.write_text(json.dumps({"observed_at_ms": int(time.time() * 1000)}) + "\n", encoding="utf-8")
                code, out, _ = run_module(doctor_mod, ["doctor.py", "--skip-herdr"])
                self.assertEqual(code, 0)
                checks = self._checks_map(json.loads(out))
                self.assertEqual(checks["omp_context_extension"]["status"], "ok")

                # Stale snapshot -> warn (still exits 0)
                snap.write_text(json.dumps({"observed_at_ms": int(time.time() * 1000) - 60000}) + "\n", encoding="utf-8")
                code2, out2, _ = run_module(doctor_mod, ["doctor.py", "--skip-herdr"])
                self.assertEqual(code2, 0)
                checks2 = self._checks_map(json.loads(out2))
                self.assertEqual(checks2["omp_context_extension"]["status"], "warn")


if __name__ == "__main__":
    unittest.main()
