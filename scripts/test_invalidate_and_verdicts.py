#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import reconcile
import run_state
import verdict as verdict_mod
from testkit import HOLDER, commit_file, load_script, make_worktree, new_run, run_module, seed_repo

INVALIDATE_MOD = load_script("invalidate-node.py")
FINISH_MOD = load_script("finish-agent.py")

NULL_MODEL = {
    "requested_role": None,
    "requested_effort": None,
    "resolved": "provider/live",
    "thinking": None,
    "effort": None,
}


def _holder_args(holder: dict[str, str] | None = None) -> list[str]:
    h = holder or HOLDER
    return [
        "--session", h["session"],
        "--holder-workspace", h["workspace"],
        "--holder-pane", h["pane"],
    ]


def _seed_succeeded_node(run: Path, node_id: str, attempt: int = 1) -> Path:
    state = run_state.load_state(run)
    if state["nodes"][node_id]["state"] != "ready":
        run_state.append_event(run, {"type": "ready", "node": node_id, "attempt": attempt}, holder=HOLDER)
    run_state.append_event(run, {"type": "dispatch_intent", "node": node_id, "attempt": attempt}, holder=HOLDER)
    run_state.append_event(
        run,
        {
            "type": "dispatched",
            "node": node_id,
            "attempt": attempt,
            "handle": {"job": f"job-{node_id}-{attempt}", "spawning_session": HOLDER["session"]},
            "model": NULL_MODEL,
        },
        holder=HOLDER,
    )
    attempt_dir = run_state.attempt_dir(run, node_id, attempt)
    summary = attempt_dir / "summary.json"
    run_state.atomic_json(summary, {"node": node_id, "attempt": attempt, "ok": True})
    digest = run_state.sha256_file(summary)
    run_state.append_event(
        run,
        {
            "type": "succeeded",
            "node": node_id,
            "from": "running",
            "to": "succeeded",
            "attempt": attempt,
            "artifacts": [{"name": "summary", "path": str(summary), "hash": digest}],
        },
        holder=HOLDER,
    )
    return summary


def _make_running_writer(td: Path, *, node_id: str = "W1") -> tuple[Path, Path, Path, Path]:
    repo = seed_repo(td / "repo")
    graph = {
        "schema_version": 1,
        "nodes": {
            node_id: {
                "id": node_id,
                "executor": "herdr-agent",
                "owns": ["a.txt"],
                "worktree": {"branch": f"herdr/{node_id}"},
                "outputs": [{"name": "vcs", "type": "vcs"}],
                "retry": {"max_attempts": 4, "on": ["failed", "invalidated"]},
            }
        },
    }
    run = new_run(td, graph, repo=repo)
    run_doc = run_state.load_run(run)
    base_sha = run_doc["base_sha"]

    run_state.append_event(run, {"type": "dispatch_intent", "node": node_id, "attempt": 1}, holder=HOLDER)

    wt = make_worktree(repo, td / "wt", f"herdr/{node_id}", base=base_sha)
    commit_file(wt, "a.txt", "updated content\n", "writer commit")

    worker_id = run_state.new_worker_id()
    attempt_dir = run_state.attempt_dir(run, node_id, 1)
    identity = {
        "kind": "herdr-agent",
        "identity_kind": run_state.IDENTITY_MANAGED_NAME,
        "worker_id": worker_id,
        "branch": f"herdr/{node_id}",
        "workspace": "ws-worker",
        "pane": "pane-worker",
        "routing": NULL_MODEL,
        "worktree_path": str(wt),
        "base": base_sha,
        "repo": str(repo),
        "run": run_state.canonical_path(run),
        "run_id": run_doc["id"],
        "node": node_id,
        "attempt": "1",
        "tokens": {
            "provenance": "orchestrator",
            "run": run_doc["id"],
            "node": node_id,
            "attempt": "1",
        },
    }
    run_state.atomic_json(attempt_dir / "dispatch-identity.json", identity)
    run_state.append_event(
        run,
        {
            "type": "dispatched",
            "node": node_id,
            "attempt": 1,
            "handle": {
                "identity_kind": run_state.IDENTITY_MANAGED_NAME,
                "worker_id": worker_id,
                "workspace": "ws-worker",
                "pane": "pane-worker",
                "worktree_path": str(wt),
                "branch": f"herdr/{node_id}",
                "base": base_sha,
                "repo": str(repo),
                "tokens": identity["tokens"],
            },
            "model": NULL_MODEL,
        },
        holder=HOLDER,
    )
    return repo, run, wt, attempt_dir


