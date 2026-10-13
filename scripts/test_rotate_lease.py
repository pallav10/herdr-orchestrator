#!/usr/bin/env python3
"""Focused lease-transfer regression checks for rotate/ack."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import ioutil
import run_state
import testkit


def load(filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCE = "OMP ExtensionContext.getContextUsage"
EXTENSION = Path(os.environ.get("HERDR_TEST_OMP_EXTENSION", Path.home() / ".omp/agent/extensions/herdr-omp-agent-state.ts"))
OMP_CLI = Path(os.environ.get("HERDR_TEST_OMP_CLI", Path.home() / "node_modules/@oh-my-pi/pi-coding-agent/dist/cli.js"))
BUN = os.environ.get("HERDR_TEST_BUN", shutil.which("bun"))
READONLY_HERDR = {("workspace", "get"), ("pane", "get"), ("pane", "process-info")}


def legacy_context_reader(session, holder):
    """Valid measured reading at exactly 50%. Mocks the reader, not the gate."""
    return {
        "source": SOURCE,
        "tokens": 1,
        "context_window": 2,
        "fraction": 0.5,
        "snapshot": str(session),
        "observed_at_ms": time.time_ns() // 1_000_000,
        "omp_session_file": str(session),
        "omp_session_id": "legacy",
        "model": {"provider": "test", "id": "test"},
    }


def run_cli(module, argv: list[str], *, context_reader: object = "legacy") -> tuple[int, str, str]:
    with contextlib.ExitStack() as patches:
        if context_reader == "legacy" and argv and argv[0] == "rotate.py" and hasattr(module, "context_usage"):
            patches.enter_context(mock.patch.object(module.context_usage, "read_context_usage", legacy_context_reader))
        elif callable(context_reader):
            patches.enter_context(mock.patch.object(module.context_usage, "read_context_usage", context_reader))
        return testkit.run_module(module, argv)




def ack_workspace(nonce: str, session: str) -> dict:
    return {"result": {"workspace": {"tokens": {"handoff-ack": nonce, "session": session}}}}


class LeaseFixtures:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.docs = self.root / "docs"

    def patch_object(self, obj, name, value):
        patcher = mock.patch.object(obj, name, value)
        replacement = patcher.start()
        self.addCleanup(patcher.stop)
        return replacement

    def new_run(self, graph: dict | None = None, *, bind: bool = True, holder: dict | None = None) -> Path:
        run = testkit.new_run(
            self.root, None,
            holder=(holder or self.holder(self.docs / "sessions" / "pred")) if bind else None,
        )
        if graph is not None:
            (run / "graph.json").write_text(json.dumps(graph) + "\n", encoding="utf-8")
        return run

    def session(self, run: Path, *, name: str = "pred", workspace: str = "old-ws", pane: str = "old-pane") -> Path:
        session = (self.docs / "sessions" / name).resolve()
        session.mkdir(parents=True)
        (session / "progress.md").write_text("progress\n", encoding="utf-8")
        (session / "HANDOFF.md").write_text(f"## Run\n{run}\n", encoding="utf-8")
        (session / "NEXT-SESSION-PROMPT.md").write_text("next\n", encoding="utf-8")
        (session / "session.json").write_text(json.dumps({
            "id": name,
            "job": "Job",
            "owner": workspace,
            "workspace": workspace,
            "pane": pane,
            "run": str(run),
        }) + "\n", encoding="utf-8")
        return session

    def events(self, run: Path) -> list[dict]:
        return [json.loads(line) for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def event_types(self, run: Path) -> list[str]:
        return [event["type"] for event in self.events(run)]

    def holder(self, session: Path, *, workspace: str = "old-ws", pane: str = "old-pane") -> dict:
        return {"workspace": workspace, "pane": pane, "session": str(session.resolve())}

    def start_attempt(self, run: Path, *, holder: dict, node: str = "A", attempt: int = 1, handle: dict | None = None) -> None:
        run_state.reconcile(run, holder=holder)
        run_state.append_event(run, {"type": "dispatch_intent", "node": node, "attempt": attempt}, holder=holder)
        run_state.append_event(
            run,
            {
                "type": "dispatched",
                "node": node,
                "attempt": attempt,
                "handle": handle or {"spawning_session": holder["session"], "job_id": f"{node}-{attempt}"},
                "model": {
                    "requested_role": None,
                    "requested_effort": None,
                    "resolved": None,
                    "thinking": None,
                    "effort": None,
                },
            },
            holder=holder,
        )



class RotateLeaseTest(LeaseFixtures, unittest.TestCase):
    def test_ten_unchanged_ticks_observe_external_terminal_with_two_reconciles(self):
        run = self.new_run({"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}})
        session = self.session(run)
        holder = self.holder(session)
        self.start_attempt(run, node="A", holder=holder)
        module = load("rotate.py")
        module.start_lease_drain(run, holder)
        ticks = []
        calls = []
        original = module.scheduler.reconcile

        def observe(run=None, *, lock_timeout=120):
            ticks.append(1)
            if len(ticks) == 10:
                run_state.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1}, holder=holder)
            return []

        def reconcile(run, **kwargs):
            calls.append(1)
            return original(run, **kwargs)

        with mock.patch.object(module.reconcile, "observe_live_herdr", observe), mock.patch.object(module.scheduler, "reconcile", reconcile), mock.patch.object(module.time, "sleep", lambda *_: None):
            module.ensure_native_quiesced(run, holder, 30)
        self.assertEqual(len(ticks), 10)
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["A"]["state"], "succeeded")

    def test_rotate_times_out_after_drain_without_spawning_while_executor_runs(self):
        for executor, node in (("task", "A"), ("workflow", "W")):
            with self.subTest(executor=executor), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as patches:
                self.addCleanup(patches.close)
                self.root = Path(tmp).resolve()
                self.docs = self.root / "docs"
                graph = {"schema_version": 1, "nodes": {node: {"id": node, "executor": executor}}}
                run = self.new_run(graph)
                session = self.session(run)
                handle = {"spawning_session": str(session.resolve()), "runId": "wf-1"} if executor == "workflow" else None
                self.start_attempt(run, holder=self.holder(session), node=node, handle=handle)
                module = load("rotate.py")
                calls = []
                seen = []
                original = module.scheduler.reconcile

                def reconcile(run_dir, **kwargs):
                    seen.append(kwargs)
                    return original(run_dir, **kwargs)

                patches.enter_context(mock.patch.object(module.ioutil, 'run_cmd', lambda cmd, **kwargs: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "{}", "")))
                patches.enter_context(mock.patch.object(module, 'herdr', lambda *_: {"result": {"workspace": {"tokens": {}}}}))
                patches.enter_context(mock.patch.object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: []))
                patches.enter_context(mock.patch.object(module.scheduler, 'reconcile', reconcile))
                with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
                    code, _, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
                self.assertEqual(code, 3, err)
                self.assertEqual(calls, [])
                self.assertEqual(run_state.replay(run)["controller"]["lease"], "draining")
                self.assertTrue(seen)
                self.assertTrue(all(item["live_native_ids"] is None for item in seen))
                self.assertTrue(all(not item["native_inventory_complete"] for item in seen))
                self.assertIn("lease_drain_started", self.event_types(run))
                self.assertNotIn("lease_transfer", self.event_types(run))
                self.assertNotIn("interrupted", self.event_types(run))
                self.assertEqual(run_state.replay(run)["state"]["nodes"][node]["state"], "running")


    def test_rotate_waits_for_native_terminal_before_spawning_successor(self):
        graph = {"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "task"}}}
        run = self.new_run(graph)
        session = self.session(run)
        holder = self.holder(session)
        self.start_attempt(run, holder=holder)
        run_state.append_event(run, {"type": "succeeded", "node": "A", "attempt": 1, "artifacts": []}, holder=holder)
        module = load("rotate.py")
        captured = []
        nonce = "n" * 32
        successor_session = str((self.docs / "sessions" / "succ").resolve())

        def spawn(cmd, **_):
            captured.append(list(cmd))
            run_state.append_event(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": successor_session,
                    "nonce": nonce,
                },
                holder=self.holder(session),
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        self.patch_object(module.ioutil, 'run_cmd', spawn)
        self.patch_object(module, 'herdr', lambda *_: ack_workspace(nonce, successor_session))
        self.patch_object(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: [])
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, _, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(captured), 1)
        self.assertLess(self.event_types(run).index("succeeded"), self.event_types(run).index("lease_transfer"))

    def test_rotate_refuses_ambiguous_executor_dispatch_intent_without_spawning(self):
        for executor, node in (("task", "A"), ("workflow", "W")):
            with self.subTest(executor=executor), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as patches:
                self.addCleanup(patches.close)
                self.root = Path(tmp).resolve()
                self.docs = self.root / "docs"
                graph = {"schema_version": 1, "nodes": {node: {"id": node, "executor": executor}}}
                run = self.new_run(graph)
                session = self.session(run)
                holder = self.holder(session)
                run_state.reconcile(run, holder=holder)
                run_state.append_event(run, {"type": "dispatch_intent", "node": node, "attempt": 1}, holder=holder)
                module = load("rotate.py")
                calls = []
                patches.enter_context(mock.patch.object(module.ioutil, 'run_cmd', lambda cmd, **kwargs: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "{}", "")))
                patches.enter_context(mock.patch.object(module, 'herdr', lambda *_: {"result": {"workspace": {"tokens": {}}}}))
                patches.enter_context(mock.patch.object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: []))
                with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
                    code, _, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
                self.assertEqual(code, 3, err)
                self.assertEqual(calls, [])
                self.assertIn("lease_drain_started", self.event_types(run))
                self.assertNotIn("lease_transfer", self.event_types(run))


    def test_unknown_herdr_inventory_with_active_herdr_fails_closed_without_spawn(self):
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent"}}}
        run = self.new_run(graph)
        session = self.session(run)
        holder = self.holder(session)
        self.start_attempt(
            run,
            holder=holder,
            node="H",
            handle={"workspace": "worker-ws", "pane": "worker-pane", "agent": "worker-agent"},
        )
        module = load("rotate.py")
        calls = []
        seen = []
        original = SimpleNamespace(reconcile=module.scheduler.reconcile)
        self.patch_object(module.ioutil, 'run_cmd', lambda cmd, **kwargs: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "{}", ""))
        self.patch_object(module, 'herdr', lambda *_: {"result": {"workspace": {"tokens": {}}}})
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: None)

        def reconcile(run_dir, **kwargs):
            seen.append(kwargs)
            return original.reconcile(run_dir, **kwargs)

        self.patch_object(module.scheduler, 'reconcile', reconcile)
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, _, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 3, err)
        self.assertIn("active Herdr executors cannot be observed", err)
        self.assertEqual(calls, [])
        self.assertTrue(seen)
        self.assertTrue(all(item["live_herdr"] is None for item in seen))
        self.assertTrue(all(item["live_native_ids"] is None for item in seen))
        self.assertTrue(all(not item["native_inventory_complete"] for item in seen))
        self.assertIn("lease_drain_started", self.event_types(run))
        self.assertNotIn("lease_transfer", self.event_types(run))
        self.assertNotIn("interrupted", self.event_types(run))
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["H"]["state"], "running")

    def test_unknown_herdr_inventory_without_active_herdr_progresses(self):
        run = self.new_run({"schema_version": 1, "nodes": {}})
        session = self.session(run)
        holder = self.holder(session)
        module = load("rotate.py")
        captured = []
        seen = []
        original = SimpleNamespace(reconcile=module.scheduler.reconcile)
        nonce = "n" * 32
        successor_session = str((self.docs / "sessions" / "succ").resolve())

        def spawn(cmd, **_):
            captured.append(list(cmd))
            run_state.append_event(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": successor_session,
                    "nonce": nonce,
                },
                holder=holder,
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        def reconcile(run_dir, **kwargs):
            seen.append(kwargs)
            return original.reconcile(run_dir, **kwargs)

        self.patch_object(module.ioutil, 'run_cmd', spawn)
        self.patch_object(module, 'herdr', lambda *_: ack_workspace(nonce, Path(successor_session).name))
        self.patch_object(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: None)
        self.patch_object(module.scheduler, 'reconcile', reconcile)
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, _, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(captured), 1)
        self.assertTrue(seen)
        self.assertTrue(all(item["live_herdr"] is None for item in seen))
        self.assertTrue(all(item["live_native_ids"] is None for item in seen))
        self.assertTrue(all(not item["native_inventory_complete"] for item in seen))
        self.assertNotIn("interrupted", self.event_types(run))
        self.assertIn("lease_transfer", self.event_types(run))

    def test_empty_herdr_inventory_interrupts_vanished_attempt_then_progresses(self):
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent"}}}
        run = self.new_run(graph)
        session = self.session(run)
        holder = self.holder(session)
        self.start_attempt(
            run,
            holder=holder,
            node="H",
            handle={"workspace": "worker-ws", "pane": "worker-pane", "agent": "worker-agent"},
        )
        module = load("rotate.py")
        captured = []
        seen = []
        original = SimpleNamespace(reconcile=module.scheduler.reconcile)
        nonce = "n" * 32
        successor_session = str((self.docs / "sessions" / "succ").resolve())

        def spawn(cmd, **_):
            captured.append(list(cmd))
            run_state.append_event(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": successor_session,
                    "nonce": nonce,
                },
                holder=holder,
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        def reconcile(run_dir, **kwargs):
            seen.append(kwargs)
            return original.reconcile(run_dir, **kwargs)

        self.patch_object(module.ioutil, 'run_cmd', spawn)
        self.patch_object(module, 'herdr', lambda *_: ack_workspace(nonce, successor_session))
        self.patch_object(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: [])
        self.patch_object(module.scheduler, 'reconcile', reconcile)
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, _, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(captured), 1)
        self.assertTrue(seen)
        self.assertTrue(all(item["live_herdr"] == [] for item in seen))
        self.assertTrue(all(item["live_native_ids"] is None for item in seen))
        self.assertTrue(all(not item["native_inventory_complete"] for item in seen))
        self.assertIn("interrupted", self.event_types(run))
        self.assertLess(self.event_types(run).index("interrupted"), self.event_types(run).index("lease_transfer"))
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["H"]["state"], "interrupted")

    def test_rotate_retains_live_herdr_without_duplicate_dispatch_intent(self):
        graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent"}}}
        run = self.new_run(graph)
        session = self.session(run)
        holder = self.holder(session)
        run_id = json.loads((run / "run.json").read_text(encoding="utf-8"))["id"]
        tokens = {"provenance": "orchestrator", "run": run_id, "node": "H", "attempt": "1"}
        self.start_attempt(
            run,
            holder=holder,
            node="H",
            handle={
                "workspace": "worker-ws",
                "pane": "worker-pane",
                "agent": "worker-agent",
                "tokens": tokens,
            },
        )
        live = [{
            "workspace": "worker-ws",
            "pane": "worker-pane",
            "agent": "worker-agent",
            "tokens": dict(tokens),
        }]
        module = load("rotate.py")
        captured = []
        seen = []
        original = SimpleNamespace(reconcile=module.scheduler.reconcile)
        nonce = "n" * 32
        successor_session = str((self.docs / "sessions" / "succ").resolve())

        def spawn(cmd, **_):
            captured.append(list(cmd))
            run_state.append_event(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": successor_session,
                    "nonce": nonce,
                },
                holder=holder,
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        def reconcile(run_dir, **kwargs):
            seen.append(kwargs)
            return original.reconcile(run_dir, **kwargs)

        self.patch_object(module.ioutil, 'run_cmd', spawn)
        self.patch_object(module, 'herdr', lambda *_: ack_workspace(nonce, successor_session))
        self.patch_object(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: live)
        self.patch_object(module.scheduler, 'reconcile', reconcile)
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, _, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(captured), 1)
        self.assertTrue(seen)
        self.assertTrue(all(item["live_herdr"] == live for item in seen))
        self.assertTrue(all(item["live_native_ids"] is None for item in seen))
        self.assertTrue(all(not item["native_inventory_complete"] for item in seen))
        self.assertEqual(self.event_types(run).count("dispatch_intent"), 1)
        self.assertNotIn("interrupted", self.event_types(run))
        self.assertEqual(run_state.replay(run)["state"]["nodes"]["H"]["state"], "running")



    def test_rotate_and_ack_refuse_invalid_session_run_before_side_effects(self):
        cases = (
            ("rotate.py", "outside-docs-runs", 3, "old-ws", "old-pane", "lease_drain_started"),
            ("rotate.py", "noncanonical", 3, "old-ws", "old-pane", "lease_drain_started"),
            ("ack-handoff.py", "outside-docs-runs", 3, "new-ws", "new-pane", "lease_transfer"),
            ("ack-handoff.py", "noncanonical", 3, "new-ws", "new-pane", "lease_transfer"),
        )
        for script, invalid_run, expected_code, workspace, pane, forbidden_event in cases:
            with self.subTest(script=script, invalid_run=invalid_run), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as patches:
                self.addCleanup(patches.close)
                self.root = Path(tmp).resolve()
                self.docs = self.root / "docs"
                foreign = Path(run_state.claim_run(self.root / "other-docs", "Other")["dir"]) if invalid_run == "outside-docs-runs" else None
                run = self.new_run({"schema_version": 1, "nodes": {}})
                session = self.session(run)
                meta = json.loads((session / "session.json").read_text(encoding="utf-8"))
                meta["run"] = str(foreign) if foreign is not None else str(run.parent / ".." / "runs" / run.name)
                (session / "session.json").write_text(json.dumps(meta) + "\n", encoding="utf-8")
                module = load(script)
                calls = []
                if script == "rotate.py":
                    patches.enter_context(mock.patch.object(module.ioutil, 'run_cmd', lambda cmd, **kwargs: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "{}", "")))
                    argv = [script, "--session", str(session), "--timeout", "1"]
                else:
                    patches.enter_context(mock.patch.object(module.session_docs, 'open_session', lambda *args, **kwargs: calls.append(kwargs) or {}))
                    argv = [script, "--nonce", "n", "--session", str(session)]
                with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': workspace, 'HERDR_PANE_ID': pane}):
                    code, _, err = run_cli(module, argv)
                self.assertEqual(code, expected_code, err)
                self.assertEqual(calls, [])
                self.assertNotIn(forbidden_event, self.event_types(foreign if foreign is not None else run))

    def test_rotate_requires_session(self):
        module = load("rotate.py")
        calls = []
        self.patch_object(module.ioutil, 'run_cmd', lambda cmd, **kwargs: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "{}", ""))
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, _, err = run_cli(module, ["rotate.py", "--timeout", "1"])
        self.assertEqual(code, 2, err)
        self.assertEqual(calls, [])

    def test_rotate_rejects_dir_flag(self):
        module = load("rotate.py")
        calls = []
        self.patch_object(module.ioutil, 'run_cmd', lambda cmd, **kwargs: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "{}", ""))
        code, _, err = run_cli(
            module, ["rotate.py", "--session", "/tmp/pred", "--dir", "docs"]
        )
        self.assertEqual(code, 2, err)
        self.assertEqual(calls, [])


    def test_rotate_refuses_missing_bootstrap_before_drain(self):
        for missing in ("HANDOFF.md", "NEXT-SESSION-PROMPT.md"):
            with self.subTest(missing=missing):
                run = self.new_run({"schema_version": 1, "nodes": {}})
                session = self.session(run, name="pred-" + missing)
                (session / missing).unlink()
                module = load("rotate.py")
                with mock.patch.object(module.session_docs, "open_session", side_effect=AssertionError("opened successor")), mock.patch.object(module.ioutil, "run_cmd", side_effect=AssertionError("spawned successor")):
                    code, _, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
                self.assertEqual(code, 3, err)
                self.assertIn(missing, err)
                self.assertNotIn("lease_drain_started", self.event_types(run))

    def test_rotate_drains_before_successor_spawn(self):
        run = self.new_run({"schema_version": 1, "nodes": {}})
        session = self.session(run)
        (session / "progress.md").unlink()
        (session / "HANDOFF.md").write_text("## Run\nInformational prose, not a machine locator.\n", encoding="utf-8")
        for note in ("HANDOFF.md", "NEXT-SESSION-PROMPT.md"):
            os.utime(session / note, (1, 1))
        holder = self.holder(session)
        run_state.append_event(run, {"type": "lease_drain_started", **holder}, holder=holder)
        module = load("rotate.py")
        captured = []
        nonce = "n" * 32
        successor_session = str((self.docs / "sessions" / "succ").resolve())

        def spawn(cmd, **_):
            captured.append(list(cmd))
            run_state.append_event(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": successor_session,
                    "nonce": nonce,
                },
                holder=holder,
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        self.patch_object(module.ioutil, 'run_cmd', spawn)
        self.patch_object(module, 'herdr', lambda *_: ack_workspace(nonce, Path(successor_session).name))
        self.patch_object(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: [])
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, out, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["run"], str(run))
        drains = [event for event in self.events(run) if event["type"] == "lease_drain_started"]
        self.assertEqual(len(drains), 1)
        drain = drains[0]
        self.assertEqual(drain["workspace"], "old-ws")
        self.assertEqual(drain["pane"], "old-pane")
        self.assertEqual(drain["session"], str(session))
        controller = run_state.replay(run)["controller"]
        self.assertEqual(controller["workspace"], "new-ws")
        self.assertEqual(controller["pane"], "new-pane")
        self.assertEqual(controller["session"], successor_session)
        types = self.event_types(run)
        self.assertLess(types.index("lease_drain_started"), types.index("lease_transfer"))
        prompt = captured[0][captured[0].index("--prompt") + 1]
        self.assertIn(str(run), prompt)
        self.assertLess(prompt.index("NEXT-SESSION-PROMPT.md"), prompt.index("HANDOFF.md"))
        self.assertLess(prompt.index("HANDOFF.md"), prompt.index("session.json"))
        self.assertLess(prompt.index("session.json"), prompt.index("ack-handoff.py"))
        self.assertLess(prompt.index("ack-handoff.py"), prompt.index("reconcile.py --run"))
        self.assertIn("$HERDR_WORKSPACE_ID", prompt)
        self.assertIn("$HERDR_PANE_ID", prompt)

    def test_rotate_user_owned_predecessor_stands_down_without_close(self):
        run = self.new_run({"schema_version": 1, "nodes": {}})
        session = self.session(run)
        holder = self.holder(session)
        module = load("rotate.py")
        herdr_calls = []
        nonce = "n" * 32
        successor_session = str((self.docs / "sessions" / "succ").resolve())

        def spawn(cmd, **_):
            run_state.append_event(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": successor_session,
                    "nonce": nonce,
                },
                holder=holder,
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        def herdr(*args):
            herdr_calls.append(args)
            if args[:2] == ("workspace", "get") and args[2] == "old-ws":
                return {"result": {"workspace": {"tokens": {}}}}
            return ack_workspace(nonce, successor_session)

        self.patch_object(module.ioutil, 'run_cmd', spawn)
        self.patch_object(module, 'herdr', herdr)
        self.patch_object(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: [])
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, out, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["predecessor"]["retire"], "stand-down")
        self.assertEqual(payload["predecessor"]["provenance"], "user-owned")
        self.assertTrue(payload["acked"])
        self.assertTrue(all(call[:1] != ("workspace",) or "close" not in call for call in herdr_calls))
        self.assertTrue(all("retire" not in str(call) for call in herdr_calls))


    def test_rotate_refuses_ack_token_for_wrong_successor_pane(self):
        run = self.new_run({"schema_version": 1, "nodes": {}})
        session = self.session(run)
        module = load("rotate.py")
        nonce = "n" * 32
        wrong_session = str((self.docs / "sessions" / "wrong").resolve())

        def spawn(cmd, **_):
            run_state.append_event(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "wrong-pane",
                    "session": wrong_session,
                    "nonce": nonce,
                },
                holder=self.holder(session),
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        self.patch_object(module.ioutil, 'run_cmd', spawn)
        self.patch_object(module, 'herdr', lambda *_: {
            "result": {"workspace": {"tokens": {"handoff-ack": nonce, "session": wrong_session}}}
        })
        self.patch_object(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: [])
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, _, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 3, err)
        self.assertIn("handoff ack does not match spawned successor", err)

    def test_successor_transfer_matches_requires_ack_tokens(self):
        run = self.new_run({"schema_version": 1, "nodes": {}})
        session = self.session(run)
        holder = self.holder(session)
        run_state.append_event(run, {"type": "lease_drain_started", **holder}, holder=holder)
        successor_session = str((self.docs / "sessions" / "succ").resolve())
        nonce = "n" * 32
        run_state.append_event(
            run,
            {
                "type": "lease_transfer",
                "workspace": "new-ws",
                "pane": "new-pane",
                "session": successor_session,
                "nonce": nonce,
            },
            holder=holder,
        )
        module = load("rotate.py")
        successor = {"name": "successor", "workspace": "new-ws", "pane": "new-pane"}
        self.assertFalse(module.successor_transfer_matches(run, successor, nonce, {}))
        self.assertFalse(module.successor_transfer_matches(run, successor, nonce, {"handoff-ack": nonce}))
        self.assertFalse(
            module.successor_transfer_matches(
                run, successor, nonce, {"handoff-ack": "other", "session": successor_session}
            )
        )
        self.assertFalse(
            module.successor_transfer_matches(
                run, successor, nonce, {"handoff-ack": nonce, "session": "other"}
            )
        )
        self.assertTrue(
            module.successor_transfer_matches(
                run, successor, nonce, {"handoff-ack": nonce, "session": successor_session}
            )
        )
        self.assertTrue(
            module.successor_transfer_matches(
                run, successor, nonce, {"handoff-ack": nonce, "session": Path(successor_session).name}
            )
        )

    def test_rotate_does_not_ack_replayed_transfer_without_tokens(self):
        run = self.new_run({"schema_version": 1, "nodes": {}})
        session = self.session(run)
        holder = self.holder(session)
        module = load("rotate.py")
        nonce = "n" * 32
        successor_session = str((self.docs / "sessions" / "succ").resolve())

        def spawn(cmd, **_):
            run_state.append_event(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": successor_session,
                    "nonce": nonce,
                },
                holder=holder,
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        self.patch_object(module.ioutil, 'run_cmd', spawn)
        self.patch_object(module, 'herdr', lambda *_: {"result": {"workspace": {"tokens": {}}}})
        self.patch_object(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: [])
        self.patch_object(module.time, 'sleep', lambda *_: None)
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, out, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 3, err)
        self.assertFalse(json.loads(out)["acked"])
        self.assertIn("lease_transfer", self.event_types(run))

    def test_rotate_recovers_when_ack_tokens_appear_after_transfer(self):
        run = self.new_run({"schema_version": 1, "nodes": {}})
        session = self.session(run)
        holder = self.holder(session)
        module = load("rotate.py")
        nonce = "n" * 32
        successor_session = str((self.docs / "sessions" / "succ").resolve())
        polls = {"n": 0}

        def spawn(cmd, **_):
            run_state.append_event(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": successor_session,
                    "nonce": nonce,
                },
                holder=holder,
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        def herdr(*args):
            if args[:2] == ("workspace", "get") and args[2] == "new-ws":
                polls["n"] += 1
                if polls["n"] == 1:
                    return {"result": {"workspace": {"tokens": {}}}}
                return ack_workspace(nonce, Path(successor_session).name)
            return {"result": {"workspace": {"tokens": {}}}}

        self.patch_object(module.ioutil, 'run_cmd', spawn)
        self.patch_object(module, 'herdr', herdr)
        self.patch_object(module.uuid, 'uuid4', lambda: SimpleNamespace(hex=nonce))
        self.patch_object(module.reconcile, 'observe_live_herdr', lambda run=None, *, lock_timeout=120: [])
        self.patch_object(module.time, 'sleep', lambda *_: None)
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "old-ws", 'HERDR_PANE_ID': "old-pane"}):
            code, out, err = run_cli(module, ["rotate.py", "--session", str(session), "--timeout", "1"])
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["acked"])
        self.assertGreaterEqual(polls["n"], 2)

    def test_ack_refuses_active_lease_without_binding_or_transfer(self):
        run = self.new_run(bind=False)
        session = self.session(run)
        module = load("ack-handoff.py")
        opened = []
        self.patch_object(module.session_docs, 'open_session', lambda *args, **kwargs: opened.append(kwargs) or {})
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "new-ws", 'HERDR_PANE_ID': "new-pane"}):
            code, _, err = run_cli(module, ["ack-handoff.py", "--nonce", "n", "--session", str(session)])
        self.assertEqual(code, 3, err)
        self.assertEqual(opened, [])
        self.assertNotIn("lease_transfer", self.event_types(run))

    def test_ack_refuses_different_predecessor_before_token_stamp(self):
        bound_session = str((self.docs / "sessions" / "bound-session").resolve())
        bound = {"workspace": "bound-ws", "pane": "bound-pane", "session": bound_session}
        run = self.new_run({"schema_version": 1, "nodes": {}}, holder=bound)
        session = self.session(run)
        run_state.append_event(run, {"type": "lease_drain_started", **bound}, holder=bound)
        module = load("ack-handoff.py")
        opened = []
        calls = []
        self.patch_object(module.session_docs, 'open_session', lambda *args, **kwargs: opened.append(kwargs) or {})
        self.patch_object(module, 'herdr', lambda *args: calls.append(args) or {})
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "new-ws", 'HERDR_PANE_ID': "new-pane"}):
            code, _, err = run_cli(module, ["ack-handoff.py", "--nonce", "n", "--session", str(session)])
        self.assertEqual(code, 3, err)
        self.assertEqual(opened, [])
        self.assertEqual(calls, [])
        self.assertNotIn("lease_transfer", self.event_types(run))


    def test_ack_recovers_stamp_failure_without_duplicate_session_or_transfer(self):
        run = self.new_run({"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "herdr-agent"}}})
        session = self.session(run)
        (session / "progress.md").unlink()
        (session / "HANDOFF.md").write_text("## Run\nInformational prose, not a machine locator.\n", encoding="utf-8")
        for note in ("HANDOFF.md", "NEXT-SESSION-PROMPT.md"):
            os.utime(session / note, (1, 1))
        holder = self.holder(session)
        self.start_attempt(
            run,
            holder=holder,
            handle={"workspace": "worker-ws", "pane": "worker-pane", "agent": "worker"},
        )
        run_state.append_event(run, {"type": "lease_drain_started", **holder}, holder=holder)
        module = load("ack-handoff.py")
        calls = []

        def stamp(*args):
            calls.append(args)
            if len(calls) == 1:
                raise RuntimeError("metadata stamp failed")
            return {}

        self.patch_object(module, 'herdr', stamp)
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "new-ws", 'HERDR_PANE_ID': "new-pane"}):
            first_code, first_out, first_err = run_cli(
                module, ["ack-handoff.py", "--nonce", "nonce", "--session", str(session)]
            )
            first_created = [
                path.resolve() for path in (self.docs / "sessions").iterdir()
                if path.is_dir() and not path.is_symlink() and path.resolve() != session.resolve()
            ]
            code, out, err = run_cli(
                module, ["ack-handoff.py", "--nonce", "nonce", "--session", str(session)]
            )
        self.assertEqual(first_code, 1, first_err)
        self.assertEqual(first_out, "")
        self.assertIn("metadata stamp failed", first_err)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        successor = Path(payload["session"]).resolve()
        self.assertTrue(Path(payload["session"]).is_absolute())
        self.assertEqual(payload["session"], run_state.canonical_path(successor))
        self.assertEqual(first_created, [successor])
        created = [
            path.resolve() for path in (self.docs / "sessions").iterdir()
            if path.is_dir() and not path.is_symlink() and path.resolve() != session.resolve()
        ]
        self.assertEqual(created, [successor])
        self.assertEqual(payload["run"], str(run))
        self.assertEqual(payload["workspace"], "new-ws")
        self.assertEqual(payload["parent"], session.name)
        self.assertTrue(successor.is_dir())
        self.assertNotEqual(successor, session.resolve())
        self.assertEqual(
            json.loads((successor / "session.json").read_text(encoding="utf-8"))["run"],
            str(run),
        )
        controller = run_state.replay(run)["controller"]
        self.assertEqual(controller["epoch"], 2)
        self.assertEqual(controller["lease"], "active")
        self.assertEqual(controller["workspace"], "new-ws")
        self.assertEqual(controller["pane"], "new-pane")
        self.assertEqual(controller["session"], str(successor))
        transfers = [event for event in self.events(run) if event["type"] == "lease_transfer"]
        self.assertEqual(len(transfers), 1)
        transfer = transfers[0]
        self.assertEqual(transfer["workspace"], "new-ws")
        self.assertEqual(transfer["pane"], "new-pane")
        self.assertEqual(transfer["session"], str(successor))
        self.assertEqual(transfer["nonce"], "nonce")
        self.assertTrue(any(f"session={payload['session_id']}" in str(arg) for arg in calls[-1]))
        self.assertTrue(any("handoff-ack=nonce" in str(arg) for arg in calls[-1]))
        with self.assertRaises(run_state.PreconditionError):
            run_state.append_event(
                run,
                {"type": "failed", "node": "A", "epoch": 1},
                holder={
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": str(successor),
                },
            )
        with self.assertRaises(run_state.PreconditionError):
            run_state.append_event(
                run,
                {"type": "failed", "node": "A", "attempt": 1},
                holder=holder,
            )

    def test_ack_refuses_missing_bootstrap_before_transfer(self):
        for missing in ("HANDOFF.md", "NEXT-SESSION-PROMPT.md"):
            with self.subTest(missing=missing):
                run = self.new_run({"schema_version": 1, "nodes": {}})
                session = self.session(run, name="pred-" + missing)
                (session / missing).unlink()
                module = load("ack-handoff.py")
                with mock.patch.object(module.session_docs, "open_session", side_effect=AssertionError("opened successor")), mock.patch.object(module.ioutil, "run_cmd", side_effect=AssertionError("spawned successor")):
                    code, _, err = run_cli(module, ["ack-handoff.py", "--nonce", "n", "--session", str(session)])
                self.assertEqual(code, 3, err)
                self.assertIn(missing, err)
                self.assertNotIn("lease_transfer", self.event_types(run))
    def test_ack_transfer_failure_does_not_stamp_or_print_success(self):
        run = self.new_run({"schema_version": 1, "nodes": {}})
        session = self.session(run)
        holder = self.holder(session)
        run_state.append_event(run, {"type": "lease_drain_started", **holder}, holder=holder)
        module = load("ack-handoff.py")
        calls = []
        original = SimpleNamespace(append=module.ledger.append_event_locked)

        def fail_transfer(run_dir, event, holder=None):
            if event.get("type") == "lease_transfer":
                raise run_state.PreconditionError("lease_transfer failed")
            return original.append(run_dir, event, holder=holder)

        self.patch_object(module, 'herdr', lambda *args: calls.append(args) or {})
        self.patch_object(module.ledger, 'append_event_locked', fail_transfer)
        with mock.patch.dict(os.environ, {'HERDR_WORKSPACE_ID': "new-ws", 'HERDR_PANE_ID': "new-pane"}):
            code, out, err = run_cli(module, ["ack-handoff.py", "--nonce", "nonce", "--session", str(session)])
        self.assertEqual(code, 3, err)
        self.assertEqual(out, "")
        self.assertEqual(calls, [])
        self.assertNotIn("lease_transfer", self.event_types(run))
        created = [
            path for path in (self.docs / "sessions").iterdir()
            if path.is_dir() and not path.is_symlink() and path.resolve() != session.resolve()
        ]
        self.assertEqual(len(created), 1)
        controller = run_state.replay(run)["controller"]
        self.assertEqual(controller["lease"], "draining")
        self.assertEqual(controller["session"], str(session.resolve()))

def context_snapshot_path(workspace: str, pane: str, home: Path) -> Path:
    digest = hashlib.sha256(f"{workspace}\n{pane}".encode()).hexdigest()
    return home / ".omp" / "run" / "herdr-context" / f"{digest}.json"


def omp_title_slot(title: str = "gate") -> bytes:
    prefix = json.dumps(
        {"type": "title", "v": 1, "title": title, "source": "auto", "updatedAt": "2026-10-05T00:00:00.000Z", "pad": ""},
        separators=(",", ":"),
    )
    bare = prefix[:-2]
    pad = 255 - (len(bare) + 2)
    line = (bare + (" " * pad) + '"}').encode() + b"\n"
    if len(line) != 256:
        raise AssertionError(len(line))
    return line


def write_omp_journal(path: Path, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-10-05T00:00:00.000Z",
        "cwd": "/tmp/gate",
    }
    secret = {
        "type": "message",
        "id": "abcd1234",
        "parentId": None,
        "timestamp": "2026-10-05T00:00:01.000Z",
        "message": {"role": "user", "content": "SECRET_TRANSCRIPT_SENTINEL"},
    }
    path.write_bytes(omp_title_slot() + (json.dumps(header) + "\n" + json.dumps(secret) + "\n").encode())


def write_secure_json(path: Path, payload: dict, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)
    path.chmod(mode)


def freeze_clock(module, frozen_s: int, patch_object) -> int:
    nanos = frozen_s * 1_000_000_000
    patch_object(module.time, 'time', lambda: float(frozen_s))
    patch_object(module.time, 'time_ns', lambda: nanos)
    if hasattr(module, "time_ns"):
        patch_object(module, 'time_ns', lambda: nanos)
    return frozen_s * 1000


def file_bytes(root: Path) -> dict[str, bytes]:
    found = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        found[str(path.resolve())] = path.read_bytes()
    return found


@contextlib.contextmanager
def change_after_read(module, snap: Path, mutated: str):
    seen = {"n": 0}
    original = module.herdr

    def observe(*args):
        result = original(*args)
        if args[:2] == ("pane", "get") and seen["n"] == 0:
            seen["n"] = 1
            replacement = snap.with_suffix(".next")
            replacement.write_text(mutated, encoding="utf-8")
            replacement.chmod(0o600)
            replacement.replace(snap)
        return result

    with mock.patch.object(module, "herdr", observe):
        yield seen


class ReadOnlyHerdr:
    def __init__(
        self,
        workspace: str,
        pane: str,
        journal: Path,
        pid: int,
        *,
        pane_id: str | None = None,
        workspace_id: str | None = None,
        agent: str = "omp",
        kind: str = "path",
        source: str = "herdr:omp",
        journal_value: str | None = None,
        replace_pane: bool = False,
        replace_pid: bool = False,
        processes: list[dict] | None = None,
    ):
        self.calls: list[tuple[str, ...]] = []
        self.workspace = workspace
        self.pane = pane
        self.journal = journal
        self.pid = pid
        self.pane_id = pane if pane_id is None else pane_id
        self.workspace_id = workspace if workspace_id is None else workspace_id
        self.agent = agent
        self.kind = kind
        self.source = source
        self.journal_value = str(journal.resolve()) if journal_value is None else journal_value
        self.replace_pane = replace_pane
        self.replace_pid = replace_pid
        self.processes = processes

    def __call__(self, *args: str):
        self.calls.append(args)
        if args[:2] == ("workspace", "get"):
            return {"result": {"workspace": {"tokens": {}}}}
        if args[:2] == ("pane", "get"):
            value = self.journal_value
            if self.replace_pane and sum(1 for call in self.calls if call[:2] == ("pane", "get")) >= 2:
                value = str(self.journal) + ".replaced"
            return {
                "result": {
                    "type": "pane_info",
                    "pane": {
                        "pane_id": self.pane_id,
                        "workspace_id": self.workspace_id,
                        "agent": self.agent,
                        "agent_session": {
                            "kind": self.kind,
                            "source": self.source,
                            "agent": self.agent,
                            "value": value,
                        },
                    },
                }
            }
        if args[:2] == ("pane", "process-info"):
            pid = self.pid
            if self.replace_pid and sum(1 for call in self.calls if call[:2] == ("pane", "process-info")) >= 2:
                pid = self.pid + 9
            if self.processes is None:
                foreground = [
                    {"pid": pid, "name": "omp", "argv0": "omp", "cmdline": "omp"},
                    {"pid": self.pid + 1, "name": "omp", "argv0": "omp", "cmdline": "omp --child"},
                    {"pid": self.pid + 2, "name": "node", "argv0": "node", "cmdline": "lsp"},
                ]
            else:
                foreground = []
                for item in self.processes:
                    row = dict(item)
                    if row.get("pid") == self.pid:
                        row["pid"] = pid
                    foreground.append(row)
            return {
                "result": {
                    "type": "pane_process_info",
                    "process_info": {
                        "pane_id": self.pane_id,
                        "shell_pid": 7,
                        "tty": "/dev/ttys000",
                        "foreground_process_group_id": pid,
                        "foreground_processes": foreground,
                    },
                }
            }
        return {"result": {}}


EXTENSION_DRIVER = r"""
import { readFileSync } from "node:fs";

