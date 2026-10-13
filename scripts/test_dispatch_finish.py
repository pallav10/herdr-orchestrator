#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import liveness
from cli_common import ChildFailed
import run_state
from testkit import HOLDER, commit_file, git, load_script, make_worktree, new_run, run_module, seed_repo


NULL_MODEL = {
    "requested_role": None,
    "requested_effort": None,
    "resolved": "provider/live",
    "thinking": None,
    "effort": None,
}




def auth_args(holder=None, epoch=1) -> list[str]:
    holder = holder or HOLDER
    return [
        "--session", holder["session"],
        "--holder-workspace", holder["workspace"],
        "--holder-pane", holder["pane"],
        "--epoch", str(epoch),
    ]


def holder_args(holder=None) -> list[str]:
    holder = holder or HOLDER
    return [
        "--session", holder["session"],
        "--holder-workspace", holder["workspace"],
        "--holder-pane", holder["pane"],
    ]


def flag(argv, name: str) -> str:
    return argv[argv.index(name) + 1]


def events_of(run, typ=None) -> list[dict]:
    rows = run_state.replay(run)["events"]
    return [event for event in rows if typ is None or event.get("type") == typ]


class FakeSpawn:
    def __init__(self, *, worker_id=None, routing=None, extra=None, prompted=True, blocked=False, write_record=True):
        self.calls: list[list[str]] = []
        self.resumed: list[bool] = []
        self.worker_id = worker_id or run_state.new_worker_id()
        self.routing = dict(routing or NULL_MODEL)
        self.extra = extra or {}
        self.prompted = prompted
        self.blocked = blocked
        self.write_record = write_record

    def __call__(self, argv, child_name=None, *, timeout=None):
        self.calls.append(list(argv))
        self.resumed.append("--resume" in argv)
        run = Path(flag(argv, "--run"))
        node = flag(argv, "--node")
        attempt = flag(argv, "--attempt")
        run_doc = json.loads((run / "run.json").read_text(encoding="utf-8"))
        identity = {
            "kind": "herdr-agent",
            "identity_kind": run_state.IDENTITY_MANAGED_NAME,
            "worker_id": self.worker_id,
            "workspace": "ws-worker",
            "pane": "pane-worker",
            "routing": self.routing,
            "tokens": {
                "provenance": "orchestrator",
                "run": run_doc["id"],
                "node": node,
                "attempt": str(attempt),
            },
            "run": run_state.canonical_path(run),
            "run_id": run_doc["id"],
            "node": node,
            "attempt": str(attempt),
            "repo": run_doc.get("repo"),
        }
        identity.update(self.extra)
        path = run_state.attempt_dir(run, node, int(attempt)) / "dispatch-identity.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(identity) + "\n", encoding="utf-8")
        if self.write_record:
            (path.parent / "launch-complete.json").write_text(
                json.dumps({
                    "worker_id": self.worker_id,
                    "workspace": identity["workspace"],
                    "pane": identity["pane"],
                    "prompted": self.prompted,
                    "blocked": self.blocked,
                    "tokens": identity["tokens"],
                })
                + "\n",
                encoding="utf-8",
            )
        return {"worker_id": self.worker_id, "workspace": identity["workspace"]}