class FakeCollect:
    def __init__(self, attempt_dir: Path, wt: Path, repo: Path, base_sha: str) -> None:
        self.attempt_dir = attempt_dir
        self.wt = wt
        self.repo = repo
        self.base_sha = base_sha

    def __call__(self, argv, child_name=None, *, timeout=None):
        head = run_state._run_git(self.wt, "rev-parse", "HEAD")
        vcs_path = self.attempt_dir / "vcs.json"
        run_state.atomic_json(
            vcs_path,
            {
                "schema_version": 1,
                "repo": str(self.repo.resolve()),
                "worktree": str(self.wt.resolve()),
                "branch": "herdr/W1",
                "base_sha": self.base_sha,
                "head": head,
                "result": {"form": "commit", "sha": head},
            },
        )
        return {
            "agents": [
                {
                    "name": "worker",
                    "artifacts": [
                        {
                            "name": "vcs",
                            "path": str(vcs_path),
                            "hash": run_state.sha256_file(vcs_path),
                        }
                    ],
                }
            ]
        }


class InvalidateNodeTests(unittest.TestCase):
    def _build_diamond_run(self, td: Path, *, retry_a: dict | None = None) -> Path:
        repo = seed_repo(td / "repo")
        node_a: dict = {
            "id": "A",
            "executor": "task",
            "outputs": [{"name": "summary", "type": "summary"}],
        }
        if retry_a is not None:
            node_a["retry"] = retry_a
        graph = {
            "schema_version": 1,
            "nodes": {
                "U": {
                    "id": "U",
                    "executor": "task",
                    "outputs": [{"name": "summary", "type": "summary"}],
                },
                "A": {
                    **node_a,
                    "depends_on": ["U"],
                    "inputs": [{"alias": "u", "from": "U", "output": "summary"}],
                },
                "B": {
                    "id": "B",
                    "executor": "task",
                    "depends_on": ["A"],
                    "inputs": [{"alias": "a", "from": "A", "output": "summary"}],
                    "outputs": [{"name": "summary", "type": "summary"}],
                    "retry": {"max_attempts": 3, "on": ["invalidated"]},
                },
                "C": {
                    "id": "C",
                    "executor": "task",
                    "depends_on": ["B"],
                    "inputs": [{"alias": "b", "from": "B", "output": "summary"}],
                    "outputs": [{"name": "summary", "type": "summary"}],
                },
            },
        }
        run = new_run(td, graph, repo=repo)
        for nid in ("U", "A", "B", "C"):
            _seed_succeeded_node(run, nid, 1)
        return run

    def test_cascade_invalidation_topo_order_and_feedback_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            run = self._build_diamond_run(td, retry_a={"max_attempts": 3, "on": ["invalidated"]})
            u_summary_before = (run_state.attempt_dir(run, "U", 1) / "summary.json").read_text(encoding="utf-8")
            fb_src = td / "sendback.md"
            fb_src.write_text("Fix edge case in parser.\n", encoding="utf-8")

            rc, out, err = run_module(
                INVALIDATE_MOD,
                [
                    "invalidate-node.py",
                    "--run", str(run),
                    "--node", "A",
                    "--reason", "parser regression",
                    *_holder_args(),
                    "--feedback", str(fb_src),
                ],
            )
            self.assertEqual(rc, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["target"], "A")
            self.assertEqual(payload["invalidated"], ["A", "B", "C"])
            self.assertEqual(payload["retried"], ["A", "B"])

            fb_dest = Path(payload["feedback_path"])
            self.assertEqual(fb_dest, run_state.attempt_dir(run, "A", 2) / "feedback.md")
            self.assertEqual(fb_dest.read_text(encoding="utf-8"), "Fix edge case in parser.\n")

            state = run_state.load_state(run)
            self.assertEqual(state["nodes"]["U"]["state"], "succeeded")
            self.assertEqual((run_state.attempt_dir(run, "U", 1) / "summary.json").read_text(encoding="utf-8"), u_summary_before)
            self.assertEqual(state["nodes"]["A"]["state"], "planned")
            self.assertEqual(state["nodes"]["A"]["attempt"], 2)
            self.assertEqual(state["nodes"]["B"]["state"], "planned")
            self.assertEqual(state["nodes"]["B"]["attempt"], 2)
            self.assertEqual(state["nodes"]["C"]["state"], "invalidated")
            self.assertEqual(state["nodes"]["C"]["attempt"], 1)

            # Verify dispatch_intent on attempt 2 accepts pre-existing feedback.md
            run_state.append_event(run, {"type": "ready", "node": "A", "attempt": 2}, holder=HOLDER)
            run_state.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 2}, holder=HOLDER)

    def test_no_cascade_and_explicit_retry_flag_with_inline_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            run = self._build_diamond_run(td, retry_a=None)

            rc, out, err = run_module(
                INVALIDATE_MOD,
                [
                    "invalidate-node.py",
                    "--run", str(run),
                    "--node", "A",
                    "--reason", "operator send-back",
                    *_holder_args(),
                    "--no-cascade",
                    "--retry",
                    "--feedback", "Please add null check.",
                ],
            )
            self.assertEqual(rc, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["invalidated"], ["A"])
            self.assertEqual(payload["retried"], ["A"])
            self.assertEqual(Path(payload["feedback_path"]).read_text(encoding="utf-8"), "Please add null check.")

            state = run_state.load_state(run)
            self.assertEqual(state["nodes"]["A"]["state"], "planned")
            self.assertEqual(state["nodes"]["A"]["attempt"], 2)
            self.assertEqual(state["nodes"]["B"]["state"], "succeeded")
            self.assertEqual(state["nodes"]["C"]["state"], "succeeded")

    def test_foreign_holder_refused_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            run = self._build_diamond_run(td)
            foreign = {"workspace": "ws-other", "pane": "pane-other", "session": "/tmp/other"}
            rc, _out, err = run_module(
                INVALIDATE_MOD,
                [
                    "invalidate-node.py",
                    "--run", str(run),
                    "--node", "A",
                    "--reason", "nope",
                    *_holder_args(foreign),
                ],
            )
            self.assertEqual(rc, 3, err)
            self.assertEqual(run_state.load_state(run)["nodes"]["A"]["state"], "succeeded")


