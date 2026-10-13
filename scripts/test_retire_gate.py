#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import ledger
import run_state
from testkit import (
    FakeHerdr, HOLDER, commit_file, git, load_script, make_worktree, new_run,
    run_module as run_script, seed_repo, snapshot_run,
)


def run_module(module, argv: list[str]) -> tuple[int, dict]:
    code, out, _err = run_script(module, argv)
    return code, json.loads(out)


def make_herdr(
    tokens: dict[str, dict[str, str]],
    status: str = "done",
    *,
    workspaces: dict[str, str] | None = None,
    panes: dict[str, str] | None = None,
    agents: dict[str, str] | None = None,
    names: dict[str, str] | None = None,
) -> FakeHerdr:
    routes = {}
    closed: list[str] = []
    for name, worker_tokens in tokens.items():
        workspace = (workspaces or {}).get(name, f"ws-{name}")
        pane = (panes or {}).get(name, f"pane-{name}")
        routes[("agent", "get", name)] = {"result": {"agent": {
            "agent_id": (agents or {}).get(name, name),
            "name": (names or {}).get(name, name),
            "workspace_id": workspace,
            "pane_id": pane,
            "agent_status": status,
        }}}
        routes[("workspace", "get", workspace)] = {"result": {"workspace": {
            "workspace_id": workspace,
            "root_pane": {"pane_id": pane},
            "tokens": worker_tokens,
        }}}
        routes[("workspace", "close", workspace)] = lambda *_args: closed.append(_args[2]) or {}
    fake = FakeHerdr(routes)
    fake.closed = closed
    return fake


class RetireGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.module = load_script("retire-agents.py")
        self.holder = dict(HOLDER)
        self.repos: dict[str, Path] = {}
        self._wt = 0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    git = staticmethod(git)

    def make_worktree(self, repo: Path, *, node: str, attempt: int, base: str) -> Path:
        self._wt += 1
        path = self.root / "wt" / f"{self._wt}-{node}-{attempt}"
        branch = f"t/{self._wt}-{node}-{attempt}"
        return make_worktree(repo, path, branch, base=base)


    def identity_for(self, run: Path, node: str, attempt: int) -> dict:
        return json.loads((run / "artifacts" / node / str(attempt) / "dispatch-identity.json").read_text(encoding="utf-8"))

    def snapshot_consumed(self, run: Path, node: str, attempt: int) -> list:
        raw = json.loads((run / "artifacts" / node / str(attempt) / "consumed_artifacts.json").read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else raw.get("consumed", [])

    def receipt_consumed(self, items: list) -> list[dict]:
        out = []
        for item in items:
            entry = {
                "producer": item["producer"],
                "producer_attempt": item.get("producer_attempt", item.get("attempt")),
                "output": item["output"],
                "hash": item["hash"],
            }
            if item.get("alias"):
                entry["alias"] = item["alias"]
            out.append(entry)
        return out

    def default_graph(self, *, b_output: str = "vcs", d_inputs: list | None = None) -> dict:
        if d_inputs is None:
            d_inputs = [{"alias": "b", "from": "B", "output": b_output}]
        return {
            "schema_version": 1,
            "nodes": {
                "B": {
                    "id": "B", "executor": "herdr-agent", "outputs": [{"name": b_output, "type": "vcs"}],
                    "worktree": "b", "owns": ["src/b/**"], "retry": {"max_attempts": 2, "on": ["invalidated", "interrupted"]},
                },
                "D": {
                    "id": "D", "executor": "herdr-agent", "depends_on": ["B"],
                    "inputs": d_inputs,
                    "outputs": [{"name": "integration", "type": "integration"}, {"name": "vcs", "type": "vcs"}],
                    "retry": {"max_attempts": 2, "on": ["invalidated"]},
                },
            },
        }

    def summary_graph(self) -> dict:
        return {
            "schema_version": 1,
            "nodes": {
                "H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]},
            },
        }

    def make_run(self, graph: dict | None = None) -> Path:
        repo = seed_repo(self.root / f"repo-{len(self.repos) + 1}")
        run = new_run(self.root, graph if graph is not None else self.default_graph(),
                      repo=repo, holder=self.holder)
        self.repos[str(run)] = repo
        return run

    def append_event(self, run: Path, event: dict):
        return run_state.append_event(run, event, holder=self.holder)

    def reconcile(self, run: Path):
        return run_state.reconcile(run, holder=self.holder)

    def tokens(self, run: Path, node: str = "B", attempt: int = 1) -> dict[str, str]:
        return {"provenance": "orchestrator", "run": run.name, "node": node, "attempt": str(attempt)}

    def start_attempt(
        self,
        run: Path,
        node: str,
        attempt: int,
        *,
        workspace: str | None = None,
        pane: str | None = None,
        agent: str | None = None,
        worker_id: str | None = None,
        dispatched: bool = True,
    ) -> None:
        self.reconcile(run)
        self.append_event(run, {"type": "dispatch_intent", "node": node, "attempt": attempt})
        repo = self.repos[str(run)]
        run_json = json.loads((run / "run.json").read_text(encoding="utf-8"))
        plan = json.loads((run / "graph.json").read_text(encoding="utf-8")).get("nodes", {}).get(node, {})
        writer = plan.get("worktree") is not None or any(
            isinstance(out, dict) and out.get("type") == "vcs" for out in (plan.get("outputs") or [])
        )
        route = {"requested_role": None, "requested_effort": None, "resolved": None, "thinking": None, "effort": None}
        workspace = workspace or f"ws-{node.lower()}"
        pane = pane or f"pane-{node.lower()}"
        agent = agent or node.lower()
        if writer:
            wt = self.make_worktree(repo, node=node, attempt=attempt, base=run_json["base_sha"])
            worktree_path = str(wt.resolve())
            branch = self.git(wt, "branch", "--show-current")
        else:
            worktree_path = str(repo.resolve())
            branch = self.git(repo, "branch", "--show-current")
        handle = {
            "workspace": workspace,
            "pane": pane,
            "tokens": self.tokens(run, node, attempt),
            "repo": str(repo.resolve()),
            "worktree_path": worktree_path,
            "branch": branch,
            "base": run_json["base_sha"],
        }
        if worker_id is not None:
            handle["identity_kind"] = run_state.IDENTITY_MANAGED_NAME
            handle["worker_id"] = worker_id
        else:
            handle["agent"] = agent
        identity = {
            **handle,
            "run": run_state.canonical_path(run),
            "run_id": run_json["id"],
            "node": node,
            "attempt": str(attempt),
            "routing": route,
        }
        attempt_dir = run / "artifacts" / node / str(attempt)
        (attempt_dir / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
        if dispatched:
            self.append_event(run, {"type": "dispatched", "node": node, "attempt": attempt, "handle": handle, "model": route})

    def vcs_payload(self, run: Path, node: str, attempt: int, sha: str | None = None) -> dict:
        identity = self.identity_for(run, node, attempt)
        run_json = json.loads((run / "run.json").read_text(encoding="utf-8"))
        if sha is None:
            sha = self.git(Path(identity["worktree_path"]), "rev-parse", "HEAD")
        return {
            "repo": identity["repo"],
            "worktree": identity["worktree_path"],
            "branch": identity["branch"],
            "base_sha": run_json["base_sha"],
            "result": {"form": "commit", "sha": sha},
        }

    def add_b_vcs(self, run: Path, attempt: int = 1, *, output: str = "vcs", **start_kw) -> str:
        if attempt > 1:
            self.append_event(run, {"type": "invalidated", "node": "B", "attempt": attempt - 1})
            self.append_event(run, {"type": "retried", "node": "B", "attempt": attempt - 1})
        self.start_attempt(run, "B", attempt, **start_kw)
        sha = commit_file(Path(self.identity_for(run, "B", attempt)["worktree_path"]), "src/b/b.txt", f"b{attempt}\n", "src/b/b.txt")
        path = run / "artifacts" / "B" / str(attempt) / "vcs.json"
        path.write_text(json.dumps(self.vcs_payload(run, "B", attempt, sha)) + "\n", encoding="utf-8")
        digest = run_state.sha256_file(path)
        self.append_event(run, {"type": "succeeded", "node": "B", "attempt": attempt, "artifacts": [{"name": output, "type": "vcs", "path": str(path.resolve()), "hash": digest}]})
        return digest

    def add_d_receipt(
        self,
        run: Path,
        digest: str,
        *,
        producer_attempt: int = 1,
        consumer_attempt: int = 1,
        consumed: object = None,
        promoted: bool = True,
        succeeded: bool = False,
        output: str = "vcs",
    ) -> None:
        self.start_attempt(run, "D", consumer_attempt)
        attempt_dir = run / "artifacts" / "D" / str(consumer_attempt)
        sha = commit_file(Path(self.identity_for(run, "D", consumer_attempt)["worktree_path"]), "src/d/result.txt", f"d{consumer_attempt}\n", "src/d/result.txt")
        if consumed is None:
            consumed = self.receipt_consumed(self.snapshot_consumed(run, "D", consumer_attempt))
        run_json = json.loads((run / "run.json").read_text(encoding="utf-8"))
        receipt = attempt_dir / "integration.json"
        receipt.write_text(json.dumps({
            "base_sha": run_json["base_sha"],
            "commit": sha,
            "head": sha,
            "consumed": consumed,
        }) + "\n", encoding="utf-8")
        (attempt_dir / "vcs.json").write_text(json.dumps(self.vcs_payload(run, "D", consumer_attempt, sha)) + "\n", encoding="utf-8")
        if promoted or succeeded:
            run_state.promote(run, node="D", attempt=consumer_attempt, new_sha=sha, repo=self.repos[str(run)], holder=self.holder)

    def retire_argv(self, run: Path, name: str = "b", extra_args: list[str] | None = None) -> list[str]:
        return [
            "retire-agents.py", name, "--run", str(run),
            "--session", self.holder["session"],
            "--holder-workspace", self.holder["workspace"],
            "--holder-pane", self.holder["pane"],
            *(extra_args or []),
        ]

    def run_retire(self, run: Path, tokens: dict[str, str], name: str = "b", extra_args: list[str] | None = None, **fake_kw) -> tuple[int, dict, FakeHerdr]:
        fake = make_herdr({name: tokens}, **fake_kw)
        self.module.herdr = fake
        argv = self.retire_argv(run, name, extra_args)
        code, payload = run_module(self.module, argv)
        return code, payload, fake

    def consumption_graph(
        self,
        *,
        output: str = "vcs",
        review: bool = False,
        writer: bool = False,
        integrator: bool = False,
        retry: bool = False,
    ) -> dict:
        nodes: dict = {
            "B": {
                "id": "B",
                "executor": "herdr-agent",
                "outputs": [{"name": output, "type": "vcs"}],
                "worktree": "b",
                "owns": ["src/b/**"],
                "retry": {"max_attempts": 2, "on": ["invalidated", "interrupted"]},
            },
        }
        inputs = [{"alias": "b", "from": "B", "output": output}]
        if review:
            nodes["R"] = {
                "id": "R",
                "executor": "herdr-agent",
                "depends_on": ["B"],
                "inputs": inputs,
                "outputs": [{"name": "summary", "type": "summary"}],
            }
            if retry:
                nodes["R"]["retry"] = {"max_attempts": 2, "on": ["invalidated"]}
        if writer:
            nodes["W"] = {
                "id": "W",
                "executor": "herdr-agent",
                "depends_on": ["B"],
                "inputs": inputs,
                "outputs": [{"name": "vcs", "type": "vcs"}],
                "worktree": "w",
                "owns": ["src/w/**"],
            }
        if integrator:
            nodes["D"] = {
                "id": "D",
                "executor": "herdr-agent",
                "depends_on": ["B"],
                "inputs": inputs,
                "outputs": [{"name": "integration", "type": "integration"}, {"name": "vcs", "type": "vcs"}],
                "retry": {"max_attempts": 2, "on": ["invalidated"]},
            }
        return {"schema_version": 1, "nodes": nodes}

    def succeed_summary(self, run: Path, node: str = "R", attempt: int = 1) -> None:
        self.start_attempt(run, node, attempt)
        path = run / "artifacts" / node / str(attempt) / "summary.json"
        path.write_text(json.dumps({"summary": node, "attempt": attempt}) + "\n", encoding="utf-8")
        self.append_event(run, {
            "type": "succeeded",
            "node": node,
            "attempt": attempt,
            "artifacts": [{
                "name": "summary",
                "type": "summary",
                "path": str(path.resolve()),
                "hash": run_state.sha256_file(path),
            }],
        })

    def succeed_writer(self, run: Path, node: str = "W", attempt: int = 1) -> None:
        self.start_attempt(run, node, attempt)
        worktree = Path(self.identity_for(run, node, attempt)["worktree_path"])
        sha = commit_file(worktree, f"src/{node.lower()}/{node.lower()}.txt", f"{node}{attempt}\n", f"src/{node.lower()}/{node.lower()}.txt")
        path = run / "artifacts" / node / str(attempt) / "vcs.json"
        path.write_text(json.dumps(self.vcs_payload(run, node, attempt, sha)) + "\n", encoding="utf-8")
        self.append_event(run, {
            "type": "succeeded",
            "node": node,
            "attempt": attempt,
            "artifacts": [{
                "name": "vcs",
                "type": "vcs",
                "path": str(path.resolve()),
                "hash": run_state.sha256_file(path),
            }],
        })

    def consumed_list(self, run: Path, node: str, attempt: int) -> list:
        raw = json.loads((run / "artifacts" / node / str(attempt) / "consumed_artifacts.json").read_text(encoding="utf-8"))
        return raw if isinstance(raw, list) else raw["consumed"]

    def align_consumed(self, run: Path, node: str, attempt: int, consumed: list) -> None:
        path = run / "events.jsonl"
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "dispatch_intent" and event.get("node") == node and event.get("attempt") == attempt:
                event["consumed"] = consumed
                line = json.dumps(event, sort_keys=True, separators=(",", ":"))
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        (run / "artifacts" / node / str(attempt) / "consumed_artifacts.json").write_text(
            json.dumps(consumed) + "\n", encoding="utf-8",
        )

    def drop_node_events(self, run: Path, node: str, typ: str) -> None:
        path = run / "events.jsonl"
        kept = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == typ and event.get("node") == node:
                continue
            kept.append(line)
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")

    def replace_succeeded_artifacts(self, run: Path, node: str, attempt: int, artifacts: list) -> None:
        path = run / "events.jsonl"
        lines = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("type") == "succeeded" and event.get("node") == node and event.get("attempt") == attempt:
                event["artifacts"] = artifacts
                line = json.dumps(event, sort_keys=True, separators=(",", ":"))
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def plant_review(self, **graph_kw) -> Path:
        output = graph_kw.get("output", "vcs")
        run = self.make_run(self.consumption_graph(review=True, **graph_kw))
        self.add_b_vcs(run, output=output)
        self.succeed_summary(run)
        return run

    def assert_git_retained(self, run: Path, node: str, attempt: int = 1) -> None:
        identity = self.identity_for(run, node, attempt)
        worktree = Path(identity["worktree_path"])
        branch = identity["branch"]
        self.assertTrue(worktree.is_dir())
        self.assertEqual(self.git(worktree, "rev-parse", "--abbrev-ref", "HEAD"), branch)
        self.assertEqual(
            self.git(self.repos[str(run)], "rev-parse", "--verify", f"refs/heads/{branch}"),
            self.git(worktree, "rev-parse", "HEAD"),
        )

    def assert_allows_and_retains(
        self, run: Path, *nodes: str, extra_args: list[str] | None = None, status: str = "done", **fake_kw,
    ) -> None:
        code, payload, fake = self.run_retire(run, self.tokens(run), extra_args=extra_args, status=status, **fake_kw)
        self.assertEqual(code, 0, payload)
        row = {"name": "b", "workspace": (fake_kw.get("workspaces") or {}).get("b", "ws-b")}
        if extra_args and "--collect" in extra_args:
            row["summary"] = "collected"
        self.assertEqual(payload["retired"], [row])
        self.assertEqual(payload["refused"], [])
        self.assertEqual(fake.closed, [row["workspace"]])
        for node in nodes or ("B",):
            self.assert_git_retained(run, node)

    def assert_blocks_despite_flags(self, run: Path, *, attempt: int = 1, **fake_kw) -> None:
        for extra, status in (([], "done"), (["--force-unsettled", "--collect"], "idle")):
            with self.subTest(status=status, extra=tuple(extra)):
                self.forbid_collect()
                code, payload, fake = self.run_retire(
                    run, self.tokens(run, attempt=attempt), extra_args=list(extra), status=status, **fake_kw,
                )
                self.assertEqual(code, 3, payload)
                self.assertEqual(payload["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
                self.assertEqual(payload["retired"], [])
                self.assertEqual(fake.closed, [])
                self.assert_git_retained(run, "B", attempt)



    def test_allows_only_exact_artifactless_interrupted_attempt(self) -> None:
        def interrupted_run() -> Path:
            run = self.make_run()
            self.start_attempt(run, "B", 1)
            self.append_event(run, {"type": "interrupted", "node": "B", "attempt": 1})
            return run

        run = interrupted_run()
        self.append_event(run, {"type": "retried", "node": "B", "attempt": 1})
        self.reconcile(run)
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["B"]["attempt"], 2)

        code, payload, fake = self.run_retire(run, self.tokens(run, attempt=1))
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "b", "workspace": "ws-b"}])
        self.assertEqual(fake.closed, ["ws-b"])

        code, payload, fake = self.run_retire(run, self.tokens(run, attempt=2))
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "identity_mismatch"}])
        self.assertEqual(fake.closed, [])

        run = interrupted_run()
        path = run / "artifacts" / "B" / "1" / "vcs.json"
        path.write_text(json.dumps(self.vcs_payload(run, "B", 1)) + "\n", encoding="utf-8")
        code, payload, fake = self.run_retire(run, self.tokens(run, attempt=1))
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
        self.assertEqual(fake.closed, [])

        run = self.make_run()
        self.start_attempt(run, "B", 1)
        self.append_event(run, {"type": "failed", "node": "B", "attempt": 1})
        code, payload, fake = self.run_retire(run, self.tokens(run, attempt=1))
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
        self.assertEqual(fake.closed, [])


    def test_refuses_nonterminal_contextual_attempts_despite_settled_herdr_and_flags(self) -> None:
        cases = (
            ("running non-writer", "summary", "H", "h", "running"),
            ("blocked non-writer", "summary", "H", "h", "blocked"),
            ("ready-intent non-writer", "summary", "H", "h", "ready"),
            ("running leaf writer", "leaf", "B", "b", "running"),
        )
        extras = (
            ([], "done"),
            (["--force-unsettled", "--collect"], "idle"),
        )
        for label, graph_kind, node, name, phase in cases:
            for extra_args, status in extras:
                with self.subTest(label=label, extra_args=extra_args, status=status):
                    graph = self.summary_graph() if graph_kind == "summary" else self.default_graph(d_inputs=[])
                    run = self.make_run(graph)
                    self.start_attempt(run, node, 1, dispatched=(phase != "ready"))
                    if phase == "blocked":
                        self.append_event(run, {"type": "blocked", "node": node, "attempt": 1, "reason": "ask"})
                    self.forbid_collect()
                    code, payload, fake = self.run_retire(
                        run, self.tokens(run, node=node), name=name, extra_args=list(extra_args), status=status,
                    )
                    self.assertEqual(code, 3)
                    self.assertEqual(payload["refused"], [{"name": name, "reason": "attempt_not_terminal"}])
                    self.assertEqual(payload["retired"], [])
                    self.assertEqual(fake.closed, [])
                    self.assertEqual(run_state.replay(run)["state"]["nodes"][node]["state"], phase)

    def test_allows_terminal_collected_non_writer(self) -> None:
        run = self.make_run(self.summary_graph())
        self.start_attempt(run, "H", 1)
        path = run / "artifacts" / "H" / "1" / "summary.json"
        path.write_text(json.dumps({"name": "h", "status": "done", "summary": "ok"}, sort_keys=True) + "\n", encoding="utf-8")
        digest = run_state.sha256_file(path)
        self.append_event(run, {
            "type": "succeeded",
            "node": "H",
            "attempt": 1,
            "artifacts": [{"name": "summary", "type": "summary", "path": str(path.resolve()), "hash": digest}],
        })
        code, payload, fake = self.run_retire(run, self.tokens(run, node="H"), name="h")
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "h", "workspace": "ws-h"}])
        self.assertEqual(fake.closed, ["ws-h"])

    def test_allows_failed_non_writer_without_declared_outputs(self) -> None:
        run = self.make_run(self.summary_graph())
        self.start_attempt(run, "H", 1)
        self.append_event(run, {"type": "failed", "node": "H", "attempt": 1})
        code, payload, fake = self.run_retire(run, self.tokens(run, node="H"), name="h")
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "h", "workspace": "ws-h"}])
        self.assertEqual(fake.closed, ["ws-h"])

    def test_twenty_candidates_index_twenty_nodes_with_one_event_iteration(self) -> None:
        class CountingEvents(list):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        graph = {
            "schema_version": 1,
            "nodes": {
                f"N{i}": {"id": f"N{i}", "executor": "herdr-agent", "outputs": []}
                for i in range(20)
            },
        }
        run = self.make_run(graph)
        tokens = {}
        for i in range(20):
            node, name = f"N{i}", f"n{i}"
            self.start_attempt(run, node, 1)
            self.append_event(run, {"type": "failed", "node": node, "attempt": 1})
            tokens[name] = self.tokens(run, node=node)
        snapshot = ledger.replay(run)
        events = CountingEvents(snapshot["events"])
        snapshot["events"] = events
        fake = make_herdr(tokens)
        self.module.herdr = fake
        names = list(tokens)
        argv = self.retire_argv(run, names[0])
        argv[2:2] = names[1:]
        with mock.patch.object(ledger, "replay", return_value=snapshot):
            code, stdout, stderr = run_script(self.module, argv)
        self.assertEqual(code, 0, stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["refused"], [])
        self.assertEqual([item["name"] for item in payload["retired"]], names)
        self.assertEqual(fake.closed, [f"ws-{name}" for name in names])
        self.assertEqual(events.iterations, 1)

    def test_refuses_writer_until_integrator_receipts_matching_vcs_digest(self) -> None:
        run = self.make_run()
        digest = self.add_b_vcs(run)

        code, payload, fake = self.run_retire(run, self.tokens(run))
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
        self.assertEqual(fake.closed, [])

        self.add_d_receipt(run, digest, succeeded=True)
        code, payload, fake = self.run_retire(run, self.tokens(run))
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "b", "workspace": "ws-b"}])
        self.assertEqual(fake.closed, ["ws-b"])

    def test_run_gate_rejects_wrong_or_legacy_tokens(self) -> None:
        run = self.make_run()
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, succeeded=True)

        cases = [
            ("wrong run", {**self.tokens(run), "run": "other-run"}),
            ("wrong node", self.tokens(run, node="C")),
            ("wrong attempt", self.tokens(run, attempt=2)),
            ("role only", {"provenance": "orchestrator", "role": "B"}),
        ]
        for label, tokens in cases:
            with self.subTest(label):
                code, payload, fake = self.run_retire(run, tokens)
                self.assertEqual(code, 3)
                self.assertEqual(payload["refused"], [{"name": "b", "reason": "identity_mismatch"}])
                self.assertEqual(fake.closed, [])

    def test_receipt_digest_must_be_in_matching_consumed_entry(self) -> None:
        cases = [
            ("malformed consumed", lambda _digest: "not-a-list"),
            ("wrong producer", lambda digest: [{"producer": "C", "producer_attempt": 1, "output": "vcs", "hash": digest}]),
            ("wrong attempt", lambda digest: [{"producer": "B", "producer_attempt": 2, "output": "vcs", "hash": digest}]),
            ("wrong output", lambda digest: [{"producer": "B", "producer_attempt": 1, "output": "summary", "hash": digest}]),
        ]
        for label, corrupt in cases:
            with self.subTest(label):
                run = self.make_run()
                digest = self.add_b_vcs(run)
                self.add_d_receipt(run, digest, succeeded=True)
                receipt = run / "artifacts" / "D" / "1" / "integration.json"
                payload = json.loads(receipt.read_text(encoding="utf-8"))
                payload["consumed"] = corrupt(digest)
                receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")

                code, result, fake = self.run_retire(run, self.tokens(run))
                self.assertEqual(code, 3)
                self.assertEqual(result["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
                self.assertEqual(fake.closed, [])

    def test_gate_uses_token_attempt_vcs_not_current_node_artifact(self) -> None:
        run = self.make_run()
        self.add_b_vcs(run, attempt=1)
        digest2 = self.add_b_vcs(run, attempt=2)
        self.add_d_receipt(run, digest2, producer_attempt=2, succeeded=True)

        code, payload, fake = self.run_retire(run, self.tokens(run, attempt=1))
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
        self.assertEqual(fake.closed, [])

        code, payload, fake = self.run_retire(run, self.tokens(run, attempt=2))
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "b", "workspace": "ws-b"}])
        self.assertEqual(fake.closed, ["ws-b"])

    def test_historical_promotion_does_not_authorize_new_consumer_attempt(self) -> None:
        run = self.make_run()
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, consumer_attempt=1, succeeded=True)
        self.append_event(run, {"type": "invalidated", "node": "D", "attempt": 1})
        self.append_event(run, {"type": "retried", "node": "D", "attempt": 1})
        self.start_attempt(run, "D", 2)
        receipt = run / "artifacts" / "D" / "2" / "integration.json"
        run_json = json.loads((run / "run.json").read_text(encoding="utf-8"))
        head = self.git(Path(self.identity_for(run, "D", 2)["worktree_path"]), "rev-parse", "HEAD")
        receipt.write_text(json.dumps({
            "base_sha": run_json["base_sha"],
            "commit": head,
            "head": head,
            "consumed": self.receipt_consumed(self.snapshot_consumed(run, "D", 2)),
        }) + "\n", encoding="utf-8")

        code, payload, fake = self.run_retire(run, self.tokens(run))
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
        self.assertEqual(fake.closed, [])

    def test_user_owned_surface_remains_uncloseable(self) -> None:
        fake = make_herdr({"b": {"provenance": "user", "node": "B"}})
        self.module.herdr = fake

        code, payload = run_module(self.module, ["retire-agents.py", "b"])
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "not_orchestrator_owned"}])
        self.assertEqual(fake.closed, [])

    def test_run_scoped_retire_refuses_foreign_holder(self) -> None:
        """Closing a workspace is irreversible: a caller without the active lease is refused, exit 3."""
        run = self.make_run()
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, succeeded=True)
        fake = make_herdr({"b": self.tokens(run)})
        self.module.herdr = fake

        argv = [
            "retire-agents.py", "b", "--run", str(run),
            "--session", "foreign-session",
            "--holder-workspace", "foreign-ws",
            "--holder-pane", "foreign-pane",
        ]
        with mock.patch.object(sys, "argv", argv), self.assertRaises(run_state.PreconditionError):
            self.module.main()
        self.assertEqual(fake.closed, [])

        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "retire-agents.py"), *argv[1:]],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 3)
        self.assertIn("active lease", proc.stderr)

    def test_without_run_keeps_old_close_behavior(self) -> None:
        fake = make_herdr({"b": {"provenance": "orchestrator", "role": "B"}})
        self.module.herdr = fake

        code, payload = run_module(self.module, ["retire-agents.py", "b"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "b", "workspace": "ws-b"}])
        self.assertEqual(fake.closed, ["ws-b"])

    def test_missing_stable_agent_id_is_not_name_fallback(self) -> None:
        run = self.make_run()
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, succeeded=True)
        fake = make_herdr({"b": self.tokens(run)}, names={"b": "b"})

        def live_herdr(*args):
            payload = fake(*args)
            if args[:2] == ("agent", "get"):
                del payload["result"]["agent"]["agent_id"]
            return payload

        self.module.herdr = live_herdr
        code, payload = run_module(self.module, self.retire_argv(run, "b"))
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "identity_mismatch"}])
        self.assertEqual(fake.closed, [])

    def test_refuses_replacement_live_identity(self) -> None:
        run = self.make_run()
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, succeeded=True)
        tokens = self.tokens(run)
        code, payload, fake = self.run_retire(
            run,
            tokens,
            workspaces={"b": "replacement-ws-B"},
            panes={"b": "replacement-pane"},
            agents={"b": "replacement-agent"},
            names={"b": "b"},
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "identity_mismatch"}])
        self.assertEqual(fake.closed, [])

    def test_name_is_not_identity_fallback(self) -> None:
        run = self.make_run()
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, succeeded=True)
        code, payload, fake = self.run_retire(
            run,
            self.tokens(run),
            agents={"b": "other-agent-id"},
            names={"b": "b"},
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "identity_mismatch"}])
        self.assertEqual(fake.closed, [])

    def test_historic_attempt_identity_from_events(self) -> None:
        run = self.make_run()
        digest = self.add_b_vcs(run, workspace="ws-b1", pane="pane-b1", agent="agent-b1")
        self.add_d_receipt(run, digest, producer_attempt=1, succeeded=True)
        self.append_event(run, {"type": "invalidated", "node": "B", "attempt": 1})
        self.append_event(run, {"type": "retried", "node": "B", "attempt": 1})
        self.start_attempt(run, "B", 2, workspace="ws-b2", pane="pane-b2", agent="agent-b2")

        code, payload, fake = self.run_retire(
            run,
            self.tokens(run, attempt=1),
            workspaces={"b": "ws-b1"},
            panes={"b": "pane-b1"},
            agents={"b": "agent-b1"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "b", "workspace": "ws-b1"}])
        self.assertEqual(fake.closed, ["ws-b1"])

        code, payload, fake = self.run_retire(
            run,
            self.tokens(run, attempt=1),
            workspaces={"b": "ws-b2"},
            panes={"b": "pane-b2"},
            agents={"b": "agent-b2"},
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "identity_mismatch"}])
        self.assertEqual(fake.closed, [])

    def test_refuses_historical_close_when_live_identity_reused_by_nonterminal(self) -> None:
        for label, dispatched in (("dispatched", True), ("ready-intent", False)):
            with self.subTest(label=label):
                run = self.make_run()
                digest = self.add_b_vcs(run)
                self.add_d_receipt(run, digest, succeeded=True)
                self.append_event(run, {"type": "invalidated", "node": "B", "attempt": 1})
                self.append_event(run, {"type": "retried", "node": "B", "attempt": 1})
                self.start_attempt(run, "B", 2, dispatched=dispatched)
                code, payload, fake = self.run_retire(run, self.tokens(run, attempt=1))
                self.assertEqual(code, 3)
                self.assertEqual(payload["refused"], [{"name": "b", "reason": "identity_mismatch"}])
                self.assertEqual(fake.closed, [])


    def test_refuses_pre_promotion_integrator_without_consumers(self) -> None:
        run = self.make_run()
        self.add_b_vcs(run)
        self.reconcile(run)
        self.start_attempt(run, "D", 1)
        for status in ("done", "idle"):
            with self.subTest(status=status):
                code, payload, fake = self.run_retire(run, self.tokens(run, node="D"), name="d", status=status)
                self.assertEqual(code, 3)
                self.assertEqual(fake.closed, [])


    def test_refuses_interrupted_integrator_without_promotion(self) -> None:
        run = self.make_run()
        self.add_b_vcs(run)
        self.reconcile(run)
        self.start_attempt(run, "D", 1)
        self.append_event(run, {"type": "interrupted", "node": "D", "attempt": 1})
        self.forbid_collect()
        for extra_args, status in (([], "done"), (["--force-unsettled", "--collect"], "idle")):
            with self.subTest(extra_args=extra_args, status=status):
                code, payload, fake = self.run_retire(
                    run, self.tokens(run, node="D"), name="d", extra_args=list(extra_args), status=status,
                )
                self.assertEqual(code, 3)
                self.assertEqual(payload["refused"], [{"name": "d", "reason": "missing_integration_receipt"}])
                self.assertEqual(payload["retired"], [])
                self.assertEqual(fake.closed, [])

    def test_allows_integrator_after_collection_promotion_success(self) -> None:
        run = self.make_run()
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, succeeded=True)
        code, payload, fake = self.run_retire(run, self.tokens(run, node="D"), name="d")
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "d", "workspace": "ws-d"}])
        self.assertEqual(fake.closed, ["ws-d"])

    def test_named_vcs_output_is_not_permanent_refusal(self) -> None:
        run = self.make_run(self.default_graph(b_output="patch"))
        digest = self.add_b_vcs(run, output="patch")
        code, payload, fake = self.run_retire(run, self.tokens(run))
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
        self.assertEqual(fake.closed, [])

        self.add_d_receipt(run, digest, succeeded=True, output="patch")
        code, payload, fake = self.run_retire(run, self.tokens(run))
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "b", "workspace": "ws-b"}])
        self.assertEqual(fake.closed, ["ws-b"])

    def test_ordering_only_depends_on_is_not_consumption(self) -> None:
        run = self.make_run(self.default_graph(d_inputs=[]))
        digest = self.add_b_vcs(run)
        code, payload, fake = self.run_retire(run, self.tokens(run))
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "b", "workspace": "ws-b"}])
        self.assertEqual(fake.closed, ["ws-b"])

        run = self.make_run(self.default_graph(d_inputs=[]))
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, consumed=[], succeeded=True)
        code, payload, fake = self.run_retire(run, self.tokens(run))
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "b", "workspace": "ws-b"}])
        self.assertEqual(fake.closed, ["ws-b"])

    def test_unsafe_receipt_base_mismatch_fails_closed(self) -> None:
        run = self.make_run()
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, succeeded=True)
        receipt = run / "artifacts" / "D" / "1" / "integration.json"
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        payload["base_sha"] = "not-the-recorded-base"
        receipt.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        code, result, fake = self.run_retire(run, self.tokens(run))
        self.assertEqual(code, 3)
        self.assertEqual(result["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
        self.assertEqual(fake.closed, [])


    def pane_graph(self) -> dict:
        return {
            "schema_version": 1,
            "nodes": {
                "P": {"id": "P", "executor": "pane", "handle": {"name": "p"}},
            },
        }

    def forbid_collect(self) -> None:
        def boom(*_a, **_k):
            raise AssertionError("run_child")
        self.module.run_child = boom

    def test_committed_pane_revision_refuses_retirement_with_stale_graph_cache(self) -> None:
        graph = self.pane_graph()
        graph["nodes"]["P"]["executor"] = "herdr-agent"
        run = self.make_run(graph)
        self.start_attempt(run, "P", 1)
        self.append_event(run, {"type": "succeeded", "node": "P", "attempt": 1, "artifacts": []})
        cached_graph = (run / "graph.json").read_bytes()
        with mock.patch.object(
            ledger, "_materialize_graph_revision_locked",
            side_effect=run_state.RunError("simulated cache-write crash"),
        ):
            with self.assertRaises(run_state.RunError):
                run_state.accept_graph(run, self.pane_graph(), repo=self.repos[str(run)], holder=self.holder)
        self.assertEqual((run / "graph.json").read_bytes(), cached_graph)
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["P"]["executor"], "pane")

        code, payload, fake = self.run_retire(run, self.tokens(run, node="P"), name="p")
        self.assertEqual(code, 3)
        self.assertEqual(payload["retired"], [])
        self.assertEqual(fake.closed, [])

    def test_concurrent_pane_revision_blocked_until_after_close(self) -> None:
        for boundary in ("after_graph_read", "at_close"):
            with self.subTest(boundary=boundary):
                self._assert_pane_revision_blocked_at(boundary)

    def _assert_pane_revision_blocked_at(self, boundary: str) -> None:
        graph = self.pane_graph()
        graph["nodes"]["P"]["executor"] = "herdr-agent"
        run = self.make_run(graph)
        self.start_attempt(run, "P", 1)
        self.append_event(run, {"type": "succeeded", "node": "P", "attempt": 1, "artifacts": []})
        children: list[subprocess.Popen] = []
        orig_load = self.module.ledger.load_graph
        fake = make_herdr({"p": self.tokens(run, node="P")})
        spawned = {"done": False}

        def reap() -> None:
            for proc in children:
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                for stream in (proc.stdout, proc.stderr):
                    if stream is not None:
                        stream.close()

        def readline(proc: subprocess.Popen, timeout: float, label: str) -> str:
            assert proc.stdout is not None
            ready, _, _ = select.select([proc.stdout], [], [], timeout)
            if not ready:
                self.fail(f"{boundary} {label}: timed out")
            line = proc.stdout.readline()
            if not line:
                err = proc.stderr.read() if proc.stderr is not None else ""
                self.fail(f"{boundary} {label}: child EOF {err}")
            return line.strip()

        def spawn_racer() -> None:
            self.assertFalse(spawned["done"])
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(Path(__file__).resolve()),
                    "--accept-graph-race-child",
                    str(run),
                    str(self.repos[str(run)]),
                    json.dumps(self.holder),
                    json.dumps(self.pane_graph()),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                close_fds=True,
            )
            children.append(proc)
            spawned["done"] = True
            status = readline(proc, 10, "lock probe")
            self.assertEqual(status, "blocked")

        def load_graph(run_dir):
            loaded = orig_load(run_dir)
            if boundary == "after_graph_read" and not spawned["done"]:
                spawn_racer()
            return loaded

        def herdr(*args: str):
            if args[:2] == ("workspace", "close") and boundary == "at_close":
                committed = orig_load(run)
                self.assertEqual(committed["nodes"]["P"]["executor"], "herdr-agent")
                spawn_racer()
            return fake(*args)

        self.module.ledger.load_graph = load_graph
        self.module.herdr = herdr
        try:
            code, payload = run_module(self.module, self.retire_argv(run, "p"))
            self.assertEqual(code, 0)
            self.assertEqual(payload["retired"], [{"name": "p", "workspace": "ws-p"}])
            self.assertEqual(payload["refused"], [])
            self.assertEqual(fake.closed, ["ws-p"])
            self.assertTrue(spawned["done"])
            self.assertEqual(len(children), 1)
            proc = children[0]
            line = readline(proc, 30, "commit")
            self.assertEqual(line, "committed")
            rc = proc.wait(timeout=10)
            err = proc.stderr.read() if proc.stderr is not None else ""
            self.assertEqual(rc, 0, err)
            self.assertEqual(orig_load(run)["nodes"]["P"]["executor"], "pane")
            self.assertEqual(run_state.replay(run)["state"]["nodes"]["P"]["executor"], "pane")
        finally:
            self.module.ledger.load_graph = orig_load
            reap()



    def test_refuses_pane_executor_terminal_orchestrator_identity(self) -> None:
        run = self.make_run(self.pane_graph())
        self.start_attempt(run, "P", 1)
        self.append_event(run, {"type": "succeeded", "node": "P", "attempt": 1, "artifacts": []})
        self.forbid_collect()
        code, payload, fake = self.run_retire(
            run, self.tokens(run, node="P"), name="p", extra_args=["--collect"],
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "p", "reason": "pane_executor"}])
        self.assertEqual(payload["retired"], [])
        self.assertEqual(fake.closed, [])

    def test_refuses_pane_interrupted_and_force_unsettled(self) -> None:
        run = self.make_run(self.pane_graph())
        self.start_attempt(run, "P", 1)
        self.append_event(run, {"type": "interrupted", "node": "P", "attempt": 1})
        self.forbid_collect()
        code, payload, fake = self.run_retire(run, self.tokens(run, node="P"), name="p")
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "p", "reason": "pane_executor"}])
        self.assertEqual(fake.closed, [])

        code, payload, fake = self.run_retire(
            run,
            self.tokens(run, node="P"),
            name="p",
            extra_args=["--force-unsettled", "--collect"],
            status="running",
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "p", "reason": "pane_executor"}])
        self.assertEqual(payload["retired"], [])
        self.assertEqual(fake.closed, [])

    def test_run_tagged_tokens_require_run(self) -> None:
        cases = [
            ("run", {"provenance": "orchestrator", "run": "some-run"}),
            ("node", {"provenance": "orchestrator", "node": "P"}),
            ("attempt", {"provenance": "orchestrator", "attempt": "1"}),
            ("all", {"provenance": "orchestrator", "run": "some-run", "node": "P", "attempt": "1"}),
        ]
        for label, tokens in cases:
            with self.subTest(label=label):
                self.forbid_collect()
                fake = make_herdr({"p": tokens})
                self.module.herdr = fake
                code, payload = run_module(self.module, ["retire-agents.py", "p", "--collect"])
                self.assertEqual(code, 3)
                self.assertEqual(payload["refused"], [{"name": "p", "reason": "run_required"}])
                self.assertEqual(payload["retired"], [])
                self.assertEqual(fake.closed, [])

    def test_managed_name_retirement_gated_by_existing_receipts(self) -> None:
        run = self.make_run()
        worker_id = run_state.new_worker_id()
        digest = self.add_b_vcs(run, worker_id=worker_id)
        code, payload, fake = self.run_retire(run, self.tokens(run), names={"b": worker_id})
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "missing_integration_receipt"}])
        self.assertEqual(fake.closed, [])

        self.add_d_receipt(run, digest, succeeded=True)
        code, payload, fake = self.run_retire(run, self.tokens(run), names={"b": worker_id})
        self.assertEqual(code, 0)
        self.assertEqual(payload["retired"], [{"name": "b", "workspace": "ws-b"}])
        self.assertEqual(fake.closed, ["ws-b"])

    def test_managed_name_exact_worker_id_mismatch_refuses(self) -> None:
        run = self.make_run()
        worker_id = run_state.new_worker_id()
        digest = self.add_b_vcs(run, worker_id=worker_id)
        self.add_d_receipt(run, digest, succeeded=True)

        other_worker_id = run_state.new_worker_id()
        code, payload, fake = self.run_retire(run, self.tokens(run), names={"b": other_worker_id})
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "b", "reason": "identity_mismatch"}])
        self.assertEqual(fake.closed, [])

    def test_managed_name_reuse_by_nonterminal_attempt_refuses(self) -> None:
        for label, dispatched in (("dispatched", True), ("ready-intent", False)):
            with self.subTest(label=label):
                run = self.make_run()
                worker_id = run_state.new_worker_id()
                digest = self.add_b_vcs(run, worker_id=worker_id)
                self.add_d_receipt(run, digest, succeeded=True)
                self.append_event(run, {"type": "invalidated", "node": "B", "attempt": 1})
                self.append_event(run, {"type": "retried", "node": "B", "attempt": 1})
                self.start_attempt(run, "B", 2, worker_id=worker_id, dispatched=dispatched)
                code, payload, fake = self.run_retire(run, self.tokens(run, attempt=1), names={"b": worker_id})
                self.assertEqual(code, 3)
                self.assertEqual(payload["refused"], [{"name": "b", "reason": "identity_mismatch"}])
                self.assertEqual(fake.closed, [])

    def test_managed_name_pane_executor_retirement_stays_refused(self) -> None:
        run = self.make_run(self.pane_graph())
        worker_id = run_state.new_worker_id()
        self.start_attempt(run, "P", 1, worker_id=worker_id)
        self.append_event(run, {"type": "succeeded", "node": "P", "attempt": 1, "artifacts": []})
        self.forbid_collect()
        code, payload, fake = self.run_retire(
            run, self.tokens(run, node="P"), name="p", extra_args=["--collect"], names={"p": worker_id},
        )
        self.assertEqual(code, 3)
        self.assertEqual(payload["refused"], [{"name": "p", "reason": "pane_executor"}])
        self.assertEqual(payload["retired"], [])
        self.assertEqual(fake.closed, [])

    def test_nonintegrator_consumption_retires_and_retains_git(self) -> None:
        run = self.plant_review()
        self.assert_allows_and_retains(run, "B")

        run = self.make_run(self.consumption_graph(writer=True))
        self.add_b_vcs(run)
        self.succeed_writer(run)
        self.assert_allows_and_retains(run, "B", "W")

        run = self.plant_review(output="patch")
        self.assertEqual(self.consumed_list(run, "R", 1)[0]["output"], "patch")
        self.assert_allows_and_retains(run, "B")

        run = self.plant_review()
        row = self.consumed_list(run, "R", 1)[0]
        self.align_consumed(run, "R", 1, [row, dict(row)])
        self.assert_allows_and_retains(run, "B")

        run = self.plant_review(output="patch", writer=True, integrator=True)
        digest = run_state.sha256_file(run / "artifacts" / "B" / "1" / "vcs.json")
        self.succeed_writer(run)
        self.add_d_receipt(run, digest, succeeded=True)
        self.assertEqual(self.consumed_list(run, "R", 1)[0]["output"], "patch")
        self.assertEqual(self.consumed_list(run, "D", 1)[0]["output"], "patch")
        self.assert_allows_and_retains(run, "B", "W")

        run = self.plant_review()
        self.module.run_child = lambda argv, child_name, **_k: {"agents": [{"name": argv[-1], "summary": "collected"}]}
        self.assert_allows_and_retains(run, "B", extra_args=["--force-unsettled", "--collect"], status="idle")

        run = self.make_run(self.consumption_graph(review=True))
        self.add_b_vcs(run, workspace="ws-b1", pane="pane-b1", agent="agent-b1")
        self.succeed_summary(run)
        self.append_event(run, {"type": "invalidated", "node": "B", "attempt": 1})
        self.append_event(run, {"type": "retried", "node": "B", "attempt": 1})
        self.start_attempt(run, "B", 2, workspace="ws-b2", pane="pane-b2", agent="agent-b2")
        self.assert_allows_and_retains(
            run, "B",
            workspaces={"b": "ws-b1"},
            panes={"b": "pane-b1"},
            agents={"b": "agent-b1"},
        )
        self.assert_git_retained(run, "B", 2)

    def test_nonintegrator_consumer_state_blocks_despite_flags(self) -> None:
        run = self.make_run(self.consumption_graph(review=True))
        self.add_b_vcs(run)
        restore = snapshot_run(run)

        # Shared cases change only run files; distinct graphs stay independent.
        def running() -> Path:
            restore()
            self.start_attempt(run, "R", 1)
            return run

        def blocked() -> Path:
            restore()
            self.start_attempt(run, "R", 1)
            self.append_event(run, {"type": "blocked", "node": "R", "attempt": 1, "reason": "ask"})
            return run

        def failed() -> Path:
            restore()
            self.start_attempt(run, "R", 1)
            self.append_event(run, {"type": "failed", "node": "R", "attempt": 1})
            return run

        def skipped() -> Path:
            restore()
            self.start_attempt(run, "R", 1)
            self.append_event(run, {"type": "skipped", "node": "R", "attempt": 1})
            return run

        def ready() -> Path:
            restore()
            self.start_attempt(run, "R", 1, dispatched=False)
            return run

        def invalidated() -> Path:
            restore()
            self.succeed_summary(run)
            self.append_event(run, {"type": "invalidated", "node": "R", "attempt": 1})
            return run

        def stale_attempt() -> Path:
            run = self.plant_review(retry=True)
            self.append_event(run, {"type": "invalidated", "node": "R", "attempt": 1})
            self.append_event(run, {"type": "retried", "node": "R", "attempt": 1})
            self.start_attempt(run, "R", 2)
            return run

        def missing_success_event() -> Path:
            restore()
            self.succeed_summary(run)
            self.drop_node_events(run, "R", "succeeded")
            return run

        def sibling_writer_failed() -> Path:
            run = self.plant_review(writer=True)
            self.start_attempt(run, "W", 1)
            self.append_event(run, {"type": "failed", "node": "W", "attempt": 1})
            return run

        cases = (
            ("running", running),
            ("blocked", blocked),
            ("failed", failed),
            ("skipped", skipped),
            ("ready", ready),
            ("invalidated", invalidated),
            ("stale-attempt", stale_attempt),
            ("missing-success-event", missing_success_event),
            ("sibling-writer-failed", sibling_writer_failed),
        )
        for label, plant in cases:
            with self.subTest(label=label):
                try:
                    self.assert_blocks_despite_flags(plant())
                finally:
                    restore()

    def test_nonintegrator_snapshot_and_digest_block_despite_flags(self) -> None:
        run = self.plant_review()
        restore = snapshot_run(run)

        def missing() -> Path:
            restore()
            (run / "artifacts" / "R" / "1" / "consumed_artifacts.json").unlink()
            return run

        def malformed() -> Path:
            restore()
            (run / "artifacts" / "R" / "1" / "consumed_artifacts.json").write_text('{"consumed": "nope"}\n', encoding="utf-8")
            return run

        def differs() -> Path:
            restore()
            rows = self.consumed_list(run, "R", 1)
            rows[0] = {**rows[0], "alias": "other"}
            (run / "artifacts" / "R" / "1" / "consumed_artifacts.json").write_text(json.dumps(rows) + "\n", encoding="utf-8")
            return run

        def tampered_bytes() -> Path:
            restore()
            path = run / "artifacts" / "B" / "1" / "vcs.json"
            path.write_bytes(path.read_bytes() + b"\n")
            return run

        def retarget(**changes) -> Path:
            restore()
            rows = self.consumed_list(run, "R", 1)
            rows[0] = {**rows[0], **changes}
            self.align_consumed(run, "R", 1, rows)
            return run

        def conflicting_duplicate() -> Path:
            restore()
            row = self.consumed_list(run, "R", 1)[0]
            bad = {**row, "hash": "sha256:" + "ab" * 32}
            self.align_consumed(run, "R", 1, [row, bad])
            return run

        def attempt_alias() -> Path:
            restore()
            row = self.consumed_list(run, "R", 1)[0]
            row = {**row, "producer_attempt": 2, "attempt": 1}
            self.align_consumed(run, "R", 1, [row])
            return run

        def changed_current_digest() -> Path:
            restore()
            art = run_state.replay(run)["state"]["nodes"]["B"]["artifacts"][0]
            self.replace_succeeded_artifacts(run, "B", 1, [{**art, "hash": "sha256:" + "ef" * 32}])
            return run

        def succeeded_without_current_artifact() -> Path:
            restore()
            self.replace_succeeded_artifacts(run, "B", 1, [])
            return run

        cases = (
            ("missing", missing),
            ("malformed", malformed),
            ("differs", differs),
            ("tampered-bytes", tampered_bytes),
            ("wrong-producer", lambda: retarget(producer="C")),
            ("wrong-output", lambda: retarget(output="summary")),
            ("wrong-attempt", lambda: retarget(producer_attempt=2)),
            ("wrong-hash", lambda: retarget(hash="sha256:" + "cd" * 32)),
            ("bool-attempt", lambda: retarget(producer_attempt=True)),
            ("malformed-attempt", lambda: retarget(producer_attempt="01")),
            ("conflicting-duplicate", conflicting_duplicate),
            ("attempt-alias", attempt_alias),
            ("changed-current-digest", changed_current_digest),
            ("succeeded-without-current-artifact", succeeded_without_current_artifact),
        )
        for label, plant in cases:
            with self.subTest(label=label):
                try:
                    self.assert_blocks_despite_flags(plant())
                finally:
                    restore()

    def test_mixed_integrator_receipt_still_blocks_when_review_succeeded(self) -> None:
        run = self.plant_review(integrator=True)
        self.assert_blocks_despite_flags(run)

        run = self.make_run(self.consumption_graph(review=True, integrator=True))
        digest = self.add_b_vcs(run)
        self.add_d_receipt(run, digest, succeeded=True)
        self.start_attempt(run, "R", 1)
        self.append_event(run, {"type": "failed", "node": "R", "attempt": 1})
        self.assert_blocks_despite_flags(run)

        def corrupt(field: str, value: str, *, both: bool = False) -> Path:
            planted = self.plant_review(integrator=True)
            produced = run_state.sha256_file(planted / "artifacts" / "B" / "1" / "vcs.json")
            self.add_d_receipt(planted, produced, succeeded=True)
            path = planted / "artifacts" / "D" / "1" / "integration.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload[field] = value
            if both:
                payload["head"] = value
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return planted

        for label, plant in (
            ("base", lambda: corrupt("base_sha", "not-the-recorded-base")),
            ("commit-head", lambda: corrupt("head", "0" * 40)),
            ("promoted-sha", lambda: corrupt("commit", "0" * 40, both=True)),
        ):
            with self.subTest(label=label):
                self.assert_blocks_despite_flags(plant())



def _accept_graph_race_child() -> None:
    import errno
    import fcntl

    run, repo, holder_s, graph_s = sys.argv[2:6]
    holder = json.loads(holder_s)
    graph = json.loads(graph_s)
    fd = os.open(os.path.join(run, "run.lock"), os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            status = "blocked"
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            status = "unlocked"
    finally:
        os.close(fd)
    print(status, flush=True)
    run_state.accept_graph(run, graph, repo=repo, holder=holder)
    print("committed", flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--accept-graph-race-child"]:
        _accept_graph_race_child()
    else:
        unittest.main()