class FakeCollect:
    def __init__(self, *, form="commit"):
        self.calls: list[list[str]] = []
        self.form = form

    def __call__(self, argv, child_name=None, *, timeout=None):
        self.calls.append(list(argv))
        run = Path(flag(argv, "--run"))
        node = flag(argv, "--node")
        attempt = flag(argv, "--attempt")
        attempt_dir = run_state.attempt_dir(run, node, int(attempt))
        identity = json.loads((attempt_dir / "dispatch-identity.json").read_text(encoding="utf-8"))
        summary_path = attempt_dir / "summary.json"
        if not summary_path.is_file():
            summary_path.write_text(json.dumps({"status": "done"}, sort_keys=True) + "\n", encoding="utf-8")
        rows = [{
            "name": "summary",
            "output": "summary",
            "type": "summary",
            "path": str(summary_path.resolve()),
            "hash": run_state.sha256_file(summary_path),
        }]
        wt = identity.get("worktree_path")
        if isinstance(wt, str) and wt:
            head = git(Path(wt), "rev-parse", "HEAD")
            result = {"form": "none"} if self.form == "none" else {"form": "commit", "sha": head}
            vcs = {
                "worktree": identity["worktree_path"],
                "repo": identity["repo"],
                "branch": identity["branch"],
                "base_sha": identity["base"],
                "result": result,
            }
            vcs_path = attempt_dir / "vcs.json"
            vcs_path.write_text(json.dumps(vcs) + "\n", encoding="utf-8")
            rows.append({
                "name": "vcs",
                "output": "vcs",
                "type": "vcs",
                "path": str(vcs_path.resolve()),
                "hash": run_state.sha256_file(vcs_path),
            })
        return {"agents": [{"name": identity.get("worker_id") or identity.get("agent"), "artifacts": rows}]}


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.holder = dict(HOLDER)
        self._wt = 0
        self.prompt = self.root / "prompt.txt"
        self.prompt.write_text("do the work\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()


    def dispatch_argv(self, run, node, *, attempt=None, name=None, epoch=1) -> list[str]:
        argv = [
            "dispatch-agent.py",
            "--run", str(run),
            "--node", node,
            *auth_args(self.holder, epoch=epoch),
            "--prompt-file", str(self.prompt),
        ]
        if attempt is not None:
            argv += ["--attempt", str(attempt)]
        if name is not None:
            argv += ["--name", name]
        return argv

    def finish_argv(self, run, node, attempt, *, since=None, force=False) -> list[str]:
        argv = [
            "finish-agent.py",
            "--run", str(run),
            "--node", node,
            "--attempt", str(attempt),
            *holder_args(self.holder),
        ]
        if since is not None:
            argv += ["--since", since]
        if force:
            argv.append("--force")
        return argv

    def plant_identity(self, run, node, attempt, handle, model) -> dict:
        run_doc = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
        identity = {
            "workspace": handle["workspace"],
            "pane": handle["pane"],
            "run": run_state.canonical_path(run),
            "run_id": run_doc["id"],
            "node": node,
            "attempt": str(attempt),
            "tokens": {"provenance": "orchestrator", "run": run_doc["id"], "node": node, "attempt": str(attempt)},
            "worktree_path": handle.get("worktree_path"),
            "repo": handle.get("repo"),
            "branch": handle.get("branch"),
            "base": handle.get("base"),
            "routing": model,
        }
        if handle.get("worker_id"):
            identity["identity_kind"] = run_state.IDENTITY_MANAGED_NAME
            identity["worker_id"] = handle["worker_id"]
        else:
            identity["agent"] = handle["agent"]
        path = run_state.attempt_dir(Path(run), node, attempt) / "dispatch-identity.json"
        path.write_text(json.dumps(identity) + "\n", encoding="utf-8")
        return identity

    def start_writer(self, run, node, repo, *, attempt=int(1), owns_worktree=True, model=None) -> dict:
        model = model or {
            "requested_role": None,
            "requested_effort": None,
            "resolved": None,
            "thinking": None,
            "effort": None,
        }
        run_state.append_event(run, {"type": "dispatch_intent", "node": node, "attempt": attempt}, holder=self.holder)
        run_doc = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
        handle = {
            "workspace": f"ws-{node}",
            "pane": f"pane-{node}",
            "agent": f"agent-{node}",
            "repo": str(repo.resolve()),
            "base": run_doc["base_sha"],
        }
        if owns_worktree:
            self._wt += 1
            wt = make_worktree(
                repo, self.root / "wt" / f"{self._wt}-{node}-{attempt}",
                f"t/{self._wt}-{node}-{attempt}", base=handle["base"],
            )
            handle["worktree_path"] = str(wt.resolve())
            handle["branch"] = git(wt, "branch", "--show-current")
        self.plant_identity(run, node, attempt, handle, model)
        run_state.append_event(
            run,
            {"type": "dispatched", "node": node, "attempt": attempt, "handle": handle, "model": model},
            holder=self.holder,
        )
        return handle


class TestDispatchAgent(RepoCase):
    def test_dispatch_appends_intent_spawns_and_records_dispatched(self):
        module = load_script("dispatch-agent.py")
        fake = FakeSpawn()
        module.run_child = fake
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
        run = str(new_run(self.root, graph, holder=self.holder))
        code, out, err = run_module(module, self.dispatch_argv(run, "H"))
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["agent"], fake.worker_id)
        types = [event["type"] for event in events_of(run) if event.get("node") == "H"]
        self.assertEqual([t for t in types if t in {"dispatch_intent", "dispatched"}], ["dispatch_intent", "dispatched"])
        dispatched = events_of(run, "dispatched")[0]
        identity = json.loads((run_state.attempt_dir(Path(run), "H", 1) / "dispatch-identity.json").read_text(encoding="utf-8"))
        self.assertEqual(dispatched["handle"]["worker_id"], fake.worker_id)
        self.assertEqual(dispatched["model"], identity["routing"])
        argv = fake.calls[0]
        self.assertEqual(fake.calls.__len__(), 1)
        self.assertIn("h", argv)
        self.assertIn("--run", argv)
        self.assertEqual(flag(argv, "--run"), run_state.canonical_path(run))
        self.assertEqual(flag(argv, "--node"), "H")
        self.assertEqual(flag(argv, "--attempt"), "1")
        self.assertEqual(flag(argv, "--session"), HOLDER["session"])
        self.assertEqual(flag(argv, "--holder-workspace"), HOLDER["workspace"])
        self.assertEqual(flag(argv, "--holder-pane"), HOLDER["pane"])
        self.assertEqual(flag(argv, "--epoch"), "1")
        self.assertIn("--prompt-file", argv)
        self.assertIn("--resume", argv)

    def test_dispatch_is_idempotent_after_partial_failure(self):
        module = load_script("dispatch-agent.py")
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}

        with self.subTest("intent already appended"):
            fake = FakeSpawn()
            module.run_child = fake
            run = str(new_run(self.root, graph, holder=self.holder))
            run_state.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1}, holder=self.holder)
            before = events_of(run, "dispatch_intent")
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(len(fake.calls), 1)
            self.assertEqual(len(events_of(run, "dispatch_intent")), len(before))
            self.assertEqual(json.loads(out)["state"], "running")
            self.assertEqual(len(events_of(run, "dispatched")), 1)

        with self.subTest("identity and launch record exist but no dispatched"):
            fake = FakeSpawn()
            module.run_child = fake
            run = str(new_run(self.root, graph, holder=self.holder))
            run_state.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1}, holder=self.holder)
            planted = FakeSpawn()
            planted( ["python", str(SCRIPTS / "spawn-agent.py"), "h", "--run", str(run), "--node", "H", "--attempt", "1"])
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(fake.calls, [])
            self.assertEqual(len(events_of(run, "dispatched")), 1)
            self.assertEqual(json.loads(out)["state"], "running")

        with self.subTest("identity exists without record, still ready"):
            fake = FakeSpawn()
            module.run_child = fake
            run = str(new_run(self.root, graph, holder=self.holder))
            run_state.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1}, holder=self.holder)
            FakeSpawn(write_record=False)(["python", str(SCRIPTS / "spawn-agent.py"), "h", "--run", str(run), "--node", "H", "--attempt", "1"])
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(fake.resumed, [True])
            self.assertEqual(len(events_of(run, "dispatched")), 1)
            self.assertEqual(json.loads(out)["state"], "running")

        with self.subTest("identity exists without record, already dispatched"):
            fake = FakeSpawn()
            module.run_child = fake
            run = str(new_run(self.root, graph, holder=self.holder))
            run_state.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1}, holder=self.holder)
            FakeSpawn(write_record=False)(["python", str(SCRIPTS / "spawn-agent.py"), "h", "--run", str(run), "--node", "H", "--attempt", "1"])
            snap = run_state.replay(run)
            event = liveness.herdr_ready_dispatch_candidate(snap["state"], "H", snap["state"]["nodes"]["H"])
            run_state.append_event(run, event, holder=self.holder)
            before = (Path(run) / "events.jsonl").read_bytes()
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(fake.calls, [])
            self.assertEqual((Path(run) / "events.jsonl").read_bytes(), before)
            payload = json.loads(out)
            self.assertEqual(payload["state"], "running")
            self.assertIsNone(payload["prompted"])

        with self.subTest("fully dispatched"):
            fake = FakeSpawn()
            module.run_child = fake
            run = str(new_run(self.root, graph, holder=self.holder))
            first = FakeSpawn()
            module.run_child = first
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            before = run_state.replay(run)["events"]
            module.run_child = fake
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(fake.calls, [])
            self.assertEqual(run_state.replay(run)["events"], before)
            self.assertEqual(json.loads(out)["state"], "running")

    def test_dispatch_refuses_unready_node(self):
        module = load_script("dispatch-agent.py")
        fake = FakeSpawn()
        module.run_child = fake
        graph = {
            "schema_version": 1,
            "nodes": {
                "P": {"id": "P", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]},
                "W": {
                    "id": "W",
                    "executor": "herdr-agent",
                    "depends_on": ["P"],
                    "outputs": [{"name": "summary", "type": "summary"}],
                },
            },
        }
        run = str(new_run(self.root, graph, holder=self.holder))
        before = run_state.replay(run)["events"]
        code, out, err = run_module(module, self.dispatch_argv(run, "W"))
        self.assertEqual(code, 3)
        self.assertRegex(err, r"planned|depend")
        self.assertEqual(fake.calls, [])
        self.assertEqual(run_state.replay(run)["events"], before)

    def test_dispatch_alias_matches_spawn_name_re(self):
        module = load_script("dispatch-agent.py")

        def dispatch(node, *, name=None):
            fake = FakeSpawn()
            module.run_child = fake
            graph = {"schema_version": 1, "nodes": {node: {"id": node, "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
            run = str(new_run(self.root, graph, holder=self.holder))
            code, out, err = run_module(module, self.dispatch_argv(run, node, name=name))
            self.assertEqual(code, 0, err)
            self.assertTrue(fake.calls)
            return fake.calls[0][2]

        self.assertEqual(dispatch("Node.1"), "node-1")
        self.assertEqual(dispatch("9x"), "n-9x")
        self.assertEqual(dispatch("H", name="custom-alias"), "custom-alias")

    def test_dispatch_rejects_wrong_epoch_before_mutating(self):
        module = load_script("dispatch-agent.py")
        fake = FakeSpawn()
        module.run_child = fake
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
        run = str(new_run(self.root, graph, holder=self.holder))
        before = (Path(run) / "events.jsonl").read_bytes()
        code, out, err = run_module(module, self.dispatch_argv(run, "H", epoch=2))
        self.assertEqual(code, 3)
        self.assertIn("epoch 2", err)
        self.assertIn("epoch 1", err)
        self.assertEqual((Path(run) / "events.jsonl").read_bytes(), before)
        self.assertEqual(fake.calls, [])

    def test_dispatch_events_carry_caller_epoch(self):
        module = load_script("dispatch-agent.py")
        module.run_child = FakeSpawn()
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
        run = str(new_run(self.root, graph, holder=self.holder))
        code, out, err = run_module(module, self.dispatch_argv(run, "H"))
        self.assertEqual(code, 0, err)
        intent = [e for e in events_of(run, "dispatch_intent") if e.get("node") == "H"][-1]
        dispatched = [e for e in events_of(run, "dispatched") if e.get("node") == "H"][-1]
        self.assertEqual(intent["epoch"], 1)
        self.assertEqual(dispatched["epoch"], 1)

    def test_dispatch_resumes_incomplete_launch_before_dispatched(self):
        module = load_script("dispatch-agent.py")
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
        plant_argv = ["python", str(SCRIPTS / "spawn-agent.py"), "h", "--run", None, "--node", "H", "--attempt", "1"]

        with self.subTest("identity present, no launch record"):
            fake = FakeSpawn()
            module.run_child = fake
            run = str(new_run(self.root, graph, holder=self.holder))
            run_state.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1}, holder=self.holder)
            plant_argv[4] = str(run)
            FakeSpawn(write_record=False)(plant_argv)
            attempt_dir = run_state.attempt_dir(Path(run), "H", 1)
            self.assertFalse((attempt_dir / "launch-complete.json").is_file())
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(len(fake.calls), 1)
            self.assertIn("--resume", fake.calls[0])
            self.assertTrue(fake.resumed[0])
            self.assertTrue((attempt_dir / "launch-complete.json").is_file())
            self.assertEqual(len(events_of(run, "dispatched")), 1)

        with self.subTest("spawn exits 0 without record"):
            fake = FakeSpawn(write_record=False)
            module.run_child = fake
            run = str(new_run(self.root, graph, holder=self.holder))
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 3)
            self.assertIn("launch record missing", err)
            self.assertEqual(len(events_of(run, "dispatched")), 0)

        with self.subTest("identity and record present"):
            fake = FakeSpawn()
            module.run_child = fake
            run = str(new_run(self.root, graph, holder=self.holder))
            run_state.append_event(run, {"type": "dispatch_intent", "node": "H", "attempt": 1}, holder=self.holder)
            plant_argv[4] = str(run)
            FakeSpawn()(plant_argv)
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(fake.calls, [])
            self.assertEqual(len(events_of(run, "dispatched")), 1)

    def test_dispatch_records_blocked_launch(self):
        module = load_script("dispatch-agent.py")
        fake = FakeSpawn(prompted=False, blocked=True)
        module.run_child = fake
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
        run = str(new_run(self.root, graph, holder=self.holder))
        code, out, err = run_module(module, self.dispatch_argv(run, "H"))
        self.assertEqual(code, 0, err)
        node_events = [e for e in events_of(run) if e.get("node") == "H"]
        self.assertEqual([e["type"] for e in node_events[-2:]], ["dispatched", "blocked"])
        self.assertEqual(node_events[-1]["reason"], "agent_not_ready")
        payload = json.loads(out)
        self.assertEqual(payload["state"], "blocked")
        self.assertTrue(payload["blocked"])

    def test_dispatch_returns_legacy_running_attempt_without_launch_record(self):
        module = load_script("dispatch-agent.py")
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
        run = str(new_run(self.root, graph, holder=self.holder))
        module.run_child = FakeSpawn()
        code, out, err = run_module(module, self.dispatch_argv(run, "H"))
        self.assertEqual(code, 0, err)
        record_path = run_state.attempt_dir(Path(run), "H", 1) / "launch-complete.json"
        record_path.unlink()
        events_path = Path(run) / "events.jsonl"

        with self.subTest("running"):
            fake = FakeSpawn()
            module.run_child = fake
            before = events_path.read_bytes()
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["state"], "running")
            self.assertIsNone(payload["prompted"])
            self.assertIsNone(payload["blocked"])
            self.assertEqual(fake.calls, [])
            self.assertEqual(events_path.read_bytes(), before)
            self.assertFalse(record_path.exists())

        with self.subTest("terminal"):
            run_state.append_event(run, {"type": "failed", "node": "H", "attempt": 1, "reason": "boom"}, holder=self.holder)
            fake = FakeSpawn()
            module.run_child = fake
            before = events_path.read_bytes()
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["state"], "failed")
            self.assertEqual(fake.calls, [])
            self.assertEqual(events_path.read_bytes(), before)

    def test_dispatch_replays_missing_blocked_edge(self):
        module = load_script("dispatch-agent.py")
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
        run = str(new_run(self.root, graph, holder=self.holder))
        module.run_child = FakeSpawn(prompted=False, blocked=True)

        def blocked_events():
            return [e for e in events_of(run, "blocked") if e.get("node") == "H" and e.get("attempt") == 1]

        real_append = module.ledger.append_event

        def crash_before_blocked(run_dir, event, **kwargs):
            if event.get("type") == "blocked":
                raise SystemExit(1)
            return real_append(run_dir, event, **kwargs)

        module.ledger.append_event = crash_before_blocked
        try:
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
        finally:
            module.ledger.append_event = real_append
        self.assertEqual(code, 1)
        self.assertEqual(len(events_of(run, "dispatched")), 1)
        self.assertEqual(blocked_events(), [])
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["H"]["state"], "running")

        with self.subTest("replays one blocked"):
            fake = FakeSpawn()
            module.run_child = fake
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["state"], "blocked")
            self.assertTrue(payload["blocked"])
            self.assertEqual(fake.calls, [])
            self.assertEqual(len(blocked_events()), 1)
            self.assertEqual(blocked_events()[0]["reason"], "agent_not_ready")

        with self.subTest("second replay appends nothing"):
            before = (Path(run) / "events.jsonl").read_bytes()
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["state"], "blocked")
            self.assertEqual((Path(run) / "events.jsonl").read_bytes(), before)

        with self.subTest("continuation to running appends nothing"):
            node = run_state.replay(run)["state"]["nodes"]["H"]
            identity = json.loads((run_state.attempt_dir(Path(run), "H", 1) / "dispatch-identity.json").read_text(encoding="utf-8"))
            live = {
                "identity_kind": run_state.IDENTITY_MANAGED_NAME,
                "worker_id": identity["worker_id"],
                "workspace": identity["workspace"],
                "pane": identity["pane"],
                "tokens": identity["tokens"],
                "routing": identity["routing"],
            }
            run_state.append_event(run, {
                "type": "attempt_started", "node": "H", "attempt": 1, "from": "blocked", "to": "running",
                "handle": node["handle"], "model": identity["routing"], "live_identity": live,
            }, holder=self.holder)
            before = (Path(run) / "events.jsonl").read_bytes()
            code, out, err = run_module(module, self.dispatch_argv(run, "H"))
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["state"], "running")
            self.assertEqual(fake.calls, [])
            self.assertEqual(len(blocked_events()), 1)
            self.assertEqual((Path(run) / "events.jsonl").read_bytes(), before)