class VerdictValidationTests(unittest.TestCase):
    def test_absent_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertIsNone(verdict_mod.load_and_validate_verdict(raw))

    def test_valid_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            for status in ("pass", "rework", "fail", "blocked"):
                doc = {
                    "status": status,
                    "summary": f"status {status}",
                    "findings": [{"severity": "high", "message": "issue", "path": "a.py"}],
                }
                if status == "rework":
                    doc["rework_target"] = "A"
                (td / "verdict.json").write_text(json.dumps(doc), encoding="utf-8")
                loaded = verdict_mod.load_and_validate_verdict(td)
                self.assertEqual(loaded, doc)

    def test_malformed_verdicts_raise_precondition(self) -> None:
        cases = [
            "not json",
            json.dumps([]),
            json.dumps({"status": "bogus", "summary": "x", "findings": []}),
            json.dumps({"status": "pass", "summary": 123, "findings": []}),
            json.dumps({"status": "pass", "summary": "ok", "findings": "bad"}),
            json.dumps({"status": "fail", "summary": "bad", "findings": [{"severity": "", "message": "m"}]}),
            json.dumps({"status": "rework", "summary": "bad", "findings": [], "rework_target": ""}),
        ]
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            for raw_text in cases:
                (td / "verdict.json").write_text(raw_text, encoding="utf-8")
                with self.assertRaises(run_state.PreconditionError):
                    verdict_mod.load_and_validate_verdict(td)


