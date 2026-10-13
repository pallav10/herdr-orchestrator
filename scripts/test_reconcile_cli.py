#!/usr/bin/env python3

from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import liveness
import reconcile
from testkit import DEFAULT_HOLDER, dispatch_intent_event, plant_run_files, run_module, write_events

LIVE_AGENT = {
    "terminal_id": "terminal-live",
    "agent_id": "agt-live",
    "name": "worker-label",
    "agent": "omp",
    "workspace_id": "ws-live",
    "pane_id": "pane-live",
}
LIVE_TOKENS = {
    "provenance": "orchestrator",
    "run": "run-abc",
    "node": "A",
    "attempt": "1",
    "role": "writer",
}
LIVE_RECORD = {
    "workspace": "ws-live",
    "pane": "pane-live",
    "agent": "agt-live",
    "tokens": {
        "provenance": "orchestrator",
        "run": "run-abc",
        "node": "A",
        "attempt": "1",
    },
}


def fake_herdr(*args: str):
    if args[:2] == ("agent", "list"):
        return {"result": {"agents": [LIVE_AGENT]}}
    if args[:2] == ("workspace", "get"):
        assert args[2] == "ws-live"
        return {
            "result": {
                "workspace": {
                    "workspace_id": "ws-live",
                    "tokens": LIVE_TOKENS,
                    "root_pane": {"pane_id": "pane-from-ws"},
                }
            }
        }
    raise AssertionError(args)


def herdr_proc(rc, stdout="", stderr="", args=("herdr", "agent", "list")):
    return subprocess.CompletedProcess(list(args), rc, stdout, stderr)


def server_absent(*args: str):
    if args[:2] == ("agent", "list"):
        exc = reconcile.ioutil.RunError(
            'herdr agent list failed: {"error":{"code":"server_not_running"}}'
        )
        exc.code = "server_not_running"
        raise exc
    raise AssertionError(args)


HERDR_RUNNING = {
    "nodes": {
        "H": {
            "executor": "herdr-agent",
            "state": "running",
            "attempt": 1,
            "handle": {"workspace": "ws-live", "pane": "pane-live", "agent": "worker-label"},
        }
    }
}


