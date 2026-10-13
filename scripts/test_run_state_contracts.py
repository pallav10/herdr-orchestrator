#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

import ledger
import promotion
import run_state
from testkit import HOLDER, commit_file, git, make_worktree, new_run, seed_repo



class RunStateContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.initial_holder = dict(HOLDER)
        self.repos: dict[str, Path] = {}
        self._wt = 0

    def tearDown(self):
        self.tmp.cleanup()

    git = staticmethod(git)

    def git_ok(self, repo: Path, *args: str) -> tuple[int, str]:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        return proc.returncode, (proc.stdout or "").strip()

    def git_ref(self, repo: Path, ref: str) -> str | None:
        code, out = self.git_ok(repo, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")
        return out if code == 0 and out else None

    def checkout_state(self, repo: Path) -> dict:
        _, symbolic = self.git_ok(repo, "symbolic-ref", "-q", "HEAD")
        return {
            "head": self.git(repo, "rev-parse", "HEAD"),
            "symbolic": symbolic,
            "porcelain": self.git(repo, "status", "--porcelain"),
            "index": self.git(repo, "write-tree"),
        }

    def assert_frozen(self, repo: Path, snap: dict, run: str | None = None, *, ref: str | None = None, ref_value: str | None = None, events: bytes | None = None):
        self.assertEqual(self.checkout_state(repo), snap)
        if ref is not None:
            self.assertEqual(self.git_ref(repo, ref), ref_value)
        if events is not None:
            self.assertEqual((Path(run) / "events.jsonl").read_bytes(), events)

    def repo(self) -> Path:
        return seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")

    def commit(self, repo: Path, text: str) -> str:
        return commit_file(repo, "file.txt", text, text.strip())

    def make_worktree(self, repo: Path, *, node: str, attempt: int, base: str) -> Path:
        self._wt += 1
        path = self.root / "wt" / f"{self._wt}-{node}-{attempt}"
        branch = f"t/{self._wt}-{node}-{attempt}"
        make_worktree(repo, path, branch, base=base)
        return path

    def commit_file(self, worktree: Path, relpath: str, text: str) -> str:
        return commit_file(worktree, relpath, text, relpath)

    def orphan_commit(self, repo: Path) -> str:
        tree = self.git(repo, "rev-parse", "HEAD^{tree}")
        proc = subprocess.run(["git", "-C", str(repo), "commit-tree", tree, "-m", "orphan"], capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    def attempt_dir(self, run: str, node: str, attempt: int) -> Path:
        return Path(run) / "artifacts" / node / str(attempt)

    def identity_for(self, run: str, node: str, attempt: int) -> dict:
        return json.loads((self.attempt_dir(run, node, attempt) / "dispatch-identity.json").read_text(encoding="utf-8"))

    def load_run_json(self, run: str) -> dict:
        return json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))

    def snapshot_consumed(self, run: str, node: str, attempt: int) -> list:
        raw = json.loads((self.attempt_dir(run, node, attempt) / "consumed_artifacts.json").read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else raw.get("consumed", [])

    def receipt_consumed(self, items: list, *, attempt_key: str = "attempt") -> list[dict]:
        out = []
        for item in items:
            entry = {
                "producer": item["producer"],
                attempt_key: item.get("producer_attempt", item.get("attempt")),
                "output": item["output"],
                "hash": item["hash"],
            }
            if item.get("alias"):
                entry["alias"] = item["alias"]
            out.append(entry)
        return out

    def bound_run(self, repo: Path, job: str = "Job") -> str:
        run = str(new_run(self.root, None, repo=repo, holder=self.initial_holder, job=job))
        self.repos[run] = repo
        return run

    def new_run(self, graph: dict, repo: Path | None = None) -> str:
        repo_path = repo or self.repo()
        run = str(new_run(self.root, graph, repo=repo_path, holder=self.initial_holder))
        self.repos[run] = repo_path
        return run

    def run_holder(self, run: str) -> dict:
        controller = run_state.replay(run)["controller"]
        return {key: controller[key] for key in run_state.HOLDER_KEYS}

    def append_event(self, run: str, event: dict, *, holder=...):
        return run_state.append_event(run, event, holder=self.run_holder(run) if holder is ... else holder)

    def reconcile(self, run: str, **kwargs):
        kwargs.setdefault("holder", self.run_holder(run))
        return run_state.reconcile(run, **kwargs)

    def bind_holder(self, run: str, holder: dict) -> None:
        current = self.run_holder(run)
        run_state.append_event(run, {"type": "lease_drain_started", **current}, holder=current)
        run_state.append_event(run, {"type": "lease_transfer", **holder}, holder=current)

    def model(self, *, resolved=None, thinking=None, effort=None, requested_role=None, requested_effort=None) -> dict:
        return {"requested_role": requested_role, "requested_effort": requested_effort, "resolved": resolved, "thinking": thinking, "effort": effort}

    def start_attempt(self, run: str, node: str, *, attempt: int = 1, holder=..., handle: dict | None = None, model: dict | None = None) -> None:
        actual_holder = self.run_holder(run) if holder is ... else holder
        model = model or self.model()
        handle = dict(handle or {"spawning_session": "s", "job_id": f"{node}-{attempt}"})
        handle.setdefault("workspace", f"ws-{node}")
        handle.setdefault("pane", f"pane-{node}")
        handle.setdefault("agent", f"agent-{node}")
        self.append_event(run, {"type": "dispatch_intent", "node": node, "attempt": attempt}, holder=actual_holder)
        attempt_dir = self.attempt_dir(run, node, attempt)
        repo = self.repos.get(run)
        run_json = self.load_run_json(run)
        if repo:
            handle.setdefault("repo", str(repo.resolve()))
            handle.setdefault("base", run_json.get("base_sha"))
            plan = json.loads((Path(run) / "graph.json").read_text(encoding="utf-8")).get("nodes", {}).get(node, {})
            writer = plan.get("worktree") is not None or any(
                isinstance(out, dict) and out.get("type") == "vcs" for out in (plan.get("outputs") or [])
            )
            if writer and "worktree_path" not in handle:
                wt = self.make_worktree(repo, node=node, attempt=attempt, base=handle["base"])
                handle["worktree_path"] = str(wt.resolve())
                handle["branch"] = f"t/{wt.name}"
            else:
                handle.setdefault("worktree_path", str(repo.resolve()))
                if "branch" not in handle:
                    handle["branch"] = self.git(repo, "branch", "--show-current")
        run_id = run_json["id"]
        identity = {
            "workspace": handle["workspace"], "pane": handle["pane"], "agent": handle["agent"],
            "run": run_state.canonical_path(run), "run_id": run_id, "node": node, "attempt": str(attempt),
            "tokens": {"provenance": "orchestrator", "run": run_id, "node": node, "attempt": str(attempt)},
            "worktree_path": handle.get("worktree_path"), "repo": handle.get("repo"),
            "branch": handle.get("branch"), "base": handle.get("base"), "routing": model,
        }
        (attempt_dir / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
        self.append_event(run, {"type": "dispatched", "node": node, "attempt": attempt, "handle": handle, "model": model}, holder=actual_holder)

    def artifact(self, run: str, node: str, attempt: int, name: str, typ: str, text: str = "x") -> dict:
        attempt_dir = self.attempt_dir(run, node, attempt)
        filename = "vcs.json" if typ == "vcs" else "integration.json" if typ == "integration" else f"{name}.json"
        path = attempt_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if typ == "vcs":
            run_json = self.load_run_json(run)
            identity = json.loads((attempt_dir / "dispatch-identity.json").read_text(encoding="utf-8"))
            head = self.git(Path(identity["worktree_path"]), "rev-parse", "HEAD")
            payload = {
                "worktree": identity["worktree_path"],
                "repo": identity["repo"],
                "branch": identity["branch"],
                "base_sha": run_json["base_sha"],
                "result": {"form": "commit", "sha": head},
            }
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        elif typ == "integration":
            consumed = json.loads((attempt_dir / "consumed_artifacts.json").read_text(encoding="utf-8"))
            run_json = self.load_run_json(run)
            path.write_text(json.dumps({"base_sha": run_json["base_sha"], "commit": text, "head": text, "consumed": consumed}) + "\n", encoding="utf-8")
        else:
            path.write_text(text, encoding="utf-8")
        return {"name": name, "type": typ, "path": str(path.resolve()), "hash": run_state.sha256_file(path)}

    def write_attempt_receipts(self, run: str, node: str, attempt: int, sha: str, *, consumed=None, base_sha=None, commit=None, head=None, vcs_sha=None) -> None:
        attempt_dir = self.attempt_dir(run, node, attempt)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot_consumed(run, node, attempt)
        if consumed is None:
            consumed = self.receipt_consumed(snapshot)
        run_json = self.load_run_json(run)
        identity = self.identity_for(run, node, attempt)
        integration = {
            "base_sha": run_json["base_sha"] if base_sha is None else base_sha,
            "commit": sha if commit is None else commit,
            "head": sha if head is None else head,
            "consumed": consumed,
        }
        (attempt_dir / "integration.json").write_text(json.dumps(integration) + "\n", encoding="utf-8")
        (attempt_dir / "vcs.json").write_text(
            json.dumps({
                "worktree": identity["worktree_path"],
                "repo": identity["repo"],
                "branch": identity["branch"],
                "base_sha": run_json["base_sha"],
                "result": {"form": "commit", "sha": sha if vcs_sha is None else vcs_sha},
            }) + "\n",
            encoding="utf-8",
        )

    def vcs_artifact(self, run: str, node: str, attempt: int, result: dict) -> dict:
        attempt_dir = self.attempt_dir(run, node, attempt)
        identity = json.loads((attempt_dir / "dispatch-identity.json").read_text(encoding="utf-8"))
        run_json = self.load_run_json(run)
        path = attempt_dir / "vcs.json"
        path.write_text(json.dumps({
            "worktree": identity["worktree_path"],
            "repo": identity["repo"],
            "branch": identity["branch"],
            "base_sha": run_json["base_sha"],
            "result": result,
        }) + "\n", encoding="utf-8")
        return {"name": "vcs", "type": "vcs", "path": str(path.resolve()), "hash": run_state.sha256_file(path)}

    def promotion_graph(self) -> dict:
        return {
            "schema_version": 1,
            "nodes": {
                "D": {
                    "id": "D",
                    "executor": "herdr-agent",
                    "outputs": [{"name": "integration", "type": "integration"}, {"name": "vcs", "type": "vcs"}],
                    "worktree": "d",
                    "owns": ["src/d/**"],
                    "retry": {"max_attempts": 2, "on": ["failed", "invalidated"]},
                }
            },
        }

    def diamond_graph(self) -> dict:
        def writer(nid: str, owns: str) -> dict:
            return {
                "id": nid,
                "executor": "herdr-agent",
                "outputs": [{"name": "patch", "type": "vcs"}],
                "worktree": nid.lower(),
                "owns": [owns],
                "retry": {"max_attempts": 3, "on": ["failed", "invalidated"]},
            }
        return {
            "schema_version": 1,
            "nodes": {
                "B": writer("B", "src/b/**"),
                "C": writer("C", "src/c/**"),
                "D": {
                    "id": "D",
                    "executor": "herdr-agent",
                    "outputs": [{"name": "integration", "type": "integration"}, {"name": "vcs", "type": "vcs"}],
                    "worktree": "d",
                    "owns": ["src/d/**"],
                    "depends_on": ["B", "C"],
                    "inputs": [
                        {"alias": "b_patch", "from": "B", "output": "patch", "required": True},
                        {"alias": "c_patch", "from": "C", "output": "patch", "required": True},
                    ],
                    "retry": {"max_attempts": 2, "on": ["failed", "invalidated"]},
                },
            },
        }

    def succeed_vcs(self, run: str, node: str, relpath: str, text: str, *, attempt: int = 1, output: str = "patch") -> tuple[str, dict, dict]:
        self.start_attempt(run, node, attempt=attempt)
        ident = self.identity_for(run, node, attempt)
        sha = self.commit_file(Path(ident["worktree_path"]), relpath, text)
        art = self.artifact(run, node, attempt, output, "vcs")
        self.append_event(run, {"type": "succeeded", "node": node, "attempt": attempt, "artifacts": [art]})
        return sha, ident, art

    def add_checkout_worktree(self, repo: Path, branch: str, *, create_branch: bool) -> Path:
        self._wt += 1
        path = self.root / "co" / f"{self._wt}"
        path.parent.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add"]
        if create_branch:
            args.extend(["-b", branch, str(path)])
        else:
            args.extend([str(path), branch])
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
        return path

    def pending_promotion_run(self, *, ref_at: str) -> tuple[str, Path, str, dict]:
        repo = self.repo()
        primary = self.checkout_state(repo)
        run = self.new_run(self.promotion_graph(), repo)
        run_json = self.load_run_json(run)
        self.start_attempt(run, "D")
        ident = self.identity_for(run, "D", 1)
        wt = Path(ident["worktree_path"])
        new_sha = self.commit_file(wt, "src/d/result.txt", "two\n")
        self.write_attempt_receipts(run, "D", 1, new_sha)
        self.append_event(run, {
            "type": "promotion_intent",
            "node": "D",
            "attempt": 1,
            "ref": run_json["promotion_ref"],
            "expected_old": run_json["base_sha"],
            "new": new_sha,
        })
        if ref_at == "new":
            self.git(repo, "update-ref", run_json["promotion_ref"], new_sha, run_json["base_sha"])
        elif ref_at == "other":
            other_sha = self.commit_file(wt, "src/d/result.txt", "three\n")
            self.git(repo, "update-ref", run_json["promotion_ref"], other_sha)
            self.git(wt, "reset", "--hard", new_sha)
            run_json["other_sha"] = other_sha
        self.assertEqual(self.checkout_state(repo), primary)
        run_json["worktree"] = str(wt)
        return run, repo, new_sha, run_json

    def promoted_run(self, job: str = "Job", repo: Path | None = None) -> tuple[str, Path, str]:
        repo = repo or self.repo()
        run = self.bound_run(repo, job)
        run_state.accept_graph(run, self.promotion_graph(), repo=repo, holder=self.initial_holder)
        self.reconcile(run)
        self.start_attempt(run, "D")
        wt = Path(self.identity_for(run, "D", 1)["worktree_path"])
        sha = self.commit_file(wt, "src/d/result.txt", "delivered\n")
        self.write_attempt_receipts(run, "D", 1, sha)
        return run, repo, sha

    def test_promote_publishes_descriptive_delivery_branch(self):
        run, repo, sha = self.promoted_run("Studio run review of FlowOps")
        result = run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=self.run_holder(run))
        ref = "refs/heads/feature/studio-run-review-of-flowops"
        self.assertEqual(result["delivery_branch"], ref)
        self.assertEqual(self.git_ref(repo, ref), sha)
        self.assertEqual(result["state"]["nodes"]["D"]["state"], "succeeded")
        run_json = self.load_run_json(run)
        self.assertEqual(run_json["promotion_ref"], f"refs/heads/herdr/{run_json['id']}")
        self.assertEqual(self.git_ref(repo, run_json["promotion_ref"]), sha)
        self.assertEqual(
            run_state.deliver_branch(run, repo=repo, holder=self.run_holder(run)),
            {"delivery_branch": ref, "sha": sha, "created": False},
        )

    def test_delivery_waits_for_the_whole_accepted_graph(self):
        repo = self.repo()
        graph = self.promotion_graph()
        graph["nodes"]["E"] = {"id": "E", "executor": "task", "depends_on": ["D"]}
        run = self.bound_run(repo, "Two wave delivery")
        run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        self.reconcile(run)
        self.start_attempt(run, "D")
        wt = Path(self.identity_for(run, "D", 1)["worktree_path"])
        sha = self.commit_file(wt, "src/d/result.txt", "wave\n")
        self.write_attempt_receipts(run, "D", 1, sha)
        ref = run_state.default_delivery_branch("Two wave delivery")
        result = run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=self.run_holder(run))
        self.assertIsNone(result["delivery_branch"])
        self.assertIn("unfinished", result["delivery_note"])
        self.assertIsNone(self.git_ref(repo, ref))
        with self.assertRaisesRegex(run_state.PreconditionError, "unfinished"):
            run_state.deliver_branch(run, repo=repo, holder=self.run_holder(run))
        self.reconcile(run)
        self.start_attempt(run, "E")
        self.append_event(run, {"type": "succeeded", "node": "E", "attempt": 1})
        self.assertEqual(
            run_state.deliver_branch(run, repo=repo, holder=self.run_holder(run)),
            {"delivery_branch": ref, "sha": sha, "created": True},
        )

    def test_delivery_branch_collision_authorization_and_alias(self):
        run, repo, sha = self.promoted_run("Collide delivery")
        ref = run_state.default_delivery_branch("Collide delivery")
        squatter = self.git(repo, "rev-parse", "HEAD")
        self.git(repo, "update-ref", ref, squatter)
        result = run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=self.run_holder(run))
        self.assertIsNone(result["delivery_branch"])
        self.assertIn("already exists", result["delivery_note"])
        self.assertEqual(self.git_ref(repo, ref), squatter)
        self.assertEqual(result["state"]["nodes"]["D"]["state"], "succeeded")

        with self.assertRaises(run_state.PreconditionError):
            run_state.deliver_branch(run, repo=repo, holder={"workspace": "x", "pane": "x", "session": "x"}, branch="feature/collide-alias")
        self.assertIsNone(self.git_ref(repo, "refs/heads/feature/collide-alias"))
        self.assertEqual(
            run_state.deliver_branch(run, repo=repo, holder=self.run_holder(run), branch="feature/collide-alias"),
            {"delivery_branch": "refs/heads/feature/collide-alias", "sha": sha, "created": True},
        )
        self.assertEqual(self.git_ref(repo, ref), squatter)
        for bad in ("", "feature/bad..name", "refs/tags/v1", "-x", "feature/trailing/"):
            with self.subTest(branch=bad), self.assertRaises(run_state.PreconditionError):
                run_state.deliver_branch(run, repo=repo, holder=self.run_holder(run), branch=bad)

    def test_recovery_ensures_delivery_branch_and_refuses_undelivered_run(self):
        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="new")
        ref = run_state.default_delivery_branch(run_json["job"])
        with self.assertRaises(run_state.PreconditionError):
            run_state.deliver_branch(run, repo=repo, holder=self.run_holder(run))
        self.assertIsNone(self.git_ref(repo, ref))
        run_state.recover_promotion(run, repo, holder=self.run_holder(run))
        self.assertEqual(run_state.load_state(run)["nodes"]["D"]["state"], "succeeded")
        self.assertEqual(self.git_ref(repo, ref), new_sha)

    def test_recovery_publishes_after_interruption_between_succeeded_and_delivery(self):
        run, repo, sha = self.promoted_run("Interrupted delivery")
        ref = run_state.default_delivery_branch("Interrupted delivery")
        holder = self.run_holder(run)
        original = promotion._autodeliver_locked
        promotion._autodeliver_locked = lambda *_args, **_kwargs: (_ for _ in ()).throw(run_state.RunError("interrupted before publishing"))
        try:
            with self.assertRaisesRegex(run_state.RunError, "interrupted before publishing"):
                run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=holder)
        finally:
            promotion._autodeliver_locked = original
        self.assertEqual(run_state.load_state(run)["nodes"]["D"]["state"], "succeeded")
        self.assertIsNone(self.git_ref(repo, ref))

        events = (Path(run) / "events.jsonl").read_bytes()
        other_repo = self.repo()
        for bad in ({"holder": {"workspace": "x", "pane": "x", "session": "x"}, "repo": repo}, {"holder": holder, "repo": other_repo}):
            with self.subTest(**{k: str(v) for k, v in bad.items()}), self.assertRaises(run_state.PreconditionError):
                run_state.recover_promotion(run, bad["repo"], holder=bad["holder"])
            self.assertIsNone(self.git_ref(repo, ref))
            self.assertEqual((Path(run) / "events.jsonl").read_bytes(), events)

        run_state.recover_promotion(run, repo, holder=holder)
        self.assertEqual(self.git_ref(repo, ref), sha)
        self.assertEqual((Path(run) / "events.jsonl").read_bytes(), events)
        run_state.recover_promotion(run, repo, holder=holder)
        self.assertEqual(self.git_ref(repo, ref), sha)


    def test_draining_exact_holder_can_finish_native_attempts_only(self):
        graph = {"schema_version": 1, "nodes": {"T": {"id": "T", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]}, "U": {"id": "U", "executor": "task"}}}
        holder = {"workspace": "ws", "pane": "pane", "session": "session"}
        other = {"workspace": "other", "pane": "pane", "session": "session"}

        for terminal in ("succeeded", "failed", "skipped"):
            with self.subTest(terminal=terminal):
                run = self.new_run(graph)
                self.bind_holder(run, holder)
                self.start_attempt(run, "T", holder=holder)
                self.append_event(run, {"type": "lease_drain_started", **holder}, holder=holder)
                event = {"type": terminal, "node": "T", "attempt": 1}
                if terminal == "succeeded":
                    event["artifacts"] = [self.artifact(run, "T", 1, "summary", "summary", "done")]
                result = self.append_event(run, event, holder=holder)
                self.assertEqual(result["state"]["nodes"]["T"]["state"], terminal)
                self.assertFalse(result["controller"]["dispatch"])

        for bad_holder in (None, other):
            with self.subTest(bad_holder=bad_holder):
                run = self.new_run(graph)
                self.bind_holder(run, holder)
                self.start_attempt(run, "T", holder=holder)
                self.append_event(run, {"type": "lease_drain_started", **holder}, holder=holder)
                with self.assertRaises(run_state.PreconditionError):
                    self.append_event(run, {"type": "failed", "node": "T", "attempt": 1}, holder=bad_holder)
                self.assertEqual(run_state.replay(run)["state"]["nodes"]["T"]["state"], "running")

        run = self.new_run(graph)
        self.bind_holder(run, holder)
        self.start_attempt(run, "T", holder=holder)
        self.append_event(run, {"type": "lease_drain_started", **holder}, holder=holder)
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "dispatch_intent", "node": "U", "attempt": 1}, holder=holder)
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "replan", "node": "U", "model": self.model(resolved="provider/next")}, holder=holder)

    def test_promotion_recovery_is_bounded_and_finishes_attempt_success(self):
        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="new")
        state = self.reconcile(run, repo=repo)["state"]["nodes"]["D"]
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), new_sha)
        receipt = json.loads((Path(run) / "artifacts" / "D" / "1" / "promoted.json").read_text())
        self.assertEqual(receipt["new"], new_sha)
        self.assertEqual(state["state"], "succeeded")
        self.assertEqual(state["attempt"], 1)
        self.assertEqual({item["name"] for item in state["artifacts"]}, {"integration", "vcs"})
        events = [event["type"] for event in run_state.replay(run)["events"] if event.get("node") == "D"]
        self.assertEqual(events[-2:], ["promoted", "succeeded"])

    def test_promotion_rejects_bad_receipts_and_blocks_unexpected_ref_without_moving_it(self):
        repo = self.repo()
        primary = self.checkout_state(repo)
        run = self.new_run(self.promotion_graph(), repo)
        run_json = self.load_run_json(run)
        self.start_attempt(run, "D")
        new_sha = self.commit_file(Path(self.identity_for(run, "D", 1)["worktree_path"]), "src/d/result.txt", "two\n")
        attempt_dir = self.attempt_dir(run, "D", 1)
        (attempt_dir / "integration.json").write_text("{}\n", encoding="utf-8")
        (attempt_dir / "vcs.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(run_state.PreconditionError):
            run_state.promote(run, node="D", attempt=1, new_sha=new_sha, repo=repo, holder=self.run_holder(run))
        self.assert_frozen(repo, primary, run, ref=run_json["promotion_ref"], ref_value=run_json["base_sha"])
        self.assertNotIn("promotion_intent", [event["type"] for event in run_state.replay(run)["events"]])

        run, repo, _new_sha, run_json = self.pending_promotion_run(ref_at="other")
        state = self.reconcile(run, repo=repo)["state"]["nodes"]["D"]
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), run_json["other_sha"])
        self.assertEqual(state["state"], "blocked")
        events = [event["type"] for event in run_state.replay(run)["events"] if event.get("node") == "D"]
        self.assertIn("blocked", events)
        self.assertNotIn("promoted", events)

    def test_terminal_events_validate_attempts_and_retry_materializes_next_attempt(self):
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}], "retry": {"max_attempts": 2, "on": ["failed"], "side_effect_safe": True}}}}
        run = self.new_run(graph)
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1})

        self.start_attempt(run, "A")
        malformed = Path(run) / "artifacts" / "A" / "1" / "malformed.txt"
        malformed.write_text("malformed\n", encoding="utf-8")
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": [{"name": "summary", "type": "summary", "path": str(malformed)}]})
        outside = self.root / "outside-summary.txt"
        outside.write_text("outside\n", encoding="utf-8")
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": [{"name": "summary", "type": "summary", "path": str(outside), "hash": run_state.sha256_file(outside)}]})
        art = self.artifact(run, "A", 1, "summary", "summary", "ok")
        self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": [art]})
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "failed", "node": "A", "attempt": 1})

        retry_run = self.new_run(graph)
        self.start_attempt(retry_run, "A")
        marker = Path(retry_run) / "artifacts" / "A" / "1" / "marker.txt"
        marker.write_text("keep\n", encoding="utf-8")
        self.append_event(retry_run, {"type": "failed", "node": "A", "attempt": 1})
        self.append_event(retry_run, {"type": "retried", "node": "A", "attempt": 1, "reason": "failed"})
        node = run_state.replay(retry_run)["state"]["nodes"]["A"]
        self.assertEqual(node["state"], "planned")
        self.assertEqual(node["attempt"], 2)
        self.assertNotIn("handle", node)
        self.assertEqual(node.get("artifacts", []), [])
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((Path(retry_run) / "artifacts" / "A" / "2").exists())

        writer_graph = {"schema_version": 1, "nodes": {"W": {"id": "W", "executor": "herdr-agent", "outputs": [{"name": "vcs", "type": "vcs"}], "worktree": "w", "owns": ["src/w/**"]}}}
        writer_run = self.new_run(writer_graph)
        self.start_attempt(writer_run, "W")
        writer_vcs = Path(writer_run) / "artifacts" / "W" / "1" / "vcs.json"
        writer_vcs.write_text("{}\n", encoding="utf-8")
        writer_art = {"name": "vcs", "type": "vcs", "path": str(writer_vcs), "hash": run_state.sha256_file(writer_vcs)}
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(writer_run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [writer_art]})

        integration_run = self.new_run(self.promotion_graph())
        self.start_attempt(integration_run, "D")
        integration_dir = Path(integration_run) / "artifacts" / "D" / "1"
        (integration_dir / "integration.json").write_text(json.dumps({"commit": "abc", "head": "abc"}) + "\n", encoding="utf-8")
        (integration_dir / "vcs.json").write_text(json.dumps({"result": {"sha": "abc"}}) + "\n", encoding="utf-8")
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(integration_run,
            {
                "type": "succeeded",
                "node": "D",
                "attempt": 1,
                "artifacts": [
                    {"name": "integration", "type": "integration", "path": str(integration_dir / "integration.json"), "hash": run_state.sha256_file(integration_dir / "integration.json")},
                    {"name": "vcs", "type": "vcs", "path": str(integration_dir / "vcs.json"), "hash": run_state.sha256_file(integration_dir / "vcs.json")},
                ],
            },)

    def test_native_retries_require_explicit_side_effect_safety(self):
        for executor in ("task", "workflow"):
            for terminal in ("failed", "interrupted"):
                for safe in (None, False, True):
                    with self.subTest(executor=executor, terminal=terminal, side_effect_safe=safe):
                        retry = {"max_attempts": 2, "on": [terminal]}
                        if safe is not None:
                            retry["side_effect_safe"] = safe
                        run = self.new_run({"schema_version": 1, "nodes": {"N": {"id": "N", "executor": executor, "retry": retry}}})
                        self.start_attempt(run, "N")
                        self.append_event(run, {"type": terminal, "node": "N", "attempt": 1})
                        event = {"type": "retried", "node": "N", "attempt": 1, "reason": terminal}
                        if safe is True:
                            self.append_event(run, event)
                            self.assertEqual(run_state.replay(run)["state"]["nodes"]["N"]["attempt"], 2)
                        else:
                            with self.assertRaisesRegex(run_state.PreconditionError, "retry not allowed"):
                                self.append_event(run, event)

        for executor in ("herdr-agent", "pane"):
            with self.subTest(executor=executor):
                node = {"id": "N", "executor": executor, "retry": {"max_attempts": 2, "on": ["failed"]}}
                if executor == "pane":
                    node["handle"] = {"name": "user-pane"}
                run = self.new_run({"schema_version": 1, "nodes": {"N": node}})
                self.start_attempt(run, "N")
                self.append_event(run, {"type": "failed", "node": "N", "attempt": 1})
                self.append_event(run, {"type": "retried", "node": "N", "attempt": 1, "reason": "failed"})
                self.assertEqual(run_state.replay(run)["state"]["nodes"]["N"]["attempt"], 2)

    def test_current_session_native_observation_uses_job_id_and_workflow_run_id(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "T": {"id": "T", "executor": "task"},
                "W": {"id": "W", "executor": "workflow"},
                "O": {"id": "O", "executor": "task"},
            },
        }
        run = self.new_run(graph)
        self.start_attempt(run, "T", handle={"spawning_session": "session", "job_id": "task-1"})
        self.start_attempt(run, "W", handle={"spawning_session": "session", "runId": "workflow-1"})
        self.start_attempt(run, "O", handle={"spawning_session": "other", "job_id": "old-1"})

        state = self.reconcile(run, live_native_ids=[])["state"]["nodes"]
        self.assertEqual(state["T"]["state"], "running")
        self.assertEqual(state["W"]["state"], "running")
        self.assertEqual(state["O"]["state"], "running")

        state = self.reconcile(run, current_session="session", live_native_ids=[])["state"]["nodes"]
        self.assertEqual(state["T"]["state"], "running")
        self.assertEqual(state["W"]["state"], "running")
        self.assertEqual(state["O"]["state"], "interrupted")

        state = self.reconcile(run, current_session="session", live_native_ids=["task-1", "workflow-1"], native_inventory_complete=True)["state"]["nodes"]
        self.assertEqual(state["T"]["state"], "running")
        self.assertEqual(state["W"]["state"], "running")
        self.assertEqual(state["O"]["state"], "interrupted")

        missing = self.new_run(graph)
        self.start_attempt(missing, "T", handle={"spawning_session": "session", "job_id": "task-1"})
        self.start_attempt(missing, "W", handle={"spawning_session": "session", "runId": "workflow-1"})
        state = self.reconcile(missing, current_session="session", live_native_ids=["task-1"], native_inventory_complete=True)["state"]["nodes"]
        self.assertEqual(state["T"]["state"], "running")
        self.assertEqual(state["W"]["state"], "interrupted")
        no_id = self.new_run({"schema_version": 1, "nodes": {"T": {"id": "T", "executor": "task"}}})
        self.start_attempt(no_id, "T", handle={"spawning_session": "session"})
        self.assertEqual(self.reconcile(no_id, current_session="session", live_native_ids=[])["state"]["nodes"]["T"]["state"], "running")
        self.assertEqual(self.reconcile(no_id, current_session="session", live_native_ids=[], native_inventory_complete=True)["state"]["nodes"]["T"]["state"], "interrupted")


    def test_wildcard_ownership_intersections_are_rejected(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "B": {"id": "B", "executor": "task", "owns": ["src/**/*.py"]},
                "C": {"id": "C", "executor": "task", "owns": ["src/a/**"]},
            },
        }
        with self.assertRaisesRegex(run_state.PreconditionError, "overlap"):
            run_state.validate_graph(graph)

    def test_replay_is_pure_first_event_epoch_and_malformed_tail_fails_closed(self):
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}
        run = run_state.claim_run(self.root / "docs", "Manual", created="2026-09-04T01:02:03+00:00")["dir"]
        run_path = Path(run)
        (run_path / "graph.json").write_text(json.dumps(graph) + "\n", encoding="utf-8")
        (run_path / "events.jsonl").write_text(
            json.dumps({"type": "graph_accepted", "epoch": 7}) + "\n" + json.dumps({"type": "succeeded", "node": "A", "attempt": 1, "artifacts": [], "epoch": 7}) + "\n",
            encoding="utf-8",
        )
        for cache in ("state.json", "controller.json", "status.md"):
            (run_path / cache).unlink(missing_ok=True)
        snap = run_state.replay(run)
        self.assertEqual(snap["controller"]["epoch"], 7)
        self.assertEqual(snap["state"]["epoch"], 7)
        self.assertEqual(snap["state"]["nodes"]["A"]["state"], "succeeded")
        self.assertFalse((run_path / "state.json").exists())
        self.assertFalse((run_path / "controller.json").exists())
        self.assertFalse((run_path / "status.md").exists())

        bad = self.new_run(graph)
        self.start_attempt(bad, "A")
        with open(Path(bad) / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"type":"succeeded"')
        state = self.reconcile(bad)["state"]
        self.assertFalse(state["dispatch"])
        self.assertEqual(state["nodes"]["A"]["state"], "interrupted")

    def test_computed_lifecycle_edges_and_required_skipped_input_are_logged(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "condition": False, "outputs": [{"name": "summary", "type": "summary"}]},
                "B": {"id": "B", "executor": "task", "depends_on": ["A"], "inputs": [{"alias": "a", "from": "A", "output": "summary", "required": True}]},
            },
        }
        run = self.new_run(graph)
        state = self.reconcile(run)["state"]["nodes"]
        self.assertEqual(state["A"]["state"], "skipped")
        self.assertEqual(state["B"]["state"], "blocked")
        self.assertEqual(state["B"]["reason"], "input a missing")
        events = [(event["type"], event.get("node"), event.get("attempt")) for event in run_state.replay(run)["events"]]
        self.assertIn(("skipped", "A", 0), events)
        self.assertIn(("blocked", "B", 0), events)

    def test_notifications_are_per_attempt_transition_edges(self):
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task", "retry": {"max_attempts": 2, "on": ["failed"], "side_effect_safe": True}}, "B": {"id": "B", "executor": "task"}}}
        run = self.new_run(graph)
        self.start_attempt(run, "A")
        self.append_event(run, {"type": "failed", "node": "A", "attempt": 1})
        first = self.reconcile(run, notify=True)["notified"]
        self.assertTrue(first)

        self.append_event(run, {"type": "retried", "node": "A", "attempt": 1, "reason": "failed"})
        self.reconcile(run)
        self.start_attempt(run, "A", attempt=2)
        self.append_event(run, {"type": "failed", "node": "A", "attempt": 2})
        second = self.reconcile(run, notify=True)["notified"]
        self.assertTrue(second)
        self.assertTrue(set(first).isdisjoint(second))

    def test_first_reconcile_batches_fifty_ready_consumers(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "source": {"id": "source", "executor": "task"},
                **{
                    f"C{i}": {"id": f"C{i}", "executor": "task", "depends_on": ["source"]}
                    for i in range(50)
                },
            },
        }
        run = self.new_run(graph)
        self.start_attempt(run, "source")
        self.append_event(run, {"type": "succeeded", "node": "source", "attempt": 1})
        holder = self.run_holder(run)
        log_path = Path(run) / "events.jsonl"
        streams = []
        original_open = open

        def counted_open(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            if mode == "a" and Path(path) == log_path:
                wrapped = mock.MagicMock(wraps=stream)
                wrapped.__enter__.return_value = wrapped
                wrapped.__exit__.side_effect = stream.__exit__
                streams.append(wrapped)
                return wrapped
            return stream

        with mock.patch("builtins.open", counted_open):
            result = run_state.reconcile(run, holder=holder)
        self.assertLessEqual(sum(stream.write.call_count for stream in streams), 3)
        self.assertEqual(result["state"]["nodes"]["source"]["state"], "succeeded")
        self.assertTrue(all(result["state"]["nodes"][f"C{i}"]["state"] == "ready" for i in range(50)))
        ready = [event for event in run_state.replay(run)["events"] if event.get("type") == "ready" and event.get("node", "").startswith("C")]
        self.assertEqual(len(ready), 50)

    def test_hundred_notification_edges_on_seven_hundred_events_under_one_second(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "source": {"id": "source", "executor": "task"},
                **{f"B{i}": {"id": f"B{i}", "executor": "task"} for i in range(100)},
                **{
                    f"C{i}": {"id": f"C{i}", "executor": "task", "depends_on": ["source"]}
                    for i in range(100)
                },
            },
        }
        run = self.new_run(graph)
        self.start_attempt(run, "source")
        self.append_event(run, {"type": "succeeded", "node": "source", "attempt": 1})
        holder = self.run_holder(run)
        events = [
            {"type": "blocked", "node": f"B{i}", "attempt": 0, "reason": "operator hold"}
            for i in range(100)
        ]
        existing = len(run_state.replay(run)["events"])
        events.extend({"type": "notification_sent", "key": f"history:{i}"} for i in range(700 - existing - len(events)))
        with ledger.with_lock(Path(run)):
            ledger.append_events_locked(Path(run), events, holder=holder)
        before = run_state.replay(run)
        self.assertEqual(len(before["events"]), 700)
        expected = {
            f"edge:{event['epoch']}:{sequence}:blocked:{event['node']}:0"
            for sequence, event in enumerate(before["events"], 1)
            if event.get("type") == "blocked"
        }
        started = time.perf_counter()
        result = run_state.reconcile(run, notify=True, holder=holder)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(len(result["notified"]), 100)
        self.assertEqual(set(result["notified"]), expected)
        self.assertTrue(all(result["state"]["nodes"][f"C{i}"]["state"] == "ready" for i in range(100)))
        self.assertTrue(all(result["state"]["nodes"][f"B{i}"]["state"] == "blocked" for i in range(100)))
        self.assertTrue(expected.issubset(result["state"]["notified"]))
        self.assertEqual(run_state.reconcile(run, notify=True, holder=holder)["notified"], [])

    def test_dispatch_snapshot_crash_windows_repair_from_event_without_duplicate(self):
        graph = {
            "schema_version": 1,
            "nodes": {"A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}], "owns": ["src/a/**"]}},
        }

        def intent_count(run: str) -> int:
            return sum(1 for line in (Path(run) / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip() and json.loads(line).get("type") == "dispatch_intent")

        def plant() -> tuple[str, Path, object, object]:
            run = self.new_run(graph)
            self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 1})
            intent = [event for event in run_state.replay(run)["events"] if event["type"] == "dispatch_intent"][-1]
            self.assertEqual(intent["owns"], ["src/a/**"])
            attempt = Path(run) / "artifacts" / "A" / "1"
            consumed = json.loads((attempt / "consumed_artifacts.json").read_text(encoding="utf-8"))
            ownership = json.loads((attempt / "ownership.json").read_text(encoding="utf-8"))
            return run, attempt, consumed, ownership

        def assert_repaired(run: str, attempt: Path, consumed, ownership) -> None:
            self.assertEqual(json.loads((attempt / "consumed_artifacts.json").read_text(encoding="utf-8")), consumed)
            self.assertEqual(json.loads((attempt / "ownership.json").read_text(encoding="utf-8")), ownership)
            self.assertEqual(intent_count(run), 1)

        run, attempt, consumed, ownership = plant()
        for child in list(attempt.iterdir()):
            child.unlink()
        attempt.rmdir()
        run_state.replay(run)
        self.assertFalse(attempt.exists())
        self.reconcile(run)
        assert_repaired(run, attempt, consumed, ownership)

        run, attempt, consumed, ownership = plant()
        (attempt / "ownership.json").unlink()
        run_state.replay(run)
        self.assertFalse((attempt / "ownership.json").exists())
        self.reconcile(run)
        assert_repaired(run, attempt, consumed, ownership)
        run, attempt, consumed, ownership = plant()
        (attempt / "consumed_artifacts.json").unlink()
        self.reconcile(run)
        assert_repaired(run, attempt, consumed, ownership)

        run, attempt, consumed, ownership = plant()
        broken = attempt / "ownership.json"
        broken.write_bytes(b'{"broken"')
        before = broken.read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            self.reconcile(run)
        self.assertEqual(broken.read_bytes(), before)
        self.assertEqual(json.loads((attempt / "consumed_artifacts.json").read_text(encoding="utf-8")), consumed)


        run, attempt, consumed, ownership = plant()
        broken = attempt / "consumed_artifacts.json"
        broken.write_bytes(b'{"broken"')
        before = broken.read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            self.reconcile(run)
        self.assertEqual(broken.read_bytes(), before)
        self.assertEqual(json.loads((attempt / "ownership.json").read_text(encoding="utf-8")), ownership)

        run, attempt, consumed, ownership = plant()
        (attempt / "consumed_artifacts.json").write_text(json.dumps(["tampered"]) + "\n", encoding="utf-8")
        (attempt / "ownership.json").write_text(json.dumps({"slices": {"A": ["nope"]}}) + "\n", encoding="utf-8")
        with self.assertRaises(run_state.PreconditionError):
            self.reconcile(run)
        self.assertEqual(json.loads((attempt / "consumed_artifacts.json").read_text(encoding="utf-8")), ["tampered"])
        self.assertEqual(json.loads((attempt / "ownership.json").read_text(encoding="utf-8")), {"slices": {"A": ["nope"]}})
        self.assertEqual(intent_count(run), 1)
        run, attempt, consumed, ownership = plant()
        revised_owns = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}], "owns": ["src/revised/**"]}}}
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, revised_owns, repo=self.repos[run], holder=self.run_holder(run))
        self.assertEqual(json.loads((attempt / "ownership.json").read_text(encoding="utf-8")), ownership)
        self.assertEqual(intent_count(run), 1)
        allowed = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}], "owns": ["src/a/**"]},
                "Z": {"id": "Z", "executor": "task"},
            },
        }
        run_state.accept_graph(run, allowed, repo=self.repos[run], holder=self.run_holder(run))
        (attempt / "ownership.json").unlink()
        self.reconcile(run)
        self.assertEqual(json.loads((attempt / "ownership.json").read_text(encoding="utf-8")), ownership)
        self.assertEqual(json.loads((attempt / "consumed_artifacts.json").read_text(encoding="utf-8")), consumed)
        self.assertEqual(intent_count(run), 1)
        self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": {"spawning_session": "s", "job_id": "A-1"}, "model": self.model()})
        self.append_event(run, {"type": "failed", "node": "A", "attempt": 1})
        run_state.accept_graph(run, revised_owns, repo=self.repos[run], holder=self.run_holder(run))
        (attempt / "ownership.json").unlink()
        self.reconcile(run)
        self.assertEqual(json.loads((attempt / "ownership.json").read_text(encoding="utf-8")), ownership)
        self.assertEqual(json.loads((attempt / "consumed_artifacts.json").read_text(encoding="utf-8")), consumed)
        self.assertEqual(intent_count(run), 1)


        run, attempt, consumed, ownership = plant()
        self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": {"spawning_session": "s", "job_id": "A-1"}, "model": self.model()})
        extra = attempt / "marker.txt"
        extra.write_text("keep\n", encoding="utf-8")
        self.append_event(run, {"type": "failed", "node": "A", "attempt": 1})
        before_consumed = (attempt / "consumed_artifacts.json").read_bytes()
        before_owns = (attempt / "ownership.json").read_bytes()
        consumed_mtime = (attempt / "consumed_artifacts.json").stat().st_mtime_ns
        self.reconcile(run)
        self.assertEqual((attempt / "consumed_artifacts.json").read_bytes(), before_consumed)
        self.assertEqual((attempt / "consumed_artifacts.json").stat().st_mtime_ns, consumed_mtime)
        (attempt / "consumed_artifacts.json").write_text(json.dumps(["tampered"]) + "\n", encoding="utf-8")
        (attempt / "ownership.json").write_text(json.dumps({"slices": {"A": ["nope"]}}) + "\n", encoding="utf-8")
        tampered_consumed = (attempt / "consumed_artifacts.json").read_bytes()
        tampered_owns = (attempt / "ownership.json").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            self.reconcile(run)
        self.assertEqual((attempt / "consumed_artifacts.json").read_bytes(), tampered_consumed)
        self.assertEqual((attempt / "ownership.json").read_bytes(), tampered_owns)
        self.assertEqual(extra.read_text(encoding="utf-8"), "keep\n")
        self.assertEqual(intent_count(run), 1)
        before_events = (Path(run) / "events.jsonl").read_bytes()
        with ledger.with_lock(run), self.assertRaises(run_state.PreconditionError):
            ledger.append_events_locked(Path(run), [
                {"type": "notification_sent", "key": "unrelated-a"},
                {"type": "notification_sent", "key": "unrelated-b"},
            ], holder=self.run_holder(run))
        self.assertEqual((Path(run) / "events.jsonl").read_bytes(), before_events)
        self.assertEqual((attempt / "consumed_artifacts.json").read_bytes(), tampered_consumed)
        self.assertEqual((attempt / "ownership.json").read_bytes(), tampered_owns)
        self.assertNotEqual(before_consumed, tampered_consumed)
        self.assertNotEqual(before_owns, tampered_owns)


    def test_owns_without_worktree_or_vcs_can_succeed(self):
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}], "owns": ["src/a/**"]}}}
        run = self.new_run(graph)
        run_state.validate_graph(graph)
        self.start_attempt(run, "A")
        art = self.artifact(run, "A", 1, "summary", "summary", "ok")
        result = self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": [art]})
        self.assertEqual(result["state"]["nodes"]["A"]["state"], "succeeded")
        self.assertFalse((self.attempt_dir(run, "A", 1) / "vcs.json").exists())

    def test_writer_success_requires_exact_identity_vcs_and_clean_ownership(self):
        graph = {"schema_version": 1, "nodes": {"W": {"id": "W", "executor": "herdr-agent", "outputs": [{"name": "vcs", "type": "vcs"}], "worktree": "w", "owns": ["src/w/**"]}}}

        repo = self.repo()
        run = self.new_run(graph, repo)
        self.start_attempt(run, "W")
        art = self.artifact(run, "W", 1, "vcs", "vcs")
        self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["W"]["state"], "succeeded")

        run = self.new_run(graph, repo)
        self.start_attempt(run, "W")
        art = self.artifact(run, "W", 1, "vcs", "vcs")
        identity_path = Path(run) / "artifacts" / "W" / "1" / "dispatch-identity.json"
        identity = json.loads(identity_path.read_text())
        identity["attempt"] = "2"
        identity_path.write_text(json.dumps(identity) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(run_state.PreconditionError, "dispatch-identity"):
            self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})
        run = self.new_run(graph, repo)
        self.start_attempt(run, "W")
        art = self.artifact(run, "W", 1, "vcs", "vcs")
        vcs_path = Path(art["path"])
        vcs = json.loads(vcs_path.read_text())
        vcs["repo"] = str(self.root / "wrong-repo")
        vcs_path.write_text(json.dumps(vcs) + "\n", encoding="utf-8")
        art["hash"] = run_state.sha256_file(vcs_path)
        with self.assertRaisesRegex(run_state.PreconditionError, "vcs repo"):
            self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})


        for field in ("workspace", "routing"):
            with self.subTest(identity_field=field):
                run = self.new_run(graph, repo)
                self.start_attempt(run, "W")
                art = self.artifact(run, "W", 1, "vcs", "vcs")
                identity_path = Path(run) / "artifacts" / "W" / "1" / "dispatch-identity.json"
                identity = json.loads(identity_path.read_text())
                identity[field] = "wrong" if field == "workspace" else self.model(resolved="different/model")
                identity_path.write_text(json.dumps(identity) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(run_state.PreconditionError, "handle|routing"):
                    self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})

        run = self.new_run(graph, repo)
        self.start_attempt(run, "W")
        art = self.artifact(run, "W", 1, "vcs", "vcs")
        identity_path = Path(run) / "artifacts" / "W" / "1" / "dispatch-identity.json"
        identity = json.loads(identity_path.read_text())
        (Path(identity["worktree_path"]) / "outside.txt").write_text("unowned\n", encoding="utf-8")
        identity["clean"] = True
        identity_path.write_text(json.dumps(identity) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(run_state.PreconditionError, "ownership"):
            self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})
        empty_repo = self.repo()
        empty_graph = {"schema_version": 1, "nodes": {"W": {"id": "W", "executor": "herdr-agent", "outputs": [{"name": "vcs", "type": "vcs"}], "worktree": "w", "owns": []}}}
        clean = self.new_run(empty_graph, empty_repo)
        self.start_attempt(clean, "W")
        clean_art = self.artifact(clean, "W", 1, "vcs", "vcs")
        self.append_event(clean, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [clean_art]})
        dirty = self.new_run(empty_graph, empty_repo)
        self.start_attempt(dirty, "W")
        dirty_art = self.artifact(dirty, "W", 1, "vcs", "vcs")
        dirty_ident = json.loads((Path(dirty) / "artifacts" / "W" / "1" / "dispatch-identity.json").read_text())
        (Path(dirty_ident["worktree_path"]) / "unowned.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(run_state.PreconditionError, "ownership"):
            self.append_event(dirty, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [dirty_art]})

    def test_ownership_cli_reads_untracked_files_and_rename_sources(self):
        script = Path(__file__).resolve().parent / "check-ownership.py"

        def run_gate(tree: Path, owns: list[str]) -> tuple[int, dict]:
            manifest = self.root / f"{tree.name}-ownership.json"
            manifest.write_text(
                json.dumps({"repos": [str(tree.resolve())], "slices": {"B": owns}}) + "\n",
                encoding="utf-8",
            )
            proc = subprocess.run(
                [sys.executable, str(script), "--manifest", str(manifest)],
                capture_output=True,
                text=True,
            )
            payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
            return proc.returncode, payload

        repo = self.repo()
        wt = self.make_worktree(repo, node="B", attempt=1, base=self.git(repo, "rev-parse", "HEAD"))
        (wt / "src" / "b.txt").parent.mkdir(parents=True, exist_ok=True)
        (wt / "src" / "b.txt").write_text("b\n", encoding="utf-8")
        code, out = run_gate(wt, ["src/b.txt"])
        self.assertEqual(code, 0)
        self.assertTrue(out["clean"])
        self.assertEqual(out["unowned"], [])
        self.assertEqual(out["by_slice"]["B"], [str(Path(str(wt.resolve()), "src/b.txt"))])

        (wt / "outside.txt").write_text("nope\n", encoding="utf-8")
        code, out = run_gate(wt, ["src/b.txt"])
        self.assertEqual(code, 3)
        self.assertIn(str(Path(str(wt.resolve()), "outside.txt")), out["unowned"])

        repo = self.repo()
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.git(repo, "add", "base.txt")
        self.git(repo, "commit", "-m", "base")
        wt = self.make_worktree(repo, node="B", attempt=2, base=self.git(repo, "rev-parse", "HEAD"))
        (wt / "src").mkdir()
        self.git(wt, "mv", "base.txt", "src/renamed.txt")
        code, out = run_gate(wt, ["src/**"])
        self.assertEqual(code, 3)
        self.assertIn(str(Path(str(wt.resolve()), "base.txt")), out["unowned"])
        self.assertNotIn(str(Path(str(wt.resolve()), "src/renamed.txt")), out["unowned"])
        self.assertIn(str(Path(str(wt.resolve()), "src/renamed.txt")), out["by_slice"]["B"])

        repo = self.repo()
        wt = self.make_worktree(repo, node="B", attempt=3, base=self.git(repo, "rev-parse", "HEAD"))
        weird = wt / "src" / "foo -> bar.txt"
        weird.parent.mkdir(parents=True, exist_ok=True)
        weird.write_text("x\n", encoding="utf-8")
        code, out = run_gate(wt, ["src/foo -> bar.txt"])
        self.assertEqual(code, 0)
        self.assertTrue(out["clean"])
        self.assertEqual(out["unowned"], [])
        self.assertEqual(out["by_slice"]["B"], [str(Path(str(wt.resolve()), "src/foo -> bar.txt"))])

    def test_native_dispatch_window_and_executor_specific_herdr_reattachment(self):
        native = self.new_run({"schema_version": 1, "nodes": {"T": {"id": "T", "executor": "task"}}})
        self.append_event(native, {"type": "dispatch_intent", "node": "T", "attempt": 1})
        self.assertFalse(run_state.native_quiesced(run_state.replay(native)["state"]))
        ambiguous = {"nodes": {"T": {"executor": "task", "state": "ready", "dispatch_intent": {"attempt": 1}, "handle": {"job_id": "stray"}}}}
        self.assertFalse(run_state.native_quiesced(ambiguous))

        def running_with(executor: str, provenance=..., mismatch: str | None = None) -> str:
            node = {"id": "N", "executor": executor}
            if executor == "pane":
                node["handle"] = {"name": "user-pane"}
            run = self.new_run({"schema_version": 1, "nodes": {"N": node}})
            handle = {"workspace": "worker-ws", "pane": "worker-pane", "agent": "worker-agent"}
            self.start_attempt(run, "N", handle=handle)
            tokens = {"run": Path(run).name, "node": "N", "attempt": "1"}
            if provenance is not ...:
                tokens["provenance"] = provenance
            live = {**handle, "tokens": tokens}
            if mismatch in handle:
                live[mismatch] = "wrong"
            elif mismatch:
                live["tokens"][mismatch] = "wrong"
            return self.reconcile(run, live_herdr=[live])["state"]["nodes"]["N"]["state"]

        self.assertEqual(running_with("herdr-agent", "orchestrator"), "running")
        self.assertEqual(running_with("herdr-agent"), "interrupted")
        self.assertEqual(running_with("herdr-agent", "user"), "interrupted")
        for field in ("workspace", "pane", "agent", "run", "node", "attempt"):
            with self.subTest(executor="herdr-agent", mismatch=field):
                self.assertEqual(running_with("herdr-agent", "orchestrator", field), "interrupted")

        self.assertEqual(running_with("pane"), "running")
        self.assertEqual(running_with("pane", "user"), "running")
        for field in ("workspace", "pane", "agent", "run", "node", "attempt"):
            with self.subTest(executor="pane", mismatch=field):
                self.assertEqual(running_with("pane", "user", field), "interrupted")

    def test_herdr_prompt_error_identity_reattaches_or_interrupts(self):
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "model": {"requested_role": "slow", "requested_effort": "high"}}}}
        routing = self.model(requested_role="slow", requested_effort="high", resolved="provider/live", thinking="high", effort="medium")

        def plant() -> tuple[str, dict, dict, dict]:
            run = self.new_run(graph)
            self.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1})
            node = run_state.replay(run)["state"]["nodes"]["H"]
            self.assertEqual(node["state"], "ready")
            self.assertEqual(int(node["dispatch_intent"]["attempt"]), 1)
            run_json = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
            repo = self.repos[run]
            handle = {
                "workspace": "ws-live",
                "pane": "pane-live",
                "agent": "agent-live",
                "repo": str(repo.resolve()),
                "worktree_path": str(repo.resolve()),
                "branch": self.git(repo, "branch", "--show-current"),
                "base": run_json["base_sha"],
            }
            tokens = {"provenance": "orchestrator", "run": run_json["id"], "node": "H", "attempt": "1"}
            identity = {
                **handle,
                "run": run_state.canonical_path(run),
                "run_id": run_json["id"],
                "node": "H",
                "attempt": "1",
                "tokens": tokens,
                "routing": routing,
            }
            (Path(run) / "artifacts" / "H" / "1" / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            live = {"workspace": handle["workspace"], "pane": handle["pane"], "agent": handle["agent"], "tokens": tokens}
            return run, handle, tokens, live

        def dispatched_events(run: str) -> list[dict]:
            return [event for event in run_state.replay(run)["events"] if event["type"] == "dispatched"]

        run, handle, tokens, live = plant()
        expected_handle = {**handle, "tokens": tokens}
        result = self.reconcile(run, live_herdr=[{**live, "status": "done"}])
        node = result["state"]["nodes"]["H"]
        dispatched = dispatched_events(run)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["node"], "H")
        self.assertEqual(dispatched[0]["attempt"], 1)
        self.assertEqual(dispatched[0]["handle"], expected_handle)
        self.assertEqual(dispatched[0]["model"], routing)
        self.assertEqual(node["state"], "running")
        self.assertEqual(node["attempt"], 1)
        self.assertEqual(node["handle"], expected_handle)
        self.assertEqual(node["model"], routing)
        self.assertNotIn("succeeded", {event["type"] for event in run_state.replay(run)["events"]})
        second = self.reconcile(run, live_herdr=[live])
        self.assertEqual(len(dispatched_events(run)), 1)
        self.assertEqual(second["state"]["nodes"]["H"]["state"], "running")
        self.assertEqual(second["state"]["nodes"]["H"]["handle"], expected_handle)
        self.assertEqual(second["state"]["nodes"]["H"]["model"], routing)

        for name, make in (
            ("empty", lambda live, tokens: []),
            ("workspace", lambda live, tokens: [{**live, "workspace": "wrong"}]),
            ("agent", lambda live, tokens: [{**live, "agent": "wrong"}]),
            ("attempt", lambda live, tokens: [{**live, "tokens": {**tokens, "attempt": "2"}}]),
        ):
            with self.subTest(inventory=name):
                run, _handle, tokens, live = plant()
                result = self.reconcile(run, live_herdr=make(live, tokens))
                node = result["state"]["nodes"]["H"]
                self.assertEqual(dispatched_events(run), [])
                self.assertEqual(node["state"], "interrupted")
                self.assertEqual(node["attempt"], 1)
                self.assertEqual(int(node["dispatch_intent"]["attempt"]), 1)

    def test_live_triple_incomplete_tokens_is_ambiguous_not_duplicate(self):
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "model": {"requested_role": "slow", "requested_effort": "high"}}}}
        routing = self.model(requested_role="slow", requested_effort="high", resolved="provider/live", thinking="high", effort="medium")

        def plant() -> tuple[str, dict]:
            run = self.new_run(graph)
            self.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1})
            run_json = self.load_run_json(run)
            repo = self.repos[run]
            handle = {
                "workspace": "ws-live",
                "pane": "pane-live",
                "agent": "agent-live",
                "repo": str(repo.resolve()),
                "worktree_path": str(repo.resolve()),
                "branch": self.git(repo, "branch", "--show-current"),
                "base": run_json["base_sha"],
            }
            tokens = {"provenance": "orchestrator", "run": run_json["id"], "node": "H", "attempt": "1"}
            identity = {
                **handle,
                "run": run_state.canonical_path(run),
                "run_id": run_json["id"],
                "node": "H",
                "attempt": "1",
                "tokens": tokens,
                "routing": routing,
            }
            (self.attempt_dir(run, "H", 1) / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            return run, handle

        def dispatched(run: str) -> list[dict]:
            return [event for event in run_state.replay(run)["events"] if event["type"] == "dispatched"]

        run, handle = plant()
        unknown = self.reconcile(run)
        self.assertEqual(unknown["state"]["nodes"]["H"]["state"], "ready")
        self.assertEqual(dispatched(run), [])

        unstamped = {"workspace": handle["workspace"], "pane": handle["pane"], "agent": handle["agent"]}
        result = self.reconcile(run, live_herdr=[unstamped])
        self.assertEqual(result["state"]["nodes"]["H"]["state"], "ready")
        self.assertEqual(int(result["state"]["nodes"]["H"]["dispatch_intent"]["attempt"]), 1)
        self.assertEqual(dispatched(run), [])

        partial = self.reconcile(run, live_herdr=[{**unstamped, "tokens": {"provenance": "orchestrator"}}])
        self.assertEqual(partial["state"]["nodes"]["H"]["state"], "ready")
        self.assertEqual(dispatched(run), [])

        empty_run, _handle = plant()
        empty = self.reconcile(empty_run, live_herdr=[])
        self.assertEqual(empty["state"]["nodes"]["H"]["state"], "interrupted")
        self.assertEqual(dispatched(empty_run), [])

        running = self.new_run(graph)
        self.start_attempt(running, "H", model=routing)
        handle = run_state.replay(running)["state"]["nodes"]["H"]["handle"]
        live_running = {"workspace": handle["workspace"], "pane": handle["pane"], "agent": handle["agent"]}
        stayed = self.reconcile(running, live_herdr=[live_running])
        self.assertEqual(stayed["state"]["nodes"]["H"]["state"], "running")
        self.assertEqual(len(dispatched(running)), 1)

    def test_missing_agent_id_live_surface_is_ambiguous_not_duplicate(self):
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "model": {"requested_role": "slow", "requested_effort": "high"}}}}
        routing = self.model(requested_role="slow", requested_effort="high", resolved="provider/live", thinking="high", effort="medium")

        def plant() -> tuple[str, dict]:
            run = self.new_run(graph)
            self.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1})
            run_json = self.load_run_json(run)
            repo = self.repos[run]
            identity = {
                "workspace": "ws-live",
                "pane": "pane-live",
                "repo": str(repo.resolve()),
                "worktree_path": str(repo.resolve()),
                "branch": self.git(repo, "branch", "--show-current"),
                "base": run_json["base_sha"],
                "run": run_state.canonical_path(run),
                "run_id": run_json["id"],
                "node": "H",
                "attempt": "1",
                "tokens": {"provenance": "orchestrator", "run": run_json["id"], "node": "H", "attempt": "1"},
                "routing": routing,
            }
            (self.attempt_dir(run, "H", 1) / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            return run, identity

        def dispatched(run: str) -> list[dict]:
            return [event for event in run_state.replay(run)["events"] if event["type"] == "dispatched"]

        run, identity = plant()
        unknown = self.reconcile(run)
        self.assertEqual(unknown["state"]["nodes"]["H"]["state"], "ready")
        self.assertEqual(dispatched(run), [])

        surface = {"workspace": identity["workspace"], "pane": identity["pane"]}
        occupied = self.reconcile(run, live_herdr=[{**surface, "agent": "later-agent"}])
        self.assertEqual(occupied["state"]["nodes"]["H"]["state"], "ready")
        self.assertEqual(int(occupied["state"]["nodes"]["H"]["dispatch_intent"]["attempt"]), 1)
        self.assertEqual(dispatched(run), [])
        self.assertIsNone(run_state.replay(run)["state"]["nodes"]["H"].get("handle"))

        empty_run, _identity = plant()
        empty = self.reconcile(empty_run, live_herdr=[])
        self.assertEqual(empty["state"]["nodes"]["H"]["state"], "interrupted")
        self.assertEqual(dispatched(empty_run), [])

    def test_accept_graph_persists_repo_and_rejects_repo_changes_atomically(self):
        repo = self.repo()
        other_repo = self.repo()
        run = run_state.claim_run(self.root / "docs", "Repo", created="2026-09-04T01:02:04+00:00")["dir"]
        ledger.bind_session_run(run, holder=self.initial_holder)
        first_graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}
        accepted = run_state.accept_graph(run, first_graph, repo=repo, holder=self.initial_holder)
        recorded = run_state.load_run(run)
        self.assertEqual(recorded["repo"], str(repo.resolve()))
        base = recorded["base_sha"]
        promoted = self.commit(repo, "promoted\n")
        self.git(repo, "update-ref", recorded["promotion_ref"], promoted, base)

        revised = {"schema_version": 1, "nodes": {"B": {"id": "B", "executor": "task"}}}
        run_state.accept_graph(run, revised, repo=repo, holder=self.initial_holder)
        self.assertEqual(run_state.load_run(run)["repo"], str(repo.resolve()))
        self.assertEqual(self.git(repo, "rev-parse", accepted["promotion_ref"]), promoted)

        names = ("run.json", "graph.json", "events.jsonl", "state.json", "controller.json", "status.md")
        before = {name: (Path(run) / name).read_bytes() for name in names}
        with self.assertRaisesRegex(run_state.PreconditionError, "repo"):
            run_state.accept_graph(run, first_graph, repo=other_repo, holder=self.initial_holder)
        self.assertEqual(before, {name: (Path(run) / name).read_bytes() for name in names})
        self.assertEqual(self.git(repo, "rev-parse", accepted["promotion_ref"]), promoted)

    def test_retry_model_is_nested_complete_and_requires_replan_to_change(self):
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task", "model": {"requested_role": "slow"}, "retry": {"max_attempts": 2, "on": ["failed"], "side_effect_safe": True}}}}
        run = self.new_run(graph)
        original = self.model(requested_role="slow", requested_effort=None, resolved="provider/a", thinking="high", effort="medium")
        changed = {**original, "resolved": "provider/b"}
        self.start_attempt(run, "A", model=original)
        dispatched = [event for event in run_state.replay(run)["events"] if event["type"] == "dispatched"][-1]
        self.assertEqual(dispatched["model"], original)
        self.assertTrue(all(key not in dispatched for key in run_state.MODEL_KEYS))
        self.append_event(run, {"type": "failed", "node": "A", "attempt": 1})
        self.append_event(run, {"type": "retried", "node": "A", "attempt": 1, "reason": "failed"})
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["A"]["model"], original)
        self.reconcile(run)
        self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 2})
        with self.assertRaisesRegex(run_state.PreconditionError, "model"):
            self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 2, "handle": {}})
        incomplete = dict(original)
        incomplete.pop("effort")
        with self.assertRaisesRegex(run_state.PreconditionError, "missing effort"):
            self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 2, "handle": {}, "model": incomplete})
        with self.assertRaisesRegex(run_state.PreconditionError, "retry"):
            self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 2, "handle": {}, "model": changed})
        self.append_event(run, {"type": "replan", "node": "A", "model": changed})
        result = self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 2, "handle": {}, "model": changed})
        self.assertEqual(result["state"]["nodes"]["A"]["model"], changed)
    def test_first_dispatch_requested_routing_matches_plan_or_replan(self):
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task", "model": {"requested_role": "slow", "requested_effort": "high"}}}}
        run = self.new_run(graph)
        self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        handle = {"spawning_session": "s", "job_id": "A-1"}
        with self.assertRaisesRegex(run_state.PreconditionError, "requested routing"):
            self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": handle, "model": self.model(requested_role="smol")})
        concrete = self.model(requested_role="slow", requested_effort="high", resolved="provider/live", thinking="high", effort="medium")
        result = self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": handle, "model": concrete})
        self.assertEqual(result["state"]["nodes"]["A"]["model"], concrete)

        run = self.new_run(graph)
        replan = self.model(requested_role="smol", requested_effort="low")
        self.append_event(run, {"type": "replan", "node": "A", "model": replan})
        self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        live = self.model(requested_role="smol", requested_effort="low", resolved="provider/other", thinking="low", effort="low")
        result = self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": handle, "model": live})
        self.assertEqual(result["state"]["nodes"]["A"]["model"], live)


    def test_plain_reconcile_atomically_repairs_caches_only(self):
        repo = self.repo()
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task", "owns": ["src/a/**"]}}}
        run = run_state.claim_run(self.root / "docs", "Read only", created="2026-09-04T01:02:06+00:00")["dir"]
        ledger.bind_session_run(run, holder=self.initial_holder)
        run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        self.reconcile(run)
        self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        run_path = Path(run)
        attempt = run_path / "artifacts" / "A" / "1"
        consumed = attempt / "consumed_artifacts.json"
        ownership = attempt / "ownership.json"
        consumed_before = (consumed.read_bytes(), consumed.stat().st_mtime_ns)
        previous = self.run_holder(run)
        successor = {"workspace": "next-ws", "pane": "next-pane", "session": "next-session"}
        self.append_event(run, {"type": "lease_drain_started", **previous}, holder=previous)
        self.append_event(run, {"type": "lease_transfer", **successor}, holder=previous)
        ownership.unlink()
        events_before = (run_path / "events.jsonl").read_bytes()
        (run_path / "state.json").write_text("stale\n", encoding="utf-8")
        (run_path / "controller.json").unlink()
        (run_path / "status.md").write_text("stale\n", encoding="utf-8")

        result = run_state.reconcile(run)

        self.assertEqual(json.loads((run_path / "state.json").read_text(encoding="utf-8")), result["state"])
        self.assertEqual(json.loads((run_path / "controller.json").read_text(encoding="utf-8")), result["controller"])
        self.assertEqual((run_path / "status.md").read_text(encoding="utf-8"), run_state.derive_status_md(result["state"], graph))
        self.assertEqual(result["controller"]["epoch"], 2)
        self.assertEqual({key: result["controller"][key] for key in run_state.HOLDER_KEYS}, successor)
        self.assertEqual((run_path / "events.jsonl").read_bytes(), events_before)
        self.assertEqual((consumed.read_bytes(), consumed.stat().st_mtime_ns), consumed_before)
        self.assertFalse(ownership.exists())

        wrong = {"workspace": "wrong", "pane": "wrong", "session": "wrong"}
        with self.assertRaises(run_state.PreconditionError):
            run_state.reconcile(run, holder=wrong)
        self.assertEqual((run_path / "events.jsonl").read_bytes(), events_before)
        self.assertFalse(ownership.exists())
        self.reconcile(run)
        self.assertTrue(ownership.exists())

    def test_promotion_recovery_checks_holder_before_receipt_writes(self):
        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="new")
        promoted = {"type": "promoted", "node": "D", "attempt": 1, "ref": run_json["promotion_ref"], "expected_old": run_json["base_sha"], "new": new_sha}
        self.append_event(run, promoted)
        receipt = Path(run) / "artifacts" / "D" / "1" / "promoted.json"
        before_events = (Path(run) / "events.jsonl").read_bytes()
        for holder in (None, {"workspace": "other", "pane": "other", "session": "other"}):
            with self.subTest(holder=holder), self.assertRaises(run_state.PreconditionError):
                run_state.recover_promotion(run, repo, holder=holder)
            self.assertFalse(receipt.exists())
            self.assertEqual((Path(run) / "events.jsonl").read_bytes(), before_events)
            self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), new_sha)

    def test_graph_accept_recovers_only_event_committed_staging(self):
        repo = self.repo()
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}
        run = run_state.claim_run(self.root / "docs", "Graph crash", created="2026-09-04T01:02:07+00:00")["dir"]
        ledger.bind_session_run(run, holder=self.initial_holder)
        ref = f"refs/heads/herdr/{Path(run).name}"
        run_path = Path(run)
        before_run = (run_path / "run.json").read_bytes()
        before_graph = (run_path / "graph.json").read_bytes()
        original_append = ledger.append_event_locked
        ledger.append_event_locked = lambda *_args, **_kwargs: (_ for _ in ()).throw(run_state.RunError("before commit"))
        try:
            with self.assertRaisesRegex(run_state.RunError, "before commit"):
                run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        finally:
            ledger.append_event_locked = original_append
        self.assertEqual((run_path / "run.json").read_bytes(), before_run)
        self.assertEqual((run_path / "graph.json").read_bytes(), before_graph)
        self.assertNotEqual(subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", ref]).returncode, 0)
        self.assertFalse(any(event["type"] == "graph_accepted" for event in run_state.replay(run)["events"]))

        original_materialize = ledger._materialize_graph_revision_locked
        ledger._materialize_graph_revision_locked = lambda *_args, **_kwargs: (_ for _ in ()).throw(run_state.RunError("after commit"))
        try:
            with self.assertRaisesRegex(run_state.RunError, "after commit"):
                run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        finally:
            ledger._materialize_graph_revision_locked = original_materialize
        self.assertEqual((run_path / "run.json").read_bytes(), before_run)
        self.assertEqual((run_path / "graph.json").read_bytes(), before_graph)
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["A"]["state"], "planned")
        self.assertNotEqual(subprocess.run(["git", "-C", str(repo), "show-ref", "--verify", "--quiet", ref]).returncode, 0)
        run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        accepted = run_state.load_run(run)
        self.assertEqual(accepted["base_sha"], self.git(repo, "rev-parse", "HEAD"))
        self.assertEqual(self.git(repo, "rev-parse", accepted["promotion_ref"]), accepted["base_sha"])
        self.assertEqual(sum(event["type"] == "graph_accepted" for event in run_state.replay(run)["events"]), 1)

    def plant_integrated_d(self):
        repo = self.repo()
        primary = self.checkout_state(repo)
        run = self.new_run(self.diamond_graph(), repo)
        run_json = self.load_run_json(run)
        self.start_attempt(run, "B")
        self.append_event(run, {"type": "failed", "node": "B", "attempt": 1})
        self.append_event(run, {"type": "retried", "node": "B", "attempt": 1, "reason": "failed"})
        c_sha, c_ident, _ = self.succeed_vcs(run, "C", "src/c/c.txt", "c1\n")
        self.reconcile(run)
        b_sha, b_ident, _ = self.succeed_vcs(run, "B", "src/b/b.txt", "b2\n", attempt=2)
        self.reconcile(run)
        self.start_attempt(run, "D")
        wt = Path(self.identity_for(run, "D", 1)["worktree_path"])
        self.git(wt, "cherry-pick", b_sha)
        self.git(wt, "cherry-pick", c_sha)
        sha = self.git(wt, "rev-parse", "HEAD")
        return run, repo, run_json, primary, sha, wt, b_sha, c_sha, b_ident, c_ident

    def test_diamond_readiness_named_patch_ordered_promote_and_d2_cas(self):
        repo = self.repo()
        primary = self.checkout_state(repo)
        run = self.new_run(self.diamond_graph(), repo)
        nodes = run_state.replay(run)["state"]["nodes"]
        self.assertEqual(nodes["B"]["state"], "ready")
        self.assertEqual(nodes["C"]["state"], "ready")
        self.assertEqual(nodes["D"]["state"], "planned")
        run_json = self.load_run_json(run)
        ref = run_json["promotion_ref"]
        base = run_json["base_sha"]
        self.assertEqual(self.git_ref(repo, ref), base)

        self.start_attempt(run, "B")
        self.append_event(run, {"type": "failed", "node": "B", "attempt": 1})
        self.append_event(run, {"type": "retried", "node": "B", "attempt": 1, "reason": "failed"})
        c_sha, c_ident, _ = self.succeed_vcs(run, "C", "src/c/c.txt", "c1\n")
        self.reconcile(run)
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["D"]["state"], "planned")
        b_sha, b_ident, _ = self.succeed_vcs(run, "B", "src/b/b.txt", "b2\n", attempt=2)
        self.reconcile(run)
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["D"]["state"], "ready")

        self.start_attempt(run, "D")
        d1 = self.identity_for(run, "D", 1)
        d1_wt = Path(d1["worktree_path"])
        self.assertNotEqual(d1_wt.resolve(), Path(repo).resolve())
        self.assertNotEqual(d1_wt.resolve(), Path(b_ident["worktree_path"]).resolve())
        self.assertNotEqual(d1_wt.resolve(), Path(c_ident["worktree_path"]).resolve())
        self.assertEqual(self.git(d1_wt, "rev-parse", "HEAD"), base)
        self.git(d1_wt, "cherry-pick", b_sha)
        self.git(d1_wt, "cherry-pick", c_sha)
        d1_sha = self.git(d1_wt, "rev-parse", "HEAD")
        self.assertEqual(self.git_ok(repo, "merge-base", "--is-ancestor", base, d1_sha)[0], 0)
        consumed = self.snapshot_consumed(run, "D", 1)
        self.assertEqual([item["producer"] for item in consumed], ["B", "C"])
        self.assertEqual([item["output"] for item in consumed], ["patch", "patch"])
        self.write_attempt_receipts(run, "D", 1, d1_sha)
        result = run_state.promote(run, node="D", attempt=1, new_sha=d1_sha, repo=repo, holder=self.run_holder(run))
        self.assertEqual(result["state"]["nodes"]["D"]["state"], "succeeded")
        self.assertEqual(self.git_ref(repo, ref), d1_sha)
        self.assert_frozen(repo, primary)
        d1_dir = self.attempt_dir(run, "D", 1)
        d1_bytes = {path.name: path.read_bytes() for path in d1_dir.iterdir() if path.is_file()}

        self.append_event(run, {"type": "invalidated", "node": "B", "attempt": 2})
        self.append_event(run, {"type": "retried", "node": "B", "attempt": 2, "reason": "invalidated"})
        self.reconcile(run)
        b3_sha, _, _ = self.succeed_vcs(run, "B", "src/b/b.txt", "b3\n", attempt=3)
        self.assertNotEqual(b3_sha, b_sha)
        result = self.reconcile(run)
        self.assertIn(result["state"]["nodes"]["D"]["state"], {"planned", "ready"})
        if result["state"]["nodes"]["D"]["state"] != "ready":
            result = self.reconcile(run)
        self.assertEqual(result["state"]["nodes"]["D"]["state"], "ready")
        self.assertEqual(result["state"]["nodes"]["D"]["attempt"], 2)

        self.start_attempt(run, "D", attempt=2)
        d2 = self.identity_for(run, "D", 2)
        d2_wt = Path(d2["worktree_path"])
        self.assertNotEqual(d2_wt.resolve(), d1_wt.resolve())
        self.assertEqual(self.git(d2_wt, "rev-parse", "HEAD"), base)
        self.git(d2_wt, "cherry-pick", b3_sha)
        self.git(d2_wt, "cherry-pick", c_sha)
        d2_sha = self.git(d2_wt, "rev-parse", "HEAD")
        self.assertNotEqual(d2_sha, d1_sha)
        self.assertEqual(self.git_ok(repo, "merge-base", "--is-ancestor", base, d2_sha)[0], 0)
        self.write_attempt_receipts(
            run, "D", 2, d2_sha,
            consumed=self.receipt_consumed(self.snapshot_consumed(run, "D", 2), attempt_key="producer_attempt"),
        )
        result = run_state.promote(run, node="D", attempt=2, new_sha=d2_sha, repo=repo, holder=self.run_holder(run))
        self.assertEqual(result["state"]["nodes"]["D"]["state"], "succeeded")
        self.assertEqual(self.git_ref(repo, ref), d2_sha)
        self.assert_frozen(repo, primary)
        for name, raw in d1_bytes.items():
            self.assertEqual((d1_dir / name).read_bytes(), raw)
        self.assertEqual(self.git(d1_wt, "rev-parse", "HEAD"), d1_sha)

    def test_promote_refuses_wrong_base_inconsistent_head_and_consumed_sequence(self):
        run, repo, run_json, primary, sha, _wt, _b_sha, _c_sha, _b_ident, _c_ident = self.plant_integrated_d()
        ref = run_json["promotion_ref"]
        base = run_json["base_sha"]
        items = self.receipt_consumed(self.snapshot_consumed(run, "D", 1))
        holder = self.run_holder(run)

        def refuse(**kwargs):
            events = (Path(run) / "events.jsonl").read_bytes()
            self.write_attempt_receipts(run, "D", 1, sha, **kwargs)
            with self.assertRaises(run_state.PreconditionError):
                run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=holder)
            self.assert_frozen(repo, primary, run, ref=ref, ref_value=base, events=events)
            self.assertEqual(run_state.replay(run)["state"]["nodes"]["D"]["state"], "running")

        def refuse_mut(mutator):
            events = (Path(run) / "events.jsonl").read_bytes()
            self.write_attempt_receipts(run, "D", 1, sha)
            path = self.attempt_dir(run, "D", 1) / "integration.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            mutator(data)
            path.write_text(json.dumps(data) + "\n", encoding="utf-8")
            with self.assertRaises(run_state.PreconditionError):
                run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=holder)
            self.assert_frozen(repo, primary, run, ref=ref, ref_value=base, events=events)

        refuse(base_sha="not-the-recorded-base")
        refuse(commit=sha, head=base)
        refuse_mut(lambda data: data.pop("commit", None))
        refuse_mut(lambda data: data.pop("head", None))
        def drop_commit_and_head(data):
            data.pop("commit", None)
            data.pop("head", None)
        refuse_mut(drop_commit_and_head)
        refuse(consumed=list(reversed(items)))
        refuse(consumed=items[:1])
        refuse(consumed=items + [{"producer": "B", "attempt": 2, "output": "patch", "hash": items[0]["hash"]}])
        refuse(consumed=items + items[:1])
        refuse(consumed=[dict(item, output="vcs") for item in items])
        refuse(consumed=[dict(item, alias="nope") for item in items])

    def test_promote_refuses_orphan_and_non_head_commits(self):
        run, repo, run_json, primary, sha, wt, _b_sha, _c_sha, _b_ident, _c_ident = self.plant_integrated_d()
        ref = run_json["promotion_ref"]
        base = run_json["base_sha"]
        holder = self.run_holder(run)
        events = (Path(run) / "events.jsonl").read_bytes()
        orphan = self.orphan_commit(repo)
        self.assertNotEqual(self.git_ok(repo, "merge-base", "--is-ancestor", base, orphan)[0], 0)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), sha)
        self.write_attempt_receipts(run, "D", 1, orphan)
        with self.assertRaises(run_state.PreconditionError):
            run_state.promote(run, node="D", attempt=1, new_sha=orphan, repo=repo, holder=holder)
        self.assert_frozen(repo, primary, run, ref=ref, ref_value=base, events=events)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), sha)

        extra = self.commit_file(wt, "src/d/extra.txt", "not-head\n")
        events = (Path(run) / "events.jsonl").read_bytes()
        self.write_attempt_receipts(run, "D", 1, sha)
        with self.assertRaises(run_state.PreconditionError):
            run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=holder)
        self.assert_frozen(repo, primary, run, ref=ref, ref_value=base, events=events)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), extra)
        self.assertNotEqual(extra, sha)

    def test_accept_promote_recover_refuse_checked_out_promotion_ref(self):
        graph = self.promotion_graph()

        repo = self.repo()
        run = self.bound_run(repo, "AcceptPrimary")
        branch = f"herdr/{self.load_run_json(run)['id']}"
        self.git(repo, "checkout", "-b", branch)
        frozen = self.checkout_state(repo)
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        self.assert_frozen(repo, frozen)
        self.assertEqual(self.git_ref(repo, f"refs/heads/{branch}"), frozen["head"])
        self.assertFalse(any(event["type"] == "graph_accepted" for event in run_state.replay(run)["events"]))

        repo = self.repo()
        run = self.bound_run(repo, "AcceptOther")
        branch = f"herdr/{self.load_run_json(run)['id']}"
        frozen = self.checkout_state(repo)
        other = self.add_checkout_worktree(repo, branch, create_branch=True)
        other_head = self.git(other, "rev-parse", "HEAD")
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        self.assert_frozen(repo, frozen)
        self.assertEqual(self.git(other, "rev-parse", "HEAD"), other_head)
        self.assertEqual(self.git_ref(repo, f"refs/heads/{branch}"), other_head)
        self.assertFalse(any(event["type"] == "graph_accepted" for event in run_state.replay(run)["events"]))

        repo = self.repo()
        run = self.new_run(graph, repo)
        run_json = self.load_run_json(run)
        ref = run_json["promotion_ref"]
        branch = ref.removeprefix("refs/heads/")
        self.start_attempt(run, "D")
        sha = self.commit_file(Path(self.identity_for(run, "D", 1)["worktree_path"]), "src/d/result.txt", "d\n")
        self.write_attempt_receipts(run, "D", 1, sha)
        self.git(repo, "checkout", branch)
        checked = self.checkout_state(repo)
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=self.run_holder(run))
        self.assert_frozen(repo, checked, run, ref=ref, ref_value=run_json["base_sha"], events=events)
        self.assertEqual(checked["head"], run_json["base_sha"])
        self.assertNotEqual(sha, run_json["base_sha"])

        repo = self.repo()
        primary = self.checkout_state(repo)
        run = self.new_run(graph, repo)
        run_json = self.load_run_json(run)
        ref = run_json["promotion_ref"]
        branch = ref.removeprefix("refs/heads/")
        self.start_attempt(run, "D")
        sha = self.commit_file(Path(self.identity_for(run, "D", 1)["worktree_path"]), "src/d/result.txt", "other-wt\n")
        self.write_attempt_receipts(run, "D", 1, sha)
        other = self.add_checkout_worktree(repo, branch, create_branch=False)
        other_state = self.checkout_state(other)
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=self.run_holder(run))
        self.assert_frozen(repo, primary, run, ref=ref, ref_value=run_json["base_sha"], events=events)
        self.assertEqual(self.checkout_state(other), other_state)

        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="old")
        branch = run_json["promotion_ref"].removeprefix("refs/heads/")
        self.git(repo, "checkout", branch)
        checked = self.checkout_state(repo)
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            run_state.recover_promotion(run, repo, holder=self.run_holder(run))
        self.assert_frozen(repo, checked, run, ref=run_json["promotion_ref"], ref_value=run_json["base_sha"], events=events)
        self.assertEqual(checked["head"], run_json["base_sha"])
        self.assertFalse((self.attempt_dir(run, "D", 1) / "promoted.json").exists())

        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="old")
        primary = self.checkout_state(repo)
        branch = run_json["promotion_ref"].removeprefix("refs/heads/")
        other = self.add_checkout_worktree(repo, branch, create_branch=False)
        other_state = self.checkout_state(other)
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            run_state.recover_promotion(run, repo, holder=self.run_holder(run))
        self.assert_frozen(repo, primary, run, ref=run_json["promotion_ref"], ref_value=run_json["base_sha"], events=events)
        self.assertEqual(self.checkout_state(other), other_state)
        self.assertFalse((self.attempt_dir(run, "D", 1) / "promoted.json").exists())

        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="new")
        branch = run_json["promotion_ref"].removeprefix("refs/heads/")
        self.git(repo, "checkout", branch)
        checked = self.checkout_state(repo)
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            run_state.recover_promotion(run, repo, holder=self.run_holder(run))
        self.assert_frozen(repo, checked, run, ref=run_json["promotion_ref"], ref_value=new_sha, events=events)
        self.assertEqual(checked["head"], new_sha)
        self.assertFalse((self.attempt_dir(run, "D", 1) / "promoted.json").exists())
        self.assertNotIn("promoted", [event["type"] for event in run_state.replay(run)["events"]])

        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="new")
        primary = self.checkout_state(repo)
        branch = run_json["promotion_ref"].removeprefix("refs/heads/")
        other = self.add_checkout_worktree(repo, branch, create_branch=False)
        self.assertEqual(self.git(other, "rev-parse", "HEAD"), new_sha)
        other_state = self.checkout_state(other)
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            run_state.recover_promotion(run, repo, holder=self.run_holder(run))
        self.assert_frozen(repo, primary, run, ref=run_json["promotion_ref"], ref_value=new_sha, events=events)
        self.assertEqual(self.checkout_state(other), other_state)
        self.assertFalse((self.attempt_dir(run, "D", 1) / "promoted.json").exists())
        self.assertNotIn("promoted", [event["type"] for event in run_state.replay(run)["events"]])
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["D"]["state"], "running")

    def test_corrupt_snapshot_refuses_new_event_and_unauthorized_repair(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}], "owns": ["src/a/**"]},
                "B": {"id": "B", "executor": "task"},
            },
        }
        run = self.new_run(graph)
        self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        attempt = self.attempt_dir(run, "A", 1)
        consumed = attempt / "consumed_artifacts.json"
        ownership = attempt / "ownership.json"
        consumed.write_text(json.dumps(["tampered"]) + "\n", encoding="utf-8")
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": {"spawning_session": "s", "job_id": "A-1"}, "model": self.model()})
        self.assertEqual((Path(run) / "events.jsonl").read_bytes(), events)
        self.assertEqual(json.loads(consumed.read_text(encoding="utf-8")), ["tampered"])

        run = self.new_run(graph)
        self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        attempt = self.attempt_dir(run, "A", 1)
        ownership = attempt / "ownership.json"
        ownership.unlink()
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(
                run,
                {"type": "dispatched", "node": "A", "attempt": 1, "handle": {"spawning_session": "s", "job_id": "A-1"}, "model": self.model()},
                holder={"workspace": "other", "pane": "other", "session": "other"},
            )
        self.assertEqual((Path(run) / "events.jsonl").read_bytes(), events)
        self.assertFalse(ownership.exists())

    def test_promote_refuses_live_consumed_digest_mismatch(self):
        run, repo, run_json, primary, sha, wt, _b_sha, _c_sha, b_ident, c_ident = self.plant_integrated_d()
        ref = run_json["promotion_ref"]
        base = run_json["base_sha"]
        self.write_attempt_receipts(run, "D", 1, sha)
        consumed = self.snapshot_consumed(run, "D", 1)
        path = Path(consumed[0]["path"])
        path.write_bytes(path.read_bytes() + b"\n")
        events = (Path(run) / "events.jsonl").read_bytes()
        b_head = self.git(Path(b_ident["worktree_path"]), "rev-parse", "HEAD")
        c_head = self.git(Path(c_ident["worktree_path"]), "rev-parse", "HEAD")
        d_head = self.git(wt, "rev-parse", "HEAD")
        with self.assertRaises(run_state.PreconditionError):
            run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=self.run_holder(run))
        self.assert_frozen(repo, primary, run, ref=ref, ref_value=base, events=events)
        self.assertNotIn("promotion_intent", [event["type"] for event in run_state.replay(run)["events"]])
        self.assertFalse((self.attempt_dir(run, "D", 1) / "promoted.json").exists())
        self.assertEqual(self.git(Path(b_ident["worktree_path"]), "rev-parse", "HEAD"), b_head)
        self.assertEqual(self.git(Path(c_ident["worktree_path"]), "rev-parse", "HEAD"), c_head)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), d_head)

    def test_promote_refuses_tampered_consumed_snapshot_before_intent(self):
        run, repo, run_json, primary, sha, wt, _b_sha, _c_sha, b_ident, c_ident = self.plant_integrated_d()
        ref = run_json["promotion_ref"]
        base = run_json["base_sha"]
        snapshot = self.attempt_dir(run, "D", 1) / "consumed_artifacts.json"
        consumed = self.snapshot_consumed(run, "D", 1)
        consumed[0] = dict(consumed[0], hash="sha256:" + ("0" * 64))
        snapshot.write_text(json.dumps(consumed) + "\n", encoding="utf-8")
        self.write_attempt_receipts(run, "D", 1, sha)
        events = (Path(run) / "events.jsonl").read_bytes()
        b_head = self.git(Path(b_ident["worktree_path"]), "rev-parse", "HEAD")
        c_head = self.git(Path(c_ident["worktree_path"]), "rev-parse", "HEAD")
        d_head = self.git(wt, "rev-parse", "HEAD")
        with self.assertRaises(run_state.PreconditionError):
            run_state.promote(run, node="D", attempt=1, new_sha=sha, repo=repo, holder=self.run_holder(run))
        self.assert_frozen(repo, primary, run, ref=ref, ref_value=base, events=events)
        self.assertNotIn("promotion_intent", [event["type"] for event in run_state.replay(run)["events"]])
        self.assertFalse((self.attempt_dir(run, "D", 1) / "promoted.json").exists())
        run_state.recover_promotion(run, repo, holder=self.run_holder(run))
        self.assert_frozen(repo, primary, run, ref=ref, ref_value=base, events=events)
        self.assertEqual(self.git(Path(b_ident["worktree_path"]), "rev-parse", "HEAD"), b_head)
        self.assertEqual(self.git(Path(c_ident["worktree_path"]), "rev-parse", "HEAD"), c_head)
        self.assertEqual(self.git(wt, "rev-parse", "HEAD"), d_head)

    def test_recover_promotion_refuses_corrupt_snapshot_before_and_after_cas(self):
        def tamper(run: str) -> None:
            path = self.attempt_dir(run, "D", 1) / "consumed_artifacts.json"
            path.write_text(json.dumps(["tampered"]) + "\n", encoding="utf-8")

        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="base")
        primary = self.checkout_state(repo)
        tamper(run)
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            run_state.recover_promotion(run, repo, holder=self.run_holder(run))
        self.assert_frozen(repo, primary, run, ref=run_json["promotion_ref"], ref_value=run_json["base_sha"], events=events)
        self.assertFalse((self.attempt_dir(run, "D", 1) / "promoted.json").exists())
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["D"]["state"], "running")

        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="new")
        primary = self.checkout_state(repo)
        tamper(run)
        events = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaises(run_state.PreconditionError):
            run_state.recover_promotion(run, repo, holder=self.run_holder(run))
        self.assert_frozen(repo, primary, run, ref=run_json["promotion_ref"], ref_value=new_sha, events=events)
        self.assertFalse((self.attempt_dir(run, "D", 1) / "promoted.json").exists())
        self.assertNotIn("promoted", [event["type"] for event in run_state.replay(run)["events"]])
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["D"]["state"], "running")

        run, repo, new_sha, run_json = self.pending_promotion_run(ref_at="new")
        result = self.reconcile(run, repo=repo)
        self.assertEqual(result["state"]["nodes"]["D"]["state"], "succeeded")
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), new_sha)

    def test_reconcile_retries_or_blocks_persisted_invalidated_checkpoint(self):
        def plant(*, max_attempts: int) -> str:
            graph = {
                "schema_version": 1,
                "nodes": {
                    "A": {
                        "id": "A",
                        "executor": "task",
                        "outputs": [{"name": "summary", "type": "summary"}],
                        "retry": {"max_attempts": max_attempts, "on": ["invalidated"]},
                    }
                },
            }
            run = self.new_run(graph)
            self.start_attempt(run, "A")
            art = self.artifact(run, "A", 1, "summary", "summary", "old")
            self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": [art]})
            self.append_event(run, {"type": "invalidated", "node": "A", "attempt": 1})
            return run

        run = plant(max_attempts=2)
        attempt_dir = self.attempt_dir(run, "A", 1)
        before = {path.name: path.read_bytes() for path in attempt_dir.iterdir() if path.is_file()}
        result = self.reconcile(run)
        self.assertIn(result["state"]["nodes"]["A"]["state"], {"planned", "ready"})
        self.assertEqual(result["state"]["nodes"]["A"]["attempt"], 2)
        types = [event["type"] for event in run_state.replay(run)["events"] if event.get("node") == "A"]
        self.assertEqual(types.count("invalidated"), 1)
        self.assertEqual(types.count("retried"), 1)
        again = self.reconcile(run)
        self.assertEqual(again["state"]["nodes"]["A"]["state"], result["state"]["nodes"]["A"]["state"])
        self.assertEqual(again["state"]["nodes"]["A"]["attempt"], 2)
        types = [event["type"] for event in run_state.replay(run)["events"] if event.get("node") == "A"]
        self.assertEqual(types.count("retried"), 1)
        for name, raw in before.items():
            self.assertEqual((attempt_dir / name).read_bytes(), raw)

        run = plant(max_attempts=1)
        attempt_dir = self.attempt_dir(run, "A", 1)
        before = {path.name: path.read_bytes() for path in attempt_dir.iterdir() if path.is_file()}
        result = self.reconcile(run)
        self.assertEqual(result["state"]["nodes"]["A"]["state"], "blocked")
        self.assertEqual(result["state"]["nodes"]["A"]["attempt"], 1)
        self.assertEqual(result["state"]["nodes"]["A"]["reason"], "invalidated digest")
        types = [event["type"] for event in run_state.replay(run)["events"] if event.get("node") == "A"]
        self.assertEqual(types.count("invalidated"), 1)
        self.assertEqual(types.count("blocked"), 1)
        self.assertEqual(types.count("retried"), 0)
        again = self.reconcile(run)
        self.assertEqual(again["state"]["nodes"]["A"]["state"], "blocked")
        self.assertEqual(again["state"]["nodes"]["A"]["attempt"], 1)
        types = [event["type"] for event in run_state.replay(run)["events"] if event.get("node") == "A"]
        self.assertEqual(types.count("blocked"), 1)
        for name, raw in before.items():
            self.assertEqual((attempt_dir / name).read_bytes(), raw)

    def test_managed_name_dispatch_and_continuation_replay_matches_exact_identity(self):
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "model": {"requested_role": "slow", "requested_effort": "high"}}}}
        routing = self.model(requested_role="slow", requested_effort="high", resolved="provider/live", thinking="high", effort="medium")
        worker_id = run_state.new_worker_id()

        def plant() -> tuple[str, dict, dict, dict]:
            run = self.new_run(graph)
            self.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1})
            run_json = self.load_run_json(run)
            repo = self.repos[run]
            handle = {
                "identity_kind": "managed-name",
                "worker_id": worker_id,
                "workspace": "ws-live",
                "pane": "pane-live",
                "repo": str(repo.resolve()),
                "worktree_path": str(repo.resolve()),
                "branch": self.git(repo, "branch", "--show-current"),
                "base": run_json["base_sha"],
            }
            tokens = {"provenance": "orchestrator", "run": run_json["id"], "node": "H", "attempt": "1"}
            identity = {**handle, "run": run_state.canonical_path(run), "run_id": run_json["id"], "node": "H", "attempt": "1", "tokens": tokens, "routing": routing}
            (self.attempt_dir(run, "H", 1) / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            live = {"identity_kind": "managed-name", "worker_id": worker_id, "workspace": handle["workspace"], "pane": handle["pane"], "tokens": tokens}
            return run, handle, tokens, live

        def dispatched_events(run: str) -> list[dict]:
            return [event for event in run_state.replay(run)["events"] if event["type"] == "dispatched"]

        run, handle, tokens, live = plant()
        expected_handle = {**handle, "tokens": tokens}
        result = self.reconcile(run, live_herdr=[live])
        node = result["state"]["nodes"]["H"]
        dispatched = dispatched_events(run)
        self.assertEqual(len(dispatched), 1)
        self.assertNotIn("agent", dispatched[0]["handle"])
        self.assertEqual(dispatched[0]["handle"]["worker_id"], worker_id)
        self.assertEqual(dispatched[0]["handle"], expected_handle)
        self.assertEqual(node["state"], "running")
        self.assertEqual(node["handle"], expected_handle)

        # a live record naming a different worker_id under the same surface never matches - no duplicate dispatch
        other_run, _handle2, _tokens2, live2 = plant()
        wrong = self.reconcile(other_run, live_herdr=[{**live2, "worker_id": run_state.new_worker_id()}])
        self.assertEqual(wrong["state"]["nodes"]["H"]["state"], "interrupted")
        self.assertEqual(dispatched_events(other_run), [])

        # legacy runtime-id records are unaffected by the managed-name path - still match on plain agent identity
        legacy_run = self.new_run(graph)
        self.start_attempt(legacy_run, "H", model=routing)
        legacy_handle = run_state.replay(legacy_run)["state"]["nodes"]["H"]["handle"]
        legacy_run_json = self.load_run_json(legacy_run)
        legacy_live = {
            "workspace": legacy_handle["workspace"], "pane": legacy_handle["pane"], "agent": legacy_handle["agent"],
            "tokens": {"provenance": "orchestrator", "run": legacy_run_json["id"], "node": "H", "attempt": "1"},
        }
        self.assertEqual(self.reconcile(legacy_run, live_herdr=[legacy_live])["state"]["nodes"]["H"]["state"], "running")
        self.assertEqual(len(dispatched_events(legacy_run)), 1)

        # blocked -> attempt_started continuation carries the managed worker_id through, never agent
        blocked_run, _handle3, _tokens3, blocked_live = plant()
        self.reconcile(blocked_run, live_herdr=[blocked_live])
        self.append_event(blocked_run, {"type": "blocked", "node": "H", "attempt": 1, "reason": "approval requested"})
        resumed = self.append_event(blocked_run, {
            "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
            "handle": run_state.replay(blocked_run)["state"]["nodes"]["H"]["handle"],
            "model": routing, "live_identity": {**blocked_live, "routing": routing},
        })
        self.assertEqual(resumed["state"]["nodes"]["H"]["state"], "running")
        self.assertNotIn("agent", resumed["state"]["nodes"]["H"]["handle"])
        self.assertEqual(resumed["state"]["nodes"]["H"]["handle"]["worker_id"], worker_id)

    def test_pane_executor_never_adopts_managed_name_identity(self):
        worker_id = run_state.new_worker_id()
        graph = {"schema_version": 1, "nodes": {"P": {"id": "P", "executor": "pane", "handle": {"name": "user-pane"}}}}
        run = self.new_run(graph)
        self.append_event(run, {"type": "dispatch_intent", "node": "P", "attempt": 1})
        run_json = self.load_run_json(run)
        tokens = {"provenance": "orchestrator", "run": run_json["id"], "node": "P", "attempt": "1"}
        managed_handle = {"workspace": "worker-ws", "pane": "worker-pane", "identity_kind": "managed-name", "worker_id": worker_id}
        identity = {**managed_handle, "run": run_state.canonical_path(run), "run_id": run_json["id"], "node": "P", "attempt": "1", "tokens": tokens, "routing": self.model(resolved="provider/x")}
        (self.attempt_dir(run, "P", 1) / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
        live = {**managed_handle, "tokens": tokens}

        result = self.reconcile(run, live_herdr=[live])
        self.assertNotEqual(result["state"]["nodes"]["P"]["state"], "running")
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["P"].get("handle"), {"name": "user-pane"})
        self.assertEqual([event for event in run_state.replay(run)["events"] if event["type"] == "dispatched"], [])

        # the parser is the actual enforcement point: pane rejects managed-name outright, independent of reconcile
        self.assertIsNone(run_state.parse_herdr_identity(managed_handle, "pane"))
        self.assertIsNone(run_state.herdr_command_target(managed_handle, "pane"))
        self.assertIsNotNone(run_state.parse_herdr_identity(managed_handle, "herdr-agent", require_context=False))




    def test_integrator_success_is_promote_only(self):
        repo = self.repo()
        run = self.new_run(self.promotion_graph(), repo)
        self.start_attempt(run, "D")
        ident = self.identity_for(run, "D", 1)
        new_sha = self.commit_file(Path(ident["worktree_path"]), "src/d/result.txt", "two\n")
        self.write_attempt_receipts(run, "D", 1, new_sha)
        attempt_dir = self.attempt_dir(run, "D", 1)
        arts = [
            {"name": "integration", "type": "integration", "path": str(attempt_dir / "integration.json"), "hash": run_state.sha256_file(attempt_dir / "integration.json")},
            {"name": "vcs", "type": "vcs", "path": str(attempt_dir / "vcs.json"), "hash": run_state.sha256_file(attempt_dir / "vcs.json")},
        ]
        with self.assertRaisesRegex(run_state.PreconditionError, "integration success requires promoted event"):
            self.append_event(run, {"type": "succeeded", "node": "D", "attempt": 1, "artifacts": arts})
        result = run_state.promote(run, node="D", attempt=1, new_sha=new_sha, repo=repo, holder=self.run_holder(run))
        self.assertEqual(result["state"]["nodes"]["D"]["state"], "succeeded")
        before = (Path(run) / "events.jsonl").read_bytes()
        with self.assertRaisesRegex(run_state.PreconditionError, "already succeeded by promotion"):
            self.append_event(run, {"type": "succeeded", "node": "D", "attempt": 1, "artifacts": arts})
        self.assertEqual((Path(run) / "events.jsonl").read_bytes(), before)

    def test_vcs_none_form_gates(self):
        graph = {"schema_version": 1, "nodes": {"W": {"id": "W", "executor": "herdr-agent", "outputs": [{"name": "vcs", "type": "vcs"}], "worktree": "w", "owns": ["src/w/**"]}}}

        def plant_writer():
            repo = self.repo()
            run = self.new_run(graph, repo)
            self.start_attempt(run, "W")
            return run, repo

        def plant_on_promoted():
            repo = self.repo()
            run = self.new_run(graph, repo)
            run_json = self.load_run_json(run)
            promoted = self.commit_file(self.make_worktree(repo, node="p", attempt=1, base=run_json["base_sha"]), "src/p/p.txt", "p\n")
            self.git(repo, "update-ref", run_json["promotion_ref"], promoted)
            wt = self.make_worktree(repo, node="W", attempt=1, base=promoted)
            self.start_attempt(run, "W", handle={"worktree_path": str(wt.resolve()), "branch": self.git(wt, "branch", "--show-current")})
            return run, wt


        with self.subTest(case="clean-base"):
            run, _repo = plant_writer()
            art = self.vcs_artifact(run, "W", 1, {"form": "none"})
            result = self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})
            self.assertEqual(result["state"]["nodes"]["W"]["state"], "succeeded")

        with self.subTest(case="dirty"):
            run, _repo = plant_writer()
            ident = self.identity_for(run, "W", 1)
            (Path(ident["worktree_path"]) / "dirty.txt").write_text("x\n", encoding="utf-8")
            art = self.vcs_artifact(run, "W", 1, {"form": "none"})
            with self.assertRaisesRegex(run_state.PreconditionError, "clean worktree"):
                self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})

        with self.subTest(case="ahead"):
            run, _repo = plant_writer()
            ident = self.identity_for(run, "W", 1)
            self.commit_file(Path(ident["worktree_path"]), "src/w/x.txt", "x\n")
            art = self.vcs_artifact(run, "W", 1, {"form": "none"})
            with self.assertRaisesRegex(run_state.PreconditionError, "HEAD == base_sha"):
                self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})

        with self.subTest(case="extra-key"):
            run, _repo = plant_writer()
            art = self.vcs_artifact(run, "W", 1, {"form": "none", "sha": "dead"})
            with self.assertRaisesRegex(run_state.PreconditionError, "only form"):
                self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})

        with self.subTest(case="integrator"):
            repo = self.repo()
            run = self.new_run(self.promotion_graph(), repo)
            run_json = self.load_run_json(run)
            ref = run_json["promotion_ref"]
            before = self.git_ref(repo, ref)
            self.start_attempt(run, "D")
            self.write_attempt_receipts(run, "D", 1, run_json["base_sha"])
            self.vcs_artifact(run, "D", 1, {"form": "none"})
            with self.assertRaisesRegex(run_state.PreconditionError, "promotion requires a commit vcs result"):
                run_state.promote(run, node="D", attempt=1, new_sha=run_json["base_sha"], repo=repo, holder=self.run_holder(run))
            self.assertEqual(self.git_ref(repo, ref), before)

        with self.subTest(case="promoted"):
            run, _wt = plant_on_promoted()
            art = self.vcs_artifact(run, "W", 1, {"form": "none"})
            result = self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})
            self.assertEqual(result["state"]["nodes"]["W"]["state"], "succeeded")

        with self.subTest(case="beyond-promoted"):
            run, wt = plant_on_promoted()
            self.commit_file(wt, "src/w/x.txt", "x\n")
            art = self.vcs_artifact(run, "W", 1, {"form": "none"})
            with self.assertRaisesRegex(run_state.PreconditionError, "already-promoted sha"):
                self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})

        with self.subTest(case="behind-base"):
            repo = self.repo()
            self.commit(repo, "two\n")
            run = self.new_run(graph, repo)
            run_json = self.load_run_json(run)
            self.assertEqual(self.git_ref(repo, run_json["promotion_ref"]), run_json["base_sha"])
            self.start_attempt(run, "W")
            wt = Path(self.identity_for(run, "W", 1)["worktree_path"])
            self.git(wt, "reset", "--hard", f"{run_json['base_sha']}^")
            art = self.vcs_artifact(run, "W", 1, {"form": "none"})
            before = (Path(run) / "events.jsonl").read_bytes()
            with self.assertRaisesRegex(run_state.PreconditionError, "descend from recorded base"):
                self.append_event(run, {"type": "succeeded", "node": "W", "attempt": 1, "artifacts": [art]})
            self.assertEqual((Path(run) / "events.jsonl").read_bytes(), before)


    def test_wave_writer_is_gated_from_its_promoted_branch_point(self):
        w1 = {"id": "W1", "executor": "herdr-agent", "outputs": [{"name": "vcs", "type": "vcs"}], "worktree": "w1", "owns": ["src/a/**"]}
        d = {
            "id": "D",
            "executor": "herdr-agent",
            "outputs": [{"name": "integration", "type": "integration"}, {"name": "vcs", "type": "vcs"}],
            "worktree": "d",
            "owns": ["src/d/**"],
            "depends_on": ["W1"],
            "inputs": [{"alias": "w1", "from": "W1", "output": "vcs", "required": True}],
        }
        w2 = {"id": "W2", "executor": "herdr-agent", "outputs": [{"name": "vcs", "type": "vcs"}], "worktree": "w2", "owns": ["src/b/**"], "depends_on": ["D"]}

        def plant(*, at_promoted: bool):
            repo = self.repo()
            run = self.new_run({"schema_version": 1, "nodes": {"W1": w1, "D": d}}, repo)
            w1_sha, _, _ = self.succeed_vcs(run, "W1", "src/a/w1.txt", "w1\n", output="vcs")
            self.reconcile(run)
            self.start_attempt(run, "D")
            d_wt = Path(self.identity_for(run, "D", 1)["worktree_path"])
            self.git(d_wt, "cherry-pick", w1_sha)
            promoted = self.commit_file(d_wt, "src/d/result.txt", "d\n")
            self.write_attempt_receipts(run, "D", 1, promoted)
            run_state.promote(run, node="D", attempt=1, new_sha=promoted, repo=repo, holder=self.run_holder(run))
            run_state.accept_graph(run, {"schema_version": 1, "nodes": {"W1": w1, "D": d, "W2": w2}}, repo=repo, holder=self.run_holder(run))
            self.reconcile(run)
            base = self.load_run_json(run)["base_sha"]
            wt = self.make_worktree(repo, node="W2", attempt=1, base=promoted if at_promoted else base)
            self.start_attempt(run, "W2", handle={"worktree_path": str(wt.resolve()), "branch": self.git(wt, "branch", "--show-current")})
            return run, wt, base, promoted

        with self.subTest(case="owned-beyond-promoted"):
            run, wt, base, _promoted = plant(at_promoted=True)
            self.commit_file(wt, "src/b/x.txt", "x\n")
            names = self.git(wt, "diff", "--name-only", f"{base}..HEAD").splitlines()
            self.assertIn("src/a/w1.txt", names)
            self.assertIn("src/d/result.txt", names)
            self.assertIn("src/b/x.txt", names)
            art = self.artifact(run, "W2", 1, "vcs", "vcs")
            result = self.append_event(run, {"type": "succeeded", "node": "W2", "attempt": 1, "artifacts": [art]})
            self.assertEqual(result["state"]["nodes"]["W2"]["state"], "succeeded")

        with self.subTest(case="unowned-beyond-promoted"):
            run, wt, _base, _promoted = plant(at_promoted=True)
            self.commit_file(wt, "src/b/x.txt", "x\n")
            self.commit_file(wt, "src/a/y.txt", "y\n")
            art = self.artifact(run, "W2", 1, "vcs", "vcs")
            with self.assertRaisesRegex(run_state.PreconditionError, r"ownership check failed: .*src/a/y\.txt$") as ctx:
                self.append_event(run, {"type": "succeeded", "node": "W2", "attempt": 1, "artifacts": [art]})
            msg = str(ctx.exception)
            self.assertNotIn("src/b/", msg)
            self.assertNotIn("w1.txt", msg)

        with self.subTest(case="classifies-from-base"):
            run, wt, base, promoted = plant(at_promoted=False)
            self.assertEqual(self.git(wt, "rev-parse", "HEAD"), base)
            self.assertNotEqual(base, promoted)
            self.commit_file(wt, "src/b/x.txt", "x\n")
            art = self.artifact(run, "W2", 1, "vcs", "vcs")
            result = self.append_event(run, {"type": "succeeded", "node": "W2", "attempt": 1, "artifacts": [art]})
            self.assertEqual(result["state"]["nodes"]["W2"]["state"], "succeeded")

    def test_integrator_wildcard_owns_derives_from_dependencies(self):
        writer_a = {"id": "A", "executor": "task", "owns": ["src/a/**"]}
        writer_b = {"id": "B", "executor": "task", "owns": ["src/b/**"]}
        integrator = {
            "id": "D",
            "executor": "task",
            "outputs": [
                {"name": "integration", "type": "integration"},
                {"name": "vcs", "type": "vcs"},
                {"name": "summary", "type": "summary"},
            ],
            "depends_on": ["A", "B"],
            "owns": ["**"],
        }
        with self.subTest(case="audit-docs"):
            run_state.validate_graph({
                "schema_version": 1,
                "nodes": {
                    "A": writer_a,
                    "B": writer_b,
                    "D": integrator,
                    "Q": {"id": "Q", "executor": "task", "owns": ["docs/**"]},
                },
            })
        with self.subTest(case="audit-src-a"):
            with self.assertRaisesRegex(run_state.PreconditionError, "owns overlap"):
                run_state.validate_graph({
                    "schema_version": 1,
                    "nodes": {
                        "A": writer_a,
                        "B": writer_b,
                        "D": integrator,
                        "Q": {"id": "Q", "executor": "task", "owns": ["src/a/**"]},
                    },
                })
        with self.subTest(case="non-integrator-star"):
            with self.assertRaisesRegex(run_state.PreconditionError, "owns overlap"):
                run_state.validate_graph({
                    "schema_version": 1,
                    "nodes": {
                        "A": writer_a,
                        "B": writer_b,
                        "D": {"id": "D", "executor": "task", "depends_on": ["A", "B"], "owns": ["**"]},
                        "Q": {"id": "Q", "executor": "task", "owns": ["docs/**"]},
                    },
                })
        with self.subTest(case="transitive"):
            with self.assertRaisesRegex(run_state.PreconditionError, "owns overlap"):
                run_state.validate_graph({
                    "schema_version": 1,
                    "nodes": {
                        "A": {"id": "A", "executor": "task", "owns": ["src/a/**"]},
                        "C": {"id": "C", "executor": "task", "depends_on": ["A"]},
                        "D": {
                            "id": "D",
                            "executor": "task",
                            "outputs": [
                                {"name": "integration", "type": "integration"},
                                {"name": "vcs", "type": "vcs"},
                                {"name": "summary", "type": "summary"},
                            ],
                            "depends_on": ["C"],
                            "owns": ["**"],
                        },
                        "Q": {"id": "Q", "executor": "task", "owns": ["src/a/**"]},
                    },
                })

    def test_state_read_path_matches_replay(self):
        run = self.new_run({"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}})
        self.start_attempt(run, "A")
        snap = run_state.replay(run)
        self.assertEqual(run_state.load_state(run), snap["state"])
        self.assertIn("nodes", run_state.load_state(run))
        self.assertNotIn("nodes", run_state.load_run(run))

if __name__ == "__main__":
    unittest.main()