class FinishAgentVerdictTests(unittest.TestCase):
    def _run_finish(self, verdict_payload: dict | str | None):
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            repo, run, wt, attempt_dir = _make_running_writer(td)
            base_sha = run_state.load_run(run)["base_sha"]
            if isinstance(verdict_payload, str):
                (attempt_dir / "verdict.json").write_text(verdict_payload, encoding="utf-8")
            elif verdict_payload is not None:
                run_state.atomic_json(attempt_dir / "verdict.json", verdict_payload)

            orig_run_child = FINISH_MOD.run_child
            FINISH_MOD.run_child = FakeCollect(attempt_dir, wt, repo, base_sha)
            try:
                rc, out, err = run_module(
                    FINISH_MOD,
                    [
                        "finish-agent.py",
                        "--run", str(run),
                        "--node", "W1",
                        "--attempt", "1",
                        *_holder_args(),
                    ],
                )
            finally:
                FINISH_MOD.run_child = orig_run_child
            return rc, out, err, run_state.replay(run)

    def test_pass_verdict_records_succeeded_and_includes_verdict(self) -> None:
        doc = {"status": "pass", "summary": "all green", "findings": []}
        rc, out, err, snap = self._run_finish(doc)
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "succeeded")
        self.assertEqual(payload["verdict"], doc)
        self.assertNotIn("rework_target", payload)
        self.assertEqual(snap["state"]["nodes"]["W1"]["state"], "succeeded")

    def test_fail_verdict_records_failed(self) -> None:
        doc = {
            "status": "fail",
            "summary": "unit tests failed",
            "findings": [{"severity": "error", "message": "assertion failed", "path": "a.txt"}],
        }
        rc, out, err, snap = self._run_finish(doc)
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["verdict"], doc)
        self.assertEqual(snap["state"]["nodes"]["W1"]["state"], "failed")
        self.assertEqual(snap["state"]["nodes"]["W1"]["reason"], "unit tests failed")

    def test_rework_verdict_records_failed_with_rework_reason_and_target(self) -> None:
        doc = {
            "status": "rework",
            "summary": "needs cleanup",
            "findings": [{"severity": "warning", "message": "missing guard", "path": "a.txt"}],
            "rework_target": "W0",
        }
        rc, out, err, snap = self._run_finish(doc)
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "failed")
        self.assertEqual(payload["rework_target"], "W0")
        self.assertEqual(payload["verdict"], doc)
        self.assertEqual(snap["state"]["nodes"]["W1"]["state"], "failed")
        self.assertEqual(snap["state"]["nodes"]["W1"]["reason"], "verdict:rework")
        failed_events = [e for e in snap["events"] if e.get("type") == "failed"]
        self.assertEqual(failed_events[-1]["findings_hash"], verdict_mod.findings_hash(doc["findings"]))

    def test_malformed_verdict_refuses_with_exit_3(self) -> None:
        rc, _out, err, snap = self._run_finish('{"status": "unknown"}')
        self.assertEqual(rc, 3, err)
        self.assertEqual(snap["state"]["nodes"]["W1"]["state"], "running")