class ReconcileCliTest(unittest.TestCase):
    def test_twenty_node_compact_and_full_output(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            nodes = {
                f"N{index:02}": {"id": f"N{index:02}", "executor": "herdr-agent"}
                for index in range(20)
            }
            events = [{"type": "session_run_bound", **DEFAULT_HOLDER}]
            for node_id in nodes:
                plant_run_files(run, node=node_id, run_id="run-abc", nodes=nodes)
                events.extend([
                    {"type": "ready", "node": node_id, "attempt": 1},
                    dispatch_intent_event(node_id, 1),
                    {
                        "type": "dispatched", "node": node_id, "attempt": 1,
                        "handle": {
                            "workspace": f"ws-{node_id}", "pane": f"pane-{node_id}",
                            "agent": f"agent-{node_id}",
                            "tokens": {
                                "provenance": "orchestrator", "run": "run-abc",
                                "node": node_id, "attempt": "1",
                            },
                        },
                        "model": {
                            "requested_role": None, "requested_effort": None,
                            "resolved": "provider/live", "thinking": "high", "effort": "medium",
                        },
                    },
                ])
            events.append({
                "type": "blocked", "node": "N00", "attempt": 1,
                "reason": "Approval required", "question": "Continue?",
            })
            write_events(run, events)
            for truncated in (False, True):
                with self.subTest(truncated_tail=truncated):
                    if truncated:
                        with (run / "events.jsonl").open("a", encoding="utf-8") as stream:
                            stream.write('{"type":')
                    expected = reconcile.scheduler.reconcile(run)
                    for full in (False, True):
                        with self.subTest(full=full):
                            code, stdout, stderr = run_module(
                                reconcile,
                                ["reconcile.py", "--run", str(run), *(["--full"] if full else [])],
                            )
                            self.assertEqual((code, stderr), (0, ""))
                            payload = json.loads(stdout)
                            if full:
                                self.assertEqual(payload, expected)
                                self.assertEqual(
                                    stdout,
                                    json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n",
                                )
                            else:
                                self.assertLessEqual(len(stdout.encode("utf-8")), 3000)
                                self.assertEqual(set(payload), {
                                    "epoch", "dispatch", "lease", "holder", "nodes", "notified",
                                    *({"truncated_tail"} if truncated else set()),
                                })
                                controller = expected["controller"]
                                self.assertEqual(payload["holder"], {
                                    key: controller[key] for key in ("workspace", "pane", "session")
                                })
                                for key in ("epoch", "dispatch", "lease"):
                                    self.assertEqual(payload[key], controller[key])
                                self.assertEqual(payload["notified"], expected["notified"])
                                self.assertEqual(payload["nodes"], {
                                    node_id: {
                                        key: node[key]
                                        for key in ("state", "attempt", "reason", "question")
                                        if key in node
                                    }
                                    for node_id, node in expected["state"]["nodes"].items()
                                })
                                self.assertTrue(all(node["attempt"] == 1 for node in payload["nodes"].values()))
                                if truncated:
                                    self.assertIs(payload["truncated_tail"], True)

    def test_run_observation_skips_five_unrelated_workspaces(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            attempt_dir = plant_run_files(run, node="H", run_id="run-abc")
            (attempt_dir / "dispatch-identity.json").write_text(
                json.dumps({**LIVE_RECORD, "tokens": {**LIVE_RECORD["tokens"], "node": "H"}}) + "\n",
                encoding="utf-8",
            )
            agents = [
                {
                    "agent_id": f"unrelated-{index}", "name": "worker-label",
                    "workspace_id": f"ws-unrelated-{index}", "pane_id": f"pane-{index}",
                }
                for index in range(5)
            ]
            with mock.patch.object(
                reconcile, "herdr", return_value={"result": {"agents": agents}},
            ) as herdr:
                self.assertEqual(reconcile.observe_live_herdr(run), [])
            self.assertEqual(
                [call for call in herdr.call_args_list if call.args[:2] == ("workspace", "get")],
                [],
            )

    def test_complete_holder_reaches_reconcile(self):
        captured = {}

        def fake_reconcile(run, **kwargs):
            captured["run"] = run
            captured.update(kwargs)
            return {"ok": True}

        argv = [
            "reconcile.py",
            "--full",
            "--run",
            "/tmp/run",
            "--event",
            json.dumps({"type": "dispatch_intent", "node": "A", "attempt": 1}),
            "--repo",
            "/tmp/repo",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
            "--session",
            "session.jsonl",
            "--live-native-id",
            "task-1",
            "--live-native-id",
            "workflow-1",
            "--native-inventory-complete",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "_recorded_herdr_identities", return_value=[(LIVE_RECORD, "herdr-agent")]),
            mock.patch.object(reconcile, "herdr", fake_herdr),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(reconcile.main(), 0)

        self.assertEqual(json.loads(out.getvalue()), {"ok": True})
        self.assertEqual(captured["run"], "/tmp/run")
        self.assertEqual(captured["event"], {"type": "dispatch_intent", "node": "A", "attempt": 1})
        self.assertEqual(captured["repo"], "/tmp/repo")
        self.assertEqual(captured["current_session"], "session.jsonl")
        self.assertEqual(captured["holder"], {"workspace": "ws", "pane": "pane", "session": "session.jsonl"})
        self.assertEqual(captured["live_native_ids"], ["task-1", "workflow-1"])
        self.assertEqual(captured["live_herdr"], [LIVE_RECORD])
        self.assertTrue(captured["native_inventory_complete"])

    def test_usage_errors(self):
        cases = [
            ("session-without-holder", ["--session", "session.jsonl"]),
            ("event-without-holder", ["--event", "{}"]),
            ("notify-without-holder", ["--notify"]),
            ("partial-holder", ["--event", "{}", "--holder-workspace", "ws"]),
            ("native-id-without-completeness", ["--session", "session", "--live-native-id", "task-1"]),
            ("complete-inventory-without-session", ["--native-inventory-complete"]),
        ]
        for name, args in cases:
            with self.subTest(case=name):
                code, _stdout, _stderr = run_module(
                    reconcile, ["reconcile.py", "--run", "/tmp/run", *args]
                )
                self.assertEqual(code, 2)

    def test_session_without_completeness_leaves_native_inventory_unknown(self):
        captured = {}

        def fake_reconcile(run, **kwargs):
            captured["run"] = run
            captured.update(kwargs)
            return {"ok": True}

        argv = [
            "reconcile.py",
            "--full",
            "--run",
            "/tmp/run",
            "--session",
            "session",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "_recorded_herdr_identities", return_value=[(LIVE_RECORD, "herdr-agent")]),
            mock.patch.object(reconcile, "herdr", fake_herdr),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(reconcile.main(), 0)

        self.assertIsNone(captured["live_native_ids"])
        self.assertFalse(captured["native_inventory_complete"])
        self.assertEqual(captured["live_herdr"], [LIVE_RECORD])
        self.assertEqual(captured["current_session"], "session")
        self.assertEqual(
            captured["holder"],
            {"workspace": "ws", "pane": "pane", "session": "session"},
        )

    def test_complete_empty_native_inventory_is_authoritative(self):
        captured = {}

        def fake_reconcile(run, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        argv = [
            "reconcile.py",
            "--full",
            "--run",
            "/tmp/run",
            "--session",
            "session",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
            "--native-inventory-complete",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "_recorded_herdr_identities", return_value=[(LIVE_RECORD, "herdr-agent")]),
            mock.patch.object(reconcile, "herdr", fake_herdr),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(reconcile.main(), 0)

        self.assertEqual(captured["live_native_ids"], [])
        self.assertTrue(captured["native_inventory_complete"])
        self.assertEqual(captured["live_herdr"], [LIVE_RECORD])
        self.assertEqual(captured["current_session"], "session")


    def test_plain_run_is_observation_free(self):
        captured = {}

        def fake_reconcile(run, **kwargs):
            captured["run"] = run
            captured.update(kwargs)
            return {"ok": True}

        argv = ["reconcile.py", "--full", "--run", "/tmp/run"]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "herdr", side_effect=AssertionError("herdr")),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(reconcile.main(), 0)

        self.assertEqual(json.loads(out.getvalue()), {"ok": True})
        self.assertIsNone(captured["live_herdr"])
        self.assertIsNone(captured["live_native_ids"])
        self.assertFalse(captured["native_inventory_complete"])
        self.assertIsNone(captured["current_session"])
        self.assertIsNone(captured["holder"])
        self.assertIsNone(captured["event"])
        self.assertFalse(captured["notify"])
        self.assertIsNone(captured["repo"])

    def test_require_native_quiesced_without_inventory_stays_unknown(self):
        captured = {}

        def fake_reconcile(run, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        argv = [
            "reconcile.py",
            "--full",
            "--run",
            "/tmp/run",
            "--session",
            "session",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
            "--require-native-quiesced",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "_recorded_herdr_identities", return_value=[(LIVE_RECORD, "herdr-agent")]),
            mock.patch.object(reconcile, "herdr", fake_herdr),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(reconcile.main(), 0)

        self.assertTrue(captured["require_native_quiesced"])
        self.assertIsNone(captured["live_native_ids"])
        self.assertFalse(captured["native_inventory_complete"])
        self.assertEqual(captured["current_session"], "session")
        self.assertEqual(captured["live_herdr"], [LIVE_RECORD])

    def test_require_native_quiesced_complete_empty_passes_empty_list(self):
        captured = {}

        def fake_reconcile(run, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        argv = [
            "reconcile.py",
            "--full",
            "--run",
            "/tmp/run",
            "--session",
            "session",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
            "--require-native-quiesced",
            "--native-inventory-complete",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "_recorded_herdr_identities", return_value=[(LIVE_RECORD, "herdr-agent")]),
            mock.patch.object(reconcile, "herdr", fake_herdr),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(reconcile.main(), 0)

        self.assertTrue(captured["require_native_quiesced"])
        self.assertEqual(captured["live_native_ids"], [])
        self.assertTrue(captured["native_inventory_complete"])

    def test_unknown_inventory_interrupts_old_session_not_same_session(self):
        state = {
            "nodes": {
                "T": {
                    "executor": "task",
                    "state": "running",
                    "attempt": 1,
                    "handle": {"spawning_session": "session", "job_id": "task-1"},
                },
                "O": {
                    "executor": "task",
                    "state": "running",
                    "attempt": 1,
                    "handle": {"spawning_session": "other", "job_id": "old-1"},
                },
                "A": {
                    "executor": "task",
                    "state": "ready",
                    "dispatch_intent": True,
                },
            }
        }
        unknown = liveness.maybe_interrupt_native(state, "session", None)
        self.assertEqual({event["node"] for event in unknown}, {"O"})
        leftover = {
            "nodes": {
                name: ({**node, "state": "interrupted"} if name == "O" else node)
                for name, node in state["nodes"].items()
            }
        }
        self.assertFalse(liveness.native_quiesced(leftover))
        vanished = liveness.maybe_interrupt_native(state, "session", [], native_inventory_complete=True)
        self.assertEqual({event["node"] for event in vanished}, {"T", "O"})


    def test_stable_agent_id_preferred_over_name(self):
        captured = {}

        def fake_reconcile(run, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        agent = {
            "terminal_id": "terminal-live",
            "agent_id": "agt-stable",
            "id": "agt-other",
            "name": "worker-label",
            "agent": "omp",
            "workspace_id": "ws-live",
            "pane_id": "pane-live",
        }

        def herdr(*args: str):
            if args[:2] == ("agent", "list"):
                return {"result": {"agents": [agent]}}
            return fake_herdr(*args)

        argv = [
            "reconcile.py",
            "--full",
            "--run",
            "/tmp/run",
            "--session",
            "session",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "_recorded_herdr_identities", return_value=[({**LIVE_RECORD, "agent": "agt-stable"}, "herdr-agent")]),
            mock.patch.object(reconcile, "herdr", herdr),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(reconcile.main(), 0)

        record = captured["live_herdr"][0]
        self.assertEqual(record["agent"], "agt-stable")
        self.assertEqual(record["workspace"], "ws-live")
        self.assertEqual(record["pane"], "pane-live")
        self.assertEqual(record["tokens"], LIVE_RECORD["tokens"])

    def test_observe_name_without_stable_id_is_failed(self):
        def herdr(*args: str):
            if args[:2] == ("agent", "list"):
                return {
                    "result": {
                        "agents": [
                            {
                                "name": "worker-label",
                                "agent": "omp",
                                "workspace_id": "ws-live",
                                "pane_id": "pane-live",
                            }
                        ]
                    }
                }
            return fake_herdr(*args)

        with mock.patch.object(reconcile, "herdr", herdr):
            with self.assertRaises(reconcile.ioutil.RunError):
                reconcile.observe_live_herdr()

    def test_observe_server_not_running_is_unknown(self):
        for payload in (
            {"error": {"code": "server_not_running"}},
            {"result": {"error": {"code": "server_not_running"}}},
        ):
            with self.subTest(payload=payload):
                with mock.patch.object(
                    reconcile.subprocess,
                    "run",
                    return_value=herdr_proc(1, stderr=json.dumps(payload)),
                ):
                    self.assertIsNone(reconcile.observe_live_herdr())

    def test_observe_server_not_running_on_stdout_is_unknown(self):
        payload = json.dumps({"error": {"code": "server_not_running"}})
        with mock.patch.object(
            reconcile.subprocess,
            "run",
            return_value=herdr_proc(1, stdout=payload),
        ):
            self.assertIsNone(reconcile.observe_live_herdr())

    def test_observe_empty_agent_list_is_authoritative(self):
        def herdr(*args: str):
            if args[:2] == ("agent", "list"):
                return {"result": {"agents": []}}
            raise AssertionError(args)

        with mock.patch.object(reconcile, "herdr", herdr):
            self.assertEqual(reconcile.observe_live_herdr(), [])
        self.assertEqual(
            {event["node"] for event in liveness.maybe_interrupt_herdr(HERDR_RUNNING, [])},
            {"H"},
        )

    def test_observe_incomplete_identity_is_failed(self):
        cases = [
            ("incomplete-agent", [LIVE_AGENT, {"name": "ghost"}], None),
            (
                "incomplete-pane",
                [{"agent_id": "agt-live", "name": "worker-label", "workspace_id": "ws-live"}],
                {"workspace_id": "ws-live", "tokens": LIVE_TOKENS},
            ),
            (
                "name-only-workspace",
                [LIVE_AGENT, {
                    "agent_id": "agt-named", "name": "ghost-label",
                    "workspace": {"name": "ws-label"}, "pane_id": "pane-named",
                }],
                {"workspace_id": "ws-live", "tokens": LIVE_TOKENS,
                 "root_pane": {"pane_id": "pane-from-ws"}},
            ),
            (
                "name-only-pane",
                [LIVE_AGENT, {
                    "agent_id": "agt-named", "name": "ghost-label",
                    "workspace_id": "ws-live", "pane": {"name": "pane-label"},
                }],
                {"workspace_id": "ws-live", "tokens": LIVE_TOKENS,
                 "root_pane": {"name": "root-pane-label"}},
            ),
            (
                "name-only-root-pane",
                [{"agent_id": "agt-named", "name": "ghost-label", "workspace_id": "ws-live"}],
                {"workspace_id": "ws-live", "tokens": LIVE_TOKENS,
                 "root_pane": {"name": "root-pane-label"}, "pane": {"name": "pane-label"}},
            ),
        ]
        for name, agents, workspace in cases:
            with self.subTest(case=name):
                def herdr(*args: str):
                    if args[:2] == ("agent", "list"):
                        return {"result": {"agents": agents}}
                    if args[:2] == ("workspace", "get") and workspace is not None:
                        return {"result": {"workspace": workspace}}
                    return fake_herdr(*args)

                with mock.patch.object(reconcile, "herdr", herdr):
                    with self.assertRaises(reconcile.ioutil.RunError):
                        reconcile.observe_live_herdr()

    def test_observe_unrelated_structured_error_raises(self):
        payload = json.dumps({"error": {"code": "protocol_mismatch"}})
        with mock.patch.object(
            reconcile.subprocess, "run", return_value=herdr_proc(1, stderr=payload)
        ):
            with self.assertRaises(reconcile.ioutil.RunError):
                reconcile.observe_live_herdr()

    def test_observe_malformed_error_output_raises(self):
        for stderr in (
            "not-json",
            "[]",
            json.dumps({"error": "server_not_running"}),
            json.dumps({"error": {"code": ["server_not_running"]}}),
        ):
            with self.subTest(stderr=stderr):
                with mock.patch.object(
                    reconcile.subprocess, "run", return_value=herdr_proc(1, stderr=stderr)
                ):
                    with self.assertRaises(reconcile.ioutil.RunError):
                        reconcile.observe_live_herdr()

    def test_observe_list_then_workspace_server_not_running_is_unknown(self):
        def run(cmd, **_kwargs):
            args = tuple(cmd[1:])
            if args[:2] == ("agent", "list"):
                return herdr_proc(
                    0,
                    stdout=json.dumps({"result": {"agents": [LIVE_AGENT]}}),
                    args=cmd,
                )
            if args[:2] == ("workspace", "get"):
                return herdr_proc(
                    1,
                    stderr=json.dumps({"error": {"code": "server_not_running"}}),
                    args=cmd,
                )
            raise AssertionError(cmd)

        with mock.patch.object(reconcile.subprocess, "run", side_effect=run):
            self.assertIsNone(reconcile.observe_live_herdr())

    def test_observe_list_then_workspace_protocol_mismatch_raises(self):
        def run(cmd, **_kwargs):
            args = tuple(cmd[1:])
            if args[:2] == ("agent", "list"):
                return herdr_proc(
                    0,
                    stdout=json.dumps({"result": {"agents": [LIVE_AGENT]}}),
                    args=cmd,
                )
            if args[:2] == ("workspace", "get"):
                return herdr_proc(
                    1,
                    stderr=json.dumps({"error": {"code": "protocol_mismatch"}}),
                    args=cmd,
                )
            raise AssertionError(cmd)

        with mock.patch.object(reconcile.subprocess, "run", side_effect=run):
            with self.assertRaises(reconcile.ioutil.RunError):
                reconcile.observe_live_herdr()

    def test_incomplete_observation_does_not_reconcile(self):
        called = []

        def fake_reconcile(*_args, **_kwargs):
            called.append(True)
            return {"ok": True}

        def herdr(*args: str):
            if args[:2] == ("agent", "list"):
                return {"result": {"agents": [{"workspace_id": "ws-live"}]}}
            if args[:2] == ("workspace", "get"):
                return {"result": {"workspace": {"tokens": {}}}}
            raise AssertionError(args)

        argv = [
            "reconcile.py",
            "--run",
            "/tmp/run",
            "--session",
            "session",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "_recorded_herdr_identities", return_value=[(LIVE_RECORD, "herdr-agent")]),
            mock.patch.object(reconcile, "herdr", herdr),
        ):
            with self.assertRaises(reconcile.ioutil.RunError):
                reconcile.main()
        self.assertEqual(called, [])

    def test_name_only_nested_workspace_does_not_reconcile(self):
        called = []

        def fake_reconcile(*_args, **_kwargs):
            called.append(True)
            return {"ok": True}

        def herdr(*args: str):
            if args[:2] == ("agent", "list"):
                return {
                    "result": {
                        "agents": [
                            {
                                "agent_id": "agt-named",
                                "name": "ghost-label",
                                "workspace": {"name": "ws-label"},
                                "pane_id": "pane-named",
                            }
                        ]
                    }
                }
            if args[:2] == ("workspace", "get"):
                return {
                    "result": {
                        "workspace": {
                            "workspace_id": "ws-live",
                            "tokens": LIVE_TOKENS,
                            "root_pane": {"pane_id": "pane-from-ws"},
                        }
                    }
                }
            raise AssertionError(args)

        argv = [
            "reconcile.py",
            "--run",
            "/tmp/run",
            "--session",
            "session",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "_recorded_herdr_identities", return_value=[(LIVE_RECORD, "herdr-agent")]),
            mock.patch.object(reconcile, "herdr", herdr),
        ):
            with self.assertRaises(reconcile.ioutil.RunError):
                reconcile.main()
        self.assertEqual(called, [])

    def test_holder_session_unknown_herdr_preserves_running(self):
        captured = {}

        def fake_reconcile(run, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        argv = [
            "reconcile.py",
            "--full",
            "--run",
            "/tmp/run",
            "--session",
            "session",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "herdr", server_absent),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(reconcile.main(), 0)

        self.assertEqual(json.loads(out.getvalue()), {"ok": True})
        self.assertIsNone(captured["live_herdr"])
        self.assertIsNone(captured["live_native_ids"])
        self.assertFalse(captured["native_inventory_complete"])
        self.assertEqual(
            liveness.maybe_interrupt_herdr(HERDR_RUNNING, captured["live_herdr"]),
            [],
        )

    def test_holder_session_herdr_free_still_reconciles(self):
        captured = {}

        def fake_reconcile(run, **kwargs):
            captured.update(kwargs)
            return {"ok": True}

        argv = [
            "reconcile.py",
            "--full",
            "--run",
            "/tmp/run",
            "--session",
            "session",
            "--holder-workspace",
            "ws",
            "--holder-pane",
            "pane",
            "--native-inventory-complete",
        ]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(reconcile.scheduler, "reconcile", fake_reconcile),
            mock.patch.object(reconcile, "herdr", server_absent),
        ):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(reconcile.main(), 0)

        self.assertEqual(json.loads(out.getvalue()), {"ok": True})
        self.assertIsNone(captured["live_herdr"])
        self.assertEqual(captured["live_native_ids"], [])
        self.assertTrue(captured["native_inventory_complete"])
        native_only = {
            "nodes": {
                "T": {
                    "executor": "task",
                    "state": "running",
                    "attempt": 1,
                    "handle": {"spawning_session": "session", "job_id": "task-1"},
                }
            }
        }
        self.assertEqual(liveness.maybe_interrupt_herdr(native_only, None), [])

    def test_observe_preserves_blocked_reason_question(self):
        def herdr(*args: str):
            if args[:2] == ("agent", "list"):
                return {
                    "result": {
                        "agents": [
                            {
                                **LIVE_AGENT,
                                "agent_status": "blocked",
                                "reason": "Approval required",
                                "question": "Continue?",
                            }
                        ]
                    }
                }
            if args[:2] == ("workspace", "get"):
                return fake_herdr(*args)
            raise AssertionError(args)

        with mock.patch.object(reconcile, "herdr", herdr):
            records = reconcile.observe_live_herdr()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["agent_status"], "blocked")
        self.assertEqual(records[0]["reason"], "Approval required")
        self.assertEqual(records[0]["question"], "Continue?")
        self.assertEqual(records[0]["workspace"], "ws-live")
        self.assertEqual(records[0]["pane"], "pane-live")
        self.assertEqual(records[0]["agent"], "agt-live")

    def test_blocked_observation_reaches_run_state(self):
        

        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            nodes = {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}
            plant_run_files(run, node="H", run_id="run-abc", nodes=nodes)
            handle = {
                "workspace": "ws-live",
                "pane": "pane-live",
                "agent": "worker-label",
                "tokens": {"provenance": "orchestrator", "run": "run-abc", "node": "H", "attempt": "1"},
            }
            model = {
                "requested_role": None,
                "requested_effort": None,
                "resolved": "provider/live",
                "thinking": "high",
                "effort": "medium",
            }
            write_events(
                run,
                [
                    {"type": "session_run_bound", **DEFAULT_HOLDER},
                    {"type": "ready", "node": "H", "from": "planned", "to": "ready", "attempt": 1},
                    dispatch_intent_event("H", 1),
                    {"type": "dispatched", "node": "H", "attempt": 1, "handle": handle, "model": model},
                ],
            )

            def herdr(*args: str):
                if args[:2] == ("agent", "list"):
                    return {
                        "result": {
                            "agents": [
                                {
                                    "name": "worker-label",
                                    "agent_id": "worker-label",
                                    "workspace_id": "ws-live",
                                    "pane_id": "pane-live",
                                    "agent_status": "blocked",
                                    "reason": "Approval required",
                                    "question": "Continue?",
                                }
                            ]
                        }
                    }
                if args[:2] == ("workspace", "get"):
                    return {
                        "result": {
                            "workspace": {
                                "workspace_id": "ws-live",
                                "tokens": {
                                    "provenance": "orchestrator",
                                    "run": "run-abc",
                                    "node": "H",
                                    "attempt": "1",
                                },
                                "root_pane": {"pane_id": "pane-live"},
                            }
                        }
                    }
                raise AssertionError(args)

            argv = [
                "reconcile.py",
                "--run",
                str(run),
                "--session",
                DEFAULT_HOLDER["session"],
                "--holder-workspace",
                DEFAULT_HOLDER["workspace"],
                "--holder-pane",
                DEFAULT_HOLDER["pane"],
                "--notify",
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(reconcile, "herdr", herdr):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(reconcile.main(), 0)
            payload = json.loads(out.getvalue())
            events = [
                json.loads(line)
                for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line
            ]
            blocked = [event for event in events if event.get("type") == "blocked"]
            self.assertEqual(len(blocked), 1)
            self.assertEqual(blocked[0].get("reason"), "Approval required")
            self.assertEqual(blocked[0].get("question"), "Continue?")
            self.assertEqual(len(payload.get("notified") or []), 1)
            state = json.loads((run / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["nodes"]["H"]["state"], "blocked")
            self.assertEqual(state["nodes"]["H"].get("reason"), "Approval required")
            self.assertEqual(state["nodes"]["H"].get("question"), "Continue?")

    def test_observe_untagged_rows_are_skipped_and_stable_ids_retained(self):
        def herdr(*args: str):
            if args[:2] == ("agent", "list"):
                return {
                    "result": {
                        "agents": [
                            LIVE_AGENT,
                            {"agent_id": "agt-quiet", "workspace_id": "ws-empty", "pane_id": "pane-quiet"},
                            {"name": "ghost-omitted", "workspace_id": "ws-empty", "pane_id": "pane-c"},
                            {"name": "ghost-empty", "workspace_id": "ws-empty", "pane_id": "pane-d", "tokens": {}},
                        ]
                    }
                }
            if args[:2] == ("workspace", "get"):
                if args[2] == "ws-live":
                    return fake_herdr(*args)
                if args[2] == "ws-empty":
                    return {"result": {"workspace": {"workspace_id": "ws-empty", "tokens": {}}}}
            raise AssertionError(args)

        with mock.patch.object(reconcile, "herdr", herdr):
            records = reconcile.observe_live_herdr()

        self.assertEqual(
            records,
            [
                LIVE_RECORD,
                {"workspace": "ws-empty", "pane": "pane-quiet", "agent": "agt-quiet", "tokens": {}},
            ],
        )

    def test_observe_untagged_row_with_agent_token_marker_is_failed(self):
        for marker in ({"role": "writer"}, {"handoff-ack": ""}, {"custom-flag": "x"}):
            with self.subTest(marker=marker):
                def herdr(*args: str, marker=marker):
                    if args[:2] == ("agent", "list"):
                        return {
                            "result": {
                                "agents": [
                                    {"workspace_id": "ws-empty", "pane_id": "pane-marked", "tokens": marker}
                                ]
                            }
                        }
                    if args[:2] == ("workspace", "get"):
                        return {"result": {"workspace": {"workspace_id": "ws-empty", "tokens": {}}}}
                    raise AssertionError(args)

                with mock.patch.object(reconcile, "herdr", herdr):
                    with self.assertRaises(reconcile.ioutil.RunError):
                        reconcile.observe_live_herdr()

    def test_observe_malformed_workspace_metadata_is_failed(self):
        for name, workspace in (
            ("tokens_null", {"workspace_id": "ws-live", "tokens": None}),
            ("tokens_list", {"workspace_id": "ws-live", "tokens": []}),
            ("tokens_nonstring_value", {"workspace_id": "ws-live", "tokens": {"role": 7}}),
            ("workspace_id_missing", {"tokens": {}}),
            ("workspace_id_mismatch", {"workspace_id": "ws-other", "tokens": {}}),
        ):
            with self.subTest(case=name):
                def herdr(*args: str, workspace=workspace):
                    if args[:2] == ("agent", "list"):
                        return {"result": {"agents": [LIVE_AGENT]}}
                    if args[:2] == ("workspace", "get"):
                        return {"result": {"workspace": workspace}}
                    raise AssertionError(args)

                with mock.patch.object(reconcile, "herdr", herdr):
                    with self.assertRaises(reconcile.ioutil.RunError):
                        reconcile.observe_live_herdr()

    def test_observe_recorded_run_surfaces_refuse_untagged_match(self):
        

        def herdr_for(workspace_id: str, pane_id: str):
            def herdr(*args: str):
                if args[:2] == ("agent", "list"):
                    return {"result": {"agents": [{"name": "ghost", "workspace_id": workspace_id, "pane_id": pane_id}]}}
                if args[:2] == ("workspace", "get"):
                    self.assertEqual(args[2], workspace_id)
                    return {"result": {"workspace": {"workspace_id": workspace_id, "tokens": {}}}}
                raise AssertionError(args)
            return herdr

        # Attempt blocked before a "dispatched" event ever recorded a handle;
        # only dispatch-identity.json (without an agent id) names the surface.
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            attempt_dir = plant_run_files(run, node="H", run_id="run-abc", nodes={"H": {"id": "H", "executor": "herdr-agent"}})
            (attempt_dir / "dispatch-identity.json").write_text(
                json.dumps({"workspace": "ws-orphan", "pane": "pane-orphan"}) + "\n",
                encoding="utf-8",
            )
            reconcile.ledger.append_event(
                run,
                {"type": "blocked", "node": "H", "attempt": 1, "reason": "spawn failed before handle recorded"},
                holder=DEFAULT_HOLDER,
            )

            with mock.patch.object(reconcile, "herdr", herdr_for("ws-unrelated", "pane-unrelated")):
                self.assertEqual(reconcile.observe_live_herdr(run), [])

            with mock.patch.object(reconcile, "herdr", herdr_for("ws-orphan", "pane-orphan")):
                with self.assertRaises(reconcile.ioutil.RunError):
                    reconcile.observe_live_herdr(run)

        # Recorded surface from a dispatched handle (no dispatch-identity.json
        # reliance) is refused the same way; protection is not handle-only.
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            plant_run_files(run, node="H", run_id="run-abc", nodes={"H": {"id": "H", "executor": "herdr-agent"}})
            reconcile.ledger.append_event(
                run,
                {
                    "type": "dispatched",
                    "node": "H",
                    "attempt": 1,
                    "handle": {"workspace": "ws-handle", "pane": "pane-handle", "agent": "agent-handle"},
                    "model": {
                        "requested_role": None,
                        "requested_effort": None,
                        "resolved": None,
                        "thinking": None,
                        "effort": None,
                    },
                },
                holder=DEFAULT_HOLDER,
            )

            with mock.patch.object(reconcile, "herdr", herdr_for("ws-handle", "pane-handle")):
                with self.assertRaises(reconcile.ioutil.RunError):
                    reconcile.observe_live_herdr(run)

    def test_recorded_surfaces_identity_read_excludes_competing_lock_holder(self):
        # A competing writer opening run.lock on its own fd must contend for
        # the exclusive lock for the whole duration of the identity read, not
        # just the replay snapshot; and must be able to acquire it the moment
        # observation finishes.
        

        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            attempt_dir = plant_run_files(
                run, node="H", run_id="run-abc", nodes={"H": {"id": "H", "executor": "herdr-agent"}}
            )
            (attempt_dir / "dispatch-identity.json").write_text(
                json.dumps({"workspace": "ws-orphan", "pane": "pane-orphan"}) + "\n",
                encoding="utf-8",
            )

            lock_path = Path(reconcile.ioutil.canonical_path(run)) / "run.lock"

            def try_adversary_lock() -> bool:
                """Attempt a nonblocking exclusive lock on a fresh fd; True if contended."""
                fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    return False
                finally:
                    os.close(fd)

            contention = []
            original = reconcile.ledger.recorded_herdr_identity

            def probe(state, node_id, node):
                contention.append(try_adversary_lock())
                return original(state, node_id, node)

            with mock.patch.object(reconcile.ledger, 'recorded_herdr_identity', probe):
                surfaces = reconcile._recorded_herdr_surfaces(run)

            # The recorded, agent-id-less surface is still retained (its
            # refusal downstream is unaffected by the locking fix).
            self.assertEqual(surfaces, {("ws-orphan", "pane-orphan")})
            # A competing writer could not take the lock while identity was read.
            self.assertEqual(contention, [True])
            # The lock is released once observation completes.
            self.assertFalse(try_adversary_lock())

    def test_observe_managed_name_matches_recorded_dispatch(self):
        

        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            nodes = {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}
            plant_run_files(run, node="H", run_id="run-abc", nodes=nodes)
            worker_id = reconcile.ioutil.new_worker_id()
            retained_worker_id = reconcile.ioutil.new_worker_id()
            handle = {
                "identity_kind": "managed-name",
                "worker_id": worker_id,
                "workspace": "ws-live",
                "pane": "pane-live",
                "tokens": {"provenance": "orchestrator", "run": "run-abc", "node": "H", "attempt": "1"},
            }
            model = {
                "requested_role": None,
                "requested_effort": None,
                "resolved": "provider/live",
                "thinking": "high",
                "effort": "medium",
            }
            write_events(
                run,
                [
                    {"type": "session_run_bound", **DEFAULT_HOLDER},
                    {"type": "ready", "node": "H", "from": "planned", "to": "ready", "attempt": 1},
                    dispatch_intent_event("H", 1),
                    {"type": "dispatched", "node": "H", "attempt": 1, "handle": handle, "model": model},
                ],
            )

            def herdr(*args: str):
                if args[:2] == ("agent", "list"):
                    return {"result": {"agents": [
                        {"workspace_id": "ws-controller", "pane_id": "pane-controller"},
                        {"name": retained_worker_id, "workspace_id": "ws-retained", "pane_id": "pane-retained"},
                        {"name": worker_id, "workspace_id": "ws-live", "pane_id": "pane-live"},
                    ]}}
                if args == ("workspace", "get", "ws-controller"):
                    return {"result": {"workspace": {
                        "workspace_id": "ws-controller",
                        "tokens": {"role": "controller", "run": "run-abc"},
                    }}}
                if args == ("workspace", "get", "ws-retained"):
                    return {"result": {"workspace": {
                        "workspace_id": "ws-retained",
                        "tokens": {"provenance": "orchestrator", "run": "run-abc", "node": "prior-node", "attempt": "1"},
                    }}}
                if args == ("workspace", "get", "ws-live"):
                    return {
                        "result": {
                            "workspace": {
                                "workspace_id": "ws-live",
                                "tokens": {
                                    "provenance": "orchestrator",
                                    "run": "run-abc",
                                    "node": "H",
                                    "attempt": "1",
                                },
                                "root_pane": {"pane_id": "pane-live"},
                            }
                        }
                    }
                raise AssertionError(args)

            with mock.patch.object(reconcile, "herdr", herdr):
                records = reconcile.observe_live_herdr(run)

            self.assertEqual(records, [{
                "identity_kind": "managed-name",
                "worker_id": worker_id,
                "workspace": "ws-live",
                "pane": "pane-live",
                "tokens": {
                    "provenance": "orchestrator",
                    "run": "run-abc",
                    "node": "H",
                    "attempt": "1",
                },
            }])

    def test_managed_name_continuation_after_resume_notifies_again(self):
        

        import run_state

        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            nodes = {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}
            plant_run_files(run, node="H", run_id="run-abc", nodes=nodes)
            worker_id = reconcile.ioutil.new_worker_id()
            handle = {
                "identity_kind": "managed-name",
                "worker_id": worker_id,
                "workspace": "ws-live",
                "pane": "pane-live",
                "tokens": {"provenance": "orchestrator", "run": "run-abc", "node": "H", "attempt": "1"},
            }
            model = {
                "requested_role": None,
                "requested_effort": None,
                "resolved": "provider/live",
                "thinking": "high",
                "effort": "medium",
            }
            write_events(
                run,
                [
                    {"type": "session_run_bound", **DEFAULT_HOLDER},
                    {"type": "ready", "node": "H", "from": "planned", "to": "ready", "attempt": 1},
                    dispatch_intent_event("H", 1),
                    {"type": "dispatched", "node": "H", "attempt": 1, "handle": handle, "model": model},
                ],
            )

            def herdr(*args: str):
                if args[:2] == ("agent", "list"):
                    return {
                        "result": {
                            "agents": [
                                {
                                    "name": worker_id,
                                    "workspace_id": "ws-live",
                                    "pane_id": "pane-live",
                                    "agent_status": "blocked",
                                    "reason": "Approval required",
                                    "question": "Continue?",
                                }
                            ]
                        }
                    }
                if args[:2] == ("workspace", "get"):
                    return {
                        "result": {
                            "workspace": {
                                "workspace_id": "ws-live",
                                "tokens": {
                                    "provenance": "orchestrator",
                                    "run": "run-abc",
                                    "node": "H",
                                    "attempt": "1",
                                },
                                "root_pane": {"pane_id": "pane-live"},
                            }
                        }
                    }
                raise AssertionError(args)

            def observe():
                argv = [
                    "reconcile.py",
                    "--run",
                    str(run),
                    "--session",
                    DEFAULT_HOLDER["session"],
                    "--holder-workspace",
                    DEFAULT_HOLDER["workspace"],
                    "--holder-pane",
                    DEFAULT_HOLDER["pane"],
                    "--notify",
                ]
                with mock.patch.object(sys, "argv", argv), mock.patch.object(reconcile, "herdr", herdr):
                    out = io.StringIO()
                    with redirect_stdout(out):
                        self.assertEqual(reconcile.main(), 0)
                return json.loads(out.getvalue())

            first = observe()
            self.assertEqual(len(first.get("notified") or []), 1)
            node = run_state.replay(run)["state"]["nodes"]["H"]
            run_state.append_event(
                run,
                {
                    "type": "attempt_started",
                    "node": "H",
                    "attempt": 1,
                    "from": "blocked",
                    "to": "running",
                    "handle": node["handle"],
                    "model": node["model"],
                    "live_identity": {
                        "identity_kind": "managed-name",
                        "worker_id": worker_id,
                        "workspace": handle["workspace"],
                        "pane": handle["pane"],
                        "tokens": handle["tokens"],
                        "routing": node["model"],
                    },
                },
                holder=DEFAULT_HOLDER,
            )
            second = observe()
            self.assertTrue(second.get("notified"))
            self.assertTrue(set(first.get("notified") or []).isdisjoint(second.get("notified") or []))
            events = [
                json.loads(line)
                for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line
            ]
            blocked = [event for event in events if event.get("type") == "blocked"]
            self.assertEqual(len(blocked), 2)

    def test_legacy_agent_id_precedence_over_managed_name_pattern(self):
        worker_id = reconcile.ioutil.new_worker_id()

        def herdr(*args: str):
            if args[:2] == ("agent", "list"):
                return {
                    "result": {
                        "agents": [
                            {
                                "agent_id": "agt-stable",
                                "name": worker_id,
                                "workspace_id": "ws-live",
                                "pane_id": "pane-live",
                            }
                        ]
                    }
                }
            return fake_herdr(*args)

        with mock.patch.object(reconcile, "herdr", herdr):
            records = reconcile.observe_live_herdr()

        self.assertEqual(records, [{
            "workspace": "ws-live",
            "pane": "pane-live",
            "agent": "agt-stable",
            "tokens": LIVE_RECORD["tokens"],
        }])

    def test_observe_managed_name_token_mismatch_is_failed(self):
        

        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            nodes = {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}
            plant_run_files(run, node="H", run_id="run-abc", nodes=nodes)
            worker_id = reconcile.ioutil.new_worker_id()
            handle = {
                "identity_kind": "managed-name",
                "worker_id": worker_id,
                "workspace": "ws-live",
                "pane": "pane-live",
                "tokens": {"provenance": "orchestrator", "run": "run-abc", "node": "H", "attempt": "1"},
            }
            model = {
                "requested_role": None,
                "requested_effort": None,
                "resolved": "provider/live",
                "thinking": "high",
                "effort": "medium",
            }
            write_events(
                run,
                [
                    {"type": "session_run_bound", **DEFAULT_HOLDER},
                    {"type": "ready", "node": "H", "from": "planned", "to": "ready", "attempt": 1},
                    dispatch_intent_event("H", 1),
                    {"type": "dispatched", "node": "H", "attempt": 1, "handle": handle, "model": model},
                ],
            )

            def herdr(*args: str):
                if args[:2] == ("agent", "list"):
                    return {"result": {"agents": [{"name": worker_id, "workspace_id": "ws-live", "pane_id": "pane-live"}]}}
                if args[:2] == ("workspace", "get"):
                    return {
                        "result": {
                            "workspace": {
                                "workspace_id": "ws-live",
                                "tokens": {
                                    "provenance": "orchestrator",
                                    "run": "run-abc",
                                    "node": "H",
                                    "attempt": "2",
                                },
                                "root_pane": {"pane_id": "pane-live"},
                            }
                        }
                    }
                raise AssertionError(args)

            with mock.patch.object(reconcile, "herdr", herdr):
                with self.assertRaises(reconcile.ioutil.RunError):
                    reconcile.observe_live_herdr(run)

    def test_observe_recorded_managed_surfaces_refuse_unmatched_name(self):
        

        worker_id = reconcile.ioutil.new_worker_id()
        other_id = reconcile.ioutil.new_worker_id()

        def herdr_for(name: str, workspace_id: str, pane_id: str):
            def herdr(*args: str):
                if args[:2] == ("agent", "list"):
                    return {"result": {"agents": [{"name": name, "workspace_id": workspace_id, "pane_id": pane_id}]}}
                if args[:2] == ("workspace", "get"):
                    return {"result": {"workspace": {"workspace_id": workspace_id, "tokens": {}}}}
                raise AssertionError(args)
            return herdr

        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            attempt_dir = plant_run_files(run, node="H", run_id="run-abc", nodes={"H": {"id": "H", "executor": "herdr-agent"}})
            (attempt_dir / "dispatch-identity.json").write_text(
                json.dumps({
                    "identity_kind": "managed-name",
                    "worker_id": worker_id,
                    "workspace": "ws-orphan",
                    "pane": "pane-orphan",
                }) + "\n",
                encoding="utf-8",
            )
            reconcile.ledger.append_event(
                run,
                {"type": "blocked", "node": "H", "attempt": 1, "reason": "spawn failed before handle recorded"},
                holder=DEFAULT_HOLDER,
            )

            with mock.patch.object(reconcile, "herdr", herdr_for(other_id, "ws-unrelated", "pane-unrelated")):
                self.assertEqual(reconcile.observe_live_herdr(run), [])

            with mock.patch.object(reconcile, "herdr", herdr_for(other_id, "ws-orphan", "pane-orphan")):
                with self.assertRaises(reconcile.ioutil.RunError):
                    reconcile.observe_live_herdr(run)

    def test_observe_unresolved_launch_intent_not_adopted(self):
        

        worker_id = reconcile.ioutil.new_worker_id()

        def herdr(*args: str):
            if args[:2] == ("agent", "list"):
                return {"result": {"agents": [{"name": worker_id, "workspace_id": "ws-pending", "pane_id": "pane-pending"}]}}
            if args[:2] == ("workspace", "get"):
                return {"result": {"workspace": {"workspace_id": "ws-pending", "tokens": {}}}}
            raise AssertionError(args)

        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            plant_run_files(run, node="H", run_id="run-abc", nodes={"H": {"id": "H", "executor": "herdr-agent"}})

            with mock.patch.object(reconcile, "herdr", herdr):
                self.assertEqual(reconcile.observe_live_herdr(run), [])



if __name__ == "__main__":
    unittest.main()
