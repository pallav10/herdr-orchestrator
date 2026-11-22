"""Tests for unified CLI front-door scripts/ho.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import ho
import ioutil
import ledger
import scheduler
import session_docs
import testkit


def _init_gitignored_repo(root: Path) -> tuple[Path, Path]:
    repo = testkit.seed_repo(root / "repo")
    (repo / ".gitignore").write_text("/docs/runs/\n/docs/sessions/\n", encoding="utf-8")
    testkit.git(repo, "add", ".gitignore")
    testkit.git(repo, "commit", "-m", "ignore runtime docs")
    docs = repo / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    return repo, docs


def _run_ho(argv: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    old_cwd = Path.cwd()
    old_env = os.environ.copy()
    try:
        if cwd is not None:
            os.chdir(cwd)
        if env is not None:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        with mock.patch.object(sys, "argv", ["ho.py", *argv]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ho.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except ioutil.UsageError as exc:
                print(f"error: {exc}", file=sys.stderr)
                code = 2
            except ioutil.PreconditionError as exc:
                print(f"error: {exc}", file=sys.stderr)
                code = 3
            except ioutil.RunError as exc:
                print(f"error: {exc}", file=sys.stderr)
                code = 1
        return code, out.getvalue(), err.getvalue()
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


class HoCliTests(unittest.TestCase):
    def test_auto_context_resolves_from_env_and_session_and_fails_closed_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, docs = _init_gitignored_repo(Path(td))
            env = {"HERDR_WORKSPACE_ID": "ws-auto", "HERDR_PANE_ID": "pane-auto"}
            code, out, err = _run_ho(["init", "--dir", str(docs), "--job", "auto-ctx"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            init_payload = json.loads(out)
            run_path = Path(init_payload["run"])
            session_path = Path(init_payload["session"])
            self.assertIn("ho.py", init_payload["next_command"])
            self.assertIn("accept", init_payload["next_command"])

            # Auto-context resolves run, session, holder, and epoch without any flags
            parser = ho.build_parser()
            args = parser.parse_args(["status"])
            old_cwd = Path.cwd()
            try:
                os.chdir(repo)
                with mock.patch.dict(os.environ, env, clear=False):
                    ctx = ho._resolve_context(args, require_run=True, require_holder=True, require_epoch=True)
            finally:
                os.chdir(old_cwd)
            self.assertEqual(ctx["run"], run_path)
            self.assertEqual(ctx["session"], session_path)
            self.assertEqual(ctx["holder"], {"workspace": "ws-auto", "pane": "pane-auto", "session": str(session_path)})
            self.assertEqual(ctx["epoch"], 1)

            # Fails closed when env has unknown pane and no explicit --run/--session
            code, _, err = _run_ho(
                ["status"],
                env={"HERDR_WORKSPACE_ID": "ws-other", "HERDR_PANE_ID": "pane-other"},
                cwd=repo,
            )
            self.assertEqual(code, 3)
            self.assertIn("ho.py init", err)

    def test_forgiving_graph_normalization_and_validation_error_example(self) -> None:
        # List form + omitted schema_version
        norm_list = ho._normalize_graph({
            "nodes": [
                {"id": "A", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]},
                {"id": "B", "executor": "herdr-agent", "depends_on": ["A"], "outputs": [{"name": "report", "type": "summary"}]},
            ]
        })
        self.assertEqual(norm_list["schema_version"], 1)
        self.assertEqual(set(norm_list["nodes"].keys()), {"A", "B"})

        # Dict form with omitted node.id
        norm_dict = ho._normalize_graph({
            "nodes": {
                "A": {"executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}
            }
        })
        self.assertEqual(norm_dict["schema_version"], 1)
        self.assertEqual(norm_dict["nodes"]["A"]["id"], "A")

        # Accept command with shorthand graph and custom summary output name
        with tempfile.TemporaryDirectory() as td:
            repo, docs = _init_gitignored_repo(Path(td))
            env = {"HERDR_WORKSPACE_ID": "ws-norm", "HERDR_PANE_ID": "pane-norm"}
            code, out, err = _run_ho(["init", "--dir", str(docs), "--job", "norm"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            graph_file = Path(td) / "graph.json"
            graph_file.write_text(json.dumps({
                "nodes": [
                    {"id": "A", "executor": "herdr-agent", "outputs": [{"name": "audit", "type": "summary"}]}
                ]
            }), encoding="utf-8")
            code, out, err = _run_ho(["accept", "--graph", str(graph_file)], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            accepted = json.loads(out)
            self.assertEqual(accepted["nodes"], ["A"])
            self.assertIn("dispatch", accepted["next_command"])

            # Invalid graph shows schema example
            bad_file = Path(td) / "bad.json"
            bad_file.write_text(json.dumps({"nodes": {"A": {"executor": "bogus"}}}), encoding="utf-8")
            code, _, err = _run_ho(["accept", "--graph", str(bad_file)], env=env, cwd=repo)
            self.assertEqual(code, 3)
            self.assertIn("Valid schema example", err)

    def test_custom_summary_output_name_maps_to_summary_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, docs = _init_gitignored_repo(Path(td))
            env = {"HERDR_WORKSPACE_ID": "ws-sum", "HERDR_PANE_ID": "pane-sum"}
            code, out, err = _run_ho(["init", "--dir", str(docs), "--job", "sum-map"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            init_data = json.loads(out)
            run_path = Path(init_data["run"])
            holder = {"workspace": "ws-sum", "pane": "pane-sum", "session": init_data["session"]}
            graph = {
                "schema_version": 1,
                "nodes": {
                    "Audit": {
                        "id": "Audit",
                        "executor": "herdr-agent",
                        "outputs": [{"name": "audit_summary", "type": "summary"}],
                    }
                },
            }
            ledger.accept_graph(run_path, graph, repo=repo, holder=holder)
            scheduler.reconcile(run_path, holder=holder)
            model = {"requested_role": None, "requested_effort": None, "resolved": "openai/gpt-5", "thinking": "medium", "effort": "medium"}
            ledger.append_event(run_path, {"type": "dispatch_intent", "node": "Audit", "attempt": 1, "consumed": [], "owns": []}, holder=holder)
            ledger.append_event(run_path, {"type": "dispatched", "node": "Audit", "from": "ready", "to": "running", "attempt": 1, "handle": {"worker_id": "fo-" + "11" * 12, "identity_kind": "managed-name"}, "model": model}, holder=holder)
            adir = ledger.attempt_dir(run_path, "Audit", 1)
            adir.mkdir(parents=True, exist_ok=True)
            summary_path = adir / "summary.json"
            summary_path.write_text(json.dumps({"summary": "ok"}) + "\n", encoding="utf-8")
            artifact = {"name": "audit_summary", "type": "summary", "path": str(summary_path), "hash": ioutil.sha256_file(summary_path)}
            res = ledger.append_event(
                run_path,
                {"type": "succeeded", "node": "Audit", "from": "running", "to": "succeeded", "attempt": 1, "artifacts": [artifact]},
                holder=holder,
            )
            self.assertEqual(res["state"]["nodes"]["Audit"]["state"], "succeeded")

    def test_ho_next_across_all_phases_and_ho_promote_auto_sha(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, docs = _init_gitignored_repo(Path(td))
            env = {"HERDR_WORKSPACE_ID": "ws-loop", "HERDR_PANE_ID": "pane-loop"}

            # Phase 0: init (no session yet)
            with mock.patch.object(ho, "_live_agents_by_name", return_value={}):
                code, out, err = _run_ho(["next"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            nxt = json.loads(out)
            self.assertEqual(nxt["phase"], "init")
            self.assertTrue(nxt["commands"][0].startswith("python3 "))

            # Initialize run -> Phase 1: accept_graph
            code, out, err = _run_ho(["init", "--dir", str(docs), "--job", "full-loop"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            init_data = json.loads(out)
            run_path = Path(init_data["run"])
            holder = {"workspace": "ws-loop", "pane": "pane-loop", "session": init_data["session"]}

            with mock.patch.object(ho, "_live_agents_by_name", return_value={}):
                code, out, err = _run_ho(["next"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["phase"], "accept_graph")

            # Accept single integrator node -> Phase 2: dispatch
            graph_file = Path(td) / "g.json"
            graph_file.write_text(json.dumps({
                "nodes": [
                    {
                        "id": "Int",
                        "executor": "herdr-agent",
                        "owns": ["**"],
                        "worktree": {},
                        "outputs": [
                            {"name": "summary", "type": "summary"},
                            {"name": "vcs", "type": "vcs"},
                            {"name": "integration", "type": "integration"},
                        ],
                    }
                ]
            }), encoding="utf-8")
            code, out, err = _run_ho(["accept", "--graph", str(graph_file)], env=env, cwd=repo)
            self.assertEqual(code, 0, err)

            with mock.patch.object(ho, "_live_agents_by_name", return_value={}):
                code, out, err = _run_ho(["next"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            nxt = json.loads(out)
            self.assertEqual(nxt["phase"], "dispatch")
            self.assertIn("dispatch Int", nxt["commands"][0])

            # Dispatch integrator -> Phase 3: monitor (while worker is running)
            scheduler.reconcile(run_path, holder=holder)
            worker_id = "fo-" + "aa" * 12
            wt_path = Path(td) / "int-wt"
            branch = f"{run_path.name}/Int/1"
            base_sha = ledger.load_run(run_path)["base_sha"]
            testkit.git(repo, "worktree", "add", "-b", branch, str(wt_path), base_sha)
            (wt_path / "feature.txt").write_text("hello\n", encoding="utf-8")
            testkit.git(wt_path, "add", "feature.txt")
            testkit.git(wt_path, "commit", "-m", "integrator commit")
            new_sha = testkit.git(wt_path, "rev-parse", "HEAD")

            ledger.append_event(run_path, {"type": "dispatch_intent", "node": "Int", "attempt": 1, "consumed": [], "owns": ["**"]}, holder=holder)
            handle = {
                "worker_id": worker_id,
                "identity_kind": "managed-name",
                "workspace": "ws-w1",
                "pane": "pane-w1",
                "repo": str(repo.resolve()),
                "worktree_path": str(wt_path.resolve()),
                "branch": branch,
                "base": base_sha,
            }
            model = {"requested_role": None, "requested_effort": None, "resolved": "openai/gpt-5", "thinking": "medium", "effort": "medium"}
            ledger.append_event(run_path, {"type": "dispatched", "node": "Int", "from": "ready", "to": "running", "attempt": 1, "handle": handle, "model": model}, holder=holder)
            adir = ledger.attempt_dir(run_path, "Int", 1)
            adir.mkdir(parents=True, exist_ok=True)
            (adir / "dispatch-identity.json").write_text(json.dumps({
                "run": str(run_path),
                "run_id": run_path.name,
                "node": "Int",
                "attempt": "1",
                **handle,
                "routing": model,
                "tokens": {"provenance": "orchestrator", "run": run_path.name, "node": "Int", "attempt": "1"},
            }) + "\n", encoding="utf-8")

            with mock.patch.object(ho, "_live_agents_by_name", return_value={worker_id: {"name": worker_id, "agent_status": "running"}}):
                code, out, err = _run_ho(["next"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            nxt = json.loads(out)
            self.assertEqual(nxt["phase"], "monitor")
            self.assertIn("monitor", nxt["commands"][0])

            # Worker settles -> Phase 4: finish
            with mock.patch.object(ho, "_live_agents_by_name", return_value={worker_id: {"name": worker_id, "agent_status": "idle"}}):
                code, out, err = _run_ho(["next"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            nxt = json.loads(out)
            self.assertEqual(nxt["phase"], "finish")
            self.assertIn("finish Int", nxt["commands"][0])

            # Plant collected receipts -> Phase 5: promote
            (adir / "summary.json").write_text(json.dumps({"name": worker_id, "status": "idle", "summary": "done"}) + "\n", encoding="utf-8")
            (adir / "vcs.json").write_text(json.dumps({
                "repo": str(repo.resolve()),
                "worktree": str(wt_path.resolve()),
                "branch": branch,
                "base_sha": base_sha,
                "head": new_sha,
                "commit": new_sha,
                "result": {"form": "commit", "sha": new_sha},
            }) + "\n", encoding="utf-8")
            (adir / "integration.json").write_text(json.dumps({
                "base_sha": base_sha,
                "commit": new_sha,
                "head": new_sha,
                "consumed": [],
            }) + "\n", encoding="utf-8")

            with mock.patch.object(ho, "_live_agents_by_name", return_value={worker_id: {"name": worker_id, "agent_status": "idle"}}):
                code, out, err = _run_ho(["next"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            nxt = json.loads(out)
            self.assertEqual(nxt["phase"], "promote")
            self.assertIn("promote Int", nxt["commands"][0])

            # Execute `ho promote Int` (auto-resolves new_sha from receipts!)
            code, out, err = _run_ho(["promote", "Int"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            prom = json.loads(out)
            self.assertEqual(prom["new_sha"], new_sha)
            self.assertEqual(prom["state"], "succeeded")

            # After promotion, worker still live -> Phase 6: retire
            with mock.patch.object(ho, "_live_agents_by_name", return_value={worker_id: {"name": worker_id, "agent_status": "idle"}}):
                code, out, err = _run_ho(["next"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            nxt = json.loads(out)
            self.assertEqual(nxt["phase"], "retire")
            self.assertIn("retire --all", nxt["commands"][0])

            # After retirement (no live workers) -> Phase 7: done
            with mock.patch.object(ho, "_live_agents_by_name", return_value={}):
                code, out, err = _run_ho(["next"], env=env, cwd=repo)
            self.assertEqual(code, 0, err)
            nxt = json.loads(out)
            self.assertEqual(nxt["phase"], "done")
            self.assertEqual(nxt["commands"], [])


if __name__ == "__main__":
    unittest.main()
