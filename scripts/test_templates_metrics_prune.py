"""Focused tests for render-graph.py, run-metrics.py, and prune-run.py."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import graph_model
import ioutil
import ledger
import promotion
from testkit import (
    HOLDER,
    commit_file,
    dispatch_intent_event,
    git,
    load_script,
    make_worktree,
    new_run,
    run_module,
    seed_repo,
)

render_graph = load_script("render-graph.py")
run_metrics = load_script("run-metrics.py")
prune_run = load_script("prune-run.py")


def _single_writer_graph() -> dict:
    return {
        "schema_version": 1,
        "nodes": {
            "W": {
                "id": "W",
                "executor": "herdr-agent",
                "depends_on": [],
                "owns": ["src/**"],
                "worktree": {"branch": "slice/w"},
                "outputs": [
                    {"name": "summary", "type": "summary"},
                    {"name": "vcs", "type": "vcs"},
                ],
                "retry": {"max_attempts": 2, "on": ["failed", "interrupted"]},
            }
        },
    }


def _complete_writer_run(tmp: Path, *, commit_on_promo: bool = False, deliver: bool = False, dirty: bool = False):
    repo = seed_repo(tmp / "repo")
    run = new_run(tmp, _single_writer_graph(), repo=repo, holder=HOLDER)
    run_info = ledger.load_run(run)
    promo_ref = run_info["promotion_ref"]
    base_sha = run_info["base_sha"]

    wt_path = make_worktree(repo, tmp / "wt-w", "slice/w", base=base_sha)
    wt_sha = commit_file(wt_path, "src/a.txt", "hello\n", "slice w")

    if commit_on_promo:
        git(repo, "update-ref", promo_ref, wt_sha)
        if deliver:
            deliv_ref = promotion.default_delivery_branch(run_info["job"])
            git(repo, "update-ref", deliv_ref, wt_sha)


    ledger.append_event(run, dispatch_intent_event("W", 1, owns=["src/**"]), holder=HOLDER)

    adir = ledger.attempt_dir(run, "W", 1)
    model = {"requested_role": None, "requested_effort": None, "resolved": "m", "thinking": None, "effort": None}
    identity = {
        "run": run_info["path"],
        "run_id": run_info["id"],
        "node": "W",
        "attempt": 1,
        "identity_kind": "managed-name",
        "worker_id": "fo-" + "a" * 24,
        "workspace": "ws-w",
        "pane": "pane-w",
        "repo": str(repo.resolve()),
        "worktree_path": str(wt_path.resolve()),
        "branch": "slice/w",
        "base": base_sha,
        "routing": model,
        "tokens": {"run": run_info["id"], "node": "W", "attempt": "1"},
    }
    ioutil.atomic_json(adir / "dispatch-identity.json", identity)

    ledger.append_event(
        run,
        {
            "type": "dispatched",
            "node": "W",
            "attempt": 1,
            "handle": identity,
            "model": model,
        },
        holder=HOLDER,
    )

    summary_path = adir / "summary.json"
    ioutil.atomic_json(summary_path, {"summary": "done", "session_path": None})
    vcs_path = adir / "vcs.json"
    ioutil.atomic_json(
        vcs_path,
        {
            "repo": str(repo.resolve()),
            "worktree": str(wt_path.resolve()),
            "branch": "slice/w",
            "base_sha": base_sha,
            "result": {"form": "commit", "sha": wt_sha},
        },
    )
    ledger.append_event(
        run,
        {
            "type": "succeeded",
            "node": "W",
            "from": "running",
            "to": "succeeded",
            "attempt": 1,
            "artifacts": [
                {"name": "summary", "type": "summary", "path": str(summary_path), "hash": ioutil.sha256_file(summary_path)},
                {"name": "vcs", "type": "vcs", "path": str(vcs_path), "hash": ioutil.sha256_file(vcs_path)},
            ],
        },
        holder=HOLDER,
    )
    if dirty:
        (wt_path / "src" / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    return repo, run, wt_path, promo_ref


class RenderGraphTests(unittest.TestCase):
    def test_starter_templates_render_and_validate(self):
        cases = [
            (
                "parallel-slices-integrate",
                [
                    "SLICE_A_OWNS=src/a/**",
                    "SLICE_A_BRANCH=slice/a",
                    "SLICE_B_OWNS=src/b/**",
                    "SLICE_B_BRANCH=slice/b",
                ],
                {"slice_a", "slice_b", "integrate"},
            ),
            (
                "readonly-audit-fanout",
                [
                    "AUDIT_A_SCOPE=security",
                    "AUDIT_B_SCOPE=performance",
                    "SYNTHESIS_GOAL=prioritized findings",
                ],
                {"audit_a", "audit_b", "synthesize"},
            ),
            (
                "writer-reviewer-loop",
                [
                    "WRITER_OWNS=src/feature/**",
                    "WRITER_BRANCH=slice/feature",
                    "REVIEW_FOCUS=correctness",
                ],
                {"writer", "reviewer"},
            ),
        ]
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "out" / "graph.json"
            for name, pairs, expected_nodes in cases:
                argv = ["render-graph.py", "--template", name, "--out", str(out_file)]
                for pair in pairs:
                    argv.extend(["--var", pair])
                code, out, err = run_module(render_graph, argv)
                self.assertEqual(code, 0, msg=f"{name} failed: {err}")
                rendered = json.loads(out)
                self.assertEqual(set(rendered["nodes"].keys()), expected_nodes)
                graph_model.validate_graph(rendered)
                self.assertEqual(json.loads(out_file.read_text(encoding="utf-8")), rendered)

    def test_missing_var_refuses_exit_2(self):
        code, _out, err = run_module(
            render_graph,
            ["render-graph.py", "--template", "writer-reviewer-loop", "--var", "WRITER_OWNS=src/**"],
        )
        self.assertEqual(code, 2)
        self.assertIn("WRITER_BRANCH", err)
        self.assertIn("REVIEW_FOCUS", err)

    def test_variable_values_keep_literal_placeholders(self):
        focus = "Check {{TEMPLATE_TOKEN}} and {{not a variable}}"
        code, out, err = run_module(
            render_graph,
            [
                "render-graph.py", "--template", "writer-reviewer-loop",
                "--var", "WRITER_OWNS=src/**", "--var", "WRITER_BRANCH=slice/w",
                "--var", f"REVIEW_FOCUS={focus}",
            ],
        )
        self.assertEqual(code, 0, msg=err)
        self.assertEqual(json.loads(out)["nodes"]["reviewer"]["description"], focus)

    def test_malformed_placeholder_in_template_is_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            template = Path(td) / "template.json"
            graph = _single_writer_graph()
            graph["nodes"]["W"]["description"] = "{{not a variable}}"
            ioutil.atomic_json(template, graph)
            code, _out, err = run_module(render_graph, ["render-graph.py", "--template", str(template)])
            self.assertEqual(code, 2)
            self.assertIn("{{not a variable}}", err)

    def test_overlapping_owns_fails_validate_graph(self):
        code, _out, err = run_module(
            render_graph,
            [
                "render-graph.py",
                "--template", "parallel-slices-integrate",
                "--var", "SLICE_A_OWNS=src/**",
                "--var", "SLICE_A_BRANCH=slice/a",
                "--var", "SLICE_B_OWNS=src/sub/**",
                "--var", "SLICE_B_BRANCH=slice/b",
            ],
        )
        self.assertEqual(code, 3)
        self.assertIn("owns overlap", err)


class RunMetricsTests(unittest.TestCase):
    def test_durations_retries_blocks_and_write(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = seed_repo(tmp / "repo")
            run = new_run(tmp, _single_writer_graph(), repo=repo, holder=HOLDER)

            # Append synthetic timestamped events for 2 attempts (1 failure + 1 block + 1 success).
            events_path = run / "events.jsonl"
            existing = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            for event in existing:
                event["t"] = "2026-11-22T10:00:00Z"
            synthetic = [
                {"type": "ready", "node": "W", "from": "planned", "to": "ready", "ts": "2026-11-22T10:00:00Z"},
                {"type": "dispatch_intent", "node": "W", "attempt": 1, "consumed": [], "owns": ["src/**"], "ts": "2026-11-22T10:00:05Z"},
                {"type": "dispatched", "node": "W", "attempt": 1, "handle": {"workspace": "w", "pane": "p"}, "model": {}, "ts": "2026-11-22T10:00:10Z"},
                {"type": "failed", "node": "W", "from": "running", "to": "failed", "attempt": 1, "reason": "boom", "ts": "2026-11-22T10:00:30Z"},
                {"type": "dispatch_intent", "node": "W", "attempt": 2, "consumed": [], "owns": ["src/**"], "ts": "2026-11-22T10:00:35Z"},
                {"type": "dispatched", "node": "W", "attempt": 2, "handle": {"workspace": "w", "pane": "p"}, "model": {}, "ts": "2026-11-22T10:00:40Z"},
                {"type": "blocked", "node": "W", "attempt": 2, "reason": "need input", "ts": "2026-11-22T10:00:50Z"},
                {"type": "attempt_started", "node": "W", "attempt": 2, "ts": "2026-11-22T10:01:00Z"},
                {"type": "succeeded", "node": "W", "from": "running", "to": "succeeded", "attempt": 2, "artifacts": [], "ts": "2026-11-22T10:01:20Z"},
            ]
            # Mix authoritative timestamps with legacy ts-only events, including a conflicting pair.
            for i in (0, 2, 5, 8):
                synthetic[i]["t"] = synthetic[i].pop("ts")
            synthetic[-1]["ts"] = "2026-11-22T10:05:00Z"
            events_path.write_text(
                "".join(json.dumps(ev) + "\n" for ev in existing + synthetic),
                encoding="utf-8",
            )

            # Only attempt 2 has a session log, recorded relative to its artifact directory.
            adir2 = ledger.attempt_dir(run, "W", 2)
            adir2.mkdir(parents=True, exist_ok=True)
            session_log = adir2 / "omp-session.jsonl"
            session_log.write_text(
                json.dumps({"message": {"usage": {"input": 100, "output": 50, "cacheRead": 25, "cost": {"total": 0.0125}}}}) + "\n",
                encoding="utf-8",
            )
            ioutil.atomic_json(adir2 / "summary.json", {"summary": "ok", "session_path": session_log.name})

            code, out, err = run_module(run_metrics, ["run-metrics.py", "--run", str(run), "--write"])
            self.assertEqual(code, 0, msg=err)
            data = json.loads(out)

            self.assertEqual(data["total_wall_sec"], 80.0)
            self.assertEqual(data["node_count"], 1)
            self.assertEqual(data["attempts_total"], 2)
            self.assertEqual(data["retries_total"], 1)
            self.assertEqual(data["token_coverage"], 0.5)

            node_m = data["nodes"]["W"]
            self.assertEqual(node_m["wall_sec"], 80.0)
            self.assertEqual([a["duration_sec"] for a in node_m["attempts"]], [25.0, 45.0])
            self.assertEqual(node_m["retries"], 1)
            self.assertEqual(node_m["failures"], 1)
            self.assertEqual(node_m["blocks"], 1)
            # Attempt 1 active=20s, wait=5s; Attempt 2 active=10s+20s=30s, wait=5s+10s=15s -> active=50s, wait=20s
            self.assertEqual(node_m["active_sec"], 50.0)
            self.assertEqual(node_m["wait_sec"], 20.0)
            self.assertIsNone(node_m["attempts"][0]["usage"])
            self.assertEqual(node_m["attempts"][1]["usage"]["total_tokens"], 175)
            self.assertEqual(node_m["attempts"][1]["usage"]["cost_usd"], 0.0125)

            written = json.loads((run / "metrics.json").read_text(encoding="utf-8"))
            self.assertEqual(written, data)

    def test_no_session_logs_yields_zero_coverage_and_null_usage(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _repo, run, _wt, _ref = _complete_writer_run(tmp)
            code, out, err = run_module(run_metrics, ["run-metrics.py", "--run", str(run)])
            self.assertEqual(code, 0, msg=err)
            data = json.loads(out)
            self.assertEqual(data["token_coverage"], 0.0)
            self.assertIsNone(data["nodes"]["W"]["attempts"][0]["usage"])

    def test_relative_session_path_cannot_escape_attempt_directory(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _repo, run, _wt_path, _promo_ref = _complete_writer_run(tmp)
            adir = ledger.attempt_dir(run, "W", 1)
            outside_log = adir.parent / "outside.jsonl"
            outside_log.write_text('{"usage": {"input": 10}}\n', encoding="utf-8")
            (adir / "link.jsonl").symlink_to(outside_log)
            args = ["run-metrics.py", "--run", str(run)]
            for candidate in ("../outside.jsonl", "link.jsonl", str(outside_log)):
                with self.subTest(candidate=candidate):
                    ioutil.atomic_json(adir / "summary.json", {"session_path": candidate})
                    code, out, err = run_module(run_metrics, args)
                    self.assertEqual(code, 0, msg=err)
                    expected = 1.0 if candidate == str(outside_log) else 0.0
                    self.assertEqual(json.loads(out)["token_coverage"], expected)

    def test_runtime_error_maps_to_exit_1(self):
        with patch.object(run_metrics, "compute_metrics", side_effect=RuntimeError("could not read metrics")):
            code, _out, err = run_module(run_metrics, ["run-metrics.py", "--run", "/run"])
        self.assertEqual(code, 1)
        self.assertIn("could not read metrics", err)


class PruneRunTests(unittest.TestCase):
    def setUp(self):
        herdr = patch.object(prune_run.herdr_cli, "herdr", return_value={"result": {"agents": []}})
        self.herdr = herdr.start()
        self.addCleanup(herdr.stop)

    def _holder_args(self, repo: Path, run: Path) -> list[str]:
        return [
            "prune-run.py",
            "--run", str(run),
            "--repo", str(repo),
            "--session", HOLDER["session"],
            "--holder-workspace", HOLDER["workspace"],
            "--holder-pane", HOLDER["pane"],
        ]

    def test_dry_run_reports_targets_without_removing(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, run, wt_path, promo_ref = _complete_writer_run(tmp, commit_on_promo=True, deliver=True)
            code, out, err = run_module(prune_run, self._holder_args(repo, run) + ["--dry-run"])
            self.assertEqual(code, 0, msg=err)
            data = json.loads(out)
            self.assertTrue(data["dry_run"])
            self.assertEqual(data["removed_worktrees"], [str(wt_path.resolve())])
            self.assertEqual(data["removed_refs"], [promo_ref])
            self.assertTrue(wt_path.exists())
            self.assertIsNotNone(ioutil._git_ref(repo, promo_ref))

    def test_invalidated_node_is_terminal_even_without_herdr_binary(self):
        with tempfile.TemporaryDirectory() as td:
            repo, run, wt_path, _promo_ref = _complete_writer_run(Path(td))
            ledger.append_event(run, {"type": "invalidated", "node": "W", "attempt": 1}, holder=HOLDER)
            missing = ioutil.RunError("herdr not on PATH")
            missing.__cause__ = FileNotFoundError("herdr")
            self.herdr.side_effect = missing
            code, out, err = run_module(prune_run, self._holder_args(repo, run) + ["--dry-run"])
            self.assertEqual(code, 0, msg=err)
            self.assertEqual(json.loads(out)["removed_worktrees"], [str(wt_path.resolve())])
            self.herdr.assert_called_once_with("agent", "list")

    def test_live_recorded_worker_refuses_prune_after_node_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            repo, run, wt_path, promo_ref = _complete_writer_run(Path(td))
            self.herdr.return_value = {"result": {"agents": [{"name": "fo-" + "a" * 24}]}}
            code, _out, err = run_module(prune_run, self._holder_args(repo, run))
            self.assertEqual(code, 3)
            self.assertIn("live Herdr worker", err)
            self.herdr.assert_called_once_with("agent", "list")
            self.assertTrue(wt_path.exists())
            self.assertIsNotNone(ioutil._git_ref(repo, promo_ref))

    def test_artifact_only_worktree_from_revised_out_node_is_pruned(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, run, wt_path, _promo_ref = _complete_writer_run(tmp)
            old_wt = make_worktree(repo, tmp / "wt-old", "slice/old")
            old_attempt = ledger.attempt_dir(run, "old", 1)
            old_attempt.mkdir(parents=True)
            ioutil.atomic_json(old_attempt / "dispatch-identity.json", {
                "worktree_path": str(old_wt.resolve()), "branch": "slice/old",
                "agent": "old-worker-id",
            })
            self.herdr.return_value = {"result": {"agents": [{"agent_id": "old-worker-id"}]}}
            code, _out, err = run_module(prune_run, self._holder_args(repo, run))
            self.assertEqual(code, 3)
            self.assertIn("live Herdr worker", err)
            self.assertTrue(old_wt.exists())

            self.herdr.return_value = {"result": {"agents": [{"agent_id": "unrelated-worker-id"}]}}
            code, out, err = run_module(prune_run, self._holder_args(repo, run))
            self.assertEqual(code, 0, msg=err)
            self.assertCountEqual(json.loads(out)["removed_worktrees"], [str(wt_path.resolve()), str(old_wt.resolve())])
            self.assertFalse(old_wt.exists())

    def test_symbolic_promotion_ref_refuses_before_pruning(self):
        with tempfile.TemporaryDirectory() as td:
            repo, run, wt_path, promo_ref = _complete_writer_run(Path(td))
            git(repo, "symbolic-ref", promo_ref, "refs/heads/main")
            code, _out, err = run_module(prune_run, self._holder_args(repo, run) + ["--force-undelivered"])
            self.assertEqual(code, 3)
            self.assertIn("symbolic promotion ref", err)
            self.assertEqual(git(repo, "symbolic-ref", promo_ref), "refs/heads/main")
            self.assertTrue(wt_path.exists())

    def test_promotion_ref_changed_after_validation_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            repo, run, wt_path, promo_ref = _complete_writer_run(Path(td))
            new_sha = git(wt_path, "rev-parse", "HEAD")

            def move_ref(*_args):
                git(repo, "update-ref", promo_ref, new_sha)
                return True

            with patch.object(prune_run, "_promotion_ref_delivered_or_merged", side_effect=move_ref):
                code, _out, err = run_module(prune_run, self._holder_args(repo, run))
            self.assertEqual(code, 1)
            self.assertIn("update-ref", err)
            self.assertEqual(ioutil._git_ref(repo, promo_ref), new_sha)

    def test_malformed_run_repo_is_precondition_error(self):
        with tempfile.TemporaryDirectory() as td:
            repo, run, _wt_path, _promo_ref = _complete_writer_run(Path(td))
            run_info = ledger.load_run(run)
            for bad_repo in (None, "repo", "/bad\0repo"):
                with self.subTest(bad_repo=bad_repo):
                    with patch.object(ledger, "load_run", return_value={**run_info, "repo": bad_repo}):
                        code, _out, err = run_module(prune_run, self._holder_args(repo, run))
                    self.assertEqual(code, 3)
                    self.assertIn("run.json.repo", err)

    def test_dirty_worktree_refuses_exit_3(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, run, wt_path, promo_ref = _complete_writer_run(tmp, commit_on_promo=True, deliver=True, dirty=True)
            code, _out, err = run_module(prune_run, self._holder_args(repo, run))
            self.assertEqual(code, 3)
            self.assertIn("dirty", err)
            self.assertTrue(wt_path.exists())
            self.assertIsNotNone(ioutil._git_ref(repo, promo_ref))

    def test_unmerged_promotion_ref_refuses_until_forced(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, run, wt_path, promo_ref = _complete_writer_run(tmp, commit_on_promo=True, deliver=False)
            code, _out, err = run_module(prune_run, self._holder_args(repo, run))
            self.assertEqual(code, 3)
            self.assertIn("unmerged", err)
            self.assertTrue(wt_path.exists())
            self.assertIsNotNone(ioutil._git_ref(repo, promo_ref))

            code2, out2, err2 = run_module(prune_run, self._holder_args(repo, run) + ["--force-undelivered"])
            self.assertEqual(code2, 0, msg=err2)
            data = json.loads(out2)
            self.assertFalse(data["dry_run"])
            self.assertEqual(data["removed_worktrees"], [str(wt_path.resolve())])
            self.assertEqual(data["removed_refs"], [promo_ref])
            self.assertFalse(wt_path.exists())
            self.assertIsNone(ioutil._git_ref(repo, promo_ref))

    def test_clean_delivered_prune_removes_worktree_and_promo_ref_only(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo, run, wt_path, promo_ref = _complete_writer_run(tmp, commit_on_promo=True, deliver=True)
            primary_head_before = git(repo, "rev-parse", "HEAD")

            code, out, err = run_module(prune_run, self._holder_args(repo, run))
            self.assertEqual(code, 0, msg=err)
            data = json.loads(out)
            self.assertFalse(data["dry_run"])
            self.assertEqual(data["removed_worktrees"], [str(wt_path.resolve())])
            self.assertEqual(data["removed_refs"], [promo_ref])
            self.assertFalse(wt_path.exists())
            self.assertIsNone(ioutil._git_ref(repo, promo_ref))
            # Primary checkout and its branch are untouched.
            self.assertTrue(repo.exists())
            self.assertEqual(git(repo, "rev-parse", "HEAD"), primary_head_before)

    def test_non_terminal_node_and_foreign_holder_refuse(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = seed_repo(tmp / "repo")
            run = new_run(tmp, _single_writer_graph(), repo=repo, holder=HOLDER)
            # Node W is still planned (non-terminal).
            code, _out, err = run_module(prune_run, self._holder_args(repo, run))
            self.assertEqual(code, 3)
            self.assertIn("non-terminal", err)

            # Foreign holder also refuses.
            bad_args = [
                "prune-run.py",
                "--run", str(run),
                "--repo", str(repo),
                "--session", HOLDER["session"],
                "--holder-workspace", "wrong-ws",
                "--holder-pane", HOLDER["pane"],
            ]
            code2, _out2, err2 = run_module(prune_run, bad_args)
            self.assertEqual(code2, 3)
            self.assertIn("active lease", err2)


if __name__ == "__main__":
    unittest.main()
