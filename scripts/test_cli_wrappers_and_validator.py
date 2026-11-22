#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ledger
import testkit


def _run_cli(script_name: str, args: list[str], *, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script_name), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
    )


def _holder_args(holder: dict[str, str]) -> list[str]:
    return [
        "--session",
        holder["session"],
        "--holder-workspace",
        holder["workspace"],
        "--holder-pane",
        holder["pane"],
    ]


class AcceptGraphCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = testkit.seed_repo(self.root / "repo")
        self.holder = dict(testkit.HOLDER)
        self.run = testkit.new_run(self.root, None, holder=self.holder)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_accept_graph_from_file_and_stdin(self) -> None:
        graph = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "owns": ["src/a/**"]},
                "B": {"id": "B", "executor": "task", "depends_on": ["A"], "owns": ["src/b/**"]},
            },
        }
        graph_file = self.root / "graph.json"
        graph_file.write_text(json.dumps(graph), encoding="utf-8")

        proc = _run_cli(
            "accept-graph.py",
            [
                "--run",
                str(self.run),
                "--graph-file",
                str(graph_file),
                "--repo",
                str(self.repo),
                *_holder_args(self.holder),
                "--lock-timeout",
                "10",
            ],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["base_sha"], testkit.git(self.repo, "rev-parse", "HEAD"))
        self.assertTrue(payload["promotion_ref"].startswith("refs/heads/herdr/"))
        self.assertEqual(sorted(payload["nodes"]), ["A", "B"])

        # Re-accept via stdin with an added node
        graph["nodes"]["C"] = {"id": "C", "executor": "task", "depends_on": ["B"], "owns": ["src/c/**"]}
        proc2 = _run_cli(
            "accept-graph.py",
            [
                "--run",
                str(self.run),
                "--graph-file",
                "-",
                "--repo",
                str(self.repo),
                *_holder_args(self.holder),
            ],
            stdin_text=json.dumps(graph),
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        payload2 = json.loads(proc2.stdout)
        self.assertEqual(payload2["base_sha"], payload["base_sha"])
        self.assertEqual(sorted(payload2["nodes"]), ["A", "B", "C"])

    def test_accept_graph_rejects_invalid_json_and_foreign_holder(self) -> None:
        proc_bad = _run_cli(
            "accept-graph.py",
            [
                "--run",
                str(self.run),
                "--graph-file",
                "-",
                "--repo",
                str(self.repo),
                *_holder_args(self.holder),
            ],
            stdin_text="{not-json",
        )
        self.assertEqual(proc_bad.returncode, 2)

        foreign = {"workspace": "other-ws", "pane": "other-pane", "session": "other-session"}
        proc_foreign = _run_cli(
            "accept-graph.py",
            [
                "--run",
                str(self.run),
                "--graph-file",
                "-",
                "--repo",
                str(self.repo),
                *_holder_args(foreign),
            ],
            stdin_text=json.dumps({"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}),
        )
        self.assertEqual(proc_foreign.returncode, 3)


class PromoteRunCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = testkit.seed_repo(self.root / "repo")
        self.holder = dict(testkit.HOLDER)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _prepare_integrator_run(self, *, extra_node: bool = False) -> tuple[Path, str]:
        nodes: dict[str, object] = {
            "D": {
                "id": "D",
                "executor": "herdr-agent",
                "outputs": [
                    {"name": "integration", "type": "integration"},
                    {"name": "vcs", "type": "vcs"},
                ],
                "worktree": "d",
                "owns": ["src/d/**"],
            }
        }
        if extra_node:
            nodes["Z"] = {"id": "Z", "executor": "task", "depends_on": ["D"]}
        graph = {"schema_version": 1, "nodes": nodes}
        run = testkit.new_run(self.root, graph, repo=self.repo, holder=self.holder)
        run_json = json.loads((run / "run.json").read_text(encoding="utf-8"))
        base_sha = run_json["base_sha"]

        wt = testkit.make_worktree(self.repo, self.root / "wt-d", "herdr-d-1", base=base_sha)
        new_sha = testkit.commit_file(wt, "src/d/out.txt", "integrated\n", "integrate d")

        ledger.append_event(run, {"type": "dispatch_intent", "node": "D", "attempt": 1}, holder=self.holder)
        attempt_dir = run / "artifacts" / "D" / "1"
        handle = {
            "workspace": "ws-D",
            "pane": "pane-D",
            "agent": "agent-D",
            "repo": str(self.repo.resolve()),
            "worktree_path": str(wt.resolve()),
            "branch": "herdr-d-1",
            "base": base_sha,
        }
        model = {
            "requested_role": None,
            "requested_effort": None,
            "resolved": None,
            "thinking": None,
            "effort": None,
        }
        identity = {
            "workspace": handle["workspace"],
            "pane": handle["pane"],
            "agent": handle["agent"],
            "run": str(run.resolve()),
            "run_id": run_json["id"],
            "node": "D",
            "attempt": "1",
            "tokens": {"provenance": "orchestrator", "run": run_json["id"], "node": "D", "attempt": "1"},
            "worktree_path": handle["worktree_path"],
            "repo": handle["repo"],
            "branch": handle["branch"],
            "base": base_sha,
            "routing": model,
        }
        (attempt_dir / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
        ledger.append_event(
            run,
            {"type": "dispatched", "node": "D", "attempt": 1, "handle": handle, "model": model},
            holder=self.holder,
        )
        consumed = json.loads((attempt_dir / "consumed_artifacts.json").read_text(encoding="utf-8"))
        (attempt_dir / "integration.json").write_text(
            json.dumps({"commit": new_sha, "head": new_sha, "base_sha": base_sha, "consumed": consumed}) + "\n",
            encoding="utf-8",
        )
        (attempt_dir / "vcs.json").write_text(
            json.dumps(
                {
                    "worktree": handle["worktree_path"],
                    "repo": handle["repo"],
                    "branch": handle["branch"],
                    "base_sha": base_sha,
                    "result": {"form": "commit", "sha": new_sha},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return run, new_sha

    def test_promote_and_deliver_only(self) -> None:
        run, new_sha = self._prepare_integrator_run(extra_node=True)
        run_json = json.loads((run / "run.json").read_text(encoding="utf-8"))

        proc = _run_cli(
            "promote-run.py",
            [
                "--run",
                str(run),
                "--node",
                "D",
                "--attempt",
                "1",
                "--new-sha",
                new_sha,
                "--repo",
                str(self.repo),
                *_holder_args(self.holder),
                "--lock-timeout",
                "10",
            ],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["event"]["type"], "succeeded")
        self.assertIsNone(payload["delivery_branch"])
        self.assertEqual(testkit.git(self.repo, "rev-parse", run_json["promotion_ref"]), new_sha)

        # Complete trailing node Z, then run --deliver-only with explicit --branch
        ledger.append_event(run, {"type": "ready", "node": "Z", "from": "planned", "to": "ready"}, holder=self.holder)
        ledger.append_event(run, {"type": "dispatch_intent", "node": "Z", "attempt": 1}, holder=self.holder)
        ledger.append_event(
            run,
            {
                "type": "dispatched",
                "node": "Z",
                "attempt": 1,
                "handle": {"spawning_session": self.holder["session"], "job_id": "z-1"},
                "model": {"requested_role": None, "requested_effort": None, "resolved": None, "thinking": None, "effort": None},
            },
            holder=self.holder,
        )
        ledger.append_event(run, {"type": "succeeded", "node": "Z", "attempt": 1, "artifacts": []}, holder=self.holder)

        proc_deliver = _run_cli(
            "promote-run.py",
            [
                "--deliver-only",
                "--run",
                str(run),
                "--repo",
                str(self.repo),
                "--branch",
                "feature/p1-delivered",
                *_holder_args(self.holder),
            ],
        )
        self.assertEqual(proc_deliver.returncode, 0, proc_deliver.stderr)
        delivered = json.loads(proc_deliver.stdout)
        self.assertEqual(delivered["delivery_branch"], "refs/heads/feature/p1-delivered")
        self.assertEqual(delivered["sha"], new_sha)
        self.assertTrue(delivered["created"])
        self.assertEqual(testkit.git(self.repo, "rev-parse", "refs/heads/feature/p1-delivered"), new_sha)

    def test_promote_cli_argument_validation(self) -> None:
        proc = _run_cli(
            "promote-run.py",
            [
                "--run",
                str(self.root / "missing"),
                "--repo",
                str(self.repo),
                *_holder_args(self.holder),
            ],
        )
        self.assertEqual(proc.returncode, 2)


class DispatchAgentCliTest(unittest.TestCase):
    def test_non_herdr_executor_cannot_mutate_ledger(self) -> None:
        holder = dict(testkit.HOLDER)
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}
        with tempfile.TemporaryDirectory() as tmp:
            run = testkit.new_run(Path(tmp), graph, holder=holder)
            snap = ledger.replay(run)
            self.assertEqual(snap["state"]["nodes"]["A"]["state"], "ready")
            events_path = run / "events.jsonl"
            before = events_path.read_bytes()
            proc = _run_cli(
                "dispatch-agent.py",
                [
                    "--run", str(run), "--node", "A", "--prompt", "work",
                    "--epoch", str(snap["controller"]["epoch"]), *_holder_args(holder),
                ],
            )
            self.assertEqual(proc.returncode, 3, proc.stderr)
            self.assertIn("executor must be herdr-agent", proc.stderr)
            self.assertEqual(events_path.read_bytes(), before)


class ValidateGraphCliTest(unittest.TestCase):
    def test_valid_dag_waves_and_max_parallelism(self) -> None:
        graph = {
            "schema_version": 1,
            "nodes": {
                "n1": {"id": "n1", "executor": "task", "owns": ["src/a/**"]},
                "n2": {"id": "n2", "executor": "task", "owns": ["src/b/**"]},
                "n3": {"id": "n3", "executor": "task", "depends_on": ["n1", "n2"], "owns": ["src/a/**", "src/b/**"]},
                "n4": {"id": "n4", "executor": "task", "depends_on": ["n1"], "owns": ["src/c/**"]},
            },
        }
        proc = _run_cli("validate-graph.py", ["--graph-file", "-"], stdin_text=json.dumps(graph))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertTrue(report["valid"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["cycles"], [])
        self.assertEqual(report["overlaps"], [])
        self.assertEqual(report["waves"], [["n1", "n2"], ["n3", "n4"]])
        self.assertEqual(report["max_parallelism"], 2)

    def test_multiple_tarjan_scc_cycles_reported(self) -> None:
        graph = {
            "schema_version": 1,
            "nodes": {
                "a": {"id": "a", "executor": "task", "depends_on": ["b"]},
                "b": {"id": "b", "executor": "task", "depends_on": ["c"]},
                "c": {"id": "c", "executor": "task", "depends_on": ["a"]},
                "x": {"id": "x", "executor": "task", "depends_on": ["y"]},
                "y": {"id": "y", "executor": "task", "depends_on": ["x"]},
                "self": {"id": "self", "executor": "task", "depends_on": ["self"]},
            },
        }
        proc = _run_cli("validate-graph.py", ["--graph-file", "-"], stdin_text=json.dumps(graph))
        self.assertEqual(proc.returncode, 3)
        report = json.loads(proc.stdout)
        self.assertFalse(report["valid"])
        self.assertEqual(report["cycles"], [["a", "b", "c"], ["self"], ["x", "y"]])
        self.assertEqual(report["waves"], [])
        self.assertEqual(report["max_parallelism"], 0)
        self.assertEqual(len(report["errors"]), 3)

    def test_ownership_overlaps_across_concurrent_writers(self) -> None:
        graph = {
            "schema_version": 1,
            "nodes": {
                "w1": {"id": "w1", "executor": "herdr-agent", "owns": ["src/shared/**"]},
                "w2": {"id": "w2", "executor": "herdr-agent", "owns": ["src/shared/api.py"]},
                "w3": {"id": "w3", "executor": "herdr-agent", "depends_on": ["w1"], "owns": ["src/shared/api.py"]},
            },
        }
        proc = _run_cli("validate-graph.py", ["--graph-file", "-"], stdin_text=json.dumps(graph))
        self.assertEqual(proc.returncode, 3)
        report = json.loads(proc.stdout)
        self.assertFalse(report["valid"])
        self.assertEqual(report["cycles"], [])
        self.assertEqual(
            report["overlaps"],
            [
                {"nodes": ["w1", "w2"], "patterns": ["src/shared/**", "src/shared/api.py"]},
                {"nodes": ["w2", "w3"], "patterns": ["src/shared/api.py", "src/shared/api.py"]},
            ],
        )
        self.assertEqual(report["waves"], [])
        self.assertEqual(report["max_parallelism"], 0)

    def test_schema_error_returns_exit_3(self) -> None:
        graph = {
            "schema_version": 99,
            "nodes": {"bad": {"id": "bad", "executor": "unknown"}},
        }
        proc = _run_cli("validate-graph.py", ["--graph-file", "-"], stdin_text=json.dumps(graph))
        self.assertEqual(proc.returncode, 3)
        report = json.loads(proc.stdout)
        self.assertFalse(report["valid"])
        self.assertTrue(any("schema_version" in err for err in report["errors"]))

    def test_malformed_dependency_output_and_retry_fields(self) -> None:
        cases = (
            ({"depends_on": "b"}, "depends_on"),
            ({"depends_on": [{}]}, "depends_on"),
            ({"depends_on": [["b"]]}, "depends_on"),
            ({"depends_on": ["unknown"]}, "unknown"),
            ({"outputs": {}}, "outputs"),
            ({"outputs": ["summary"]}, "outputs"),
            ({"retry": {"max_attempts": "invalid"}}, "retry.max_attempts"),
            ({"retry": {"max_attempts": "2"}}, "retry.max_attempts"),
            ({"retry": {"max_attempts": True}}, "retry.max_attempts"),
            ({"retry": {"max_attempts": 1.5}}, "retry.max_attempts"),
            ({"retry": {"max_attempts": []}}, "retry.max_attempts"),
        )
        for fields, error in cases:
            with self.subTest(fields=fields):
                graph = {
                    "schema_version": 1,
                    "nodes": {"a": {"id": "a", "executor": "task", **fields}},
                }
                proc = _run_cli("validate-graph.py", ["--graph-file", "-"], stdin_text=json.dumps(graph))
                self.assertEqual(proc.returncode, 3, proc.stderr)
                report = json.loads(proc.stdout)
                self.assertFalse(report["valid"])
                self.assertTrue(any(error in item for item in report["errors"]), report)
                self.assertEqual(report["waves"], [])
                self.assertEqual(report["max_parallelism"], 0)

    def test_chorded_cycles_in_same_component(self) -> None:
        graph = {
            "schema_version": 1,
            "nodes": {
                "a": {"id": "a", "executor": "task", "depends_on": ["b", "c"]},
                "b": {"id": "b", "executor": "task", "depends_on": ["c"]},
                "c": {"id": "c", "executor": "task", "depends_on": ["a", "b"]},
            },
        }
        proc = _run_cli("validate-graph.py", ["--graph-file", "-"], stdin_text=json.dumps(graph))
        self.assertEqual(proc.returncode, 3, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["cycles"], [["a", "b", "c"], ["a", "c"], ["b", "c"]])
        self.assertEqual(report["waves"], [])

    def test_deep_chain_iterative_tarjan_no_recursion_error(self) -> None:
        count = 1500
        nodes = {
            f"n{i:04d}": {
                "id": f"n{i:04d}",
                "executor": "task",
                "depends_on": [f"n{i - 1:04d}"] if i > 0 else [f"n{count - 1:04d}"],
            }
            for i in range(count)
        }
        graph = {"schema_version": 1, "nodes": nodes}
        proc = _run_cli("validate-graph.py", ["--graph-file", "-"], stdin_text=json.dumps(graph))
        self.assertEqual(proc.returncode, 3)
        report = json.loads(proc.stdout)
        self.assertFalse(report["valid"])
        self.assertEqual(len(report["cycles"]), 1)
        self.assertEqual(len(report["cycles"][0]), count)


if __name__ == "__main__":
    unittest.main()