const scenario = JSON.parse(readFileSync(process.argv[2], "utf8"));
const mod = await import(process.env.EXT);
const factory = mod.default;

function makePi() {
  const handlers = new Map();
  return {
    handlers,
    on(name, fn) {
      const list = handlers.get(name) ?? [];
      list.push(fn);
      handlers.set(name, list);
    },
    events: { on() {} },
  };
}

function readSnap(file) {
  try {
    return JSON.parse(readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

function makeCtx(step) {
  return {
    hasUI: step.hasUI !== false,
    agent: step.agent ?? { kind: "main", id: "main", name: "main", depth: 0 },
    model: step.model ?? { provider: "anthropic", id: "claude" },
    isIdle() { return true; },
    getContextUsage() {
      if (step.usage === "throw") throw new Error("no usage");
      if (step.usage === "nan") return { tokens: Number.NaN, contextWindow: 100, percent: 0.5 };
      return step.usage;
    },
    sessionManager: {
      getSessionFile() { return step.sessionFile; },
      getSessionId() { return step.sessionId; },
    },
  };
}

async function fire(target, step) {
  const list = target.handlers.get(step.hook) ?? [];
  if (!list.length) return { missing: true, hook: step.hook, threw: false, immediate: null, after: null };
  const ctx = makeCtx(step);
  const pending = [];
  let threw = false;
  try {
    for (const fn of list) pending.push(fn(step.event ?? {}, ctx));
  } catch {
    threw = true;
  }
  const immediate = readSnap(scenario.snap);
  try {
    await Promise.all(pending.map((item) => Promise.resolve(item)));
  } catch {
    threw = true;
  }
  return { missing: false, hook: step.hook, threw, immediate, after: readSnap(scenario.snap) };
}

const root = makePi();
const loaded = factory(root);
if (loaded && typeof loaded.then === "function") await loaded;
const results = [];
for (const step of scenario.steps) {
  let target = root;
  if (step.bind === "child") {
    target = makePi();
    const childLoaded = factory(target);
    if (childLoaded && typeof childLoaded.then === "function") await childLoaded;
  }
  results.push(await fire(target, step));
}
console.log(JSON.stringify({ pid: process.pid, results }));
"""


class ContextUsageTest(LeaseFixtures, unittest.TestCase):
    FROZEN = 1_700_000_000
    WORKSPACE = "ws1"
    PANE = "ws1:p1"
    PID = 4242
    SESSION_ID = "019c625b-b900-7000-8000-0000000000aa"

    def setUp(self):
        super().setUp()
        self.module = load("context_usage.py")
        self.rotate = load("rotate.py")
        self.patch_object(self.rotate, "context_usage", self.module)
        self.assertTrue(issubclass(self.module.ContextUsageError, run_state.PreconditionError))
        self.assertFalse(issubclass(self.module.ContextUsageError, run_state.UsageError))


    def herdr_env(self, home: Path, *, workspace: str | None = None, pane: str | None = None) -> dict[str, str]:
        (home / "chosen.sock").write_text("", encoding="utf-8")
        return {
            "HOME": str(home),
            "HERDR_ENV": "1",
            "HERDR_SOCKET_PATH": str(home / "chosen.sock"),
            "HERDR_SESSION": "isolated",
            "HERDR_WORKSPACE_ID": workspace or self.WORKSPACE,
            "HERDR_PANE_ID": pane or self.PANE,
        }

    def plant(
        self,
        *,
        tokens: int = 5,
        window: int = 10,
        observed_at_ms: int | None = None,
        available: bool = True,
        write: bool = True,
        workspace: str | None = None,
        pane: str | None = None,
        payload_extra: dict | None = None,
        herdr_kwargs: dict | None = None,
        mode: int = 0o600,
        dir_mode: int = 0o700,
    ):
        workspace = workspace or self.WORKSPACE
        pane = pane or self.PANE
        self._plant_n = getattr(self, "_plant_n", 0) + 1
        home = self.root / f"home-{self._plant_n}"
        home.mkdir()
        journal = home / "journal.jsonl"
        write_omp_journal(journal, self.SESSION_ID)
        snap = context_snapshot_path(workspace, pane, home)
        now_ms = freeze_clock(self.module, self.FROZEN, self.patch_object)
        payload = {
            "schema_version": 1,
            "source": SOURCE,
            "workspace": workspace,
            "pane": pane,
            "pid": self.PID,
            "observed_at_ms": now_ms if observed_at_ms is None else observed_at_ms,
            "available": available,
            "omp_session_file": str(journal.resolve()),
            "omp_session_id": self.SESSION_ID,
            "model": {"provider": "anthropic", "id": "claude"},
        }
        if available:
            payload["context_usage"] = {"tokens": tokens, "context_window": window}
        else:
            payload["reason"] = "compaction"
        if payload_extra:
            payload.update(payload_extra)
        if write:
            write_secure_json(snap, payload, mode=mode)
            snap.parent.chmod(dir_mode)
        fake = ReadOnlyHerdr(workspace, pane, journal, self.PID, **(herdr_kwargs or {}))
        self.patch_object(self.module, 'herdr', fake)
        self.patch_object(self.rotate, 'herdr', fake)
        name = f"pred{self._plant_n}"
        session_path = (self.docs / "sessions" / name).resolve()
        holder = {"workspace": workspace, "pane": pane, "session": str(session_path)}
        run = self.new_run({"schema_version": 1, "nodes": {}}, holder=holder)
        session = self.session(run, name=name, workspace=workspace, pane=pane)
        return SimpleNamespace(
            home=home,
            journal=journal,
            snap=snap,
            payload=payload,
            fake=fake,
            run=run,
            session=session,
            holder=holder,
            now_ms=now_ms,
            tokens=tokens,
            window=window,
            workspace=workspace,
            pane=pane,
        )

    def refusal(self, err: str) -> dict:
        payload = json.loads(err)
        self.assertIn("fraction", payload)
        self.assertIn("source", payload)
        self.assertIn("reason", payload)
        return payload

    def assert_reader_refuses(self, planted):
        with mock.patch.dict(os.environ, {**self.herdr_env(planted.home, workspace=planted.workspace, pane=planted.pane)}):
            with self.assertRaises(self.module.ContextUsageError) as caught:
                self.module.read_context_usage(planted.session, planted.holder)
        self.assertIsNone(caught.exception.fraction)
        return caught.exception

    def assert_reader_accepts(self, planted):
        with mock.patch.dict(os.environ, {**self.herdr_env(planted.home, workspace=planted.workspace, pane=planted.pane)}):
            evidence = self.module.read_context_usage(planted.session, planted.holder)
        self.assertEqual(evidence["tokens"], planted.tokens)
        self.assertEqual(evidence["context_window"], planted.window)
        self.assertEqual(evidence["fraction"], planted.tokens / planted.window)
        self.assertEqual(evidence["source"], SOURCE)
        self.assertEqual(Path(evidence["snapshot"]), planted.snap)
        self.assertEqual(evidence["omp_session_id"], self.SESSION_ID)
        self.assertEqual(evidence["omp_session_file"], str(planted.journal.resolve()))
        return evidence

    def run_refused(self, planted) -> dict:
        before = file_bytes(self.docs)
        sessions_before = file_bytes(self.docs / "sessions")

        def boom(*_args, **_kwargs):
            raise AssertionError("side effect before context refusal")

        with (
            mock.patch.object(self.rotate, "start_lease_drain", boom),
            mock.patch.object(ioutil, "run_cmd", boom),
            mock.patch.object(self.rotate.reconcile, "observe_live_herdr", boom),
            mock.patch.object(self.rotate.scheduler, "reconcile", boom),
            mock.patch.dict(os.environ, {**self.herdr_env(planted.home, workspace=planted.workspace, pane=planted.pane)}),
        ):
            code, out, err = run_cli(
                self.rotate,
                ["rotate.py", "--session", str(planted.session), "--timeout", "1"],
                context_reader=False,
            )
        self.assertEqual(code, 3, err)
        self.assertEqual(out, "")
        self.assertEqual(file_bytes(self.docs), before)
        self.assertEqual(file_bytes(self.docs / "sessions"), sessions_before)
        self.assertNotIn("lease_drain_started", self.event_types(planted.run))
        self.assertTrue(all(call[:2] in READONLY_HERDR for call in planted.fake.calls), planted.fake.calls)
        return self.refusal(err)

    def run_success(self, planted):
        captured = []
        nonce = "n" * 32
        successor = str((self.docs / "sessions" / "succ").resolve())

        def spawn(cmd, **_kwargs):
            captured.append(list(cmd))
            run_state.append_event(
                planted.run,
                {
                    "type": "lease_transfer",
                    "workspace": "new-ws",
                    "pane": "new-pane",
                    "session": successor,
                    "nonce": nonce,
                },
                holder=planted.holder,
            )
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"name": "successor", "workspace": "new-ws", "pane": "new-pane"}), ""
            )

        def herdr(*args):
            if args[:2] == ("workspace", "get") and len(args) > 2 and args[2] == "new-ws":
                planted.fake.calls.append(args)
                return ack_workspace(nonce, Path(successor).name)
            return planted.fake(*args)

        with (
            mock.patch.object(ioutil, "run_cmd", spawn),
            mock.patch.object(self.rotate, "herdr", herdr),
            mock.patch.object(self.rotate.uuid, "uuid4", lambda: SimpleNamespace(hex=nonce)),
            mock.patch.object(self.rotate.reconcile, "observe_live_herdr", lambda run=None, *, lock_timeout=120: []),
            mock.patch.object(self.rotate.time, "sleep", lambda *_args: None),
            mock.patch.dict(os.environ, {**self.herdr_env(planted.home, workspace=planted.workspace, pane=planted.pane)}),
        ):
            code, out, err = run_cli(
                self.rotate,
                ["rotate.py", "--session", str(planted.session), "--timeout", "1"],
                context_reader=False,
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(captured), 1)
        self.assertTrue(any("spawn-agent.py" in str(part) for part in captured[0]))
        self.assertTrue(all(call[:2] in READONLY_HERDR or call[:2] == ("workspace", "get") for call in planted.fake.calls))
        self.assertTrue(all("close" not in call for call in planted.fake.calls))
        payload = json.loads(out)
        usage = payload["context_usage"]
        self.assertEqual(usage["tokens"], planted.tokens)
        self.assertEqual(usage["context_window"], planted.window)
        self.assertGreaterEqual(usage["tokens"] * 2, usage["context_window"])
        self.assertEqual(usage["source"], SOURCE)
        self.assertEqual(usage["omp_session_id"], self.SESSION_ID)
        self.assertNotEqual(usage["omp_session_id"], "legacy")
        return payload

    def test_reader_accepts_exact_boundary_age_and_over_window(self):
        exact = self.plant(tokens=5, window=10, observed_at_ms=self.FROZEN * 1000 - 5000)
        with mock.patch.dict(os.environ, {**self.herdr_env(exact.home)}):
            evidence = self.module.read_context_usage(exact.session, exact.holder)
            self.module.require_context_usage(exact.session, exact.holder)
        self.assertEqual(evidence["tokens"], 5)
        self.assertEqual(evidence["context_window"], 10)
        self.assertEqual(evidence["fraction"], 0.5)
        self.assertEqual(evidence["source"], SOURCE)
        self.assertEqual(Path(evidence["snapshot"]), exact.snap)
        self.assertGreaterEqual(sum(1 for call in exact.fake.calls if call[:2] == ("pane", "get")), 2)
        self.assertGreaterEqual(sum(1 for call in exact.fake.calls if call[:2] == ("pane", "process-info")), 2)
        self.assertIn(("pane", "get", self.PANE), exact.fake.calls)
        self.assertIn(("pane", "process-info", "--pane", self.PANE), exact.fake.calls)
        over = self.plant(tokens=3, window=2)
        with mock.patch.dict(os.environ, {**self.herdr_env(over.home)}):
            evidence = self.module.read_context_usage(over.session, over.holder)
            self.module.require_context_usage(over.session, over.holder)
        self.assertEqual(evidence["fraction"], 1.5)
        self.assertNotIn("SECRET_TRANSCRIPT_SENTINEL", json.dumps(evidence))

    @unittest.skipUnless(OMP_CLI.is_file(), f"OMP CLI missing: {OMP_CLI}")
    @unittest.skipUnless(BUN and Path(BUN).is_file(), f"Bun missing: {BUN or 'not found on PATH'}")
    def test_reader_accepts_restarted_bun_cli_and_refuses_lookalikes(self):
        cli = str(OMP_CLI)
        bun = BUN
        resume = "01a10a98-f1d5-7000-829f-5ccbfcac4cb6"
        observed = [
            bun,
            cli,
            "--model",
            "openai-codex/gpt-6.1-sol",
            "--thinking",
            "xhigh",
            "--resume",
            resume,
        ]

        def leader(name, argv, *, argv0=None, cmdline=None, pid=self.PID):
            row = {"pid": pid, "name": name, "argv": argv}
            if argv0 is not None:
                row["argv0"] = argv0
            if cmdline is not None:
                row["cmdline"] = cmdline
            return row

        accepts = {
            "bun-cli-resume": [
                leader("bun", observed, argv0="bun"),
                leader("bun", [bun, cli, "__omp_worker_mnemopi_embed"], argv0="bun", pid=self.PID + 1),
            ],
            "bun-cli-no-resume": [leader("bun", [bun, cli, "--model", "openai-codex/gpt-6.1-sol"], argv0="bun")],
            "node-cli": [leader("node", ["/usr/local/bin/node", cli], argv0="node")],
            "legacy-bun-omp": [leader("bun", [bun, str(Path.home() / ".omp/bin/omp")], argv0="bun")],
            "legacy-node-omp": [leader("node", ["/usr/local/bin/node", "/opt/homebrew/bin/omp"], argv0="node")],
            "compiled-omp": [leader("omp", ["/usr/local/bin/omp", "--resume", resume], argv0="/usr/local/bin/omp")],
            "compiled-argv0-only": [{"pid": self.PID, "argv": ["/usr/local/bin/omp"]}],
            "exact-cli-entry": [leader("bun", [bun, "@oh-my-pi/pi-coding-agent/dist/cli.js"], argv0="bun")],
            "backslash-cli": [leader("node", ["/usr/local/bin/node", cli.replace("/", "\\")], argv0="node")],
        }
        for name, processes in accepts.items():
            with self.subTest(case=name):
                tokens, window = (189819, 922000) if name == "bun-cli-resume" else (6, 10)
                planted = self.plant(tokens=tokens, window=window, herdr_kwargs={"processes": processes})
                evidence = self.assert_reader_accepts(planted)
                if name == "bun-cli-resume":
                    self.assertLess(evidence["fraction"], 0.5)
                    self.assertEqual(evidence["fraction"], 189819 / 922000)
        rejects = {
            "unrelated-cli": [leader("bun", [bun, "/tmp/cli.js", "--resume", resume], argv0="bun")],
            "dist-cli": [leader("bun", [bun, "dist/cli.js"], argv0="bun")],
            "cli-basename": [leader("bun", [bun, "cli.js", "--resume", resume], argv0="bun")],
            "cli-later-slot": [leader("bun", [bun, "/tmp/other.js", cli, "--resume", resume], argv0="bun")],
            "bare-bun": [leader("bun", [bun], argv0="bun")],
            "unrelated-node": [leader("node", ["/usr/local/bin/node", "/tmp/app.js"], argv0="node")],
            "cli-as-argv0": [leader("node", [cli, "--resume", resume], argv0="node")],
            "non-host": [leader("python", ["/usr/bin/python", cli, "--resume", resume], argv0="python")],
            "worker-on-omp": [leader("omp", ["/usr/local/bin/omp", "__omp_worker_mnemopi_embed"], argv0="omp")],
            "worker-on-cli": [leader("bun", [*observed, "__omp_worker_mnemopi_embed"], argv0="bun")],
            "worker-substring": [
                leader("bun", [*observed, "--note=prefix__omp_worker_mnemopi_embed_suffix"], argv0="bun")
            ],
            "worker-cmdline": [
                leader("omp", ["/usr/local/bin/omp"], argv0="omp", cmdline="omp __omp_worker_mnemopi_embed")
            ],
            "lsp-pyright": [leader("pyright", ["/usr/bin/pyright", cli], argv0="pyright")],
            "lsp-cmdline": [leader("omp", ["/usr/local/bin/omp"], argv0="/usr/local/bin/omp", cmdline="pylsp")],
            "lsp-script": [leader("bun", [bun, "/opt/bin/language-server"], argv0="bun")],
            "lsp-suffix": [leader("foo-lsp", ["/usr/local/bin/omp"], argv0="omp")],
            "sibling-omp-no-rescue": [
                leader("bun", [bun, "/tmp/cli.js"], argv0="bun"),
                leader("omp", ["/usr/local/bin/omp"], argv0="omp", pid=self.PID + 1),
            ],
            "cmdline-only-legacy": [
                leader("bun", [bun], argv0="bun", cmdline=f"bun {Path.home() / '.omp/bin/omp'}")
            ],
            "missing-argv": [{"pid": self.PID, "name": "bun", "argv0": "bun", "cmdline": "bun /opt/omp"}],
            "case-host": [leader("Bun", ["Bun", cli, "--resume", resume], argv0="Bun")],
            "case-entry": [leader("bun", [bun, cli.replace("dist/cli.js", "dist/CLI.js")], argv0="bun")],
        }
        for name, processes in rejects.items():
            with self.subTest(case=name):
                error = self.assert_reader_refuses(self.plant(herdr_kwargs={"processes": processes}))
                self.assertIn("owning pid is not the OMP foreground process", error.reason)

    def test_reader_rejects_malformed_nonfinite_source_and_unavailable(self):
        cases = {
            "footer": {"source": "footer"},
            "schema": {"schema_version": 2},
            "bool": {"context_usage": {"tokens": True, "context_window": 2}},
            "string": {"context_usage": {"tokens": "5", "context_window": 10}},
            "negative": {"context_usage": {"tokens": -1, "context_window": 10}},
            "zero-window": {"context_usage": {"tokens": 1, "context_window": 0}},
            "missing-usage": {"context_usage": None},
        }
        for name, extra in cases.items():
            with self.subTest(case=name):
                self.assert_reader_refuses(self.plant(payload_extra=extra))
        unavailable = self.plant(available=False)
        self.assertNotIn("context_usage", unavailable.payload)
        self.assert_reader_refuses(unavailable)
        for name, token in (("nan", "NaN"), ("inf", "Infinity")):
            with self.subTest(case=name):
                planted = self.plant()
                text = planted.snap.read_text(encoding="utf-8").replace('"tokens": 5', f'"tokens": {token}')
                planted.snap.write_text(text, encoding="utf-8")
                planted.snap.chmod(0o600)
                self.assert_reader_refuses(planted)
        with self.subTest(case="truncated"):
            planted = self.plant()
            planted.snap.write_text("{", encoding="utf-8")
            planted.snap.chmod(0o600)
            self.assert_reader_refuses(planted)
        with self.subTest(case="missing"):
            planted = self.plant(write=False)
            error = self.assert_reader_refuses(planted)
            self.assertIsNone(error.source)

    def test_reader_rejects_identity_process_prefix_and_replacement(self):
        mismatches = {
            "agent": {"agent": "claude"},
            "kind": {"kind": "id"},
            "session-source": {"source": "manual"},
            "journal": {"journal_value": "/tmp/other-session.jsonl"},
            "pane": {"pane_id": "ws1:p2"},
            "workspace": {"workspace_id": "other"},
            "child-pid": {},
        }
        for name, kwargs in mismatches.items():
            with self.subTest(case=name):
                extra = {"pid": self.PID + 1} if name == "child-pid" else None
                self.assert_reader_refuses(self.plant(herdr_kwargs=kwargs, payload_extra=extra))
        with self.subTest(case="lsp-pid"):
            self.assert_reader_refuses(self.plant(payload_extra={"pid": self.PID + 2}))
        with self.subTest(case="prefix"):
            self.assert_reader_refuses(self.plant(workspace="w4", pane="w4G:p1"))
        with self.subTest(case="pane-replaced"):
            self.assert_reader_refuses(self.plant(herdr_kwargs={"replace_pane": True}))
        with self.subTest(case="pid-replaced"):
            self.assert_reader_refuses(self.plant(herdr_kwargs={"replace_pid": True}))

    def test_reader_rejects_stale_future_insecure_and_changed_bytes(self):
        now_ms = self.FROZEN * 1000
        with self.subTest(case="stale"):
            self.assert_reader_refuses(self.plant(observed_at_ms=now_ms - 5001))
        with self.subTest(case="future"):
            self.assert_reader_refuses(self.plant(observed_at_ms=now_ms + 1))
        with self.subTest(case="mode"):
            self.assert_reader_refuses(self.plant(mode=0o644))
        with self.subTest(case="dir-mode"):
            self.assert_reader_refuses(self.plant(dir_mode=0o707))
        with self.subTest(case="file-symlink"):
            planted = self.plant(write=False)
            planted.snap.parent.mkdir(parents=True, exist_ok=True)
            planted.snap.parent.chmod(0o700)
            target = planted.home / "linked.json"
            write_secure_json(target, planted.payload)
            planted.snap.symlink_to(target)
            before = target.read_bytes()
            self.assert_reader_refuses(planted)
            self.assertEqual(target.read_bytes(), before)
        with self.subTest(case="dir-symlink"):
            planted = self.plant(write=False)
            target_dir = planted.home / "elsewhere"
            target_dir.mkdir()
            planted.snap.parent.parent.mkdir(parents=True, exist_ok=True)
            planted.snap.parent.symlink_to(target_dir, target_is_directory=True)
            write_secure_json(target_dir / planted.snap.name, planted.payload)
            before = (target_dir / planted.snap.name).read_bytes()
            self.assert_reader_refuses(planted)
            self.assertEqual((target_dir / planted.snap.name).read_bytes(), before)
        with self.subTest(case="changed"):
            planted = self.plant(tokens=5, window=10)
            mutated = json.dumps({**planted.payload, "context_usage": {"tokens": 9, "context_window": 10}}) + "\n"
            with change_after_read(self.module, planted.snap, mutated):
                self.assert_reader_refuses(planted)
        for name, drop in (("missing-session-env", "HERDR_SESSION"), ("missing-herdr-env", "HERDR_ENV")):
            with self.subTest(case=name):
                planted = self.plant()
                env = self.herdr_env(planted.home)
                env.pop(drop, None)
                with mock.patch.dict(os.environ, {**env}):
                    os.environ.pop(drop, None)
                    with self.assertRaises(self.module.ContextUsageError) as caught:
                        self.module.read_context_usage(planted.session, planted.holder)
                self.assertIsNone(caught.exception.fraction)

    def test_main_boundaries_do_not_mutate_on_denial(self):
        under = self.plant(tokens=1, window=16)
        payload = self.run_refused(under)
        self.assertEqual(payload["source"], SOURCE)
        self.assertLess(payload["fraction"], 0.1)
        self.assertGreater(payload["fraction"], 0)
        below = self.plant(tokens=4999, window=10000)
        payload = self.run_refused(below)
        self.assertEqual(payload["source"], SOURCE)
        self.assertLess(payload["fraction"], 0.5)
        self.assertGreater(payload["fraction"], 0.49)
        missing = self.run_refused(self.plant(write=False))
        self.assertIsNone(missing["fraction"])
        self.assertIsNone(missing["source"])
        boolean = self.run_refused(self.plant(payload_extra={"context_usage": {"tokens": True, "context_window": 2}}))
        self.assertIsNone(boolean["fraction"])
        stale = self.run_refused(self.plant(observed_at_ms=self.FROZEN * 1000 - 5001))
        self.assertIsNone(stale["fraction"])
        mismatch = self.run_refused(self.plant(herdr_kwargs={"workspace_id": "other"}))
        self.assertIsNone(mismatch["fraction"])
        changed = self.plant(tokens=5, window=10)
        mutated = json.dumps({**changed.payload, "context_usage": {"tokens": 9, "context_window": 10}}) + "\n"
        with change_after_read(self.module, changed.snap, mutated):
            changed_payload = self.run_refused(changed)
        self.assertIsNone(changed_payload["fraction"])

    def test_main_allows_exactly_50_and_above_with_fake_successor_only(self):
        exact = self.run_success(self.plant(tokens=5, window=10))
        self.assertEqual(exact["context_usage"]["fraction"], 0.5)
        above = self.run_success(self.plant(tokens=2, window=3))
        self.assertGreater(above["context_usage"]["fraction"], 0.5)
        self.assertEqual(above["context_usage"]["tokens"], 2)

    def run_extension(self, home: Path, steps: list[dict], *, env: dict | None = None) -> dict:
        snap = context_snapshot_path(self.WORKSPACE, self.PANE, home)
        script = home / "drive-extension.mjs"
        scenario = home / "scenario.json"
        script.write_text(EXTENSION_DRIVER, encoding="utf-8")
        scenario.write_text(json.dumps({"snap": str(snap), "steps": steps}), encoding="utf-8")
        base = {key: os.environ[key] for key in ("PATH", "TMPDIR", "LANG") if key in os.environ}
        base.update({
            "HOME": str(home),
            "HERDR_ENV": "1",
            "HERDR_SOCKET_PATH": str(home / "missing.sock"),
            "HERDR_PANE_ID": self.PANE,
            "HERDR_WORKSPACE_ID": self.WORKSPACE,
            "HERDR_SESSION": "isolated",
            "EXT": str(EXTENSION),
        })
        if env:
            for key, value in env.items():
                if value is None:
                    base.pop(key, None)
                else:
                    base[key] = value
        proc = subprocess.run(
            [BUN, str(script), str(scenario)],
            cwd=home,
            env=base,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("SECRET_TRANSCRIPT_SENTINEL", proc.stdout)
        return json.loads(proc.stdout)

    def extension_home(self) -> tuple[Path, Path, str]:
        home = self.root / "ext-home"
        home.mkdir()
        journal = home / "sessions" / "main.jsonl"
        write_omp_journal(journal, "sid-main")
        raw = str(journal.parent / ".." / journal.parent.name / journal.name)
        return home, journal, raw

    @unittest.skipUnless(EXTENSION.is_file(), f"OMP extension missing: {EXTENSION}")
    @unittest.skipUnless(BUN and Path(BUN).is_file(), f"Bun missing: {BUN or 'not found on PATH'}")
    def test_extension_exports_native_arithmetic_securely(self):
        home, journal, raw = self.extension_home()
        usage = {"tokens": 100, "contextWindow": 10000, "percent": 0.99}
        result = self.run_extension(home, [{
            "hook": "session_start",
            "usage": usage,
            "sessionFile": raw,
            "sessionId": "sid-main",
        }])
        step = result["results"][0]
        self.assertFalse(step["missing"], step)
        self.assertFalse(step["threw"], step)
        immediate = step["immediate"]
        self.assertIsNotNone(immediate)
        self.assertEqual(immediate["schema_version"], 1)
        self.assertEqual(immediate["source"], SOURCE)
        self.assertEqual(immediate["context_usage"]["tokens"], 100)
        self.assertEqual(immediate["context_usage"]["context_window"], 10000)
        self.assertNotEqual(immediate["context_usage"]["tokens"], 9900)
        self.assertEqual(immediate["pid"], result["pid"])
        self.assertEqual(immediate["omp_session_file"], str(journal.resolve()))
        self.assertEqual(immediate["omp_session_id"], "sid-main")
        self.assertEqual(immediate["model"], {"provider": "anthropic", "id": "claude"})
        self.assertNotIn("SECRET_TRANSCRIPT_SENTINEL", json.dumps(immediate))
        snap = context_snapshot_path(self.WORKSPACE, self.PANE, home)
        self.assertEqual(stat.S_IMODE(snap.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snap.parent.stat().st_mode), 0o700)
        self.assertEqual(snap.stat().st_uid, os.getuid())
        self.assertFalse(snap.is_symlink())
        self.assertEqual([path.name for path in snap.parent.iterdir()], [snap.name])

    @unittest.skipUnless(EXTENSION.is_file(), f"OMP extension missing: {EXTENSION}")
    @unittest.skipUnless(BUN and Path(BUN).is_file(), f"Bun missing: {BUN or 'not found on PATH'}")
    def test_extension_refuses_symlink_export(self):
        home, journal, _ = self.extension_home()
        snap = context_snapshot_path(self.WORKSPACE, self.PANE, home)
        target_dir = home / "elsewhere"
        target_dir.mkdir()
        snap.parent.parent.mkdir(parents=True)
        snap.parent.symlink_to(target_dir, target_is_directory=True)
        sentinel = target_dir / snap.name
        sentinel.write_text("untouched\n", encoding="utf-8")
        self.run_extension(home, [{
            "hook": "session_start",
            "usage": {"tokens": 100, "contextWindow": 1000, "percent": 0.9},
            "sessionFile": str(journal),
            "sessionId": "sid-main",
        }])
        self.assertTrue(snap.parent.is_symlink())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "untouched\n")
        home2 = self.root / "ext-file-link"
        home2.mkdir()
        journal2 = home2 / "sessions" / "main.jsonl"
        write_omp_journal(journal2, "sid-main")
        snap2 = context_snapshot_path(self.WORKSPACE, self.PANE, home2)
        snap2.parent.mkdir(parents=True)
        snap2.parent.chmod(0o700)
        sentinel2 = home2 / "sentinel.json"
        sentinel2.write_text("untouched\n", encoding="utf-8")
        snap2.symlink_to(sentinel2)
        self.run_extension(home2, [{
            "hook": "session_start",
            "usage": {"tokens": 100, "contextWindow": 1000, "percent": 0.9},
            "sessionFile": str(journal2),
            "sessionId": "sid-main",
        }])
        self.assertTrue(snap2.is_symlink())
        self.assertEqual(sentinel2.read_text(encoding="utf-8"), "untouched\n")

    @unittest.skipUnless(EXTENSION.is_file(), f"OMP extension missing: {EXTENSION}")
    @unittest.skipUnless(BUN and Path(BUN).is_file(), f"Bun missing: {BUN or 'not found on PATH'}")
    def test_extension_isolates_child_and_switches_identity(self):
        home, journal, raw = self.extension_home()
        other = home / "sessions" / "other.jsonl"
        write_omp_journal(other, "sid-other")
        other_raw = str(other.parent / ".." / other.parent.name / other.name)
        result = self.run_extension(home, [
            {"hook": "session_start", "usage": {"tokens": 8000, "contextWindow": 10000, "percent": 0.01}, "sessionFile": raw, "sessionId": "sid-main"},
            {"hook": "tool_execution_start", "event": {"toolName": "read"}, "usage": {"tokens": 9000, "contextWindow": 10000, "percent": 0.01}, "sessionFile": raw, "sessionId": "sid-main"},
            {"hook": "session_switch", "usage": {"tokens": 9000, "contextWindow": 10000, "percent": 0.5}, "sessionFile": other_raw, "sessionId": "sid-other"},
            {"hook": "model_changed", "usage": {"tokens": 9000, "contextWindow": 10000, "percent": 0.5}, "model": {"provider": "openai", "id": "gpt"}, "sessionFile": other_raw, "sessionId": "sid-other"},
            {"bind": "child", "hook": "tool_execution_start", "event": {"toolName": "read"}, "hasUI": True, "agent": {"kind": "sub", "id": "child", "name": "explore", "depth": 1}, "usage": {"tokens": 1, "contextWindow": 10000, "percent": 0.99}, "sessionFile": raw, "sessionId": "sid-child"},
        ])
        start, tool, switched, model, child = result["results"]
        self.assertFalse(any(step["missing"] or step["threw"] for step in result["results"]))
        self.assertEqual(tool["immediate"]["context_usage"]["tokens"], 9000)
        self.assertEqual(switched["immediate"]["omp_session_id"], "sid-other")
        self.assertEqual(switched["immediate"]["omp_session_file"], str(other.resolve()))
        self.assertEqual(model["immediate"]["model"], {"provider": "openai", "id": "gpt"})
        self.assertEqual(child["after"]["context_usage"]["tokens"], 9000)
        self.assertEqual(child["after"]["omp_session_id"], "sid-other")
        self.assertEqual(start["immediate"]["pid"], result["pid"])
        child_only = self.root / "child-only"
        child_only.mkdir()
        journal_only = child_only / "sessions" / "main.jsonl"
        write_omp_journal(journal_only, "sid-main")
        self.run_extension(child_only, [{
            "bind": "child",
            "hook": "session_start",
            "hasUI": True,
            "agent": {"kind": "sub", "id": "child", "name": "explore", "depth": 0},
            "usage": {"tokens": 9000, "contextWindow": 10000, "percent": 0.9},
            "sessionFile": str(journal_only),
            "sessionId": "sid-main",
        }])
        self.assertFalse(context_snapshot_path(self.WORKSPACE, self.PANE, child_only).exists())
        hidden = self.root / "no-ui"
        hidden.mkdir()
        hidden_journal = hidden / "sessions" / "main.jsonl"
        write_omp_journal(hidden_journal, "sid-main")
        self.run_extension(hidden, [{
            "hook": "session_start",
            "hasUI": False,
            "usage": {"tokens": 9000, "contextWindow": 10000, "percent": 0.9},
            "sessionFile": str(hidden_journal),
            "sessionId": "sid-main",
        }])
        self.assertFalse(context_snapshot_path(self.WORKSPACE, self.PANE, hidden).exists())

    @unittest.skipUnless(EXTENSION.is_file(), f"OMP extension missing: {EXTENSION}")
    @unittest.skipUnless(BUN and Path(BUN).is_file(), f"Bun missing: {BUN or 'not found on PATH'}")
    def test_extension_invalidates_compaction_shutdown_and_malformed_usage(self):
        home, _, raw = self.extension_home()
        result = self.run_extension(home, [
            {"hook": "session_start", "usage": {"tokens": 8000, "contextWindow": 10000, "percent": 0.1}, "sessionFile": raw, "sessionId": "sid-main"},
            {"hook": "auto_compaction_start", "usage": {"tokens": 8000, "contextWindow": 10000, "percent": 0.99}, "sessionFile": raw, "sessionId": "sid-main"},
            {"hook": "auto_compaction_end", "usage": {"tokens": 100, "contextWindow": 10000, "percent": 0.8}, "sessionFile": raw, "sessionId": "sid-main"},
            {"hook": "session_shutdown", "usage": {"tokens": 8000, "contextWindow": 10000, "percent": 0.99}, "sessionFile": raw, "sessionId": "sid-main"},
        ])
        start, compacting, compacted, shutdown = result["results"]
        self.assertFalse(any(step["missing"] or step["threw"] for step in result["results"]))
        self.assertEqual(start["immediate"]["context_usage"]["tokens"], 8000)
        self.assertFalse(compacting["immediate"]["available"])
        self.assertEqual(compacting["immediate"]["reason"], "compaction")
        self.assertNotIn("context_usage", compacting["immediate"])
        self.assertEqual(compacted["immediate"]["context_usage"]["tokens"], 100)
        self.assertTrue(compacted["immediate"]["available"])
        self.assertFalse(shutdown["immediate"]["available"])
        self.assertEqual(shutdown["immediate"]["reason"], "session_shutdown")
        self.assertNotIn("context_usage", shutdown["immediate"])
        for name, usage in {
            "percent-only": {"percent": 0.9},
            "bool": {"tokens": True, "contextWindow": 2, "percent": 0.5},
            "nan": "nan",
            "throw": "throw",
            "null": None,
        }.items():
            with self.subTest(case=name):
                case_home = self.root / f"bad-{name}"
                case_home.mkdir()
                case_journal = case_home / "sessions" / "main.jsonl"
                write_omp_journal(case_journal, "sid-main")
                outcome = self.run_extension(case_home, [
                    {"hook": "session_start", "usage": {"tokens": 50, "contextWindow": 100, "percent": 0.1}, "sessionFile": str(case_journal), "sessionId": "sid-main"},
                    {"hook": "turn_end", "usage": usage, "sessionFile": str(case_journal), "sessionId": "sid-main"},
                ])
                refreshed = outcome["results"][1]
                self.assertFalse(refreshed["missing"])
                self.assertFalse(refreshed["threw"])
                snapshot = refreshed["immediate"]
                self.assertIsNotNone(snapshot)
                self.assertFalse(snapshot["available"])
                self.assertIn(snapshot["reason"], {"invalid", "unavailable"})
                self.assertNotIn("context_usage", snapshot)
                self.assertNotEqual(snapshot.get("context_usage", {}).get("tokens"), 50)

    @unittest.skipUnless(EXTENSION.is_file(), f"OMP extension missing: {EXTENSION}")
    @unittest.skipUnless(BUN and Path(BUN).is_file(), f"Bun missing: {BUN or 'not found on PATH'}")
    def test_extension_disabled_import_does_not_export(self):
        home, journal, _ = self.extension_home()
        self.run_extension(home, [{
            "hook": "session_start",
            "usage": {"tokens": 9000, "contextWindow": 10000, "percent": 0.9},
            "sessionFile": str(journal),
            "sessionId": "sid-main",
        }], env={"HERDR_ENV": None, "HERDR_PANE_ID": None})
        self.assertFalse(context_snapshot_path(self.WORKSPACE, self.PANE, home).exists())

if __name__ == "__main__":
    unittest.main()