class TestFinishAgent(RepoCase):
    def writer_graph(self):
        return {
            "schema_version": 1,
            "nodes": {
                "W": {
                    "id": "W",
                    "executor": "herdr-agent",
                    "outputs": [{"name": "summary", "type": "summary"}, {"name": "vcs", "type": "vcs"}],
                    "worktree": "w",
                    "owns": ["src/w/**"],
                }
            },
        }

    def test_finish_writer_publishes_vcs_and_succeeds(self):
        module = load_script("finish-agent.py")
        fake = FakeCollect()
        module.run_child = fake
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
        handle = self.start_writer(run, "W", repo)
        commit_file(Path(handle["worktree_path"]), "src/w/owned.txt", "ok\n", "src/w/owned.txt")
        code, out, err = run_module(module, self.finish_argv(run, "W", 1))
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "succeeded")
        self.assertEqual(payload["vcs_form"], "commit")
        succeeded = events_of(run, "succeeded")
        self.assertEqual(len(succeeded), 1)
        self.assertEqual(succeeded[0]["artifacts"], payload["artifacts"])
        before = run_state.replay(run)["events"]
        code, out, err = run_module(module, self.finish_argv(run, "W", 1))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["state"], "succeeded")
        self.assertEqual(run_state.replay(run)["events"], before)

    def test_finish_force_keeps_receipts_for_foreign_holder(self):
        module = load_script("finish-agent.py")
        module.run_child = FakeCollect()
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
        handle = self.start_writer(run, "W", repo)
        commit_file(Path(handle["worktree_path"]), "src/w/owned.txt", "ok\n", "src/w/owned.txt")
        stale = run_state.attempt_dir(Path(run), "W", 1) / "vcs.json"
        stale.write_text("{}\n", encoding="utf-8")
        foreign = {**self.holder, "pane": "other-pane"}
        argv = [
            "finish-agent.py",
            "--run", str(run),
            "--node", "W",
            "--attempt", "1",
            *holder_args(foreign),
            "--force",
        ]
        code, out, err = run_module(module, argv)
        self.assertEqual(code, 3)
        self.assertIn("lease", err)
        self.assertTrue(stale.is_file())

    def test_finish_refuses_dirty_unowned_and_wrong_since(self):
        module = load_script("finish-agent.py")
        module.run_child = FakeCollect()

        with self.subTest("dirty"):
            repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
            run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
            handle = self.start_writer(run, "W", repo)
            commit_file(Path(handle["worktree_path"]), "src/w/owned.txt", "ok\n", "src/w/owned.txt")
            (Path(handle["worktree_path"]) / "src/w/dirty.txt").write_text("dirty\n", encoding="utf-8")
            before = events_of(run, "succeeded")
            code, out, err = run_module(module, self.finish_argv(run, "W", 1))
            self.assertEqual(code, 3)
            self.assertIn("dirty", err)
            self.assertEqual(events_of(run, "succeeded"), before)

        with self.subTest("unowned"):
            repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
            run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
            handle = self.start_writer(run, "W", repo)
            commit_file(Path(handle["worktree_path"]), "outside.txt", "nope\n", "outside.txt")
            before = events_of(run, "succeeded")
            code, out, err = run_module(module, self.finish_argv(run, "W", 1))
            self.assertEqual(code, 3)
            self.assertIn("unowned", err)
            payload = json.loads(out)
            abs_outside = str(Path(handle["worktree_path"], "outside.txt"))
            self.assertTrue(any(item.endswith("outside.txt") or item == abs_outside for item in payload["unowned"]))
            self.assertEqual(events_of(run, "succeeded"), before)

        with self.subTest("does not descend"):
            repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
            run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
            handle = self.start_writer(run, "W", repo)
            commit_file(Path(handle["worktree_path"]), "src/w/owned.txt", "ok\n", "src/w/owned.txt")
            tree = git(repo, "rev-parse", "HEAD^{tree}")
            orphan = subprocess.run(
                ["git", "-C", str(repo), "commit-tree", tree, "-m", "orphan"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            before = events_of(run, "succeeded")
            code, out, err = run_module(module, self.finish_argv(run, "W", 1, since=orphan))
            self.assertEqual(code, 3)
            self.assertIn("does not descend", err)
            self.assertEqual(events_of(run, "succeeded"), before)

    def test_finish_integrator_writes_integration_and_prints_promote(self):
        module = load_script("finish-agent.py")
        module.run_child = FakeCollect()
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        graph = {
            "schema_version": 1,
            "nodes": {
                "W": {
                    "id": "W",
                    "executor": "herdr-agent",
                    "outputs": [{"name": "vcs", "type": "vcs"}],
                    "worktree": "w",
                    "owns": ["src/w/**"],
                },
                "D": {
                    "id": "D",
                    "executor": "herdr-agent",
                    "depends_on": ["W"],
                    "inputs": [{"alias": "patch", "from": "W", "output": "vcs"}],
                    "outputs": [
                        {"name": "summary", "type": "summary"},
                        {"name": "vcs", "type": "vcs"},
                        {"name": "integration", "type": "integration"},
                    ],
                    "worktree": "d",
                    "owns": ["src/d/**"],
                },
            },
        }
        run = str(new_run(self.root, graph, repo=repo, holder=self.holder))
        w_handle = self.start_writer(run, "W", repo)
        commit_file(Path(w_handle["worktree_path"]), "src/w/owned.txt", "w\n", "src/w/owned.txt")
        finish = load_script("finish-agent.py")
        finish.run_child = FakeCollect()
        code, out, err = run_module(finish, self.finish_argv(run, "W", 1))
        self.assertEqual(code, 0, err)
        run_state.reconcile(run, holder=self.holder)
        d_handle = self.start_writer(run, "D", repo)
        commit_file(Path(d_handle["worktree_path"]), "src/d/merged.txt", "d\n", "src/d/merged.txt")
        head = git(Path(d_handle["worktree_path"]), "rev-parse", "HEAD")
        code, out, err = run_module(module, self.finish_argv(run, "D", 1))
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "running")
        self.assertIsNotNone(payload["promote"])
        self.assertIn("run_state.promote(", payload["promote"])
        self.assertIn(run_state.canonical_path(run), payload["promote"])
        self.assertIn('node="D"', payload["promote"])
        self.assertIn("attempt=1", payload["promote"])
        self.assertIn(head, payload["promote"])
        self.assertIn(str(repo.resolve()), payload["promote"])
        self.assertFalse(any(event.get("node") == "D" and event.get("type") == "succeeded" for event in events_of(run)))
        integration = json.loads((run_state.attempt_dir(Path(run), "D", 1) / "integration.json").read_text(encoding="utf-8"))
        self.assertEqual(integration["commit"], head)
        self.assertEqual(integration["head"], head)
        consumed = json.loads((run_state.attempt_dir(Path(run), "D", 1) / "consumed_artifacts.json").read_text(encoding="utf-8"))
        vcs_items = run_state.vcs_typed_consumed(json.loads((Path(run) / "graph.json").read_text(encoding="utf-8")), consumed)
        self.assertEqual([item["hash"] for item in integration["consumed"]], [item["hash"] for item in vcs_items])
        self.assertTrue(integration["consumed"])

    def test_finish_refuses_a_writer_that_committed_nothing(self):
        # run 20260918-220934: T and C declared worktrees, committed nothing because
        # their prompts never ran, and were still recorded succeeded at form none.
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
        self.start_writer(run, "W", repo)
        module = load_script("finish-agent.py")
        module.run_child = FakeCollect(form="none")
        code, out, err = run_module(module, self.finish_argv(run, "W", 1))
        self.assertEqual(code, 3)
        self.assertIn("committed nothing", err)
        self.assertEqual(json.loads(out)["vcs_form"], "none")
        self.assertEqual(events_of(run, "succeeded"), [])

        code, out, err = run_module(module, self.finish_argv(run, "W", 1, force=True))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["vcs_form"], "none")
        self.assertEqual(len(events_of(run, "succeeded")), 1)

    def test_finish_report_node_succeeds_without_a_worktree(self):
        module = load_script("finish-agent.py")
        module.run_child = FakeCollect()
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        graph = {
            "schema_version": 1,
            "nodes": {"R": {"id": "R", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}},
        }
        run = str(new_run(self.root, graph, repo=repo, holder=self.holder))
        self.start_writer(run, "R", repo, owns_worktree=False)
        code, out, err = run_module(module, self.finish_argv(run, "R", 1))
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["state"], "succeeded")
        self.assertIsNone(payload["vcs_form"])

    def test_finish_force_recollects_an_already_succeeded_attempt(self):
        # run 20260918-220934: `finish-agent.py --force` on a succeeded attempt exited 0,
        # reprinted byte-identical hashes and appended no artifact_forced.
        module = load_script("finish-agent.py")
        fake = FakeCollect()
        module.run_child = fake
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
        handle = self.start_writer(run, "W", repo)
        commit_file(Path(handle["worktree_path"]), "src/w/owned.txt", "ok\n", "src/w/owned.txt")
        code, out, err = run_module(module, self.finish_argv(run, "W", 1))
        self.assertEqual(code, 0, err)
        stale = json.loads(out)["artifacts"]
        fake.calls.clear()

        summary = run_state.attempt_dir(Path(run), "W", 1) / "summary.json"
        summary.write_text(json.dumps({"status": "the work it really did"}, sort_keys=True) + "\n", encoding="utf-8")

        code, out, err = run_module(module, self.finish_argv(run, "W", 1, force=True))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("--force", fake.calls[0])
        payload = json.loads(out)
        self.assertEqual(payload["state"], "succeeded")
        self.assertNotEqual(payload["artifacts"], stale)
        self.assertEqual(len(events_of(run, "succeeded")), 1)

    def test_finish_defaults_since_to_promoted_branch_point(self):
        module = load_script("finish-agent.py")
        module.run_child = FakeCollect()
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
        run_doc = json.loads((Path(run) / "run.json").read_text(encoding="utf-8"))
        promoted = commit_file(repo, "outside.txt", "nope\n", "outside.txt")
        git(repo, "update-ref", run_doc["promotion_ref"], promoted)
        handle = self.start_writer(run, "W", repo)
        wt = Path(handle["worktree_path"])
        git(wt, "reset", "--hard", promoted)
        commit_file(wt, "src/w/owned.txt", "ok\n", "src/w/owned.txt")

        code, out, err = run_module(module, self.finish_argv(run, "W", 1, since=run_doc["base_sha"]))
        self.assertEqual(code, 3)
        self.assertIn("unowned files", err)
        self.assertIn("outside.txt", err)
        self.assertEqual(events_of(run, "succeeded"), [])

        code, out, err = run_module(module, self.finish_argv(run, "W", 1))
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["since"], promoted)
        self.assertEqual(payload["state"], "succeeded")
        self.assertEqual(len(events_of(run, "succeeded")), 1)

    def integrator_graph(self):
        return {
            "schema_version": 1,
            "nodes": {
                "D": {
                    "id": "D",
                    "executor": "herdr-agent",
                    "outputs": [
                        {"name": "summary", "type": "summary"},
                        {"name": "vcs", "type": "vcs"},
                        {"name": "integration", "type": "integration"},
                    ],
                    "worktree": "d",
                    "owns": ["src/d/**"],
                }
            },
        }

    def test_finish_filters_artifacts_to_declared_outputs(self):
        module = load_script("finish-agent.py")
        module.run_child = FakeCollect()

        with self.subTest("vcs only"):
            repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
            graph = {
                "schema_version": 1,
                "nodes": {
                    "W": {
                        "id": "W",
                        "executor": "herdr-agent",
                        "outputs": [{"name": "vcs", "type": "vcs"}],
                        "worktree": "w",
                        "owns": ["src/w/**"],
                    }
                },
            }
            run = str(new_run(self.root, graph, repo=repo, holder=self.holder))
            handle = self.start_writer(run, "W", repo)
            commit_file(Path(handle["worktree_path"]), "src/w/owned.txt", "ok\n", "src/w/owned.txt")
            code, out, err = run_module(module, self.finish_argv(run, "W", 1))
            self.assertEqual(code, 0, err)
            artifacts = events_of(run, "succeeded")[0]["artifacts"]
            self.assertEqual([run_state.artifact_name(row) for row in artifacts], ["vcs"])

        with self.subTest("summary and vcs"):
            repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
            run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
            handle = self.start_writer(run, "W", repo)
            commit_file(Path(handle["worktree_path"]), "src/w/owned.txt", "ok\n", "src/w/owned.txt")
            code, out, err = run_module(module, self.finish_argv(run, "W", 1))
            self.assertEqual(code, 0, err)
            artifacts = events_of(run, "succeeded")[0]["artifacts"]
            self.assertEqual([run_state.artifact_name(row) for row in artifacts], ["summary", "vcs"])

    def test_finish_requires_holder_for_integrators(self):
        module = load_script("finish-agent.py")
        module.run_child = FakeCollect()
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        run = str(new_run(self.root, self.integrator_graph(), repo=repo, holder=self.holder))
        handle = self.start_writer(run, "D", repo)
        commit_file(Path(handle["worktree_path"]), "src/d/merged.txt", "d\n", "src/d/merged.txt")
        attempt_dir = run_state.attempt_dir(Path(run), "D", 1)
        code, out, err = run_module(
            module,
            ["finish-agent.py", "--run", str(run), "--node", "D", "--attempt", "1"],
        )
        self.assertEqual(code, 2)
        self.assertFalse((attempt_dir / "integration.json").exists())
        self.assertFalse((attempt_dir / "vcs.json").exists())

        code, out, err = run_module(module, self.finish_argv(run, "D", 1))
        self.assertEqual(code, 0, err)
        promote = json.loads(out)["promote"]
        self.assertIn('"workspace": ', promote)
        self.assertNotIn("{…}", promote)

    def test_finish_rejects_integrator_without_commit_before_receipts(self):
        module = load_script("finish-agent.py")
        module.run_child = FakeCollect()
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        run = str(new_run(self.root, self.integrator_graph(), repo=repo, holder=self.holder))
        handle = self.start_writer(run, "D", repo)
        attempt_dir = run_state.attempt_dir(Path(run), "D", 1)
        code, out, err = run_module(module, self.finish_argv(run, "D", 1))
        self.assertEqual(code, 3)
        self.assertIn("no commit beyond the promoted line", err)
        self.assertFalse((attempt_dir / "integration.json").exists())
        self.assertFalse((attempt_dir / "vcs.json").exists())

        commit_file(Path(handle["worktree_path"]), "src/d/merged.txt", "d\n", "src/d/merged.txt")
        code, out, err = run_module(module, self.finish_argv(run, "D", 1))
        self.assertEqual(code, 0, err)
        self.assertTrue((attempt_dir / "integration.json").is_file())
        self.assertTrue((attempt_dir / "vcs.json").is_file())

    def test_finish_propagates_collector_exit_code(self):
        module = load_script("finish-agent.py")
        repo = seed_repo(self.root / f"repo-{len(list(self.root.glob('repo-*')))}")
        run = str(new_run(self.root, self.writer_graph(), repo=repo, holder=self.holder))
        handle = self.start_writer(run, "W", repo)
        commit_file(Path(handle["worktree_path"]), "src/w/owned.txt", "ok\n", "src/w/owned.txt")
        for rc in (1, 2):
            with self.subTest(rc=rc):
                def boom(_argv, _child_name, *, timeout=None, code=rc):
                    raise ChildFailed(code, "")
                module.run_child = boom
                code, out, err = run_module(module, self.finish_argv(run, "W", 1))
                self.assertEqual(code, rc)


if __name__ == "__main__":
    unittest.main()
