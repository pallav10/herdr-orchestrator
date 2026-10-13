#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import graph_model
import ledger
import run_state
from testkit import HOLDER, commit_file, git, new_run, seed_repo


class RunStateTest(unittest.TestCase):
    def test_validate_graph_200_node_chain_plus_fanout_under_100ms(self):
        from time import perf_counter

        nodes = {}
        for index in range(180):
            node_id = f"C{index}"
            nodes[node_id] = {
                "id": node_id,
                "executor": "task",
                "depends_on": [f"C{index - 1}"] if index else [],
                "owns": ["src/**"],
            }
        for index in range(20):
            node_id = f"F{index}"
            nodes[node_id] = {
                "id": node_id,
                "executor": "task",
                "depends_on": ["C179"],
                "owns": [f"fanout/{index}.txt"],
            }
        graph = {"schema_version": 1, "nodes": nodes}
        started = perf_counter()
        result = graph_model.validate_graph(graph)
        elapsed = perf_counter() - started
        self.assertIsNone(result)
        self.assertLess(elapsed, 0.1)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.initial_holder = dict(HOLDER)
        self.repos: dict[str, Path] = {}
    def tearDown(self):
        self.tmp.cleanup()

    git = staticmethod(git)

    def repo(self) -> Path:
        return seed_repo(self.root / "repo")

    def commit(self, repo: Path, text: str) -> str:
        return commit_file(repo, "file.txt", text, text.strip())

    def new_run(self, repo: Path | None = None, graph: dict | None = None) -> str:
        repo_path = repo or (self.repo() if graph is not None else None)
        run = str(new_run(self.root, graph, repo=repo_path, holder=self.initial_holder))
        if graph is not None:
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

    def model(self, *, resolved=None, thinking=None, effort=None, requested_role=None, requested_effort=None, requested_model=None) -> dict:
        out = {"requested_role": requested_role, "requested_effort": requested_effort, "resolved": resolved, "thinking": thinking, "effort": effort}
        if requested_model is not None:
            out["requested_model"] = requested_model
        return out

    def start_attempt(self, run: str, node: str, *, attempt: int = 1, holder=..., handle: dict | None = None, model: dict | None = None) -> None:
        actual_holder = self.run_holder(run) if holder is ... else holder
        model = model or self.model()
        handle = dict(handle or {"spawning_session": "s", "job_id": f"{node}-{attempt}"})
        handle.setdefault("workspace", f"ws-{node}")
        handle.setdefault("pane", f"pane-{node}")
        handle.setdefault("agent", f"agent-{node}")
        self.append_event(run, {"type": "dispatch_intent", "node": node, "attempt": attempt}, holder=actual_holder)
        attempt_dir = Path(run) / "artifacts" / node / str(attempt)
        repo = self.repos.get(run)
        run_json = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
        if repo:
            handle.setdefault("repo", str(repo.resolve()))
            handle.setdefault("worktree_path", str(repo.resolve()))
            if "branch" not in handle:
                handle["branch"] = self.git(repo, "branch", "--show-current")
            handle.setdefault("base", run_json.get("base_sha"))
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

    def herdr_live(self, run: str, node: str, attempt: int = 1, **extra) -> dict:
        run_id = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))["id"]
        node_state = run_state.replay(run)["state"]["nodes"][node]
        handle = node_state.get("handle") or {}
        identity_path = Path(run) / "artifacts" / node / str(attempt) / "dispatch-identity.json"
        loaded = json.loads(identity_path.read_text(encoding="utf-8")) if identity_path.is_file() else {}
        rec = {
            "workspace": handle.get("workspace") or loaded.get("workspace") or f"ws-{node}",
            "pane": handle.get("pane") or loaded.get("pane") or f"pane-{node}",
            "agent": handle.get("agent") or loaded.get("agent") or f"agent-{node}",
            "tokens": {"provenance": "orchestrator", "run": run_id, "node": node, "attempt": str(attempt)},
        }
        if isinstance(loaded.get("tokens"), dict):
            rec["tokens"].update(loaded["tokens"])
        routing = loaded.get("routing") if isinstance(loaded.get("routing"), dict) else node_state.get("model")
        if isinstance(routing, dict):
            rec["routing"] = dict(routing)
        rec.update(extra)
        return rec

    def write_attempt_receipts(self, run: str, node: str, attempt: int, sha: str) -> Path:
        attempt_dir = Path(run) / "artifacts" / node / str(attempt)
        consumed = json.loads((attempt_dir / "consumed_artifacts.json").read_text(encoding="utf-8"))
        run_json = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
        identity = json.loads((attempt_dir / "dispatch-identity.json").read_text(encoding="utf-8"))
        (attempt_dir / "integration.json").write_text(json.dumps({"commit": sha, "head": sha, "base_sha": run_json["base_sha"], "consumed": consumed}) + "\n", encoding="utf-8")
        (attempt_dir / "vcs.json").write_text(
            json.dumps({"worktree": identity["worktree_path"], "repo": identity["repo"], "branch": identity["branch"], "base_sha": run_json["base_sha"], "result": {"form": "commit", "sha": sha}}) + "\n",
            encoding="utf-8",
        )
        return attempt_dir

    def promotion_attempt(self, run: str, node: str = "D", attempt: int = 1, sha: str | None = None) -> Path:
        run_json = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
        return self.write_attempt_receipts(run, node, attempt, sha or run_json["base_sha"])

    def promotion_event_types(self, run: str) -> list[str]:
        return [event["type"] for event in run_state.replay(run)["events"] if event["type"] in {"promotion_intent", "promoted", "blocked"}]
    def promotion_node(self, node_id: str = "D") -> dict:
        return {
            "id": node_id,
            "executor": "herdr-agent",
            "outputs": [{"name": "integration", "type": "integration"}, {"name": "vcs", "type": "vcs"}],
            "worktree": node_id.lower(),
            "owns": [f"src/{node_id.lower()}/**"],
        }


    def graph(self) -> dict:
        return {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
                "B": {
                    "id": "B",
                    "executor": "herdr-agent",
                    "depends_on": ["A"],
                    "inputs": [{"alias": "a", "from": "A", "output": "summary"}],
                    "outputs": [{"name": "vcs", "type": "vcs"}],
                    "worktree": "b",
                    "owns": ["src/b/**"],
                },
                "C": {
                    "id": "C",
                    "executor": "herdr-agent",
                    "depends_on": ["A"],
                    "inputs": [{"alias": "a", "from": "A", "output": "summary"}],
                    "outputs": [{"name": "vcs", "type": "vcs"}],
                    "worktree": "c",
                    "owns": ["src/c/**"],
                },
                "D": {
                    "id": "D",
                    "executor": "herdr-agent",
                    "depends_on": ["B", "C"],
                    "inputs": [
                        {"alias": "b", "from": "B", "output": "vcs"},
                        {"alias": "c", "from": "C", "output": "vcs"},
                    ],
                    "outputs": [{"name": "integration", "type": "integration"}, {"name": "vcs", "type": "vcs"}],
                    "worktree": "d",
                    "owns": ["src/d/**"],
                },
            },
        }

    def artifact(self, run: str, node: str, attempt: int, name: str, typ: str, text: str = "x") -> dict:
        attempt_dir = Path(run) / "artifacts" / node / str(attempt)
        filename = "vcs.json" if typ == "vcs" else "integration.json" if typ == "integration" else f"{name}.json"
        path = attempt_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if typ == "vcs":
            run_json = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
            identity = json.loads((attempt_dir / "dispatch-identity.json").read_text(encoding="utf-8"))
            payload = {"worktree": identity["worktree_path"], "repo": identity["repo"], "branch": identity["branch"], "base_sha": run_json["base_sha"], "result": {"form": "commit", "sha": run_json["base_sha"]}}
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        elif typ == "integration":
            consumed = json.loads((attempt_dir / "consumed_artifacts.json").read_text(encoding="utf-8"))
            run_json = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
            path.write_text(json.dumps({"commit": text, "head": text, "base_sha": run_json["base_sha"], "consumed": consumed}) + "\n", encoding="utf-8")
        else:
            path.write_text(text, encoding="utf-8")
        return {"name": name, "type": typ, "path": str(path.resolve()), "hash": run_state.sha256_file(path)}

    def test_cycle_and_unserialized_overlap_rejected_serialized_overlap_allowed(self):
        cycle = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task", "depends_on": ["B"]}, "B": {"id": "B", "executor": "task", "depends_on": ["A"]}}}
        with self.assertRaisesRegex(run_state.PreconditionError, "cycle"):
            run_state.validate_graph(cycle)

        overlap = {"schema_version": 1, "nodes": {"B": {"id": "B", "executor": "task", "owns": ["src/**"]}, "C": {"id": "C", "executor": "task", "owns": ["src/app.py"]}}}
        with self.assertRaisesRegex(run_state.PreconditionError, "overlap"):
            run_state.validate_graph(overlap)

        serialized = {"schema_version": 1, "nodes": {"B": {"id": "B", "executor": "task", "owns": ["src/**"]}, "D": {"id": "D", "executor": "task", "depends_on": ["B"], "owns": ["src/app.py"]}}}
        run_state.validate_graph(serialized)

    def test_ready_fanout_and_integration_waits_for_vcs_hashes(self):
        run = self.new_run(self.repo(), self.graph())
        state = run_state.replay(run)["state"]
        self.assertEqual(state["nodes"]["A"]["state"], "ready")
        self.assertEqual(state["nodes"]["B"]["state"], "planned")
        self.assertEqual(state["nodes"]["C"]["state"], "planned")

        self.start_attempt(run, "A")
        self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": [self.artifact(run, "A", 1, "summary", "summary", "a")]})
        state = self.reconcile(run)["state"]
        self.assertEqual(state["nodes"]["B"]["state"], "ready")
        self.assertEqual(state["nodes"]["C"]["state"], "ready")
        self.assertEqual(state["nodes"]["D"]["state"], "planned")

        self.start_attempt(run, "B")
        self.append_event(run, {"type": "succeeded", "node": "B", "attempt": 1, "artifacts": [self.artifact(run, "B", 1, "vcs", "vcs", "b")]})
        self.start_attempt(run, "C")
        self.append_event(run, {"type": "succeeded", "node": "C", "attempt": 1, "artifacts": [self.artifact(run, "C", 1, "vcs", "vcs", "c")]})
        self.assertEqual(self.reconcile(run)["state"]["nodes"]["D"]["state"], "ready")

    def test_replay_uses_active_epoch_and_skips_stale_old_epoch_mutation(self):
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}
        run = self.new_run(self.repo(), graph)
        predecessor = dict(self.initial_holder)
        self.start_attempt(run, "A")
        self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": []})
        self.append_event(run, {"type": "notification_sent", "key": "pre-transfer"})
        self.append_event(run, {"type": "lease_drain_started", **predecessor}, holder=predecessor)
        self.append_event(run, {"type": "lease_transfer", "workspace": "new-ws", "pane": "new-pane", "session": "new-session"}, holder=predecessor)
        with open(Path(run) / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "failed", "node": "A", "epoch": 1}) + "\n")
            fh.write(json.dumps({"type": "notification_sent", "key": "stale", "epoch": 1}) + "\n")
        (Path(run) / "controller.json").unlink()

        replayed = run_state.replay(run)
        self.assertEqual(replayed["controller"]["epoch"], 2)
        self.assertFalse((Path(run) / "controller.json").exists())
        self.assertEqual(replayed["state"]["nodes"]["A"]["state"], "succeeded")
        self.assertEqual(replayed["skipped_stale"], 2)
        self.assertEqual(replayed["state"]["notified"], ["pre-transfer"])
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "failed", "node": "A", "epoch": 1})

    def test_initial_drain_fences_other_holders(self):
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}
        run = self.new_run(self.repo(), graph)
        predecessor = dict(self.initial_holder)
        other = {"workspace": "ws-b", "pane": "pane-b", "session": "session-b"}
        successor = {"workspace": "ws-c", "pane": "pane-c", "session": "session-c"}
        self.start_attempt(run, "A")

        drained = self.append_event(run, {"type": "lease_drain_started", **predecessor}, holder=predecessor)
        self.assertEqual(drained["controller"]["lease"], "draining")
        self.assertFalse(drained["controller"]["dispatch"])
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "failed", "node": "A", "attempt": 1}, holder=other)
        with self.assertRaises(run_state.PreconditionError):
            run_state.append_event(run, {"type": "failed", "node": "A", "attempt": 1})
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "lease_transfer", **successor}, holder=other)

        transferred = self.append_event(run, {"type": "lease_transfer", **successor}, holder=predecessor)
        self.assertEqual({key: transferred["controller"][key] for key in run_state.HOLDER_KEYS}, successor)
        self.assertEqual(transferred["controller"]["epoch"], 2)
        self.assertEqual(transferred["controller"]["lease"], "active")
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "failed", "node": "A", "attempt": 1}, holder=predecessor)
        with self.assertRaises(run_state.PreconditionError):
            run_state.append_event(run, {"type": "failed", "node": "A", "attempt": 1})

    def test_identical_consumed_hash_stays_succeeded_mismatch_invalidates_and_retries(self):
        graph = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}], "retry": {"max_attempts": 2, "on": ["invalidated"]}},
                "B": {"id": "B", "executor": "task", "depends_on": ["A"], "inputs": [{"alias": "a", "from": "A", "output": "summary"}], "retry": {"max_attempts": 2, "on": ["invalidated"]}},
            },
        }
        run = self.new_run(self.repo(), graph)
        self.start_attempt(run, "A")
        old = self.artifact(run, "A", 1, "summary", "summary", "old")
        self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": [old]})
        self.reconcile(run)
        self.start_attempt(run, "B")
        self.append_event(run, {"type": "succeeded", "node": "B", "attempt": 1, "artifacts": []})
        self.assertEqual(run_state.invalidate_if_stale(run_state.replay(run)["state"], graph), [])

        self.append_event(run, {"type": "invalidated", "node": "A", "attempt": 1})
        self.append_event(run, {"type": "retried", "node": "A", "attempt": 1, "reason": "invalidated"})
        self.reconcile(run)
        self.assertEqual(run_state.invalidate_if_stale(run_state.replay(run)["state"], graph), [])
        self.start_attempt(run, "A", attempt=2)
        new = self.artifact(run, "A", 2, "summary", "summary", "new")
        self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 2, "artifacts": [new]})
        result = self.reconcile(run)
        events = [event["type"] for event in run_state.replay(run)["events"]]
        self.assertIn("invalidated", events)
        self.assertIn("retried", events)
        self.assertTrue((Path(run) / "artifacts" / "B" / "1" / "consumed_artifacts.json").exists())
        self.assertIn(result["state"]["nodes"]["B"]["state"], {"planned", "ready"})

    def test_native_executor_spawned_by_other_session_interrupts(self):
        state = {"nodes": {"T": {"executor": "task", "state": "running", "attempt": 1, "handle": {"spawning_session": "old", "job": "job-1"}}, "H": {"executor": "herdr-agent", "state": "running"}}}
        events = run_state.maybe_interrupt_native(state, "current")
        self.assertEqual([event["node"] for event in events], ["T"])
        self.assertEqual(events[0]["type"], "interrupted")

    def test_promotion_recovery_appends_promoted_or_blocks_unexpected_ref(self):
        graph = {"schema_version": 1, "nodes": {"D": self.promotion_node()}}
        repo = self.repo()
        run = self.new_run(repo, graph)
        new_sha = self.commit(repo, "two\n")
        run_json = json.loads((Path(run) / "run.json").read_text())
        self.start_attempt(run, "D")
        self.promotion_attempt(run, sha=new_sha)
        self.append_event(run, {"type": "promotion_intent", "node": "D", "attempt": 1, "ref": run_json["promotion_ref"], "expected_old": run_json["base_sha"], "new": new_sha})
        self.git(repo, "update-ref", run_json["promotion_ref"], new_sha, run_json["base_sha"])
        run_state.recover_promotion(run, repo, holder=self.run_holder(run))
        self.assertIn("promoted", [event["type"] for event in run_state.replay(run)["events"]])

        repo2 = seed_repo(self.root / "repo2")
        run2 = self.new_run(repo2, graph)
        run2_json = json.loads((Path(run2) / "run.json").read_text())
        d_wt = self.root / "wt-d"
        self.git(repo2, "worktree", "add", "-b", "d-head", str(d_wt), run2_json["base_sha"])
        next_sha = self.commit(d_wt, "two\n")
        self.start_attempt(run2, "D", handle={"worktree_path": str(d_wt.resolve()), "branch": self.git(d_wt, "branch", "--show-current")})
        self.promotion_attempt(run2, sha=next_sha)
        self.append_event(run2, {"type": "promotion_intent", "node": "D", "attempt": 1, "ref": run2_json["promotion_ref"], "expected_old": run2_json["base_sha"], "new": next_sha})
        other_wt = self.root / "wt-unrelated"
        self.git(repo2, "worktree", "add", "-b", "unrelated-ref", str(other_wt), run2_json["base_sha"])
        other_sha = self.commit(other_wt, "three\n")
        self.git(repo2, "update-ref", run2_json["promotion_ref"], other_sha)
        self.assertEqual(self.git(d_wt, "rev-parse", "HEAD"), next_sha)
        run_state.recover_promotion(run2, repo2, holder=self.run_holder(run2))
        snap = run_state.replay(run2)
        self.assertEqual(self.git(repo2, "rev-parse", run2_json["promotion_ref"]), other_sha)
        self.assertEqual(snap["state"]["nodes"]["D"]["state"], "blocked")


    def test_already_promoted_recovery_blocks_when_live_ref_diverged(self):
        graph = {"schema_version": 1, "nodes": {"D": self.promotion_node()}}
        repo = self.repo()
        run = self.new_run(repo, graph)
        new_sha = self.commit(repo, "two\n")
        run_json = json.loads((Path(run) / "run.json").read_text())
        self.start_attempt(run, "D")
        self.promotion_attempt(run, sha=new_sha)
        self.append_event(run, {"type": "promotion_intent", "node": "D", "attempt": 1, "ref": run_json["promotion_ref"], "expected_old": run_json["base_sha"], "new": new_sha})
        self.git(repo, "update-ref", run_json["promotion_ref"], new_sha, run_json["base_sha"])
        self.append_event(run, {"type": "promoted", "node": "D", "attempt": 1, "ref": run_json["promotion_ref"], "expected_old": run_json["base_sha"], "new": new_sha})
        receipt = Path(run) / "artifacts" / "D" / "1" / "promoted.json"
        self.assertFalse(receipt.exists())
        other_sha = self.commit(repo, "orphan\n")
        self.git(repo, "update-ref", run_json["promotion_ref"], other_sha)
        result = self.reconcile(run, repo=repo)
        self.assertEqual(result["state"]["nodes"]["D"]["state"], "blocked")
        self.assertFalse(receipt.exists())
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), other_sha)
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), other_sha)
        types = self.promotion_event_types(run)
        self.assertEqual(types.count("blocked"), 1)
        self.assertEqual(types.count("promoted"), 1)
        self.assertFalse(any(event.get("type") == "succeeded" and event.get("node") == "D" for event in run_state.replay(run)["events"]))
        again = self.reconcile(run, repo=repo)
        self.assertEqual(again["state"]["nodes"]["D"]["state"], "blocked")
        self.assertEqual(sum(1 for event in run_state.replay(run)["events"] if event.get("type") == "blocked" and event.get("node") == "D"), 1)
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), other_sha)
        self.assertFalse(receipt.exists())

    def test_bound_promotion_requires_exact_holder_before_ref_changes(self):
        graph = {"schema_version": 1, "nodes": {"D": self.promotion_node()}}
        holder = {"workspace": "ws-a", "pane": "pane-a", "session": "session-a"}
        other = {"workspace": "ws-b", "pane": "pane-b", "session": "session-b"}
        repo = self.repo()

        def ready_run(text: str):
            run = self.new_run(repo, graph)
            self.bind_holder(run, holder)
            new_sha = self.commit(repo, text)
            self.start_attempt(run, "D", holder=holder)
            self.promotion_attempt(run, sha=new_sha)
            run_json = json.loads((Path(run) / "run.json").read_text())
            return run, new_sha, run_json

        run, new_sha, run_json = ready_run("promote exact\n")
        run_state.promote(run, node="D", attempt=1, new_sha=new_sha, repo=repo, holder=holder)
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), new_sha)
        self.assertIn("promoted", self.promotion_event_types(run))

        run, new_sha, run_json = ready_run("promote omitted\n")
        with self.assertRaises(run_state.PreconditionError):
            run_state.promote(run, node="D", attempt=1, new_sha=new_sha, repo=repo)
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), run_json["base_sha"])
        self.assertEqual(self.promotion_event_types(run), [])

        run, new_sha, run_json = ready_run("promote mismatch\n")
        with self.assertRaises(run_state.PreconditionError):
            run_state.promote(run, node="D", attempt=1, new_sha=new_sha, repo=repo, holder=other)
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), run_json["base_sha"])
        self.assertEqual(self.promotion_event_types(run), [])

    def test_bound_promotion_recovery_requires_exact_holder_before_ref_changes(self):
        graph = {"schema_version": 1, "nodes": {"D": self.promotion_node()}}
        holder = {"workspace": "ws-a", "pane": "pane-a", "session": "session-a"}
        other = {"workspace": "ws-b", "pane": "pane-b", "session": "session-b"}
        repo = self.repo()

        def pending_run(text: str):
            run = self.new_run(repo, graph)
            self.bind_holder(run, holder)
            new_sha = self.commit(repo, text)
            self.start_attempt(run, "D", holder=holder)
            self.promotion_attempt(run, sha=new_sha)
            run_json = json.loads((Path(run) / "run.json").read_text())
            self.append_event(run, {"type": "promotion_intent", "node": "D", "attempt": 1, "ref": run_json["promotion_ref"], "expected_old": run_json["base_sha"], "new": new_sha}, holder=holder)
            return run, new_sha, run_json

        run, new_sha, run_json = pending_run("recover exact\n")
        run_state.recover_promotion(run, repo, holder=holder)
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), new_sha)
        self.assertIn("promoted", self.promotion_event_types(run))

        run, new_sha, run_json = pending_run("recover omitted\n")
        with self.assertRaises(run_state.PreconditionError):
            run_state.recover_promotion(run, repo)
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), run_json["base_sha"])
        self.assertEqual(self.promotion_event_types(run), ["promotion_intent"])

        run, new_sha, run_json = pending_run("recover mismatch\n")
        with self.assertRaises(run_state.PreconditionError):
            run_state.recover_promotion(run, repo, holder=other)
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), run_json["base_sha"])
        self.assertEqual(self.promotion_event_types(run), ["promotion_intent"])

        run, new_sha, run_json = pending_run("recover blocked omitted\n")
        other_sha = self.commit(repo, "recover other\n")
        self.git(repo, "update-ref", run_json["promotion_ref"], other_sha)
        with self.assertRaises(run_state.PreconditionError):
            run_state.recover_promotion(run, repo)
        self.assertEqual(self.git(repo, "rev-parse", run_json["promotion_ref"]), other_sha)
        self.assertEqual(self.promotion_event_types(run), ["promotion_intent"])

    def test_dispatch_intent_writes_consumed_artifact_snapshot(self):
        run = self.new_run(self.repo(), self.graph())
        self.start_attempt(run, "A")
        art = self.artifact(run, "A", 1, "summary", "summary", "a")
        self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": [art]})
        self.reconcile(run)
        self.append_event(run, {"type": "dispatch_intent", "node": "B", "attempt": 1})
        consumed = json.loads((Path(run) / "artifacts" / "B" / "1" / "consumed_artifacts.json").read_text())
        self.assertEqual(consumed, [{"alias": "a", "producer": "A", "producer_attempt": 1, "output": "summary", "path": art["path"], "hash": art["hash"]}])

    def test_unbound_run_requires_one_exact_session_binding(self):
        run = run_state.claim_run(self.root / "docs", "Unbound", created="2026-09-04T01:02:04+00:00")["dir"]
        self.assertFalse(run_state.replay(run)["state"]["dispatch"])
        with self.assertRaises(run_state.PreconditionError):
            run_state.append_event(run, {"type": "notification_sent", "key": "arbitrary"})
        first = ledger.bind_session_run(run, holder=self.initial_holder)
        self.assertTrue(first["state"]["dispatch"])
        self.assertIsNone(ledger.bind_session_run(run, holder=self.initial_holder)["event"])
        other = {"workspace": "other", "pane": "other", "session": "other"}
        with self.assertRaises(run_state.PreconditionError):
            ledger.bind_session_run(run, holder=other)
        bound = [event for event in run_state.replay(run)["events"] if event["type"] == "session_run_bound"]
        self.assertEqual(len(bound), 1)
        self.assertEqual({key: bound[0][key] for key in run_state.HOLDER_KEYS}, self.initial_holder)

    def test_claim_run_refuses_runs_symlink(self):
        docs = self.root / "docs"
        docs.mkdir()
        outside = self.root / "outside-runs"
        outside.mkdir()
        (docs / "runs").symlink_to(outside)
        with self.assertRaises(run_state.PreconditionError):
            run_state.claim_run(docs, "Escaped")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(any(outside.rglob("run.json")))

    def test_graph_revisions_preserve_initial_base_and_promoted_ref(self):
        repo = self.repo()
        run = run_state.claim_run(self.root / "docs", "Graph", created="2026-09-04T01:02:05+00:00")["dir"]
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        ledger.bind_session_run(run, holder=self.initial_holder)
        accepted = run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        base = accepted["base_sha"]
        promoted = self.commit(repo, "promoted\n")
        self.git(repo, "update-ref", accepted["promotion_ref"], promoted, base)
        revised = {"schema_version": 1, "nodes": {"B": {"id": "B", "executor": "task"}}}
        run_state.accept_graph(run, revised, repo=repo, holder=self.initial_holder)
        self.assertEqual(json.loads((Path(run) / "run.json").read_text())["base_sha"], base)
        self.assertEqual(self.git(repo, "rev-parse", accepted["promotion_ref"]), promoted)
        before = {name: (Path(run) / name).read_bytes() for name in ("run.json", "graph.json", "events.jsonl", "state.json", "controller.json", "status.md")}
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, graph, repo=repo, holder={"workspace": "x", "pane": "y", "session": "z"})
        self.assertEqual(before, {name: (Path(run) / name).read_bytes() for name in before})
        self.assertEqual(self.git(repo, "rev-parse", accepted["promotion_ref"]), promoted)

    def test_prepare_dispatch_prompt_graph_order_refs_and_optional_prompt(self):
        inputs = [
            {"alias": "b", "from": "B", "output": "summary"},
            {"alias": "a", "from": "A", "output": "summary"},
            {"alias": "opt", "from": "O", "output": "summary", "required": False},
        ]
        nodes = {
            "A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
            "B": {"id": "B", "executor": "workflow", "outputs": [{"name": "summary", "type": "summary"}]},
            "O": {"id": "O", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
        }
        for node_id, executor in (("T", "task"), ("W", "workflow"), ("H", "herdr-agent"), ("P", "pane")):
            node = {"id": node_id, "executor": executor, "depends_on": ["B", "A", "O"], "inputs": inputs}
            if executor == "pane":
                node["handle"] = {"name": "planner"}
            nodes[node_id] = node
        run = self.new_run(self.repo(), {"schema_version": 1, "nodes": nodes})
        arts = {}
        for node_id in ("A", "B", "O"):
            self.start_attempt(run, node_id)
            arts[node_id] = self.artifact(run, node_id, 1, "summary", "summary", node_id.lower())
            self.append_event(run, {"type": "succeeded", "node": node_id, "attempt": 1, "artifacts": [arts[node_id]]})
        self.reconcile(run)
        expected_refs = [
            {"alias": "b", "producer": "B", "producer_attempt": 1, "output": "summary", "path": arts["B"]["path"], "hash": arts["B"]["hash"]},
            {"alias": "a", "producer": "A", "producer_attempt": 1, "output": "summary", "path": arts["A"]["path"], "hash": arts["A"]["hash"]},
            {"alias": "opt", "producer": "O", "producer_attempt": 1, "output": "summary", "path": arts["O"]["path"], "hash": arts["O"]["hash"]},
        ]
        for node_id in ("T", "W", "H", "P"):
            self.append_event(run, {"type": "dispatch_intent", "node": node_id, "attempt": 1})
            text = run_state.prepare_dispatch_prompt(run, node_id, 1, prompt="go\n")
            self.assertTrue(text.startswith("# herdr-dispatch\n"))
            line, _, rest = text[len("# herdr-dispatch\n"):].partition("\n")
            payload = json.loads(line)
            self.assertEqual(payload["attempt_dir"], run_state.canonical_path(Path(run) / "artifacts" / node_id / "1"))
            self.assertEqual([ref["alias"] for ref in payload["consumed"]], ["b", "a", "opt"])
            self.assertEqual(payload["consumed"], expected_refs)
            for ref in payload["consumed"]:
                self.assertTrue(Path(ref["path"]).is_absolute())
            self.assertEqual(rest, "go\n")
            bare = run_state.prepare_dispatch_prompt(run, node_id, 1)
            self.assertEqual(bare, "# herdr-dispatch\n" + line + "\n")
        broken = json.loads((Path(run) / "artifacts" / "T" / "1" / "consumed_artifacts.json").read_text(encoding="utf-8"))
        broken.append({**broken[0], "alias": "nope"})
        (Path(run) / "artifacts" / "T" / "1" / "consumed_artifacts.json").write_text(json.dumps(broken) + "\n", encoding="utf-8")
        with self.assertRaises(run_state.PreconditionError):
            run_state.prepare_dispatch_prompt(run, "T", 1)

    def test_graph_revision_rejects_active_changes_retains_terminal_history(self):
        repo = self.repo()
        concrete = self.model(requested_role="slow", requested_effort="high", resolved="provider/live", thinking="high", effort="medium")
        first = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}], "model": {"requested_role": "slow", "requested_effort": "high"}},
                "B": {"id": "B", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
                "C": {"id": "C", "executor": "task"},
                "R": {"id": "R", "executor": "task", "depends_on": ["A"]},
                "P": {"id": "P", "executor": "pane", "handle": {"name": "old-pane"}},
            },
        }
        run = self.new_run(repo, first)
        self.start_attempt(run, "A", model=concrete)
        self.start_attempt(run, "B")
        art = self.artifact(run, "B", 1, "summary", "summary", "keep")
        self.append_event(run, {"type": "succeeded", "node": "B", "attempt": 1, "artifacts": [art]})
        before = run_state.replay(run)["state"]["nodes"]
        graph_bytes = (Path(run) / "graph.json").read_bytes()
        material = {
            "schema_version": 1,
            "nodes": {
                "A": {**first["nodes"]["A"], "owns": ["src/a/**"]},
                "B": first["nodes"]["B"],
                "C": first["nodes"]["C"],
                "R": first["nodes"]["R"],
                "P": first["nodes"]["P"],
            },
        }
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, material, repo=repo, holder=self.run_holder(run))
        self.assertEqual((Path(run) / "graph.json").read_bytes(), graph_bytes)
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["A"]["state"], "running")
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["A"]["executor"], "task")
        removed_active = {
            "schema_version": 1,
            "nodes": {
                "B": first["nodes"]["B"],
                "C": first["nodes"]["C"],
                "P": first["nodes"]["P"],
            },
        }
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, removed_active, repo=repo, holder=self.run_holder(run))
        self.assertEqual((Path(run) / "graph.json").read_bytes(), graph_bytes)
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["A"]["state"], "running")
        allowed = {
            "schema_version": 1,
            "nodes": {
                "A": first["nodes"]["A"],
                "C": first["nodes"]["C"],
                "P": {"id": "P", "executor": "pane", "handle": {"name": "new-pane"}},
            },
        }
        run_state.accept_graph(run, allowed, repo=repo, holder=self.run_holder(run))
        self.reconcile(run)
        nodes = run_state.replay(run)["state"]["nodes"]
        self.assertEqual(nodes["B"]["state"], "succeeded")
        self.assertEqual(nodes["B"]["attempt"], 1)
        self.assertEqual(nodes["B"]["artifacts"], [art])
        self.assertEqual(nodes["R"]["state"], "planned")
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "dispatch_intent", "node": "R", "attempt": 1})
        self.assertEqual(nodes["A"]["executor"], "task")
        self.assertEqual(nodes["A"]["outputs"], first["nodes"]["A"]["outputs"])
        self.assertEqual(nodes["A"]["state"], "running")
        self.assertEqual(nodes["A"]["attempt"], 1)
        self.assertEqual(nodes["A"]["handle"], before["A"]["handle"])
        self.assertEqual(nodes["A"]["model"], concrete)
        self.assertEqual(nodes["C"]["state"], before["C"]["state"])
        self.assertEqual(nodes["C"]["attempt"], before["C"]["attempt"])
        self.assertEqual(nodes["C"]["executor"], "task")
        self.assertEqual(nodes["P"]["handle"], {"name": "new-pane"})
        graph_nodes = json.loads((Path(run) / "graph.json").read_text(encoding="utf-8"))["nodes"]
        self.assertNotIn("B", graph_nodes)
        self.assertNotIn("R", graph_nodes)
        self.assertIn("A", graph_nodes)


    def test_graph_revision_freezes_active_material_not_runtime_overlay(self):
        repo = self.repo()
        plan_model = {"requested_role": "slow", "requested_effort": "high"}
        concrete = self.model(requested_role="slow", requested_effort="high", resolved="provider/live", thinking="high", effort="medium")
        first = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "phase": "extract", "outputs": [{"name": "summary", "type": "summary"}], "model": plan_model},
                "B": {"id": "B", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
            },
        }
        run = self.new_run(repo, first)
        self.start_attempt(run, "A", model=concrete)
        self.append_event(run, {"type": "dispatch_intent", "node": "B", "attempt": 1})
        run_path = Path(run)
        snap_files = [
            run_path / "run.json",
            run_path / "graph.json",
            run_path / "events.jsonl",
            run_path / "state.json",
            run_path / "controller.json",
            run_path / "status.md",
            run_path / "artifacts" / "A" / "1" / "consumed_artifacts.json",
            run_path / "artifacts" / "A" / "1" / "ownership.json",
            run_path / "artifacts" / "B" / "1" / "consumed_artifacts.json",
            run_path / "artifacts" / "B" / "1" / "ownership.json",
        ]

        def frozen():
            return {str(path): path.read_bytes() for path in snap_files}

        holder = self.run_holder(run)
        before = frozen()
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["A"]["model"], concrete)
        executor_change = {"schema_version": 1, "nodes": {"A": {**first["nodes"]["A"], "executor": "workflow"}, "B": first["nodes"]["B"]}}
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, executor_change, repo=repo, holder=holder)
        self.assertEqual(frozen(), before)
        output_change = {"schema_version": 1, "nodes": {"A": first["nodes"]["A"], "B": {**first["nodes"]["B"], "outputs": [{"name": "notes", "type": "summary"}]}}}
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, output_change, repo=repo, holder=holder)
        self.assertEqual(frozen(), before)

        run_rec = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
        staged = {
            "run": dict(run_rec),
            "graph": {
                "schema_version": 1,
                "run": run_rec["path"],
                "base_sha": run_rec["base_sha"],
                "promotion_ref": run_rec["promotion_ref"],
                "nodes": {"A": {**first["nodes"]["A"], "executor": "herdr-agent"}, "B": first["nodes"]["B"]},
            },
            "initialize_ref": False,
        }
        revision = ledger._revision_digest(staged)
        revision_path = ledger._revision_path(run_path, revision)
        revision_path.parent.mkdir(parents=True, exist_ok=True)
        run_state.atomic_json(revision_path, staged)
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "graph_accepted", "revision": revision})
        self.assertEqual(frozen(), before)
        nodes = run_state.replay(run)["state"]["nodes"]
        self.assertEqual(nodes["A"]["executor"], "task")
        self.assertEqual(nodes["A"]["outputs"], first["nodes"]["A"]["outputs"])
        self.assertEqual(nodes["A"]["model"], concrete)
        self.assertEqual(nodes["A"]["state"], "running")
        self.assertEqual(nodes["B"]["outputs"], first["nodes"]["B"]["outputs"])
        self.assertTrue(graph_model.has_current_dispatch_intent(nodes["B"]))

        allowed = {
            "schema_version": 1,
            "nodes": {
                "A": {**first["nodes"]["A"], "phase": "write"},
                "B": first["nodes"]["B"],
                "Z": {"id": "Z", "executor": "task"},
            },
        }
        run_state.accept_graph(run, allowed, repo=repo, holder=holder)
        nodes = run_state.replay(run)["state"]["nodes"]
        self.assertEqual(nodes["A"]["state"], "running")
        self.assertEqual(nodes["A"]["model"], concrete)
        self.assertEqual(nodes["A"]["phase"], "write")
        self.assertEqual(nodes["A"]["executor"], "task")
        self.assertEqual(nodes["A"]["outputs"], first["nodes"]["A"]["outputs"])
        self.assertEqual(nodes["Z"]["state"], "planned")
        self.assertEqual(nodes["B"]["outputs"], first["nodes"]["B"]["outputs"])
        graph_nodes = json.loads((run_path / "graph.json").read_text(encoding="utf-8"))["nodes"]
        self.assertEqual(graph_nodes["A"]["model"], plan_model)
        self.assertEqual(graph_nodes["A"]["phase"], "write")
        self.assertIn("Z", graph_nodes)


    def test_ready_dispatch_intent_orphans_interrupt_on_complete_inventory(self):
        repo = self.repo()
        graph = {"schema_version": 1, "nodes": {"T": {"id": "T", "executor": "task"}, "H": {"id": "H", "executor": "herdr-agent"}}}
        run = self.new_run(repo, graph)
        self.append_event(run, {"type": "dispatch_intent", "node": "T", "attempt": 1})
        self.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1})
        self.assertFalse(run_state.native_quiesced(run_state.replay(run)["state"]))
        state = self.reconcile(run, current_session="s", live_native_ids=[], native_inventory_complete=False)["state"]["nodes"]
        self.assertEqual(state["T"]["state"], "ready")
        self.assertEqual(state["H"]["state"], "ready")
        state = self.reconcile(run, current_session="s", live_native_ids=[], native_inventory_complete=True)["state"]["nodes"]
        self.assertEqual(state["T"]["state"], "interrupted")
        self.assertEqual(state["H"]["state"], "ready")
        self.assertTrue(run_state.native_quiesced(run_state.replay(run)["state"]))
        state = self.reconcile(run, live_herdr=[])["state"]["nodes"]
        self.assertEqual(state["H"]["state"], "interrupted")
        idle = self.new_run(repo, {"schema_version": 1, "nodes": {"T": {"id": "T", "executor": "task"}}})
        with self.assertRaisesRegex(run_state.PreconditionError, "running or blocked"):
            self.append_event(idle, {"type": "interrupted", "node": "T", "attempt": 0})


    def test_promotion_intent_rejects_foreign_ref_and_stale_expected_old(self):
        repo = self.repo()
        run = self.new_run(repo, {"schema_version": 1, "nodes": {"D": self.promotion_node()}})
        new_sha = self.commit(repo, "two\n")
        run_json = json.loads((Path(run) / "run.json").read_text())
        self.start_attempt(run, "D")
        self.promotion_attempt(run, sha=new_sha)
        with self.assertRaisesRegex(run_state.PreconditionError, "promotion_ref"):
            self.append_event(run, {"type": "promotion_intent", "node": "D", "attempt": 1, "ref": "refs/heads/other", "expected_old": run_json["base_sha"], "new": new_sha})
        with self.assertRaisesRegex(run_state.PreconditionError, "expected_old"):
            self.append_event(run, {"type": "promotion_intent", "node": "D", "attempt": 1, "ref": run_json["promotion_ref"], "expected_old": new_sha, "new": new_sha})
        self.assertNotIn("promotion_intent", [event["type"] for event in run_state.replay(run)["events"]])

    def test_promotion_requires_declared_integration_and_vcs(self):
        repo = self.repo()
        run = self.new_run(repo, {"schema_version": 1, "nodes": {"D": {"id": "D", "executor": "herdr-agent"}}})
        new_sha = self.commit(repo, "two\n")
        run_json = json.loads((Path(run) / "run.json").read_text())
        self.start_attempt(run, "D")
        self.promotion_attempt(run, sha=new_sha)
        with self.assertRaisesRegex(run_state.PreconditionError, "declared integration and vcs"):
            self.append_event(run, {"type": "promotion_intent", "node": "D", "attempt": 1, "ref": run_json["promotion_ref"], "expected_old": run_json["base_sha"], "new": new_sha})

    def test_diff_vcs_path_must_be_attempt_local_change_bin(self):
        attempt = self.root / "attempt"
        attempt.mkdir()
        local = attempt / "change.bin"
        local.write_bytes(b"diff")
        digest = run_state.sha256_file(local)
        external = self.root / "outside.bin"
        external.write_bytes(b"diff")
        vcs = attempt / "vcs.json"
        base = {"worktree": str(self.root.resolve()), "repo": str(self.root.resolve()), "branch": "b", "base_sha": "abc"}
        payload = {**base, "result": {"form": "diff", "path": str(external.resolve()), "sha256": digest}}
        vcs.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(run_state.PreconditionError, "change.bin"):
            run_state.validate_vcs_payload(vcs, attempt_dir=attempt)
        payload["result"]["path"] = str(local.resolve())
        vcs.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        self.assertIsNone(run_state.validate_vcs_payload(vcs, attempt_dir=attempt))

    def test_vcs_relative_repo_worktree_is_precondition(self):
        attempt = self.root / "rel-attempt"
        attempt.mkdir()
        vcs = attempt / "vcs.json"
        base = {"branch": "b", "base_sha": "abc", "result": {"form": "diff", "path": str(attempt / "change.bin"), "sha256": "sha256:" + "ab" * 32}}
        for key, payload in (
            ("worktree", {"worktree": "rel-tree", "repo": str(self.root.resolve()), **base}),
            ("repo", {"worktree": str(self.root.resolve()), "repo": "rel-repo", **base}),
        ):
            with self.subTest(field=key):
                vcs.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                with self.assertRaises(run_state.PreconditionError):
                    run_state.validate_vcs_payload(vcs, attempt_dir=attempt)

    def test_all_done_ignores_removed_planned_history(self):
        repo = self.repo()
        run = self.new_run(repo, {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}, "R": {"id": "R", "executor": "task", "depends_on": ["A"]}}})
        self.start_attempt(run, "A")
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["R"]["state"], "planned")
        run_state.accept_graph(run, {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}, repo=repo, holder=self.run_holder(run))
        self.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": []})
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["R"]["state"], "planned")
        notified = self.reconcile(run, notify=True)["notified"]
        self.assertTrue(any(str(key).startswith("all-done:") for key in notified))


    def test_first_dispatch_requested_routing_matches_plan_or_replan(self):
        repo = self.repo()
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task", "model": {"requested_role": "slow", "requested_effort": "high"}}}}
        run = self.new_run(repo, graph)
        self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        handle = {"spawning_session": "s", "job_id": "A-1"}
        wrong = self.model(requested_role="smol", requested_effort="low", resolved="provider/x")
        with self.assertRaisesRegex(run_state.PreconditionError, "requested routing"):
            self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": handle, "model": wrong})
        concrete = self.model(requested_role="slow", requested_effort="high", resolved="provider/live", thinking="high", effort="medium")
        result = self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": handle, "model": concrete})
        self.assertEqual(result["state"]["nodes"]["A"]["model"], concrete)

        run = self.new_run(repo, graph)
        replan = self.model(requested_role="smol", requested_effort="low")
        self.append_event(run, {"type": "replan", "node": "A", "model": replan})
        self.append_event(run, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        with self.assertRaisesRegex(run_state.PreconditionError, "requested routing"):
            self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": handle, "model": concrete})
        live = self.model(requested_role="smol", requested_effort="low", resolved="provider/other", thinking="low", effort="low")
        result = self.append_event(run, {"type": "dispatched", "node": "A", "attempt": 1, "handle": handle, "model": live})
        self.assertEqual(result["state"]["nodes"]["A"]["model"], live)

    def test_unsafe_graph_ids_and_symlink_allocation_stay_contained(self):
        repo = self.repo()
        run = self.new_run()
        for node_id in ("..", "../escaped", "/tmp/abs", "", ".", "-bad", "a/b", "x" * 33):
            graph = {"schema_version": 1, "nodes": {node_id: {"id": node_id, "executor": "task"}}}
            with self.assertRaises(run_state.PreconditionError):
                run_state.validate_graph(graph)
            with self.assertRaises(run_state.PreconditionError):
                run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
            with self.assertRaises(run_state.PreconditionError):
                ledger._ensure_attempt_snapshot(Path(run_state.canonical_path(run)), str(node_id), 1, [], [])
        run_path = Path(run)
        artifacts = run_path / "artifacts"
        snapshot_names = ("consumed_artifacts.json", "ownership.json", "vcs.json", "integration.json")
        leaked_snapshots = [
            path for name in snapshot_names
            for path in (
                *self.root.rglob(name),
                Path("/tmp/abs") / "1" / name,
                Path("/tmp/abs") / name,
                run_path / "1" / name,
                run_path / "escaped" / "1" / name,
            )
            if path.is_file() and (not artifacts.exists() or artifacts.resolve() not in path.resolve().parents)
        ]
        self.assertEqual(leaked_snapshots, [])
        self.assertFalse((run_path / "1").exists())
        self.assertFalse((run_path / "escaped").exists())
        self.assertFalse((self.root / "escaped").exists())
        if artifacts.exists():
            self.assertTrue(artifacts.is_dir())
            self.assertFalse(artifacts.is_symlink())
            self.assertEqual(list(artifacts.iterdir()), [])
        valid = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "task"}}}
        run_state.accept_graph(run, valid, repo=repo, holder=self.initial_holder)
        self.reconcile(run)
        leaked = self.root / "leaked"
        leaked.mkdir()
        artifacts = Path(run) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        (artifacts / "H").symlink_to(leaked, target_is_directory=True)
        with self.assertRaises(run_state.PreconditionError):
            ledger._ensure_attempt_snapshot(Path(run_state.canonical_path(run)), "H", 1, [], ["src/h/**"])
        self.assertEqual(list(leaked.iterdir()), [])
        run2 = self.new_run()
        run_state.accept_graph(run2, valid, repo=repo, holder=self.initial_holder)
        self.reconcile(run2)
        escaped = self.root / "escaped-art"
        escaped.mkdir()
        (Path(run2) / "artifacts").rmdir()
        (Path(run2) / "artifacts").symlink_to(escaped, target_is_directory=True)
        with self.assertRaises(run_state.PreconditionError):
            ledger._ensure_attempt_snapshot(Path(run_state.canonical_path(run2)), "H", 1, [], [])
        self.assertEqual(list(escaped.iterdir()), [])
        run3 = self.new_run(repo, valid)
        self.append_event(run3, {"type": "dispatch_intent", "node": "H", "attempt": 1})
        h_dir = Path(run3) / "artifacts" / "H"
        attempt_dir = h_dir / "1"
        self.assertTrue((attempt_dir / "consumed_artifacts.json").is_file())
        leak3 = self.root / "leak3"
        leak3.mkdir()
        for child in list(attempt_dir.iterdir()):
            child.unlink()
        attempt_dir.rmdir()
        h_dir.rmdir()
        h_dir.symlink_to(leak3, target_is_directory=True)
        with self.assertRaises(run_state.PreconditionError):
            self.reconcile(run3)
        self.assertFalse((leak3 / "1" / "consumed_artifacts.json").exists())
        self.assertFalse((leak3 / "1" / "ownership.json").exists())
        run4 = self.new_run(repo, valid)
        self.append_event(run4, {"type": "dispatch_intent", "node": "H", "attempt": 1})
        target = self.root / "file-leak.json"
        snap = Path(run4) / "artifacts" / "H" / "1" / "consumed_artifacts.json"
        snap.unlink()
        snap.symlink_to(target)
        with self.assertRaises(run_state.PreconditionError):
            self.reconcile(run4)
        self.assertFalse(target.exists())

    def test_contradictory_event_target_cannot_bypass_success_gates(self):
        repo = self.repo()
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
        run = self.new_run(repo, graph)
        self.start_attempt(run, "H")
        self.append_event(run, {"type": "blocked", "node": "H", "attempt": 1, "reason": "approval requested"})
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "failed", "to": "succeeded", "node": "H", "attempt": 1})
        snap = run_state.replay(run)
        self.assertEqual(snap["state"]["nodes"]["H"]["state"], "blocked")
        self.assertFalse(any(event.get("type") == "succeeded" and event.get("node") == "H" for event in snap["events"]))
        self.assertFalse((Path(run) / "artifacts" / "H" / "1" / "summary.json").exists())
        self.assertEqual(json.loads((Path(run) / "state.json").read_text(encoding="utf-8"))["nodes"]["H"]["state"], "blocked")

    def test_graph_revision_rejects_active_writer_overlap(self):
        repo = self.repo()
        first = {
            "schema_version": 1,
            "nodes": {
                "B": {"id": "B", "executor": "task", "owns": ["src/shared/**"]},
                "A": {"id": "A", "executor": "task"},
            },
        }
        run = self.new_run(repo, first)
        self.start_attempt(run, "B")
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["B"]["state"], "running")
        before = (Path(run) / "graph.json").read_bytes()
        revision = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task"},
                "C": {"id": "C", "executor": "task", "owns": ["src/shared/**"]},
            },
        }
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, revision, repo=repo, holder=self.run_holder(run))
        self.assertEqual((Path(run) / "graph.json").read_bytes(), before)
        nodes = run_state.replay(run)["state"]["nodes"]
        self.assertEqual(nodes["B"]["state"], "running")
        self.assertNotIn("C", json.loads((Path(run) / "graph.json").read_text(encoding="utf-8"))["nodes"])

    def test_same_attempt_herdr_pane_resume_and_invalid_identities(self):
        repo = self.repo()
        concrete = self.model(resolved="provider/live", thinking="high", effort="medium")
        graph = {
            "schema_version": 1,
            "nodes": {
                "H": {"id": "H", "executor": "herdr-agent"},
                "P": {"id": "P", "executor": "pane", "handle": {"name": "planner"}},
                "T": {"id": "T", "executor": "task"},
                "A": {"id": "A", "executor": "task"},
                "B": {"id": "B", "executor": "task", "depends_on": ["A"]},
            },
        }
        run = self.new_run(repo, graph)
        self.start_attempt(run, "H", model=concrete)
        self.append_event(run, {"type": "blocked", "node": "H", "attempt": 1, "reason": "approval requested"})
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["H"]["state"], "blocked")
        handle = run_state.replay(run)["state"]["nodes"]["H"]["handle"]
        resumed = self.append_event(run, {
            "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
            "handle": handle, "model": concrete, "live_identity": self.herdr_live(run, "H"),
        })
        self.assertEqual(resumed["state"]["nodes"]["H"]["state"], "running")
        self.assertEqual(resumed["state"]["nodes"]["H"]["attempt"], 1)
        started_h = resumed["event"]
        self.assertEqual(started_h["model"], concrete)
        self.assertEqual(started_h["live_identity"]["routing"], concrete)
        self.assertEqual(sum(1 for event in run_state.replay(run)["events"] if event["type"] == "dispatch_intent" and event["node"] == "H"), 1)
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1})
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 2})
        self.start_attempt(run, "P", model=concrete)
        self.append_event(run, {"type": "blocked", "node": "P", "attempt": 1, "reason": "approval requested"})
        pane = self.append_event(run, {
            "type": "attempt_started", "node": "P", "attempt": 1, "from": "blocked", "to": "running",
            "handle": run_state.replay(run)["state"]["nodes"]["P"]["handle"],
            "model": concrete, "live_identity": self.herdr_live(run, "P"),
        })
        self.assertEqual(pane["state"]["nodes"]["P"]["state"], "running")
        started_p = pane["event"]
        self.assertEqual(started_p["model"], concrete)
        self.assertEqual(started_p["live_identity"]["routing"], concrete)
        self.assertEqual(sum(1 for event in run_state.replay(run)["events"] if event["type"] == "dispatch_intent" and event["node"] == "P"), 1)
        self.start_attempt(run, "T", model=concrete)
        self.append_event(run, {"type": "blocked", "node": "T", "attempt": 1, "reason": "approval requested"})
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {
                "type": "attempt_started", "node": "T", "attempt": 1, "from": "blocked", "to": "running",
                "handle": run_state.replay(run)["state"]["nodes"]["T"]["handle"],
                "model": concrete, "live_identity": self.herdr_live(run, "T"),
            })
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["T"]["state"], "blocked")
        self.append_event(run, {"type": "blocked", "node": "H", "attempt": 1, "reason": "approval requested"})
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {
                "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
                "handle": run_state.replay(run)["state"]["nodes"]["H"]["handle"],
                "model": concrete, "live_identity": dict(self.herdr_live(run, "H"), workspace="other-ws"),
            })
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {
                "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
                "handle": run_state.replay(run)["state"]["nodes"]["H"]["handle"], "model": concrete,
            })
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {
                "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
                "handle": run_state.replay(run)["state"]["nodes"]["H"]["handle"],
                "model": {**concrete, "resolved": "provider/other"},
                "live_identity": self.herdr_live(run, "H"),
            })
        before_started = [event for event in run_state.replay(run)["events"] if event["type"] == "attempt_started"]
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {
                "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
                "handle": run_state.replay(run)["state"]["nodes"]["H"]["handle"],
                "model": concrete,
                "live_identity": dict(self.herdr_live(run, "H"), routing={**concrete, "resolved": "provider/other"}),
            })
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["H"]["state"], "blocked")
        self.assertEqual([event for event in run_state.replay(run)["events"] if event["type"] == "attempt_started"], before_started)
        continued = self.append_event(run, {
            "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
            "handle": run_state.replay(run)["state"]["nodes"]["H"]["handle"],
            "model": concrete, "live_identity": dict(self.herdr_live(run, "H"), routing=self.model()),
        })
        self.assertEqual(continued["state"]["nodes"]["H"]["state"], "running")
        self.assertEqual(continued["state"]["nodes"]["H"]["model"], concrete)
        started_unknown = continued["event"]
        self.assertEqual(started_unknown["model"], concrete)
        self.assertEqual(started_unknown["live_identity"]["routing"], self.model())
        self.start_attempt(run, "A", model=concrete)
        self.append_event(run, {"type": "failed", "node": "A", "attempt": 1})
        self.reconcile(run)
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["B"]["state"], "blocked")
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(run, {
                "type": "attempt_started", "node": "B", "attempt": 0, "from": "blocked", "to": "running",
                "live_identity": self.herdr_live(run, "B", 0),
            })
        idle = self.new_run(repo, {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent"}}})
        self.append_event(idle, {"type": "dispatch_intent", "node": "H", "attempt": 1})
        self.append_event(idle, {"type": "blocked", "node": "H", "attempt": 1, "reason": "approval requested"})
        with self.assertRaises(run_state.PreconditionError):
            self.append_event(idle, {
                "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
                "model": concrete,
                "live_identity": {
                    "workspace": "ws-H", "pane": "pane-H", "agent": "agent-H",
                    "tokens": {"provenance": "orchestrator", "run": json.loads((Path(idle) / "run.json").read_text(encoding="utf-8"))["id"], "node": "H", "attempt": "1"},
                },
            })
        self.assertEqual(run_state.replay(idle)["state"]["nodes"]["H"]["state"], "blocked")
        self.assertIsNone(run_state.replay(idle)["state"]["nodes"]["H"].get("handle"))

    def test_continuation_routing_helper_unknown_and_mismatch(self):
        recorded = self.model(resolved="provider/live", thinking="high", effort="medium")
        unknown = self.model()
        self.assertEqual(graph_model.validate_continuation_routing(recorded, unknown), unknown)
        self.assertEqual(graph_model.validate_continuation_routing(recorded, recorded), recorded)
        with self.assertRaises(run_state.PreconditionError):
            graph_model.validate_continuation_routing(recorded, {**recorded, "resolved": "provider/other"})
        with self.assertRaises(run_state.PreconditionError):
            graph_model.validate_continuation_routing(recorded, {**recorded, "thinking": "low"})
        with self.assertRaises(run_state.PreconditionError):
            graph_model.validate_continuation_routing(recorded, {**recorded, "effort": "low"})

    def test_old_continuation_event_replays_without_live_routing(self):
        repo = self.repo()
        concrete = self.model(resolved="provider/live", thinking="high", effort="medium")
        run = self.new_run(repo, {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent"}}})
        self.start_attempt(run, "H", model=concrete)
        self.append_event(run, {"type": "blocked", "node": "H", "attempt": 1, "reason": "approval requested"})
        handle = run_state.replay(run)["state"]["nodes"]["H"]["handle"]
        live = self.herdr_live(run, "H")
        live.pop("routing", None)
        with open(Path(run) / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
                "handle": handle, "model": concrete, "live_identity": live,
            }) + "\n")
        replayed = run_state.replay(run)
        self.assertEqual(replayed["state"]["nodes"]["H"]["state"], "running")
        self.assertEqual(replayed["state"]["nodes"]["H"]["model"], concrete)

    def test_live_blocked_observation_notifies_once(self):
        repo = self.repo()
        run = self.new_run(repo, {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent"}}})
        self.start_attempt(run, "H")
        rec = self.herdr_live(run, "H", agent_status="blocked", reason="Approval required", question="Approve this?")
        first = self.reconcile(run, live_herdr=[rec], notify=True)
        self.assertEqual(first["state"]["nodes"]["H"]["state"], "blocked")
        self.assertEqual(first["state"]["nodes"]["H"].get("reason"), "Approval required")
        blocked = [event for event in run_state.replay(run)["events"] if event.get("type") == "blocked" and event.get("node") == "H"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].get("reason"), "Approval required")
        self.assertEqual(blocked[0].get("question"), "Approve this?")
        self.assertTrue(first["notified"])
        status = (Path(run) / "status.md").read_text(encoding="utf-8")
        self.assertIn("blocked", status)
        self.assertEqual(json.loads((Path(run) / "state.json").read_text(encoding="utf-8"))["nodes"]["H"]["state"], "blocked")
        sent = [event for event in run_state.replay(run)["events"] if event.get("type") == "notification_sent"]
        second = self.reconcile(run, live_herdr=[rec], notify=True)
        self.assertEqual(second["state"]["nodes"]["H"]["state"], "blocked")
        self.assertEqual(len([event for event in run_state.replay(run)["events"] if event.get("type") == "blocked" and event.get("node") == "H"]), 1)
        self.assertEqual(len([event for event in run_state.replay(run)["events"] if event.get("type") == "notification_sent"]), len(sent))
        self.assertEqual(second["notified"], [])
        running = self.new_run(repo, {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent"}}})
        self.start_attempt(running, "H")
        unknown = self.reconcile(running)
        self.assertEqual(unknown["state"]["nodes"]["H"]["state"], "running")
        empty = self.reconcile(running, live_herdr=[])
        self.assertEqual(empty["state"]["nodes"]["H"]["state"], "interrupted")

    def test_requested_model_recovery_stays_canonical(self):
        repo = self.repo()
        run = self.new_run(repo, {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent"}}})
        routing = self.model(resolved="provider/live", thinking="high", effort="medium", requested_model="provider/explicit")
        self.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1})
        attempt_dir = Path(run) / "artifacts" / "H" / "1"
        run_json = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
        handle = {
            "workspace": "ws-H", "pane": "pane-H", "agent": "agent-H",
            "repo": str(repo.resolve()), "worktree_path": str(repo.resolve()),
            "branch": self.git(repo, "branch", "--show-current"), "base": run_json.get("base_sha"),
        }
        identity = {
            "workspace": handle["workspace"], "pane": handle["pane"], "agent": handle["agent"],
            "run": run_state.canonical_path(run), "run_id": run_json["id"], "node": "H", "attempt": "1",
            "tokens": {"provenance": "orchestrator", "run": run_json["id"], "node": "H", "attempt": "1"},
            "worktree_path": handle["worktree_path"], "repo": handle["repo"],
            "branch": handle["branch"], "base": handle["base"], "routing": dict(routing),
        }
        (attempt_dir / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
        result = self.reconcile(run, live_herdr=[{
            "workspace": handle["workspace"], "pane": handle["pane"], "agent": handle["agent"],
            "tokens": identity["tokens"],
        }])
        self.assertEqual(result["state"]["nodes"]["H"]["state"], "running")
        self.assertEqual(result["state"]["nodes"]["H"]["model"].get("requested_model"), "provider/explicit")
        dispatched = [event for event in run_state.replay(run)["events"] if event["type"] == "dispatched" and event.get("node") == "H"]
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["model"].get("requested_model"), "provider/explicit")
        loaded = json.loads((attempt_dir / "dispatch-identity.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded["routing"].get("requested_model"), result["state"]["nodes"]["H"]["model"].get("requested_model"))
        run2 = self.new_run(repo, {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}})
        self.append_event(run2, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        with_null = {**self.model(resolved="provider/x", thinking="low", effort="low"), "requested_model": None}
        dropped = self.append_event(run2, {"type": "dispatched", "node": "A", "attempt": 1, "handle": {"job_id": "A-1"}, "model": with_null})
        self.assertNotIn("requested_model", dropped["state"]["nodes"]["A"]["model"])
        run3 = self.new_run(repo, {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}})
        self.append_event(run3, {"type": "dispatch_intent", "node": "A", "attempt": 1})
        kept = self.model(resolved="provider/x", thinking="low", effort="low", requested_model="provider/explicit")
        stored = self.append_event(run3, {"type": "dispatched", "node": "A", "attempt": 1, "handle": {"job_id": "A-1"}, "model": kept})
        self.assertEqual(stored["state"]["nodes"]["A"]["model"].get("requested_model"), "provider/explicit")

    def test_disjoint_and_intersecting_fnmatch_ownership(self):
        disjoint = {
            "schema_version": 1,
            "nodes": {
                "B": {"id": "B", "executor": "task", "owns": ["src/*.py"]},
                "C": {"id": "C", "executor": "task", "owns": ["src/*.js"]},
            },
        }
        run_state.validate_graph(disjoint)
        self.assertFalse(run_state.owns_overlap("src/*.py", "src/*.js"))
        intersecting = {
            "schema_version": 1,
            "nodes": {
                "B": {"id": "B", "executor": "task", "owns": ["src/**"]},
                "C": {"id": "C", "executor": "task", "owns": ["src/*.py"]},
            },
        }
        with self.assertRaises(run_state.PreconditionError):
            run_state.validate_graph(intersecting)
        self.assertTrue(run_state.owns_overlap("src/**", "src/*.py"))
        serialized = {
            "schema_version": 1,
            "nodes": {
                "B": {"id": "B", "executor": "task", "owns": ["src/**"]},
                "D": {"id": "D", "executor": "task", "depends_on": ["B"], "owns": ["src/*.py"]},
            },
        }
        run_state.validate_graph(serialized)

    def test_duplicate_input_alias_rejected_before_allocation(self):
        repo = self.repo()
        run = self.new_run()
        graph = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
                "B": {
                    "id": "B",
                    "executor": "task",
                    "depends_on": ["A"],
                    "inputs": [
                        {"alias": "source", "from": "A", "output": "summary"},
                        {"alias": "source", "from": "A", "output": "summary"},
                    ],
                },
            },
        }
        with self.assertRaises(run_state.PreconditionError):
            run_state.validate_graph(graph)
        with self.assertRaises(run_state.PreconditionError):
            run_state.accept_graph(run, graph, repo=repo, holder=self.initial_holder)
        self.assertFalse((Path(run) / "artifacts" / "B").exists())
        empty_alias = {
            "schema_version": 1,
            "nodes": {
                "A": {"id": "A", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
                "B": {"id": "B", "executor": "task", "depends_on": ["A"], "inputs": [{"alias": "", "from": "A", "output": "summary"}]},
            },
        }
        with self.assertRaises(run_state.PreconditionError):
            run_state.validate_graph(empty_alias)
        self.assertFalse((Path(run) / "artifacts").exists() and any((Path(run) / "artifacts").rglob("consumed_artifacts.json")))

    def test_blocked_observation_after_same_attempt_resume_notifies_each_edge(self):
        repo = self.repo()
        run = self.new_run(repo, {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent"}}})
        self.start_attempt(run, "H")
        rec = self.herdr_live(run, "H", agent_status="blocked", reason="approval required")
        first = self.reconcile(run, live_herdr=[rec], notify=True)
        self.assertEqual(first["state"]["nodes"]["H"]["state"], "blocked")
        self.assertTrue(first["notified"])
        self.assertEqual(sum(1 for event in run_state.replay(run)["events"] if event.get("type") == "blocked" and event.get("node") == "H"), 1)
        handle = run_state.replay(run)["state"]["nodes"]["H"]["handle"]
        model = run_state.replay(run)["state"]["nodes"]["H"]["model"]
        self.append_event(run, {
            "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
            "handle": handle, "model": model, "live_identity": self.herdr_live(run, "H"),
        })
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["H"]["state"], "running")
        second = self.reconcile(run, live_herdr=[rec], notify=True)
        self.assertEqual(second["state"]["nodes"]["H"]["state"], "blocked")
        self.assertTrue(second["notified"])
        self.assertTrue(set(first["notified"]).isdisjoint(second["notified"]))
        self.assertEqual(sum(1 for event in run_state.replay(run)["events"] if event.get("type") == "blocked" and event.get("node") == "H"), 2)
        sent = sum(1 for event in run_state.replay(run)["events"] if event.get("type") == "notification_sent")
        again = self.reconcile(run, live_herdr=[rec], notify=True)
        self.assertEqual(again["notified"], [])
        self.assertEqual(sum(1 for event in run_state.replay(run)["events"] if event.get("type") == "blocked" and event.get("node") == "H"), 2)
        self.assertEqual(sum(1 for event in run_state.replay(run)["events"] if event.get("type") == "notification_sent"), sent)
        self.bind_holder(run, {"workspace": "new-ws", "pane": "new-pane", "session": "new-session"})
        rotated = self.reconcile(run, live_herdr=[rec], notify=True)
        self.assertEqual(rotated["state"]["nodes"]["H"]["state"], "blocked")
        self.assertEqual(rotated["notified"], [])
        self.assertEqual(sum(1 for event in run_state.replay(run)["events"] if event.get("type") == "blocked" and event.get("node") == "H"), 2)
        self.assertIn("blocked", (Path(run) / "status.md").read_text(encoding="utf-8"))

    def test_all_done_follows_terminal_occurrence_not_lease_epoch(self):
        repo = self.repo()
        node = {"id": "A", "executor": "task", "retry": {"max_attempts": 2, "on": ["failed"], "side_effect_safe": True}}
        run = self.new_run(repo, {"schema_version": 1, "nodes": {"A": node}})
        self.start_attempt(run, "A")
        self.append_event(run, {"type": "failed", "node": "A", "attempt": 1})
        first = self.reconcile(run, notify=True)["notified"]
        first_done = [key for key in first if str(key).startswith("all-done:")]
        self.assertTrue(any(str(key).startswith("edge:") for key in first))
        self.assertEqual(len(first_done), 1)
        self.append_event(run, {"type": "retried", "node": "A", "attempt": 1, "reason": "failed"})
        self.reconcile(run)
        self.start_attempt(run, "A", attempt=2)
        self.append_event(run, {"type": "failed", "node": "A", "attempt": 2})
        second = self.reconcile(run, notify=True)["notified"]
        second_done = [key for key in second if str(key).startswith("all-done:")]
        self.assertTrue(any(str(key).startswith("edge:") for key in second))
        self.assertEqual(len(second_done), 1)
        self.assertTrue(set(first).isdisjoint(second))
        run_state.accept_graph(
            run,
            {"schema_version": 1, "nodes": {"A": node, "Z": {"id": "Z", "executor": "task", "condition": False}}},
            repo=repo,
            holder=self.run_holder(run),
        )
        phased = self.reconcile(run, notify=True)["notified"]
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["Z"]["state"], "skipped")
        self.assertEqual(len([key for key in phased if str(key).startswith("all-done:")]), 1)
        self.assertTrue(set(first + second).isdisjoint(phased))
        sent = [event.get("key") for event in run_state.replay(run)["events"] if event.get("type") == "notification_sent"]
        self.bind_holder(run, {"workspace": "new-ws", "pane": "new-pane", "session": "new-session"})
        rotated = self.reconcile(run, notify=True)
        self.assertFalse(any(str(key).startswith("all-done:") for key in rotated["notified"]))
        self.assertFalse(any(str(key).startswith("edge:") for key in rotated["notified"]))
        replayed = run_state.replay(run)
        self.assertEqual(replayed["state"]["nodes"]["A"]["state"], "failed")
        self.assertEqual(replayed["state"]["nodes"]["Z"]["state"], "skipped")
        self.assertEqual(replayed["controller"]["epoch"], 2)
        self.assertEqual([event.get("key") for event in replayed["events"] if event.get("type") == "notification_sent"], sent)
        cached = json.loads((Path(run) / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(cached["nodes"]["A"]["state"], "failed")
        self.assertEqual(cached["epoch"], replayed["state"]["epoch"])
        self.assertEqual(sorted(cached.get("notified") or []), sorted(replayed["state"].get("notified") or []))
        self.assertEqual(self.reconcile(run, notify=True)["notified"], [])

    def test_new_worker_id_generates_unique_managed_names(self):
        self.assertEqual(run_state.IDENTITY_MANAGED_NAME, "managed-name")
        self.assertEqual(run_state.IDENTITY_RUNTIME_ID, "runtime-id")
        first = run_state.new_worker_id()
        second = run_state.new_worker_id()
        self.assertNotEqual(first, second)
        for worker_id in (first, second):
            self.assertTrue(run_state.WORKER_ID_RE.fullmatch(worker_id))
            self.assertTrue(worker_id.startswith("fo-"))
            self.assertEqual(len(worker_id), 27)
        self.assertIsNone(run_state.WORKER_ID_RE.fullmatch("fo-" + "g" * 24))
        self.assertIsNone(run_state.WORKER_ID_RE.fullmatch("fo-" + "a" * 23))

    def test_parse_herdr_identity_mode_ambiguity_and_pane_rejection(self):
        worker_id = run_state.new_worker_id()
        tokens = {"provenance": "orchestrator", "run": "r1", "node": "H", "attempt": "1"}
        legacy = {"workspace": "ws", "pane": "pane-1", "agent": "agent-1", "tokens": dict(tokens)}
        managed = {"workspace": "ws", "pane": "pane-1", "identity_kind": "managed-name", "worker_id": worker_id, "tokens": dict(tokens)}

        parsed_legacy = run_state.parse_herdr_identity(legacy, "herdr-agent")
        self.assertEqual(parsed_legacy["identity_kind"], run_state.IDENTITY_RUNTIME_ID)
        self.assertEqual(parsed_legacy["target"], "agent-1")

        parsed_managed = run_state.parse_herdr_identity(managed, "herdr-agent")
        self.assertEqual(parsed_managed["identity_kind"], run_state.IDENTITY_MANAGED_NAME)
        self.assertEqual(parsed_managed["target"], worker_id)

        # missing discriminator requires legacy agent - a bare worker_id never turns a legacy record managed
        self.assertIsNone(run_state.parse_herdr_identity({"workspace": "ws", "pane": "pane-1", "tokens": dict(tokens)}, "herdr-agent"))
        self.assertIsNone(run_state.parse_herdr_identity({"workspace": "ws", "pane": "pane-1", "worker_id": worker_id, "tokens": dict(tokens)}, "herdr-agent"))
        # explicit identity_kind=None behaves exactly like a missing discriminator
        self.assertEqual(run_state.parse_herdr_identity({**legacy, "identity_kind": None}, "herdr-agent")["target"], "agent-1")
        # explicit runtime-id also requires agent
        self.assertIsNone(run_state.parse_herdr_identity({"workspace": "ws", "pane": "pane-1", "identity_kind": "runtime-id", "tokens": dict(tokens)}, "herdr-agent"))

        # managed-name never falls back to agent when worker_id is missing or malformed
        self.assertIsNone(run_state.parse_herdr_identity({**managed, "worker_id": "not-a-worker-id"}, "herdr-agent"))
        no_worker_but_agent = {"workspace": "ws", "pane": "pane-1", "identity_kind": "managed-name", "agent": "agent-1", "tokens": dict(tokens)}
        self.assertIsNone(run_state.parse_herdr_identity(no_worker_but_agent, "herdr-agent"))

        # managed-name with an otherwise-valid worker_id is still rejected outright when a nonempty legacy
        # agent field leaks in alongside it - a mixed record is never treated as a clean managed identity
        mixed = {**managed, "agent": "agent-1"}
        self.assertIsNone(run_state.parse_herdr_identity(mixed, "herdr-agent"))
        self.assertIsNone(run_state.parse_herdr_identity(mixed, "herdr-agent", require_context=False))
        # an empty-string legacy agent is not "nonempty" and never disturbs an otherwise-valid managed record
        self.assertIsNotNone(run_state.parse_herdr_identity({**managed, "agent": ""}, "herdr-agent"))

        # pane always rejects managed-name even with an otherwise-valid worker_id and full context
        self.assertIsNone(run_state.parse_herdr_identity(managed, "pane"))
        self.assertIsNotNone(run_state.parse_herdr_identity(legacy, "pane"))

        # herdr-agent requires provenance=orchestrator under full context; pane never does
        no_provenance = {**managed, "tokens": {"run": "r1", "node": "H", "attempt": "1"}}
        self.assertIsNone(run_state.parse_herdr_identity(no_provenance, "herdr-agent"))
        legacy_no_provenance = {**legacy, "tokens": {"run": "r1", "node": "H", "attempt": "1"}}
        self.assertIsNotNone(run_state.parse_herdr_identity(legacy_no_provenance, "pane"))

        # require_context=False only needs workspace/pane/target, no tokens at all
        bare_managed = {"workspace": "ws", "pane": "pane-1", "identity_kind": "managed-name", "worker_id": worker_id}
        self.assertIsNone(run_state.parse_herdr_identity(bare_managed, "herdr-agent"))
        relaxed = run_state.parse_herdr_identity(bare_managed, "herdr-agent", require_context=False)
        self.assertEqual(relaxed["target"], worker_id)
        self.assertEqual(relaxed["tokens"], {})

        self.assertIsNone(run_state.parse_herdr_identity({"workspace": "ws", "pane": "pane-1", "identity_kind": "bogus", "agent": "agent-1"}, "herdr-agent"))
        self.assertIsNone(run_state.parse_herdr_identity("not-a-dict", "herdr-agent"))

    def test_herdr_identity_matches_and_command_target_are_mode_aware(self):
        worker_id = run_state.new_worker_id()
        tokens = {"provenance": "orchestrator", "run": "r1", "node": "H", "attempt": "1"}
        managed_recorded = {"workspace": "ws", "pane": "pane-1", "identity_kind": "managed-name", "worker_id": worker_id, "tokens": dict(tokens)}
        managed_live = {"workspace": "ws", "pane": "pane-1", "identity_kind": "managed-name", "worker_id": worker_id, "tokens": dict(tokens)}
        self.assertTrue(run_state.herdr_identity_matches(managed_recorded, managed_live, "herdr-agent"))
        self.assertEqual(run_state.herdr_command_target(managed_recorded, "herdr-agent"), worker_id)

        legacy_recorded = {"workspace": "ws", "pane": "pane-1", "agent": "agent-1", "tokens": dict(tokens)}
        self.assertFalse(run_state.herdr_identity_matches(managed_recorded, legacy_recorded, "herdr-agent"))
        # a managed record leaking a nonempty legacy agent field never matches, even byte-identical on both sides
        mixed_recorded = {**managed_recorded, "agent": "agent-1"}
        mixed_live = {**managed_live, "agent": "agent-1"}
        self.assertFalse(run_state.herdr_identity_matches(mixed_recorded, managed_live, "herdr-agent"))
        self.assertFalse(run_state.herdr_identity_matches(managed_recorded, mixed_live, "herdr-agent"))
        self.assertFalse(run_state.herdr_identity_matches(mixed_recorded, mixed_live, "herdr-agent"))
        self.assertIsNone(run_state.herdr_command_target(mixed_recorded, "herdr-agent"))


        for mismatch in (
            {"workspace": "other"},
            {"pane": "other"},
            {"worker_id": run_state.new_worker_id()},
            {"tokens": {**tokens, "attempt": "2"}},
            {"tokens": {**tokens, "provenance": "user"}},
        ):
            with self.subTest(mismatch=mismatch):
                self.assertFalse(run_state.herdr_identity_matches(managed_recorded, {**managed_live, **mismatch}, "herdr-agent"))

        self.assertFalse(run_state.herdr_identity_matches(managed_recorded, managed_live, "pane"))
        pane_tokens = {"run": "r1", "node": "P", "attempt": "1"}
        pane_recorded = {"workspace": "ws", "pane": "pane-1", "agent": "agent-1", "tokens": pane_tokens}
        pane_live = {"workspace": "ws", "pane": "pane-1", "agent": "agent-1", "tokens": {**pane_tokens, "provenance": "user"}}
        self.assertTrue(run_state.herdr_identity_matches(pane_recorded, pane_live, "pane"))

        self.assertIsNone(run_state.herdr_command_target({"workspace": "ws", "pane": "pane-1", "identity_kind": "managed-name", "worker_id": worker_id}, "pane"))
        self.assertIsNone(run_state.herdr_command_target({}, "herdr-agent"))

    def test_integrator_declared_branch_is_refused_at_accept(self):
        node = {"id": "D", "executor": "herdr-agent", "owns": ["**"], "worktree": {"branch": "slice/d"},
                "outputs": [{"name": "vcs", "type": "vcs"}, {"name": "integration", "type": "integration"}]}
        with self.assertRaisesRegex(run_state.PreconditionError, "derived per attempt"):
            run_state.validate_graph({"schema_version": 1, "nodes": {"D": node}})
        del node["worktree"]
        run_state.validate_graph({"schema_version": 1, "nodes": {"D": node}})

    def test_unlaunched_herdr_intent_allows_plan_rewrite_but_not_owns(self):
        repo = self.repo()
        node = {"id": "P", "executor": "herdr-agent", "owns": ["a/**"], "worktree": {"branch": "slice/p"},
                "outputs": [{"name": "summary", "type": "summary"}, {"name": "vcs", "type": "vcs"}]}
        run = self.new_run(repo, {"schema_version": 1, "nodes": {"P": dict(node)}})
        self.append_event(run, {"type": "dispatch_intent", "node": "P", "attempt": 1})
        holder = self.run_holder(run)

        rewritten = {"schema_version": 1, "nodes": {"P": {**node, "worktree": {"branch": "slice/p2"}}}}
        run_state.accept_graph(run, rewritten, repo=repo, holder=holder)
        state = run_state.load_state(run)["nodes"]["P"]
        self.assertEqual((state["state"], state["worktree"]["branch"], state["dispatch_intent"]["attempt"]), ("ready", "slice/p2", 1))

        with self.assertRaisesRegex(run_state.PreconditionError, "cannot rewrite owns"):
            run_state.accept_graph(run, {"schema_version": 1, "nodes": {"P": {**node, "owns": ["b/**"]}}}, repo=repo, holder=holder)

        (Path(run) / "artifacts" / "P" / "1" / "launch-intent.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(run_state.PreconditionError, "cannot rewrite active node"):
            run_state.accept_graph(run, {"schema_version": 1, "nodes": {"P": {**node, "worktree": {"branch": "slice/p3"}}}}, repo=repo, holder=holder)

if __name__ == "__main__":
    unittest.main()