class ReworkStagnationTests(unittest.TestCase):
    def test_detects_repeated_reason_and_identical_findings_hash(self) -> None:
        reason_events = [
            {"type": "failed", "node": "A", "attempt": 1, "reason": "verdict:rework"},
            {"type": "retried", "node": "A", "attempt": 1, "next_attempt": 2, "reason": "retry"},
            {"type": "failed", "node": "A", "attempt": 2, "reason": "verdict:rework"},
            {"type": "retried", "node": "A", "attempt": 2, "next_attempt": 3, "reason": "retry"},
            {"type": "failed", "node": "A", "attempt": 3, "reason": "verdict:rework"},
        ]
        report = verdict_mod.detect_rework_stagnation(reason_events, "A", window=3)
        self.assertTrue(report["stagnated"])
        self.assertEqual(report["reason"], "verdict:rework")
        self.assertEqual(report["attempts"], [1, 2, 3])

        fhash = verdict_mod.findings_hash([{"severity": "high", "message": "same bug", "path": "x.py"}])
        hash_events = [
            {"type": "failed", "node": "A", "attempt": 1, "reason": "r1", "findings_hash": fhash},
            {"type": "failed", "node": "A", "attempt": 2, "reason": "r2", "findings_hash": fhash},
            {"type": "failed", "node": "A", "attempt": 3, "reason": "r3", "findings_hash": fhash},
        ]
        hash_report = verdict_mod.detect_rework_stagnation(hash_events, "A", window=3)
        self.assertTrue(hash_report["stagnated"])
        self.assertIsNone(hash_report["reason"])
        self.assertEqual(hash_report["findings_hash"], fhash)

    def test_non_stagnated_when_reasons_differ_or_succeeded_breaks_streak(self) -> None:
        differing = [
            {"type": "failed", "node": "A", "attempt": 1, "reason": "err-1"},
            {"type": "failed", "node": "A", "attempt": 2, "reason": "err-2"},
            {"type": "failed", "node": "A", "attempt": 3, "reason": "err-3"},
        ]
        self.assertFalse(verdict_mod.detect_rework_stagnation(differing, "A", window=3)["stagnated"])

        broken = [
            {"type": "failed", "node": "A", "attempt": 1, "reason": "same"},
            {"type": "succeeded", "node": "A", "attempt": 2},
            {"type": "failed", "node": "A", "attempt": 3, "reason": "same"},
            {"type": "failed", "node": "A", "attempt": 4, "reason": "same"},
        ]
        report = verdict_mod.detect_rework_stagnation(broken, "A", window=3)
        self.assertFalse(report["stagnated"])
        self.assertEqual(report["streak"], 2)

    def test_reconcile_exposes_stagnation_warning_when_non_empty(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            td = Path(raw)
            repo = seed_repo(td / "repo")
            graph = {
                "schema_version": 1,
                "nodes": {
                    "A": {
                        "id": "A",
                        "executor": "task",
                        "retry": {"max_attempts": 4, "on": ["failed"], "side_effect_safe": True},
                    }
                },
            }
            run = new_run(td, graph, repo=repo)

            for attempt in (1, 2, 3):
                if run_state.load_state(run)["nodes"]["A"]["state"] != "ready":
                    run_state.append_event(run, {"type": "ready", "node": "A", "attempt": attempt}, holder=HOLDER)
                run_state.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": attempt}, holder=HOLDER)
                run_state.append_event(
                    run,
                    {
                        "type": "dispatched",
                        "node": "A",
                        "attempt": attempt,
                        "handle": {"job": f"j-{attempt}", "spawning_session": HOLDER["session"]},
                        "model": NULL_MODEL,
                    },
                    holder=HOLDER,
                )
                run_state.append_event(
                    run,
                    {
                        "type": "failed",
                        "node": "A",
                        "from": "running",
                        "to": "failed",
                        "attempt": attempt,
                        "reason": "verdict:rework",
                    },
                    holder=HOLDER,
                )
                if attempt < 3:
                    run_state.append_event(
                        run,
                        {
                            "type": "retried",
                            "node": "A",
                            "from": "failed",
                            "to": "planned",
                            "attempt": attempt,
                            "next_attempt": attempt + 1,
                            "reason": "retry",
                        },
                        holder=HOLDER,
                    )

            rc, out, err = run_module(reconcile, ["reconcile.py", "--run", str(run)])
            self.assertEqual(rc, 0, err)
            payload = json.loads(out)
            self.assertIn("stagnation", payload)
            self.assertTrue(payload["stagnation"]["A"]["stagnated"])
            self.assertEqual(payload["stagnation"]["A"]["reason"], "verdict:rework")


if __name__ == "__main__":
    unittest.main()
