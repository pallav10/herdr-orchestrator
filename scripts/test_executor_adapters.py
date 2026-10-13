#!/usr/bin/env python3
"""Regression checks for spawn-agent.py, prompt-agent.py and collect-agent.py dispatch/collection contracts."""

from __future__ import annotations


import hashlib
import re


import json
import subprocess
import sys
import tempfile
import threading
import select
from pathlib import Path
import unittest
T = unittest.TestCase()

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import herdr_cli
import ioutil
import run_state
from testkit import (DEFAULT_HOLDER, FakeHerdr, commit_file, dispatch_intent_event,
                     git, load_script, make_worktree, plant_run_files, run_module,
                     seed_repo, write_events, mutating_calls)

# every prompt is now submitted with a delivery confirmation window
PROMPT_WAIT = ("--wait", "--until", "working", "--timeout", str(herdr_cli.PROMPT_WAIT_MS))




def auth_args(holder=None, epoch=1) -> list[str]:
    holder = holder or DEFAULT_HOLDER
    return [
        "--session", holder["session"],
        "--holder-workspace", holder["workspace"],
        "--holder-pane", holder["pane"],
        "--epoch", str(epoch),
    ]








def spawn_herdr(*, create=None, start=None, get=None, prompt_error=None):
    return FakeHerdr({
        ("workspace", "create"): create if create is not None else {"result": {"workspace": {"workspace_id": "ws"}, "root_pane": {"pane_id": "pane"}}},
        ("worktree", "create"): create if create is not None else {"result": {
            "workspace": {"workspace_id": "ws"}, "root_pane": {"pane_id": "pane"},
            "worktree": {"worktree_path": "/tmp/wt", "branch": "slice/a", "base_sha": "base"},
        }},
        ("agent", "start"): start if start is not None else {"result": {"agent": {"id": "agent-id"}}},
        ("agent", "prompt"): prompt_error if prompt_error is not None else {},
        ("agent", "get"): get if get is not None else lambda *args: {"result": {"agent": {"id": "agent-id", "name": args[2]}}},
        ("agent", "wait"): {},
        ("agent", "send-keys"): {},
        ("workspace", "get"): {"result": {"workspace": {"tokens": {}}}},
        ("workspace", "list"): {"result": {"workspaces": []}},
        ("workspace", "report-metadata"): {},
        ("workspace", "rename"): {},
        ("pane", "rename"): {},
        ("workspace", "close"): {},
        ("worktree", "remove"): {},
    })


def pane_prompt_herdr(*, workspace_pane="pane-live", agent_pane="pane-live", tokens=None, get=None):
    return FakeHerdr({
        ("agent", "get"): get if get is not None else lambda *args: {"result": {"agent": {
            "agent_id": "agent-live", "id": "unstable-id", "name": args[2],
            "workspace_id": "ws-live", "pane_id": agent_pane,
            "model": "provider/live:high", "effort": "medium",
        }}},
        ("workspace", "get"): {"result": {"workspace": {
            "tokens": tokens if tokens is not None else {"provenance": "user", "keep": "value"},
            "root_pane": {"pane_id": workspace_pane},
        }}},
        ("workspace", "report-metadata"): {},
        ("agent", "prompt"): {},
        ("agent", "wait"): {},
        ("agent", "send-keys"): {},
    })


def pane_nodes(executor: str = "pane", name: str = "worker") -> dict:
    return {
        "P": {
            "id": "P",
            "executor": executor,
            "handle": {"name": name},
            "model": {"requested_role": "slow", "requested_effort": "low"},
            "outputs": [{"name": "summary", "type": "summary"}],
        }
    }


def collect_herdr(session, *, workspace="ws", pane="pane", agent_id="agent-id", tokens=None):
    return FakeHerdr({
        ("agent", "get"): lambda *args: {"result": {"agent": {
            "id": agent_id, "name": args[2], "agent_status": "done",
            "agent_session": {"value": str(session)}, "workspace_id": workspace, "pane_id": pane,
        }}},
        ("workspace", "get", workspace): {"result": {"workspace": {
            "tokens": tokens if tokens is not None else {"run": "run-abc", "node": "Node.1", "attempt": "1"},
            "root_pane": {"pane_id": pane},
        }}},
    })











def pane_ready_events(node: str = "P", attempt: int = 1, holder=None) -> list[dict]:
    holder = holder or DEFAULT_HOLDER
    return [
        {"type": "session_run_bound", **holder},
        {"type": "ready", "node": node, "from": "planned", "to": "ready", "attempt": attempt},
        dispatch_intent_event(node, attempt),
    ]



def attempt_tree(tmp: Path, *, create_attempt: bool = True) -> tuple[Path, Path]:
    run = tmp / "run-abc"
    attempt = run / "artifacts" / "Node.1" / "1"
    if create_attempt:
        plant_run_files(run)
    else:
        run.mkdir()
    return run, attempt



def session_file(tmp: Path, text: str = "done") -> Path:
    path = tmp / "session.jsonl"
    row = {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return path


def identity_doc(run: Path, **overrides) -> dict:
    run_doc = {}
    if (run / "run.json").is_file():
        try:
            loaded = json.loads((run / "run.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            run_doc = loaded
    repo = run_doc.get("repo")
    if not isinstance(repo, str) or not repo:
        repo = str((run.parent / "repo").resolve())
    data = {
        "workspace": "ws",
        "pane": "pane",
        "agent": "agent-id",
        "run": str(run.resolve()),
        "run_id": run.name,
        "node": "Node.1",
        "attempt": "1",
        "repo": repo,
        "worktree": True,
        "worktree_path": "/tmp/wt",
        "branch": "slice/a",
        "base": "base",
        "tokens": {"provenance": "orchestrator", "run": run.name, "node": "Node.1", "attempt": "1"},
    }
    data.update(overrides)
    return data


def write_graph(run: Path, outputs: list[dict], *, extra_nodes: dict | None = None, inputs: list | None = None) -> None:
    run_doc = {}
    if (run / "run.json").is_file():
        run_doc = json.loads((run / "run.json").read_text(encoding="utf-8"))
    graph = {}
    if (run / "graph.json").is_file():
        try:
            graph = json.loads((run / "graph.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            graph = {}
    if not isinstance(graph, dict):
        graph = {}
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), dict) else {}
    current = nodes.get("Node.1") if isinstance(nodes.get("Node.1"), dict) else {"id": "Node.1", "executor": "herdr-agent"}
    current["outputs"] = outputs
    if inputs is not None:
        current["inputs"] = inputs
    nodes["Node.1"] = current
    if extra_nodes:
        nodes.update(extra_nodes)
    graph.update({
        "schema_version": graph.get("schema_version", 1),
        "run": graph.get("run") or str(run.resolve()),
        "base_sha": graph.get("base_sha") or run_doc.get("base_sha") or "base",
        "nodes": nodes,
    })
    (run / "graph.json").write_text(json.dumps(graph) + "\n", encoding="utf-8")


def integration_doc(base_sha: str, consumed: list, commit: str = "cafebabe") -> dict:
    return {"base_sha": base_sha, "commit": commit, "head": commit, "consumed": consumed}


def diff_vcs(attempt: Path, identity: dict, content: bytes = b"change") -> dict:
    diff = attempt / "change.bin"
    diff.write_bytes(content)
    raw = hashlib.sha256(content).hexdigest()
    return {
        "repo": identity["repo"],
        "worktree": identity["worktree_path"],
        "branch": identity["branch"],
        "base_sha": identity["base"],
        "result": {"form": "diff", "path": str(diff), "sha256": raw},
    }


def check_prompt_contextual_dispatch() -> None:
    module = load_script("prompt-agent.py")
    fake = pane_prompt_herdr(workspace_pane="w1:p1", agent_pane="w1:p6")
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-id"
            plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, pane_ready_events())
            code, out, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            T.assertEqual(payload, {'name': 'worker', 'workspace': 'ws-live', 'pane': 'w1:p6', 'agent': 'agent-live', 'routing': {'requested_role': 'slow', 'requested_effort': 'low', 'resolved': 'provider/live', 'thinking': 'high', 'effort': 'medium'}, 'tokens': {'provenance': 'user', 'keep': 'value', 'run': 'run-id', 'node': 'P', 'attempt': '1'}, 'prompted': True, 'run': str(run.resolve()), 'node': 'P', 'attempt': '1'})
            T.assertEqual([call[:2] for call in mutating_calls(fake.calls)], [('workspace', 'report-metadata'), ('agent', 'prompt')])
            stamp = next(call for call in mutating_calls(fake.calls) if call[:2] == ('workspace', 'report-metadata'))
            T.assertEqual(stamp[stamp.index('--source') + 1], 'orchestrator')
            stamped = [stamp[i + 1] for i, arg in enumerate(stamp) if arg == "--token"]
            T.assertEqual(stamped, ['run=run-id', 'node=P', 'attempt=1'])
            T.assertTrue(all((not token.startswith('provenance=') for token in stamped)))
            expected = module.prepare_prompt(
                module.artifacts.prepare_dispatch_prompt(run, "P", "1", "work"), orchestrate=True
            )
            T.assertEqual(next(call for call in mutating_calls(fake.calls) if call[:2] == ("agent", "prompt")), ('agent', 'prompt', 'worker', expected, *PROMPT_WAIT))
    finally:
        module.herdr = original


def check_prompt_contextual_premutation_failures() -> None:
    module = load_script("prompt-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            fake = pane_prompt_herdr()
            module.herdr = fake
            code, _, _ = run_module(module, [
                "prompt-agent.py", "worker", "--run", td, "--node", "P", "--prompt", "work",
            ])
            T.assertEqual(code, 2)
            T.assertEqual(fake.calls, [])

            run = Path(td) / "missing-consumed" / "run-id"
            attempt = plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, pane_ready_events())
            (attempt / "consumed_artifacts.json").unlink()
            code, _, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 2)
            T.assertEqual(fake.calls, [])

            run = Path(td) / "wrong-pane" / "run-id"
            nodes = pane_nodes()
            nodes["P"]["handle"] = {"name": "worker", "workspace": "ws-live", "pane": "w1:p9", "agent": "agent-live"}
            plant_run_files(run, node="P", nodes=nodes)
            write_events(run, pane_ready_events())
            fake = pane_prompt_herdr(workspace_pane="w1:p1", agent_pane="w1:p6")
            module.herdr = fake
            code, _, _ = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 2)
            T.assertEqual(mutating_calls(fake.calls), [])

            run = Path(td) / "wrong-attempt" / "run-id"
            plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, pane_ready_events())
            fake = pane_prompt_herdr()
            module.herdr = fake
            code, _, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "2", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 2)
            T.assertIn('does not match current attempt', err)
            T.assertEqual(fake.calls, [])

            run = Path(td) / "herdr-initial" / "run-id"
            plant_run_files(run, node="H", nodes={"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}})
            write_events(run, pane_ready_events("H"))
            fake = pane_prompt_herdr()
            module.herdr = fake
            code, _, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "H", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 2)
            T.assertIn('spawn-agent', err)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original


def check_prompt_blocked_herdr_continuation() -> None:
    module = load_script("prompt-agent.py")
    worker_id = "fo-0123456789abcdef01234567"
    tokens = {"provenance": "orchestrator", "run": "run-id", "node": "H", "attempt": "1", "keep": "value"}
    fake = pane_prompt_herdr(workspace_pane="w1:p1", agent_pane="w1:p6", tokens=tokens)
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, handle, model, _, before = _blocked_herdr_run(td)
            code, out, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "H", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            T.assertEqual(payload['workspace'], 'ws-live')
            T.assertEqual(payload['pane'], 'w1:p6')
            T.assertEqual(payload['identity_kind'], run_state.IDENTITY_MANAGED_NAME)
            T.assertEqual(payload['worker_id'], worker_id)
            T.assertEqual(payload['routing'], model)
            T.assertEqual(payload['tokens']['provenance'], 'orchestrator')
            event = payload["event"]
            T.assertEqual(event['type'], 'attempt_started')
            T.assertEqual(event['from'], 'blocked')
            T.assertEqual(event['to'], 'running')
            T.assertEqual(event['node'], 'H')
            T.assertEqual(event['attempt'], 1)
            T.assertEqual(event['handle'], handle)
            T.assertEqual(event['model'], model)
            T.assertEqual(event['live_identity']['workspace'], 'ws-live')
            T.assertEqual(event['live_identity']['pane'], 'w1:p6')
            T.assertEqual(event['live_identity']['worker_id'], worker_id)
            T.assertEqual(event['live_identity']['routing'], model)
            T.assertEqual((run / 'events.jsonl').read_text(encoding='utf-8'), before)
            # The descriptor must be appendable as-is: a managed-name live identity
            # without its discriminator is parsed as runtime-id and refused.
            T.assertEqual(event['live_identity']['identity_kind'], run_state.IDENTITY_MANAGED_NAME)
            resumed = run_state.append_event(run, event, holder=DEFAULT_HOLDER)
            T.assertEqual(resumed['state']['nodes']['H']['state'], 'running')
            T.assertEqual(resumed['state']['nodes']['H']['attempt'], 1)
            T.assertEqual([call[:2] for call in mutating_calls(fake.calls)], [('workspace', 'report-metadata'), ('agent', 'prompt')])
            stamp = next(call for call in mutating_calls(fake.calls) if call[:2] == ('workspace', 'report-metadata'))
            stamped = [stamp[i + 1] for i, arg in enumerate(stamp) if arg == "--token"]
            T.assertEqual(stamped, ['run=run-id', 'node=H', 'attempt=1'])
            T.assertTrue(all((not token.startswith('provenance=') for token in stamped)))
            expected = module.prepare_prompt(
                module.artifacts.prepare_dispatch_prompt(run, "H", "1", "work"), orchestrate=True
            )
            T.assertEqual(next(call for call in mutating_calls(fake.calls) if call[:2] == ("agent", "prompt")), ('agent', 'prompt', worker_id, expected, *PROMPT_WAIT))
    finally:
        module.herdr = original



def _blocked_herdr_run(td: str) -> tuple[Path, dict, dict, dict, str]:
    run = Path(td) / "run-id"
    tokens = {
        "provenance": "orchestrator",
        "run": "run-id",
        "node": "H",
        "attempt": "1",
        "keep": "value",
    }
    nodes = {
        "H": {
            "id": "H",
            "executor": "herdr-agent",
            "model": {"requested_role": "slow", "requested_effort": "low"},
            "outputs": [{"name": "summary", "type": "summary"}],
        }
    }
    attempt = plant_run_files(run, node="H", nodes=nodes)
    handle = {
        "workspace": "ws-live",
        "pane": "w1:p6",
        "identity_kind": run_state.IDENTITY_MANAGED_NAME,
        "worker_id": "fo-0123456789abcdef01234567",
        "tokens": tokens,
    }
    model = {
        "requested_role": "slow",
        "requested_effort": "low",
        "resolved": "provider/live",
        "thinking": "high",
        "effort": "medium",
    }
    write_events(run, [
        {"type": "session_run_bound", **DEFAULT_HOLDER},
        {"type": "ready", "node": "H", "from": "planned", "to": "ready", "attempt": 1},
        dispatch_intent_event("H", 1),
        {"type": "dispatched", "node": "H", "attempt": 1, "handle": handle, "model": model},
        {"type": "blocked", "node": "H", "from": "running", "to": "blocked", "attempt": 1, "reason": "approval requested"},
    ])
    (attempt / "dispatch-identity.json").write_text(json.dumps({
        "workspace": "ws-live",
        "pane": "w1:p6",
        "identity_kind": run_state.IDENTITY_MANAGED_NAME,
        "worker_id": "fo-0123456789abcdef01234567",
        "alias": "worker",
        "run": str(run.resolve()),
        "run_id": "run-id",
        "node": "H",
        "attempt": "1",
        "tokens": tokens,
    }) + "\n", encoding="utf-8")
    return run, handle, model, tokens, (run / "events.jsonl").read_text(encoding="utf-8")

def check_prompt_continuation_live_model_mismatch() -> None:
    module = load_script("prompt-agent.py")
    tokens = {"provenance": "orchestrator", "run": "run-id", "node": "H", "attempt": "1", "keep": "value"}
    fake = pane_prompt_herdr(workspace_pane="w1:p1",
    agent_pane="w1:p6",
    tokens=tokens,
    get={
        "result": {
            "agent": {
                "agent_id": "agent-live",
                "id": "unstable-id",
                "name": "fo-0123456789abcdef01234567",
                "workspace_id": "ws-live",
                "pane_id": "w1:p6",
                "model": "provider/other:low",
                "effort": "high",
            }
        }
    },)
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, _, _, _, before = _blocked_herdr_run(td)
            code, out, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "H", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 3, err)
            T.assertIn('continuation live routing does not match recorded model', err)
            T.assertEqual(out, '')
            T.assertEqual((run / 'events.jsonl').read_text(encoding='utf-8'), before)
            T.assertEqual(run_state.replay(run)['state']['nodes']['H']['state'], 'blocked')
            T.assertEqual(mutating_calls(fake.calls), [])
            T.assertFalse(any((call[:2] in (('workspace', 'report-metadata'), ('agent', 'prompt')) for call in fake.calls)))
    finally:
        module.herdr = original


def check_prompt_continuation_missing_live_routing() -> None:
    module = load_script("prompt-agent.py")
    tokens = {"provenance": "orchestrator", "run": "run-id", "node": "H", "attempt": "1", "keep": "value"}
    fake = pane_prompt_herdr(workspace_pane="w1:p1",
    agent_pane="w1:p6",
    tokens=tokens,
    get={
        "result": {
            "agent": {
                "agent": "omp",
                "agent_id": "agent-live",
                "name": "fo-0123456789abcdef01234567",
                "workspace_id": "ws-live",
                "pane_id": "w1:p6",
                "agent_status": "idle",
            }
        }
    },)
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, handle, model, _, before = _blocked_herdr_run(td)
            code, out, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "H", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            observed = {
                "requested_role": "slow",
                "requested_effort": "low",
                "resolved": None,
                "thinking": None,
                "effort": None,
            }
            T.assertEqual(payload['routing'], observed)
            event = payload["event"]
            T.assertEqual(event['model'], model)
            T.assertEqual(event['handle'], handle)
            T.assertEqual(event['live_identity']['routing'], observed)
            T.assertEqual((run / 'events.jsonl').read_text(encoding='utf-8'), before)
            T.assertEqual([call[:2] for call in mutating_calls(fake.calls)], [('workspace', 'report-metadata'), ('agent', 'prompt')])
    finally:
        module.herdr = original



def check_prompt_replacement_identity_refused() -> None:
    module = load_script("prompt-agent.py")
    fake = pane_prompt_herdr(workspace_pane="w1:p1", agent_pane="w1:p6")
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-id"
            attempt = plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, pane_ready_events())
            (attempt / "dispatch-identity.json").write_text(json.dumps({
                "workspace": "ws-other",
                "pane": "pane-other",
                "agent": "agent-other",
                "tokens": {"provenance": "user", "run": "run-id", "node": "P", "attempt": "1"},
            }) + "\n", encoding="utf-8")
            code, _, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 2)
            T.assertIn('does not match live identity', err)
            T.assertEqual(mutating_calls(fake.calls), [])
            T.assertFalse(any((call[:2] in (('workspace', 'report-metadata'), ('agent', 'prompt')) for call in fake.calls)))
    finally:
        module.herdr = original


def check_spawn_wrong_executor() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            for executor, node, extra in (
                ("task", "T", {}),
                ("workflow", "W", {}),
                ("pane", "P", {"handle": {"name": "worker"}}),
            ):
                run = Path(td) / executor / "run-abc"
                nodes = {node: {"id": node, "executor": executor, "outputs": [{"name": "summary", "type": "summary"}], **extra}}
                plant_run_files(run, node=node, nodes=nodes)
                fake.calls.clear()
                code, _, err = run_module(module, [
                    "spawn-agent.py", "worker", "--run", str(run), "--node", node, "--attempt", "1", *auth_args(),
                ])
                T.assertEqual(code, 2)
                T.assertIn(f'node {node} executor must be herdr-agent', err)
                T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original


def check_spawn_identity_before_prompt() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr(prompt_error=_herdr_error("unrelated", "boom"))
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('boom', err)
            identity_path = attempt / "dispatch-identity.json"
            T.assertTrue(identity_path.is_file())
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            T.assertEqual(identity['identity_kind'], module.graph_model.IDENTITY_MANAGED_NAME)
            T.assertTrue(module.graph_model.WORKER_ID_RE.fullmatch(identity['worker_id']))
            T.assertNotIn('agent', identity)
            T.assertEqual(identity['tokens'], {'provenance': 'orchestrator', 'run': 'run-abc', 'node': 'Node.1', 'attempt': '1'})
            kinds = [call[:2] for call in mutating_calls(fake.calls)]
            T.assertLess(kinds.index(('agent', 'start')), kinds.index(('workspace', 'report-metadata')))
            T.assertLess(kinds.index(('workspace', 'report-metadata')), kinds.index(('agent', 'prompt')))
            T.assertFalse(any((call[:2] in (('workspace', 'close'), ('worktree', 'remove')) for call in fake.calls)))
            retry = spawn_herdr()
            module.herdr = retry
            code, _, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 2)
            T.assertIn('dispatch identity already exists', err)
            T.assertEqual(retry.calls, [])
    finally:
        module.herdr = original


def check_spawn_run_token() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            code, out, _ = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 0)
            payload = json.loads(out)
            T.assertEqual(payload['name'], payload['worker_id'])
            T.assertEqual(payload['alias'], 'worker')
            T.assertTrue(module.graph_model.WORKER_ID_RE.fullmatch(payload['worker_id']))
            T.assertEqual(payload['workspace'], 'ws')
            T.assertEqual(payload['pane'], 'pane')
            stamp = next(call for call in fake.calls if call[:2] == ("workspace", "report-metadata"))
            tokens = [stamp[i + 1] for i, arg in enumerate(stamp) if arg == "--token"]
            T.assertLessEqual({'provenance=orchestrator', 'run=run-abc', 'node=Node.1', 'attempt=1'}, set(tokens))
            T.assertTrue(all((str(run) not in token for token in tokens)))
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(identity['identity_kind'], module.graph_model.IDENTITY_MANAGED_NAME)
            T.assertEqual(identity['worker_id'], payload['worker_id'])
            T.assertNotIn('agent', identity)
            T.assertEqual(identity['tokens'], {'provenance': 'orchestrator', 'run': 'run-abc', 'node': 'Node.1', 'attempt': '1'})
    finally:
        module.herdr = original


def check_spawn_all_or_none() -> None:
    module = load_script("spawn-agent.py")
    with tempfile.TemporaryDirectory() as td:
        code, _, _ = run_module(module, ["spawn-agent.py", "worker", "--run", td, "--node", "Node.1"])
        T.assertEqual(code, 2)


def check_spawn_missing_attempt_dir() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, _ = attempt_tree(Path(td), create_attempt=False)
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('attempt artifact dir missing', err)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original


def check_spawn_duplicate_identity() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            (attempt / "dispatch-identity.json").write_text("{}\n", encoding="utf-8")
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('dispatch identity already exists', err)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original


def check_spawn_model_exclusive() -> None:
    module = load_script("spawn-agent.py")
    code, _, err = run_module(module, ["spawn-agent.py", "worker", "--model", "p/m", "--model-role", "strong"])
    T.assertEqual(code, 2)
    T.assertIn('not allowed with argument', err)


def check_spawn_legacy_scalar_and_explicit_routing() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = (module.herdr, module.resolve_role)
    module.herdr = fake
    module.resolve_role = lambda role: "provider/model:think"
    try:
        code, out, err = run_module(module, [
            "spawn-agent.py", "worker", "--model-role", "strong", "--requested-effort", "medium",
        ])
        T.assertEqual(code, 0, err)
        payload = json.loads(out)
        T.assertEqual(payload['model'], 'provider/model')
        T.assertEqual(payload['thinking'], 'think')
        T.assertEqual(payload['routing'], {'requested_role': 'strong', 'requested_effort': 'medium', 'resolved': 'provider/model', 'thinking': 'think', 'effort': None})
    finally:
        module.herdr, module.resolve_role = original


def check_spawn_actual_routing_from_payload() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr(start={"result": {"agent": {"id": "agent-id", "model": "actual/model:deep", "effort": "high"}}})
    original = module.herdr
    module.herdr = fake
    try:
        code, out, err = run_module(module, ["spawn-agent.py", "worker"])
        T.assertEqual(code, 0, err)
        payload = json.loads(out)
        T.assertEqual(payload['model'], 'actual/model')
        T.assertEqual(payload['thinking'], 'deep')
        T.assertEqual(payload['routing']['effort'], 'high')
    finally:
        module.herdr = original

def check_spawn_actual_routing_from_get_payload() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr(get={"result": {"agent": {"id": "agent-id", "name": "worker", "model": "get/model", "thinking": "deep", "effort": "medium"}}})
    original = module.herdr
    module.herdr = fake
    try:
        code, out, err = run_module(module, ["spawn-agent.py", "worker"])
        T.assertEqual(code, 0, err)
        payload = json.loads(out)
        T.assertEqual(payload['model'], 'get/model')
        T.assertEqual(payload['thinking'], 'deep')
        T.assertEqual(payload['routing']['effort'], 'medium')
    finally:
        module.herdr = original



def check_spawn_inherited_without_actual_is_null() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        code, out, err = run_module(module, ["spawn-agent.py", "worker"])
        T.assertEqual(code, 0, err)
        payload = json.loads(out)
        T.assertIs(payload['model'], None)
        T.assertIs(payload['thinking'], None)
        T.assertIs(payload['routing']['resolved'], None)
        T.assertIs(payload['routing']['effort'], None)
    finally:
        module.herdr = original


def check_spawn_worktree_identity_uses_response() -> None:
    module = load_script("spawn-agent.py")
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "wt"
        create = {
            "result": {
                "workspace": {"workspace_id": "ws"},
                "root_pane": {"pane_id": "pane"},
                "worktree": {"worktree_path": str(wt), "branch": "auto/branch", "base_sha": "resolved-base"},
            }
        }
        fake = spawn_herdr(create=create)
        original = module.herdr
        module.herdr = fake
        try:
            run, attempt = attempt_tree(Path(td))
            init_primary_repo(run.parent / "repo")
            code, out, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args(), "--base", "main",
            ])
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(payload['branch'], identity['branch'])
            T.assertEqual(identity['branch'], 'auto/branch')
            T.assertEqual(payload['base'], identity['base'])
            T.assertEqual(identity['base'], 'resolved-base')
            T.assertEqual(payload['worktree_path'], identity['worktree_path'])
            T.assertEqual(identity['worktree_path'], str(wt.resolve()))
        finally:
            module.herdr = original


def check_spawn_worktree_missing_base_uses_head() -> None:
    module = load_script("spawn-agent.py")
    with tempfile.TemporaryDirectory() as td:
        wt = seed_repo(Path(td) / "wt")
        head = git(wt, "rev-parse", "HEAD")
        create = {
            "result": {
                "workspace": {"workspace_id": "ws"},
                "root_pane": {"pane_id": "pane"},
                "worktree": {"worktree_path": str(wt), "branch": "auto/branch"},
            }
        }
        fake = spawn_herdr(create=create)
        original = module.herdr
        module.herdr = fake
        try:
            run, attempt = attempt_tree(Path(td))
            init_primary_repo(run.parent / "repo")
            requested = "main"
            code, out, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args(), "--base", requested,
            ])
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(payload['branch'], identity['branch'])
            T.assertEqual(identity['branch'], 'auto/branch')
            T.assertEqual(payload['worktree_path'], identity['worktree_path'])
            T.assertEqual(identity['worktree_path'], str(wt.resolve()))
            T.assertEqual(payload['base'], identity['base'])
            T.assertEqual(identity['base'], head)
            T.assertNotEqual(head, requested)
        finally:
            module.herdr = original


def check_spawn_dispatch_injection() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr(start={"result": {"agent": {"id": "agent-id", "model": "actual/model:deep", "effort": "high"}}})
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-abc"
            nodes = {
                "Node.1": {
                    "id": "Node.1",
                    "executor": "herdr-agent",
                    "depends_on": ["B", "C", "D"],
                    "inputs": [
                        {"alias": "b", "from": "B", "output": "summary"},
                        {"alias": "c", "from": "C", "output": "vcs"},
                        {"alias": "d", "from": "D", "output": "summary", "required": False},
                    ],
                    "outputs": [{"name": "summary", "type": "summary"}],
                },
                "B": {"id": "B", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
                "C": {"id": "C", "executor": "task", "outputs": [{"name": "vcs", "type": "vcs"}]},
                "D": {"id": "D", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
            }
            consumed = [
                {"alias": "c", "producer": "C", "producer_attempt": 1, "output": "vcs", "path": "/tmp/c", "hash": "sha256:" + "c" * 64},
                {"alias": "b", "producer": "B", "producer_attempt": 2, "output": "summary", "path": "/tmp/b", "hash": "sha256:" + "b" * 64},
            ]
            attempt = plant_run_files(run, run_id="run-id", nodes=nodes, consumed=consumed)
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            create_call = next(call for call in mutating_calls(fake.calls) if call[:2] == ('workspace', 'create'))
            T.assertEqual(create_call[create_call.index('--cwd') + 1], str((run.parent / 'repo').resolve()))
            prompt = next(call[3] for call in fake.calls if call[:2] == ("agent", "prompt"))
            block = json.loads(prompt.split("# herdr-dispatch\n", 1)[1].split("\n", 1)[0])
            T.assertEqual(block['attempt_dir'], str(attempt.resolve()))
            T.assertEqual([item['alias'] for item in block['consumed']], ['b', 'c'])
            T.assertEqual(block['consumed'][0], {'alias': 'b', 'producer': 'B', 'producer_attempt': 2, 'output': 'summary', 'path': '/tmp/b', 'hash': 'sha256:' + 'b' * 64})
            stamp = next(call for call in fake.calls if call[:2] == ("workspace", "report-metadata"))
            tokens = [stamp[i + 1] for i, arg in enumerate(stamp) if arg == "--token"]
            T.assertIn('run=run-id', tokens)
            T.assertTrue(all((str(run) not in token for token in tokens)))
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(identity['run_id'], identity['tokens']['run'])
            T.assertEqual(identity['tokens']['run'], 'run-id')
            T.assertEqual(identity['tokens']['provenance'], 'orchestrator')
            T.assertEqual(identity['routing'], payload['routing'])
            T.assertEqual(payload['routing'], {'requested_role': None, 'requested_effort': None, 'resolved': 'actual/model', 'thinking': 'deep', 'effort': 'high'})
            T.assertIn('work', prompt)
    finally:
        module.herdr = original


def check_spawn_premutation_failures() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, _ = attempt_tree(Path(td) / "graph")
            (run / "graph.json").unlink()
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('accepted graph missing', err)
            T.assertEqual(fake.calls, [])

            run, attempt = attempt_tree(Path(td) / "consumed")
            (attempt / "consumed_artifacts.json").unlink()
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertEqual(fake.calls, [])

            run, _ = attempt_tree(Path(td) / "run-mismatch")
            graph = json.loads((run / "graph.json").read_text(encoding="utf-8"))
            graph["run"] = "/tmp/other-run"
            (run / "graph.json").write_text(json.dumps(graph) + "\n", encoding="utf-8")
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('graph.run mismatch', err)
            T.assertEqual(fake.calls, [])

            run, _ = attempt_tree(Path(td) / "base-mismatch")
            graph = json.loads((run / "graph.json").read_text(encoding="utf-8"))
            graph["base_sha"] = "other"
            (run / "graph.json").write_text(json.dumps(graph) + "\n", encoding="utf-8")
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('graph.base_sha mismatch', err)
            T.assertEqual(fake.calls, [])

            run, _ = attempt_tree(Path(td) / "invalid-graph")
            graph = json.loads((run / "graph.json").read_text(encoding="utf-8"))
            graph["nodes"]["Node.1"]["executor"] = "invalid"
            (run / "graph.json").write_text(json.dumps(graph) + "\n", encoding="utf-8")
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('graph is not accepted', err)
            T.assertEqual(fake.calls, [])

            run, _ = attempt_tree(Path(td) / "missing-repo")
            run_doc = json.loads((run / "run.json").read_text(encoding="utf-8"))
            run_doc.pop("repo")
            (run / "run.json").write_text(json.dumps(run_doc) + "\n", encoding="utf-8")
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('run.json missing repo', err)
            T.assertEqual(fake.calls, [])

            run, _ = attempt_tree(Path(td) / "noncanonical-repo")
            run_doc = json.loads((run / "run.json").read_text(encoding="utf-8"))
            run_doc["repo"] = str(Path(td) / "source" / ".." / "repo")
            (run / "run.json").write_text(json.dumps(run_doc) + "\n", encoding="utf-8")
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('run.json.repo must be canonical', err)
            T.assertEqual(fake.calls, [])

            run, _ = attempt_tree(Path(td) / "cwd-mismatch")
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args(),
                "--cwd", str(Path(td) / "other-repo"),
            ])
            T.assertEqual(code, 2)
            T.assertIn('--cwd must resolve to accepted run repo', err)
            T.assertEqual(fake.calls, [])

            run = Path(td) / "refs" / "run-abc"
            nodes = {
                "Node.1": {
                    "id": "Node.1",
                    "executor": "herdr-agent",
                    "depends_on": ["B"],
                    "inputs": [{"alias": "b", "from": "B", "output": "summary"}],
                    "outputs": [{"name": "summary", "type": "summary"}],
                },
                "B": {"id": "B", "executor": "task", "outputs": [{"name": "summary", "type": "summary"}]},
            }
            good = {"alias": "b", "producer": "B", "producer_attempt": 1, "output": "summary", "path": "/tmp/b", "hash": "sha256:" + "b" * 64}
            plant_run_files(run, nodes=nodes, consumed=[good, {**good, "alias": "extra", "path": "/tmp/e"}])
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('extra alias', err)
            T.assertEqual(fake.calls, [])
            plant_run_files(run, nodes=nodes, consumed=[good, good])
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('duplicate alias', err)
            T.assertEqual(fake.calls, [])
            plant_run_files(run, nodes=nodes, consumed=[{**good, "producer_attempt": 0}])
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 2)
            T.assertIn('producer_attempt', err)
            T.assertEqual(fake.calls, [])

            run = Path(td) / "integ" / "run-abc"
            nodes = {"D": {"id": "D", "executor": "herdr-agent", "outputs": [{"name": "integration", "type": "integration"}]}}
            plant_run_files(run, node="D", run_id="run-id", base_sha="deadbeef", nodes=nodes)
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "D", "--attempt", "1", *auth_args(), "--branch", "other",
            ])
            T.assertEqual(code, 2)
            T.assertIn('--branch must equal run-id/D/1', err)
            T.assertEqual(fake.calls, [])

            existing = Path(td) / "old-wt"
            existing.mkdir()
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "D", "--attempt", "1", *auth_args(), "--path", str(existing),
            ])
            T.assertEqual(code, 2)
            T.assertIn('refusing to reuse', err)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original


def check_spawn_integration_identity() -> None:
    module = load_script("spawn-agent.py")
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "wt"
        run = Path(td) / "run-abc"
        nodes = {"D": {"id": "D", "executor": "herdr-agent", "outputs": [{"name": "integration", "type": "integration"}]}}
        attempt = plant_run_files(run, node="D", run_id="run-id", base_sha="deadbeef", nodes=nodes)
        init_primary_repo(run.parent / "repo")
        create = {
            "result": {
                "workspace": {"workspace_id": "ws"},
                "root_pane": {"pane_id": "pane"},
                "worktree": {"worktree_path": str(wt), "branch": "run-id/D/1", "base_sha": "deadbeef"},
            }
        }
        fake = spawn_herdr(create=create)
        original = module.herdr
        module.herdr = fake
        try:
            code, out, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "D", "--attempt", "1", *auth_args(),
            ])
            T.assertEqual(code, 0, err)
            create_call = next(call for call in mutating_calls(fake.calls) if call[:2] == ('worktree', 'create'))
            T.assertEqual(create_call[:2], ('worktree', 'create'))
            T.assertEqual(create_call[create_call.index('--base') + 1], 'deadbeef')
            T.assertEqual(create_call[create_call.index('--branch') + 1], 'run-id/D/1')
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            payload = json.loads(out)
            T.assertEqual(identity['run'], str(run.resolve()))
            T.assertEqual(identity['run_id'], identity['tokens']['run'])
            T.assertEqual(identity['tokens']['run'], 'run-id')
            T.assertEqual(identity['node'], identity['tokens']['node'])
            T.assertEqual(identity['tokens']['node'], 'D')
            T.assertEqual(identity['attempt'], identity['tokens']['attempt'])
            T.assertEqual(identity['tokens']['attempt'], '1')
            T.assertEqual(identity['branch'], payload['branch'])
            T.assertEqual(payload['branch'], 'run-id/D/1')
            T.assertEqual(identity['base'], payload['base'])
            T.assertEqual(payload['base'], 'deadbeef')
            T.assertEqual(identity['worktree_path'], payload['worktree_path'])
            T.assertEqual(payload['worktree_path'], str(wt.resolve()))
            T.assertEqual(identity['repo'], str((run.parent / 'repo').resolve()))
            T.assertEqual(identity['routing'], payload['routing'])
            stamp = next(call for call in fake.calls if call[:2] == ("workspace", "report-metadata"))
            T.assertIn('run=run-id', stamp)
        finally:
            module.herdr = original


def check_spawn_created_base_mismatch() -> None:
    module = load_script("spawn-agent.py")
    with tempfile.TemporaryDirectory() as td:
        run = Path(td) / "run-abc"
        nodes = {"D": {"id": "D", "executor": "herdr-agent", "outputs": [{"name": "integration", "type": "integration"}]}}
        attempt = plant_run_files(run, node="D", base_sha="deadbeef", nodes=nodes)
        init_primary_repo(run.parent / "repo")
        fake = spawn_herdr(create={"result": {
            "workspace": {"workspace_id": "ws"},
            "root_pane": {"pane_id": "pane"},
            "worktree": {"worktree_path": str(Path(td) / "wt"), "branch": "run-abc/D/1", "base_sha": "wrong"},
        }})
        original = module.herdr
        module.herdr = fake
        try:
            code, out, err = run_module(module, ['spawn-agent.py', 'worker', '--run', str(run), '--node', 'D', '--attempt', '1', *auth_args()])
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('contextual worktree base must be deadbeef', err)
            T.assertFalse((attempt / 'dispatch-identity.json').exists())
            T.assertFalse(any((call[:2] == ('agent', 'start') for call in fake.calls)))
        finally:
            module.herdr = original


def check_spawn_writer_graph_branch() -> None:
    module = load_script("spawn-agent.py")
    with tempfile.TemporaryDirectory() as td:
        run = Path(td) / "run-abc"
        wt = Path(td) / "writer-wt"
        nodes = {"W": {
            "id": "W", "executor": "herdr-agent", "worktree": {"branch": "writers/w"},
            "outputs": [{"name": "vcs", "type": "vcs"}],
        }}
        attempt = plant_run_files(run, node="W", base_sha="deadbeef", nodes=nodes)
        init_primary_repo(run.parent / "repo")
        refused = spawn_herdr()
        original = module.herdr
        module.herdr = refused
        try:
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "W", "--attempt", "1", *auth_args(), "--branch", "other",
            ])
            T.assertEqual(code, 2)
            T.assertIn('--branch must equal writers/w', err)
            T.assertEqual(refused.calls, [])

            fake = spawn_herdr(create={"result": {
                "workspace": {"workspace_id": "ws"},
                "root_pane": {"pane_id": "pane"},
                "worktree": {"worktree_path": str(wt), "branch": "writers/w", "base_sha": "deadbeef"},
            }})
            module.herdr = fake
            code, _, err = run_module(module, ["spawn-agent.py", "worker", "--run", str(run), "--node", "W", "--attempt", "1", *auth_args()])
            T.assertEqual(code, 0, err)
            create_call = next(call for call in mutating_calls(fake.calls) if call[:2] == ('worktree', 'create'))
            T.assertEqual(create_call[create_call.index('--branch') + 1], 'writers/w')
            T.assertEqual(create_call[create_call.index('--base') + 1], 'deadbeef')
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(identity['branch'], 'writers/w')
            T.assertEqual(identity['base'], 'deadbeef')
        finally:
            module.herdr = original


def check_spawn_integration_rejects_primary_and_prior_worktrees() -> None:
    module = load_script("spawn-agent.py")
    nodes = {"D": {"id": "D", "executor": "herdr-agent", "outputs": [{"name": "integration", "type": "integration"}]}}
    with tempfile.TemporaryDirectory() as td:
        primary_run = Path(td) / "primary" / "run-abc"
        primary_attempt = plant_run_files(primary_run, node="D", base_sha="deadbeef", nodes=nodes)
        primary_repo = str((primary_run.parent / "repo").resolve())

        prior_run = Path(td) / "prior" / "run-abc"
        prior_attempt = plant_run_files(prior_run, node="D", attempt="2", base_sha="deadbeef", nodes=nodes)
        prior_path = str((Path(td) / "prior-worktree").resolve())
        prior_identity = prior_run / "artifacts" / "D" / "1" / "dispatch-identity.json"
        prior_identity.parent.mkdir(parents=True)
        prior_identity.write_text(json.dumps({"worktree_path": prior_path}) + "\n", encoding="utf-8")
        init_primary_repo(Path(primary_repo))
        init_primary_repo(prior_run.parent / "repo")

        original = module.herdr
        try:
            for run, attempt, path, branch, protected in (
                (primary_run, primary_attempt, primary_repo, "run-abc/D/1", True),
                (prior_run, prior_attempt, prior_path, "run-abc/D/2", False),
            ):
                fake = spawn_herdr(create={"result": {
                    "workspace": {"workspace_id": "ws"},
                    "root_pane": {"pane_id": "pane"},
                    "worktree": {"worktree_path": path, "branch": branch, "base_sha": "deadbeef"},
                }})
                module.herdr = fake
                code, out, err = run_module(module, ['spawn-agent.py', 'worker', '--run', str(run), '--node', 'D', '--attempt', attempt.name, *auth_args()])
                T.assertEqual(code, 1, err)
                T.assertEqual(out, "")
                T.assertIn('integration worktree must be fresh', err)
                T.assertFalse((attempt / 'dispatch-identity.json').exists())
                T.assertFalse(any((call[:2] in (('agent', 'start'), ('worktree', 'remove')) for call in fake.calls)))
                if protected:
                    T.assertFalse(any((call[:2] == ('workspace', 'close') for call in fake.calls)))
                    T.assertTrue(Path(path).is_dir())
                else:
                    T.assertIn(('workspace', 'close', 'ws'), fake.calls)
        finally:
            module.herdr = original


def collect_module(
    tmp: Path,
    attempt: Path,
    *,
    vcs: dict | None = None,
    identity: dict | None = None,
    consumed: object | None = None,
    integration: object | None = None,
    outputs: list[dict] | None = None,
    fake: FakeHerdr | None = None,
    names: list[str] | None = None,
) -> tuple[int, dict | None, str]:
    module = load_script("collect-agent.py")
    session = session_file(tmp)
    run = attempt.parents[2]
    if identity is not None:
        (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
    if vcs is not None:
        (attempt / "vcs.json").write_text(json.dumps(vcs) + "\n", encoding="utf-8")
    if consumed is not None:
        (attempt / "consumed_artifacts.json").write_text(json.dumps(consumed) + "\n", encoding="utf-8")
    if integration is not None:
        (attempt / "integration.json").write_text(json.dumps(integration) + "\n", encoding="utf-8")
    if outputs is not None:
        write_graph(run, outputs)
    original = module.herdr
    module.herdr = fake or collect_herdr(session, tokens=(identity or {}).get("tokens"))
    try:
        code, out, err = run_module(module, [
            "collect-agent.py", *(names or ["worker"]), "--run", str(run), "--node", "Node.1", "--attempt", "1",
            "--session", DEFAULT_HOLDER["session"],
            "--holder-workspace", DEFAULT_HOLDER["workspace"],
            "--holder-pane", DEFAULT_HOLDER["pane"],
        ])
    finally:
        module.herdr = original
    return code, json.loads(out) if out.strip() else None, err


def check_collect_vcs_mismatch_exit_3() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, attempt = attempt_tree(Path(td))
        identity = identity_doc(run)
        vcs = diff_vcs(attempt, identity)
        vcs["branch"] = "other"
        code, out, err = collect_module(Path(td), attempt, vcs=vcs, identity=identity)
        T.assertEqual(code, 3)
        T.assertIs(out, None)
        T.assertIn('branch', err)


def check_collect_vcs_match_hashes() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, attempt = attempt_tree(Path(td))
        identity = identity_doc(run)
        vcs = diff_vcs(attempt, identity)
        code, out, err = collect_module(Path(td), attempt, vcs=vcs, identity=identity, outputs=[{"name": "vcs", "type": "vcs"}])
        T.assertEqual(code, 0, err)
        agent = out["agents"][0]
        T.assertTrue(agent['summary_hash'].startswith('sha256:'))
        T.assertEqual({(a['name'], a['output'], a['type']) for a in agent['artifacts']}, {('summary', 'summary', 'summary'), ('vcs', 'vcs', 'vcs')})
        T.assertTrue(all((Path(a['path']).is_absolute() and a['hash'].startswith('sha256:') for a in agent['artifacts'])))
        T.assertEqual(json.loads((attempt / 'summary.json').read_text(encoding='utf-8'))['status'], 'done')


def check_collect_without_run_flags() -> None:
    module = load_script("collect-agent.py")
    original = module.herdr
    with tempfile.TemporaryDirectory() as td:
        session = session_file(Path(td))
        module.herdr = collect_herdr(session)
        try:
            code, out, err = run_module(module, ["collect-agent.py", "worker", "helper", "--status-only"])
            T.assertEqual(code, 0, err)
            T.assertEqual([agent['name'] for agent in json.loads(out)['agents']], ['worker', 'helper'])
        finally:
            module.herdr = original


def check_collect_run_allows_one_name() -> None:
    module = load_script("collect-agent.py")
    with tempfile.TemporaryDirectory() as td:
        run, _ = attempt_tree(Path(td))
        code, _, err = run_module(module, ["collect-agent.py", "worker", "helper", "--run", str(run), "--node", "Node.1", "--attempt", "1"])
        T.assertEqual(code, 2)
        T.assertIn('exactly one', err)


def check_collect_wrong_workspace_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, attempt = attempt_tree(Path(td))
        identity = identity_doc(run)
        code, out, err = collect_module(Path(td), attempt, identity=identity, fake=collect_herdr(session_file(Path(td)), workspace="ws-other", tokens=identity["tokens"]))
        T.assertEqual(code, 3)
        T.assertIs(out, None)
        T.assertIn('workspace', err)

def check_collect_wrong_pane_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, attempt = attempt_tree(Path(td))
        identity = identity_doc(run)
        code, out, err = collect_module(Path(td), attempt, identity=identity, fake=collect_herdr(session_file(Path(td)), pane="pane-other", tokens=identity["tokens"]))
        T.assertEqual(code, 3)
        T.assertIs(out, None)
        T.assertIn('pane', err)


def check_collect_wrong_agent_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, attempt = attempt_tree(Path(td))
        identity = identity_doc(run)
        code, out, err = collect_module(Path(td), attempt, identity=identity, fake=collect_herdr(session_file(Path(td)), agent_id="agent-other", tokens=identity["tokens"]))
        T.assertEqual(code, 3)
        T.assertIs(out, None)
        T.assertIn('agent id', err)



def check_collect_wrong_tokens_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, attempt = attempt_tree(Path(td))
        identity = identity_doc(run)
        bad_tokens = {"run": run.name, "node": "Node.1", "attempt": "2"}
        code, out, err = collect_module(Path(td), attempt, identity=identity, fake=collect_herdr(session_file(Path(td)), tokens=bad_tokens))
        T.assertEqual(code, 3)
        T.assertIs(out, None)
        T.assertIn('token', err)


def check_collect_missing_identity_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, attempt = attempt_tree(Path(td))
        identity = identity_doc(run)
        vcs = diff_vcs(attempt, identity)
        code, out, err = collect_module(Path(td), attempt, vcs=vcs)
        T.assertEqual(code, 3)
        T.assertIs(out, None)
        T.assertIn('dispatch-identity', err)


def check_collect_external_diff_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        run, attempt = attempt_tree(Path(td))
        identity = identity_doc(run)
        external = Path(td) / "change.bin"
        external.write_bytes(b"change")
        raw = hashlib.sha256(external.read_bytes()).hexdigest()
        vcs = {
            "repo": identity["repo"],
            "worktree": identity["worktree_path"],
            "branch": identity["branch"],
            "base_sha": identity["base"],
            "result": {"form": "diff", "path": str(external), "sha256": raw},
        }
        code, out, err = collect_module(Path(td), attempt, vcs=vcs, identity=identity)
        T.assertEqual(code, 3)
        T.assertIs(out, None)
        T.assertIn('change.bin', err)



def check_collect_integration_output_hashes() -> None:
    with tempfile.TemporaryDirectory() as td:
        wt = seed_repo(Path(td) / "wt")
        base = git(wt, "rev-parse", "HEAD")
        head = commit_file(wt, "file.txt", "two\n", "result")
        branch = git(wt, "branch", "--show-current")
        run = Path(td) / "run-abc"
        attempt = plant_run_files(run, base_sha=base, repo=str(wt.resolve()))
        identity = identity_doc(run, worktree_path=str(wt.resolve()), branch=branch, base=base)
        vcs = {
            "repo": identity["repo"],
            "worktree": str(wt.resolve()),
            "branch": branch,
            "base_sha": base,
            "result": {"form": "commit", "sha": head},
        }
        consumed = [{"producer": "B", "producer_attempt": 1, "output": "vcs", "path": "/tmp/b", "hash": "sha256:good"}]
        integration = integration_doc(base, [{"producer": "B", "attempt": 1, "output": "vcs", "hash": "sha256:good"}], commit=head)
        write_graph(
            run,
            [{"name": "vcs", "type": "vcs"}, {"name": "integration", "type": "integration"}],
            extra_nodes={"B": {"id": "B", "executor": "task", "outputs": [{"name": "vcs", "type": "vcs"}]}},
            inputs=[{"alias": "b", "from": "B", "output": "vcs"}],
        )
        code, out, err = collect_module(
            Path(td),
            attempt,
            vcs=vcs,
            identity=identity,
            consumed=consumed,
            integration=integration,
            outputs=[{"name": "vcs", "type": "vcs"}, {"name": "integration", "type": "integration"}],
        )
        T.assertEqual(code, 0, err)
        artifacts = out["agents"][0]["artifacts"]
        T.assertEqual({a['name'] for a in artifacts}, {'summary', 'vcs', 'integration'})
        T.assertTrue(all((Path(a['path']).is_absolute() and a['hash'].startswith('sha256:') for a in artifacts)))


def drain_run(run: Path, holder=None) -> None:
    holder = holder or DEFAULT_HOLDER
    run_state.append_event(run, {"type": "lease_drain_started", **holder}, holder=holder)


def check_spawn_draining_refuses_launch() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            drain_run(run)
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args(),
            ])
            T.assertEqual(code, 3, err)
            T.assertEqual(fake.calls, [])
            T.assertFalse((attempt / 'dispatch-identity.json').exists())
    finally:
        module.herdr = original


def check_spawn_stale_epoch_refuses_launch() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args(epoch=9),
            ])
            T.assertEqual(code, 3, err)
            T.assertEqual(fake.calls, [])
            T.assertFalse((attempt / 'dispatch-identity.json').exists())
    finally:
        module.herdr = original


def check_spawn_contextual_requires_holder_flags() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1",
            ])
            T.assertEqual(code, 2)
            T.assertTrue('holder' in err or 'epoch' in err)
            T.assertEqual(fake.calls, [])
            T.assertFalse((attempt / 'dispatch-identity.json').exists())
    finally:
        module.herdr = original


LAUNCH_RACE_CHILD = """
import errno, fcntl, json, os, sys
sys.path.insert(0, sys.argv[1])
import run_state
run, holder_path = sys.argv[2], sys.argv[3]
holder = json.loads(open(holder_path).read())
fd = os.open(os.path.join(run, 'run.lock'), os.O_RDWR | os.O_CREAT, 0o644)
try:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
            raise
        status = 'blocked'
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        status = 'unlocked'
finally:
    os.close(fd)
print(status, flush=True)
run_state.append_event(run, {'type': 'lease_drain_started', **holder}, holder=holder)
"""


def check_spawn_launch_race_holds_lock() -> None:
    """Launch holds run.lock: the child probes with a non-blocking flock and
    reports 'blocked', instead of inferring ownership from wallclock non-completion."""
    module = load_script("spawn-agent.py")
    started = threading.Event()
    release = threading.Event()

    def blocking_herdr():
        fake = spawn_herdr()
        for verb in (("workspace", "create"), ("worktree", "create")):
            response = fake.routes[verb]
    
            def create(*args, response=response):
                started.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("release timeout")
                return response
    
            fake.routes[verb] = create
        return fake

    fake = blocking_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            holder_path = Path(td) / "holder.json"
            holder_path.write_text(json.dumps(DEFAULT_HOLDER), encoding="utf-8")
            result: dict = {}

            def spawn():
                result["ret"] = run_module(module, [
                    "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args(),
                ])

            worker = threading.Thread(target=spawn)
            worker.start()
            T.assertTrue(started.wait(5), 'spawn never reached workspace create')
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    LAUNCH_RACE_CHILD,
                    str(SCRIPTS),
                    str(run),
                    str(holder_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            ready, _, _ = select.select([proc.stdout], [], [], 5)
            T.assertTrue(ready, 'lock probe child produced no output')
            T.assertEqual(proc.stdout.readline().strip(), 'blocked')
            T.assertFalse((attempt / 'dispatch-identity.json').exists())
            release.set()
            worker.join(15)
            stdout, stderr = proc.communicate(timeout=15)
            code, out, err = result["ret"]
            T.assertEqual(code, 0, err)
            T.assertTrue((attempt / 'dispatch-identity.json').is_file())
            T.assertEqual(proc.returncode, 0, stderr or stdout)
            types = [json.loads(line)["type"] for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
            T.assertIn('lease_drain_started', types)
    finally:
        release.set()
        module.herdr = original


def check_spawn_explicit_requested_model_preserved() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        code, out, err = run_module(module, ["spawn-agent.py", "worker", "--model", "provider/explicit:high"])
        T.assertEqual(code, 0, err)
        payload = json.loads(out)
        T.assertEqual(payload['routing']['requested_model'], 'provider/explicit:high')
        T.assertEqual(payload['routing']['resolved'], 'provider/explicit')
        T.assertEqual(payload['routing']['thinking'], 'high')
        T.assertIn('requested_role', payload['routing'])
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            fake.calls.clear()
            code, out, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1",
                *auth_args(), "--model", "provider/explicit:high",
            ])
            T.assertEqual(code, 0, err)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            payload = json.loads(out)
            T.assertEqual(identity['routing'], payload['routing'])
            T.assertEqual(identity['routing']['requested_model'], 'provider/explicit:high')
    finally:
        module.herdr = original


def check_spawn_crash_recovery_routing() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr(start={"result": {"agent": {"id": "agent-id", "model": "actual/model:deep", "effort": "high"}}})
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            code, out, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1",
                *auth_args(), "--model", "actual/model:deep",
            ])
            T.assertEqual(code, 0, err)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            payload = json.loads(out)
            T.assertEqual(identity['routing'], payload['routing'])
            T.assertTrue('requested_model' not in identity['routing'] or identity['routing']['requested_model'] == 'actual/model:deep')
            types = [json.loads(line)["type"] for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines() if line]
            T.assertNotIn('dispatched', types)
            live = [{
                "workspace": identity["workspace"],
                "pane": identity["pane"],
                "identity_kind": identity["identity_kind"],
                "worker_id": identity["worker_id"],
                "tokens": identity["tokens"],
            }]
            result = run_state.reconcile(
                run,
                holder=DEFAULT_HOLDER,
                current_session=DEFAULT_HOLDER["session"],
                live_herdr=live,
            )
            node = result["state"]["nodes"]["Node.1"]
            T.assertEqual(node['state'], 'running')
            recovered = node["model"]
            for key in run_state.MODEL_KEYS:
                T.assertEqual(recovered[key], identity['routing'][key])
            T.assertEqual(recovered.get('requested_model'), identity['routing'].get('requested_model'))
    finally:
        module.herdr = original


def check_spawn_metadata_boundary_blocks_duplicate() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            identity_path = attempt / "dispatch-identity.json"

            def stamp_fail():
                fake = spawn_herdr()
                fake.routes[("workspace", "report-metadata")] = RuntimeError("herdr workspace report-metadata failed: boom")
                return fake

            fake = stamp_fail()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('boom', err)
            T.assertTrue(identity_path.is_file())
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            T.assertEqual(identity['workspace'], 'ws')
            T.assertEqual(identity['pane'], 'pane')
            T.assertEqual(identity['identity_kind'], module.graph_model.IDENTITY_MANAGED_NAME)
            T.assertTrue(module.graph_model.WORKER_ID_RE.fullmatch(identity['worker_id']))
            T.assertNotIn('agent', identity)
            kinds = [call[:2] for call in mutating_calls(fake.calls)]
            T.assertTrue(('workspace', 'create') in kinds or ('worktree', 'create') in kinds)
            T.assertIn(('agent', 'start'), kinds)
            T.assertNotIn(('agent', 'prompt'), kinds)
            T.assertFalse(any((call[:2] in (('workspace', 'close'), ('worktree', 'remove')) for call in fake.calls)))

            retry = spawn_herdr()
            module.herdr = retry
            code, _, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 2)
            T.assertIn('dispatch identity already exists', err)
            T.assertEqual(retry.calls, [])
            T.assertFalse(any((call[:2] in (('workspace', 'create'), ('worktree', 'create'), ('agent', 'start')) for call in retry.calls)))
    finally:
        module.herdr = original


def check_spawn_identity_recheck_before_create() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    original = module.herdr
    original_path = module.sidecar_path

    def skip_exists(kind, context, parser, *, require_absent=False):
        run, node, attempt = context
        return run_state.attempt_dir(run, node, int(attempt)) / f"{kind}.json"

    module.herdr = fake
    module.sidecar_path = skip_exists
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            (attempt / "dispatch-identity.json").write_text("{}\n", encoding="utf-8")
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args(),
            ])
            T.assertEqual(code, 2)
            T.assertIn('dispatch identity already exists', err)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original
        module.sidecar_path = original_path

def check_spawn_missing_stable_agent_id_fences() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr(start={"result": {"agent": {"name": "worker"}}},
    get={"result": {"agent": {"name": "worker"}}},)
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('does not match worker_id', err)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertNotIn('agent', identity)
            T.assertEqual(identity['workspace'], 'ws')
            T.assertEqual(identity['pane'], 'pane')
            T.assertEqual(identity['identity_kind'], module.graph_model.IDENTITY_MANAGED_NAME)
            T.assertTrue(module.graph_model.WORKER_ID_RE.fullmatch(identity['worker_id']))
            T.assertNotEqual(identity['worker_id'], 'worker')
            T.assertEqual(identity.get('repo'), str((run.parent / 'repo').resolve()))
            kinds = [call[:2] for call in mutating_calls(fake.calls)]
            T.assertNotIn(('workspace', 'report-metadata'), kinds)
            T.assertNotIn(('agent', 'prompt'), kinds)
            T.assertFalse(any((call[:2] in (('workspace', 'close'), ('worktree', 'remove')) for call in fake.calls)))
            retry = spawn_herdr()
            module.herdr = retry
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args(),
            ])
            T.assertEqual(code, 2)
            T.assertIn('dispatch identity already exists', err)
            T.assertEqual(retry.calls, [])
    finally:
        module.herdr = original


def check_spawn_persists_agent_id_not_label() -> None:
    module = load_script("spawn-agent.py")

    def name_only_herdr():
        fake = spawn_herdr()
        for verb in (("agent", "start"), ("agent", "get")):
            fake.routes[verb] = lambda *args: {"result": {"agent": {"name": args[2]}}}
        return fake

    fake = name_only_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 0, err)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            payload = json.loads(out)
            worker_id = identity["worker_id"]
            T.assertTrue(module.graph_model.WORKER_ID_RE.fullmatch(worker_id))
            T.assertEqual(payload['name'], payload['worker_id'])
            T.assertEqual(payload['worker_id'], worker_id)
            T.assertEqual(payload['alias'], 'worker')
            T.assertNotIn('agent', identity)
            T.assertNotIn('agent', payload)
            T.assertIn(('agent', 'start', worker_id, '--kind', 'omp', '--pane', 'pane'), fake.calls)
            T.assertTrue(any((call[:3] == ('agent', 'get', worker_id) for call in fake.calls)))
            T.assertTrue(any((call[:3] == ('agent', 'prompt', worker_id) for call in fake.calls)))
    finally:
        module.herdr = original


def check_spawn_uncontextual_name_only_response_accepted() -> None:
    module = load_script("spawn-agent.py")
    shape = {"result": {"agent": {"name": "worker", "pane_id": "pane", "workspace_id": "ws"}}}
    fake = spawn_herdr(start=shape, get=shape)
    original = module.herdr
    module.herdr = fake
    try:
        code, out, err = run_module(module, ["spawn-agent.py", "worker", "--prompt", "work"])
        T.assertEqual(code, 0, err)
        payload = json.loads(out)
        T.assertEqual(payload['agent'], 'worker')
        T.assertIs(payload['prompted'], True)
        T.assertTrue(any((call[:3] == ('agent', 'prompt', 'worker') for call in fake.calls)))
    finally:
        module.herdr = original


def check_spawn_uncontextual_wrong_name_still_refused() -> None:
    module = load_script("spawn-agent.py")
    shape = {"result": {"agent": {"name": "other", "pane_id": "pane", "workspace_id": "ws"}}}
    fake = spawn_herdr(start=shape, get=shape)
    original = module.herdr
    module.herdr = fake
    try:
        code, out, err = run_module(module, ['spawn-agent.py', 'worker', '--prompt', 'work'])
        T.assertEqual(code, 1, err)
        T.assertEqual(out, "")
        msg = err
        T.assertIn('missing stable agent id', msg)
        T.assertIn('workspace ws', msg)
        T.assertIn('pane pane', msg)
        T.assertFalse(any((call[:2] in (('workspace', 'close'), ('worktree', 'remove')) for call in fake.calls)))
    finally:
        module.herdr = original


def check_spawn_post_start_prompt_failure_reports_follow_up() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr(prompt_error=_herdr_error("unrelated", "boom"))
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            prompt_file = Path(td) / "prompt.txt"
            prompt_file.write_text("work", encoding="utf-8")
            code, out, err = run_module(module, ['spawn-agent.py', 'worker', '--prompt-file', str(prompt_file)])
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            msg = err
            T.assertIn('boom', msg)
            T.assertIn('pane pane', msg)
            T.assertFalse(any((call[:2] in (('workspace', 'close'), ('worktree', 'remove')) for call in fake.calls)))
    finally:
        module.herdr = original



def check_prompt_name_only_agent_refused() -> None:
    module = load_script("prompt-agent.py")
    fake = pane_prompt_herdr(get={
        "result": {
            "agent": {
                "name": "worker",
                "workspace_id": "ws-live",
                "pane_id": "w1:p6",
            }
        }
    })
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-id"
            plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, pane_ready_events())
            code, _, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1",
                *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 3)
            T.assertIn('stable id', err)
            T.assertEqual(mutating_calls(fake.calls), [])
            T.assertFalse(any((call[:2] in (('workspace', 'report-metadata'), ('agent', 'prompt')) for call in fake.calls)))
    finally:
        module.herdr = original


def check_prompt_name_only_workspace_refused() -> None:
    module = load_script("prompt-agent.py")
    fake = pane_prompt_herdr(get={
        "result": {
            "agent": {
                "agent_id": "agent-live",
                "name": "worker",
                "workspace": {"name": "ws-label"},
                "pane_id": "w1:p6",
            }
        }
    })
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-id"
            plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, pane_ready_events())
            code, _, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1",
                *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 3)
            T.assertIn('workspace', err)
            T.assertEqual(mutating_calls(fake.calls), [])
            T.assertFalse(any((call[:2] in (('workspace', 'get'), ('workspace', 'report-metadata'), ('agent', 'prompt')) for call in fake.calls)))
    finally:
        module.herdr = original


def check_prompt_name_only_pane_refused() -> None:
    module = load_script("prompt-agent.py")

    def name_only_pane(**kwargs):
        fake = pane_prompt_herdr(**kwargs)
        fake.routes[("workspace", "get")] = {"result": {"workspace": {
            "tokens": fake.routes[("workspace", "get")]["result"]["workspace"]["tokens"],
            "root_pane": {"name": "main"}, "pane": {"name": "main"},
        }}}
        return fake

    fake = name_only_pane(get={
        "result": {
            "agent": {
                "agent_id": "agent-live",
                "name": "worker",
                "workspace_id": "ws-live",
                "pane": {"name": "main"},
                "root_pane": {"name": "main"},
            }
        }
    })
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-id"
            plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, pane_ready_events())
            code, _, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1",
                *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 3)
            T.assertIn('pane', err)
            T.assertEqual(mutating_calls(fake.calls), [])
            T.assertTrue(any(call[:3] == ("workspace", "get", "ws-live") for call in fake.calls))
            T.assertFalse(any((call[:2] in (('workspace', 'report-metadata'), ('agent', 'prompt')) for call in fake.calls)))
    finally:
        module.herdr = original


def check_spawn_name_only_create_ids_rejected() -> None:
    module = load_script("spawn-agent.py")
    fake = spawn_herdr(create={"result": {
        "workspace": {"name": "ws-label"},
        "root_pane": {"name": "pane-label"},
    }})
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertTrue('workspace_id' in err or 'pane_id' in err)
            T.assertFalse((attempt / 'dispatch-identity.json').exists())
            T.assertEqual([call[:2] for call in mutating_calls(fake.calls)], [('workspace', 'create')])
            T.assertFalse(any((call[:2] in (('agent', 'start'), ('workspace', 'report-metadata'), ('agent', 'prompt')) for call in fake.calls)))
    finally:
        module.herdr = original



def check_prompt_draining_refuses() -> None:
    module = load_script("prompt-agent.py")
    fake = pane_prompt_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-id"
            plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, pane_ready_events())
            drain_run(run)
            code, _, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 3, err)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original


def check_prompt_stale_holder_refuses() -> None:
    module = load_script("prompt-agent.py")
    fake = pane_prompt_herdr()
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-id"
            plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, pane_ready_events())
            stale = {"workspace": "other-ws", "pane": "other-pane", "session": "other-session"}
            code, _, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1",
                *auth_args(holder=stale), "--prompt", "work",
            ])
            T.assertEqual(code, 3, err)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original


def check_prompt_blocked_pane_continuation() -> None:
    module = load_script("prompt-agent.py")
    fake = pane_prompt_herdr(workspace_pane="w1:p1", agent_pane="w1:p6")
    original = module.herdr
    module.herdr = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-id"
            plant_run_files(run, node="P", nodes=pane_nodes())
            handle = {"name": "worker", "workspace": "ws-live", "pane": "w1:p6", "agent": "agent-live"}
            model = {
                "requested_role": "slow",
                "requested_effort": "low",
                "resolved": "provider/live",
                "thinking": "high",
                "effort": "medium",
            }
            write_events(run, [
                {"type": "session_run_bound", **DEFAULT_HOLDER},
                {"type": "ready", "node": "P", "from": "planned", "to": "ready", "attempt": 1},
                dispatch_intent_event("P", 1),
                {"type": "dispatched", "node": "P", "attempt": 1, "handle": handle, "model": model},
                {"type": "blocked", "node": "P", "from": "running", "to": "blocked", "attempt": 1, "reason": "approval requested"},
            ])
            before = (run / "events.jsonl").read_text(encoding="utf-8")
            code, out, err = run_module(module, [
                "prompt-agent.py", "worker", "--run", str(run), "--node", "P", "--attempt", "1", *auth_args(), "--prompt", "work",
            ])
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            T.assertEqual(payload['event']['type'], 'attempt_started')
            T.assertEqual(payload['event']['from'], 'blocked')
            T.assertEqual(payload['event']['handle'], handle)
            T.assertEqual(payload['event']['live_identity']['agent'], 'agent-live')
            T.assertEqual(payload['event']['model'], model)
            T.assertEqual(payload['event']['live_identity']['routing'], model)
            T.assertEqual((run / 'events.jsonl').read_text(encoding='utf-8'), before)
            T.assertFalse(any((call[:2] in (('workspace', 'create'), ('worktree', 'create'), ('agent', 'start')) for call in fake.calls)))
    finally:
        module.herdr = original


def check_spawn_launch_intent_and_failure_evidence() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td) / "ordered")
            launch_path = attempt / "launch-intent.json"

            def intent_aware_herdr():
                fake = spawn_herdr()
                response = fake.routes[("workspace", "create")]
            
                def create(*args):
                    T.assertTrue(launch_path.is_file())
                    return response
            
                fake.routes[("workspace", "create")] = create
                return fake

            fake = intent_aware_herdr()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            launch = json.loads(launch_path.read_text(encoding="utf-8"))
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(launch['worker_id'], identity['worker_id'])
            T.assertEqual(identity['worker_id'], payload['worker_id'])
            T.assertEqual(launch['identity_kind'], run_state.IDENTITY_MANAGED_NAME)

            run, attempt = attempt_tree(Path(td) / "duplicate")
            (attempt / "launch-intent.json").write_text("{}\n", encoding="utf-8")
            fake = spawn_herdr()
            module.herdr = fake
            code, _, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1", *auth_args(),
            ])
            T.assertEqual(code, 2)
            T.assertIn('launch intent already exists', err)
            T.assertEqual(fake.calls, [])

            run, attempt = attempt_tree(Path(td) / "create-failure")

            def create_fail_herdr():
                fake = spawn_herdr()
                fake.routes[('workspace', 'create')] = RuntimeError('herdr workspace create failed: boom')
                return fake

            fake = create_fail_herdr()
            module.herdr = fake
            code, out, err = run_module(module, ['spawn-agent.py', 'worker', '--run', str(run), '--node', 'Node.1', '--attempt', '1', *auth_args()])
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('boom', err)
            T.assertTrue((attempt / 'launch-intent.json').is_file())
            T.assertFalse((attempt / 'dispatch-identity.json').exists())
            T.assertEqual([call[:2] for call in mutating_calls(fake.calls)], [('workspace', 'create')])

            run, attempt = attempt_tree(Path(td) / "start-failure")

            def start_fail_herdr():
                fake = spawn_herdr()
                fake.routes[('agent', 'start')] = RuntimeError('herdr agent start failed: boom')
                return fake

            fake = start_fail_herdr()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('boom', err)
            launch = json.loads((attempt / "launch-intent.json").read_text(encoding="utf-8"))
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(identity['worker_id'], launch['worker_id'])
            T.assertEqual(identity['workspace'], 'ws')
            T.assertEqual(identity['pane'], 'pane')
            T.assertNotIn('routing', identity)
            T.assertNotIn('agent', identity)
            T.assertFalse(any((call[:2] in (('workspace', 'close'), ('worktree', 'remove'), ('workspace', 'report-metadata'), ('agent', 'prompt')) for call in fake.calls)))
    finally:
        module.herdr = original


def _spawn_argv(run: Path, *extra: str) -> list[str]:
    return [
        "spawn-agent.py", "worker", "--run", str(run), "--node", "Node.1", "--attempt", "1",
        *auth_args(), "--prompt", "work", *extra,
    ]


def stamp_fail_herdr():
    fake = spawn_herdr()
    fake.routes[("workspace", "report-metadata")] = RuntimeError("herdr workspace report-metadata failed: boom")
    return fake


def intent_resume_herdr(*, get_error="agent_not_found", workspaces=None):
    fake = spawn_herdr()
    live = False
    get_route = fake.routes[("agent", "get")]
    start_route = fake.routes[("agent", "start")]

    def get(*args):
        if get_error and not live:
            if get_error == "agent_not_found":
                raise _herdr_error("agent_not_found")
            raise RuntimeError(get_error)
        return get_route(*args)

    def start(*args):
        nonlocal live
        live = True
        return start_route

    fake.routes.update({
        ("agent", "get"): get,
        ("agent", "start"): start,
        ("workspace", "list"): {"result": {"workspaces": workspaces if workspaces is not None else []}},
    })
    return fake


def _plant_intent(run: Path, attempt: Path, worker_id: str) -> None:
    (attempt / "launch-intent.json").write_text(json.dumps({
        "identity_kind": run_state.IDENTITY_MANAGED_NAME,
        "worker_id": worker_id,
        "run": str(run.resolve()),
        "node": "Node.1",
        "attempt": "1",
    }) + "\n", encoding="utf-8")


def check_spawn_resume_delivers_pending_prompt() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            module.herdr = stamp_fail_herdr()
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('boom', err)
            T.assertTrue((attempt / 'dispatch-identity.json').is_file())
            T.assertFalse((attempt / 'prompt-attempt.json').exists())
            T.assertFalse((attempt / 'launch-complete.json').exists())
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            worker_id = identity["worker_id"]
            fake = spawn_herdr()
            module.herdr = fake
            events = (run / "events.jsonl").read_text(encoding="utf-8")
            code, out, err = run_module(module, _spawn_argv(run, "--resume"))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            T.assertIs(payload['resumed'], True)
            T.assertIs(payload['prompted'], True)
            T.assertEqual(payload['worker_id'], worker_id)
            T.assertFalse(any((call[:2] in {('workspace', 'create'), ('worktree', 'create')} for call in fake.calls)))
            T.assertEqual((run / 'events.jsonl').read_text(encoding='utf-8'), events)
            stamp = next(call for call in fake.calls if call[:2] == ("workspace", "report-metadata"))
            tokens = [stamp[i + 1] for i, arg in enumerate(stamp) if arg == "--token"]
            T.assertLessEqual({'provenance=orchestrator', 'run=run-abc', 'node=Node.1', 'attempt=1'}, set(tokens))
            marker = json.loads((attempt / "prompt-attempt.json").read_text(encoding="utf-8"))
            T.assertEqual(marker['worker_id'], worker_id)
            T.assertEqual(marker['workspace'], 'ws')
            T.assertEqual(marker['pane'], 'pane')
            record = json.loads((attempt / "launch-complete.json").read_text(encoding="utf-8"))
            T.assertIs(record['prompted'], True)
            T.assertEqual(record['worker_id'], worker_id)
    finally:
        module.herdr = original


def check_spawn_resume_fails_closed_after_prompt_sent() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            first = spawn_herdr(prompt_error=_herdr_error("connection_reset", "connection reset"))
            module.herdr = first
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('connection reset', err)
            T.assertTrue(any((call[:2] == ('agent', 'prompt') for call in first.calls)))
            T.assertTrue((attempt / 'dispatch-identity.json').is_file())
            T.assertTrue((attempt / 'prompt-attempt.json').is_file())
            T.assertFalse((attempt / 'launch-complete.json').exists())
            worker_id = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))["worker_id"]

            fake = spawn_herdr()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run, "--resume"))
            T.assertEqual(code, 3, (code, out, err))
            T.assertIn('ambiguous', err)
            T.assertIn('workspace ws pane pane', err)
            T.assertIn(f'herdr agent read {worker_id} --source recent --lines 40', err)
            T.assertIn('--resume --delivered', err)
            T.assertIn(f'--run {run}', err)
            T.assertIn('--holder-workspace controller-ws', err)
            T.assertEqual(fake.calls, [])
            T.assertFalse((attempt / 'launch-complete.json').exists())

            code, out, err = run_module(module, _spawn_argv(run, "--resume", "--delivered"))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            T.assertIs(payload['resumed'], True)
            T.assertIs(payload['delivered'], True)
            T.assertIs(payload['prompted'], True)
            T.assertIs(payload['blocked'], False)
            T.assertEqual(payload['worker_id'], worker_id)
            record = json.loads((attempt / "launch-complete.json").read_text(encoding="utf-8"))
            T.assertIs(record['prompted'], True)
            T.assertIs(record['blocked'], False)
            T.assertEqual(record['worker_id'], worker_id)
            T.assertEqual(record['workspace'], 'ws')
            T.assertEqual(record['pane'], 'pane')
            T.assertEqual(fake.calls, [])

            code, out, err = run_module(module, _spawn_argv(run, "--resume"))
            T.assertEqual(code, 0, err)
            T.assertIs(json.loads(out)['prompted'], True)
            T.assertEqual(fake.calls, [])

            run, attempt = attempt_tree(Path(td) / "usage")
            code, _, err = run_module(module, _spawn_argv(run, "--delivered"))
            T.assertEqual(code, 2)
            T.assertIn('--delivered requires --resume', err)
            code, _, err = run_module(module, _spawn_argv(run, "--resume", "--delivered"))
            T.assertEqual(code, 2)
            T.assertIn('dispatch-identity.json', err)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original


def check_spawn_intent_resume_fails_closed_on_inconclusive_lookup() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    worker_id = "fo-" + "cd" * 12
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td) / "server-down")
            _plant_intent(run, attempt, worker_id)
            fake = intent_resume_herdr(get_error="herdr agent get failed: server_not_running")
            module.herdr = fake
            code, _, err = run_module(module, _spawn_argv(run, "--resume"))
            T.assertEqual(code, 3)
            T.assertIn(f'launch intent worker {worker_id} lookup inconclusive', err)
            T.assertIn('server_not_running', err)
            T.assertEqual(mutating_calls(fake.calls), [])
            T.assertFalse((attempt / 'dispatch-identity.json').exists())

            run, attempt = attempt_tree(Path(td) / "live")
            _plant_intent(run, attempt, worker_id)
            fake = intent_resume_herdr(get_error=None)
            module.herdr = fake
            code, _, err = run_module(module, _spawn_argv(run, "--resume"))
            T.assertEqual(code, 3)
            T.assertIn('live without dispatch identity', err)
            T.assertIn(worker_id, err)
            T.assertEqual(mutating_calls(fake.calls), [])
            T.assertFalse((attempt / 'dispatch-identity.json').exists())

            run, attempt = attempt_tree(Path(td) / "orphan")
            _plant_intent(run, attempt, worker_id)
            fake = intent_resume_herdr(workspaces=[
                {"workspace_id": "w12", "label": "other"},
                {"workspace_id": "w33", "label": f"worker · Node.1/1 · {worker_id}"},
            ])
            module.herdr = fake
            code, _, err = run_module(module, _spawn_argv(run, "--resume"))
            T.assertEqual(code, 3, err)
            T.assertIn(f'workspace w33 exists for launch intent worker {worker_id}', err)
            T.assertIn('herdr workspace close w33', err)
            T.assertIn('herdr worktree remove --workspace w33', err)
            T.assertEqual(mutating_calls(fake.calls), [])
            T.assertFalse((attempt / 'dispatch-identity.json').exists())
    finally:
        module.herdr = original


def check_spawn_intent_resume_creates_after_explicit_not_found() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    worker_id = "fo-" + "ab" * 12
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            _plant_intent(run, attempt, worker_id)
            fake = intent_resume_herdr(workspaces=[{"workspace_id": "w12", "label": "other"}])
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run, "--resume"))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            T.assertEqual(payload['worker_id'], worker_id)
            T.assertIs(payload['prompted'], True)
            T.assertTrue(any(call[:2] == ("workspace", "create") for call in mutating_calls(fake.calls)))
            create_call = next(call for call in mutating_calls(fake.calls) if call[:2] == ('workspace', 'create'))
            initial = create_call[create_call.index("--label") + 1]
            T.assertIn(worker_id, initial.split(' · '))
            renamed = [call[-1] for call in fake.calls if call[:2] == ("workspace", "rename")]
            T.assertTrue(renamed)
            T.assertTrue(all((worker_id not in label.split(' · ') for label in renamed)))
            T.assertIn(('agent', 'start', worker_id, '--kind', 'omp', '--pane', 'pane'), fake.calls)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(identity['worker_id'], worker_id)
            T.assertIs(json.loads((attempt / 'launch-complete.json').read_text(encoding='utf-8'))['prompted'], True)
    finally:
        module.herdr = original


def check_spawn_human_labels() -> None:
    label = load_script("spawn-agent.py").human_label
    same = label("implement-api", "implement-api", "1")
    retry = label("implement-api", "implement-api", "2")
    alias = label("reviewer", "implement-api", "4")
    T.assertEqual(same.count('implement-api'), 1)
    T.assertNotEqual(same, retry)
    T.assertIn('1', same)
    T.assertIn('2', retry)
    T.assertIn('reviewer', alias)
    T.assertIn('implement-api', alias)
    T.assertIn('4', alias)
    T.assertEqual(label('worker'), 'worker')

def check_spawn_readonly_cwd_accepts_repo_worktree() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            repo = seed_repo(Path(td) / "repo")
            linked = make_worktree(repo, Path(td) / "linked-wt", "linked-wt")
            other = Path(td) / "other"
            other.mkdir()

            run, _ = attempt_tree(Path(td))  # Node.1 is read-only (summary only), repo = td/repo
            fake = spawn_herdr()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run, "--cwd", str(linked)))
            T.assertEqual(code, 0, err)
            create_call = next(call for call in fake.calls if call[:2] == ("workspace", "create"))
            T.assertEqual(create_call[create_call.index('--cwd') + 1], str(linked.resolve()))

            run2, _ = attempt_tree(Path(td) / "refused")
            fake = spawn_herdr()
            module.herdr = fake
            code, _, err = run_module(module, _spawn_argv(run2, "--cwd", str(other)))
            T.assertEqual(code, 2)
            T.assertIn('or one of its worktrees', err)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original



def check_spawn_resume_refuses_replaced_worker() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            worker_id = "fo-" + "ab" * 12
            identity = identity_doc(run, worker_id=worker_id, identity_kind=run_state.IDENTITY_MANAGED_NAME)
            identity.pop("agent", None)
            (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            fake = spawn_herdr(get={"result": {"agent": {"name": "other"}}})
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run, '--resume'))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn(worker_id, err)
            T.assertFalse(any((call[:2] == ('agent', 'prompt') for call in fake.calls)))
            T.assertFalse((attempt / 'launch-complete.json').exists())
    finally:
        module.herdr = original


def check_spawn_resume_reuses_launch_intent() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            launch_path = attempt / "launch-intent.json"

            def create_fail():
                fake = spawn_herdr()
                fake.routes[('workspace', 'create')] = RuntimeError('herdr workspace create failed: boom')
                return fake

            module.herdr = create_fail()
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('boom', err)
            intent = json.loads(launch_path.read_text(encoding="utf-8"))
            worker_id = intent["worker_id"]
            before = launch_path.read_text(encoding="utf-8")

            fake = intent_resume_herdr()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run, "--resume"))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(payload['worker_id'], identity['worker_id'])
            T.assertEqual(identity['worker_id'], worker_id)
            T.assertIn(('agent', 'start', worker_id, '--kind', 'omp', '--pane', 'pane'), fake.calls)
            T.assertEqual(launch_path.read_text(encoding='utf-8'), before)
            T.assertTrue(launch_path.is_file())
    finally:
        module.herdr = original


def check_spawn_resume_short_circuits_on_launch_record() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td))
            worker_id = "fo-" + "ef" * 12
            identity = identity_doc(
                run,
                worker_id=worker_id,
                identity_kind=run_state.IDENTITY_MANAGED_NAME,
                routing={
                    "requested_role": None,
                    "requested_effort": None,
                    "resolved": None,
                    "thinking": None,
                    "effort": None,
                },
            )
            identity.pop("agent", None)
            (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            (attempt / "launch-complete.json").write_text(json.dumps({
                "worker_id": worker_id,
                "workspace": "ws",
                "pane": "pane",
                "prompted": True,
                "blocked": False,
                "tokens": {"provenance": "orchestrator"},
            }) + "\n", encoding="utf-8")
            fake = spawn_herdr()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run, "--resume"))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            T.assertIs(payload['resumed'], True)
            T.assertEqual(payload['worker_id'], worker_id)
            T.assertIs(payload['prompted'], True)
            T.assertEqual(fake.calls, [])
    finally:
        module.herdr = original


def check_spawn_first_launch_writes_launch_record() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            run, attempt = attempt_tree(Path(td) / "ready")
            fake = spawn_herdr()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            record = json.loads((attempt / "launch-complete.json").read_text(encoding="utf-8"))
            T.assertIs(record['prompted'], True)
            T.assertIs(record['blocked'], False)
            T.assertEqual(record['worker_id'], payload['worker_id'])
            T.assertEqual(record['workspace'], 'ws')
            T.assertEqual(record['pane'], 'pane')

            run, attempt = attempt_tree(Path(td) / "not-ready")

            def not_ready_herdr():
                fake = spawn_herdr()
                fake.routes[("agent", "start")] = _herdr_error("agent_not_ready")
                return fake

            fake = not_ready_herdr()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            record = json.loads((attempt / "launch-complete.json").read_text(encoding="utf-8"))
            T.assertIs(record['prompted'], False)
            T.assertIs(record['blocked'], True)
            T.assertIs(payload['prompted'], False)
            T.assertIs(payload['blocked'], True)
            T.assertFalse(any((call[:2] == ('agent', 'prompt') for call in fake.calls)))
    finally:
        module.herdr = original


def composer_stall_herdr(*, rescued=True):
    """Model a stalled composer and its single Enter recovery."""
    fake = spawn_herdr()

    def prompt(*args):
        if "--wait" not in args:
            return {}
        raise _herdr_error("agent_prompt_stalled")

    fake.routes.update({
        ("agent", "prompt"): prompt,
        ("agent", "wait"): {} if rescued else _herdr_error("timeout"),
    })
    return fake


def _herdr_error(code: str, message: str | None = None) -> run_state.RunError:
    exc = run_state.RunError(message or f"herdr failed: {code}")
    exc.code = code
    return exc


def check_spawn_records_delivery_only_when_the_agent_leaves_idle() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            # the incident: the composer holds the prompt and Enter does not rescue it
            run, attempt = attempt_tree(Path(td) / "wedged")
            fake = composer_stall_herdr(rescued=False)
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            T.assertIs(payload['prompted'], False)
            T.assertIs(payload['blocked'], True)
            record = json.loads((attempt / "launch-complete.json").read_text(encoding="utf-8"))
            T.assertIs(record['prompted'], False)
            T.assertIs(record['blocked'], True)
            T.assertTrue((attempt / 'prompt-attempt.json').is_file())
            T.assertEqual(len([call for call in fake.calls if call[:2] == ('agent', 'prompt')]), 1)
            T.assertEqual(len([call for call in fake.calls if call[:2] == ('agent', 'send-keys')]), 1)

            # the recovery the operator had to do by hand: submit the pending composer
            run, attempt = attempt_tree(Path(td) / "rescued")
            fake = composer_stall_herdr()
            module.herdr = fake
            code, out, err = run_module(module, _spawn_argv(run))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            T.assertIs(payload['prompted'], True)
            T.assertIs(payload['blocked'], False)
            record = json.loads((attempt / "launch-complete.json").read_text(encoding="utf-8"))
            T.assertIs(record['prompted'], True)
            T.assertIs(record['blocked'], False)
            T.assertIn(('agent', 'send-keys', record['worker_id'], 'Enter'), fake.calls)
            T.assertTrue(any((call[:2] == ('agent', 'wait') for call in fake.calls)))
    finally:
        module.herdr = original








def init_primary_repo(path: Path) -> str:
    seed_repo(path)
    return git(path, "rev-parse", "HEAD")


def advance_primary(path: Path) -> str:
    return commit_file(path, "file.txt", "advanced\n", "advance")


def linked_pair(root: Path) -> tuple[Path, Path, str, str]:
    primary, linked = root / "primary", root / "linked"
    base = init_primary_repo(primary)
    make_worktree(primary, linked, "linked")
    advanced = advance_primary(primary)
    T.assertEqual(git(linked, 'rev-parse', 'HEAD'), base)
    T.assertNotEqual(base, advanced)
    return primary, linked, base, advanced


def _writer_nodes(node: str = "W", branch: str = "writers/w") -> dict:
    return {node: {
        "id": node, "executor": "herdr-agent", "worktree": {"branch": branch},
        "outputs": [{"name": "vcs", "type": "vcs"}],
    }}


def _node_argv(run: Path, node: str, *extra: str) -> list[str]:
    return ["spawn-agent.py", "worker", "--run", str(run), "--node", node, "--attempt", "1", *auth_args(), *extra]


def _require_primary(cwd: str, primary: Path) -> None:
    proc = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
        capture_output=True, text=True, check=True,
    )
    git_dir, common = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    T.assertEqual(Path(git_dir).resolve(), Path(common).resolve())
    T.assertEqual(Path(common).resolve(), (primary / '.git').resolve())
    T.assertEqual(Path(cwd).resolve(), primary.resolve())


def source_checked_herdr(primary, worker_root, *, fail_creates=0, steal=None):
    """Creation cwd must be primary; materialize the worker at --base."""
    fake = intent_resume_herdr()
    fake.created = []

    def create(*args):
        nonlocal fail_creates
        cwd = args[args.index("--cwd") + 1]
        base = args[args.index("--base") + 1]
        _require_primary(cwd, primary)
        if fail_creates:
            fail_creates -= 1
            raise RuntimeError("herdr worktree create failed: boom")
        branch = args[args.index("--branch") + 1] if "--branch" in args else "auto/branch"
        if steal:
            path, head = steal, base
        else:
            worker_root.mkdir(parents=True, exist_ok=True)
            dest = worker_root / f"w{len(fake.created)}"
            git(Path(cwd), "worktree", "add", "--detach", "-q", str(dest), base)
            fake.created.append(dest)
            path, head = str(dest.resolve()), git(dest, "rev-parse", "HEAD")
        return {"result": {
            "workspace": {"workspace_id": "ws"}, "root_pane": {"pane_id": "pane"},
            "worktree": {"worktree_path": path, "branch": branch, "base_sha": head},
        }}

    fake.routes[("worktree", "create")] = create
    return fake


def _assert_frozen_writer(run: Path, attempt: Path, payload: dict, identity: dict, *, accepted: str, primary: Path, base: str, advanced: str) -> None:
    worker = Path(identity["worktree_path"])
    T.assertEqual(payload['repo'], identity['repo'])
    T.assertEqual(identity['repo'], accepted)
    T.assertEqual(payload['base'], identity['base'])
    T.assertEqual(identity['base'], base)
    T.assertEqual(json.loads((run / 'graph.json').read_text(encoding='utf-8'))['base_sha'], base)
    intent = json.loads((attempt / "launch-intent.json").read_text(encoding="utf-8"))
    T.assertEqual(intent['base'], base)
    T.assertNotIn('cwd', intent)
    T.assertNotIn('source', intent)
    T.assertEqual(identity['tokens']['provenance'], 'orchestrator')
    T.assertEqual(git(worker, 'rev-parse', 'HEAD'), base)
    T.assertNotEqual(base, advanced)
    T.assertEqual(advanced, git(primary, 'rev-parse', 'HEAD'))
    T.assertNotIn(worker.resolve(), {primary.resolve(), Path(accepted).resolve()})


def check_spawn_primary_creation_source_unchanged() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary = root / "primary"
            base = init_primary_repo(primary)
            advanced = advance_primary(primary)
            accepted = str(primary.resolve())
            run = root / "run-abc"
            attempt = plant_run_files(run, node="W", base_sha=base, nodes=_writer_nodes(), repo=accepted)
            graph = (run / "graph.json").read_bytes()
            fake = source_checked_herdr(primary, root / "workers")
            module.herdr = fake
            code, out, err = run_module(module, _node_argv(run, "W"))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            create = next(call for call in fake.calls if call[:2] == ("worktree", "create"))
            T.assertEqual(create[create.index('--cwd') + 1], accepted)
            T.assertEqual(create[create.index('--base') + 1], base)
            T.assertNotEqual(base, advanced)
            T.assertEqual((run / 'graph.json').read_bytes(), graph)
            _assert_frozen_writer(run, attempt, payload, identity, accepted=accepted, primary=primary, base=base, advanced=advanced)
    finally:
        module.herdr = original


def check_spawn_linked_creation_uses_primary_frozen_base() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary, linked, base, advanced = linked_pair(root)
            accepted = str(linked.resolve())
            run = root / "run-abc"
            attempt = plant_run_files(run, node="W", base_sha=base, nodes=_writer_nodes(), repo=accepted)
            graph = (run / "graph.json").read_bytes()
            fake = source_checked_herdr(primary, root / "workers")
            module.herdr = fake
            code, out, err = run_module(module, _node_argv(run, "W"))
            T.assertEqual(code, 0, err)
            payload = json.loads(out)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            create = next(call for call in fake.calls if call[:2] == ("worktree", "create"))
            cwd = create[create.index("--cwd") + 1]
            T.assertEqual(Path(cwd).resolve(), primary.resolve())
            T.assertNotEqual(primary.resolve(), linked.resolve())
            T.assertEqual(create[create.index('--base') + 1], base)
            T.assertNotEqual(base, advanced)
            T.assertEqual((run / 'graph.json').read_bytes(), graph)
            T.assertEqual(git(linked, 'rev-parse', 'HEAD'), base)
            _assert_frozen_writer(run, attempt, payload, identity, accepted=accepted, primary=primary, base=base, advanced=advanced)
    finally:
        module.herdr = original


def check_spawn_readonly_accepted_linked_cwd_stays_linked() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary, linked, base, advanced = linked_pair(root)
            accepted = str(linked.resolve())
            for name, extra in (("explicit", ("--cwd", accepted)), ("omitted", ())):
                run = root / name / "run-abc"
                attempt = plant_run_files(run, base_sha=base, repo=accepted)
                fake = spawn_herdr()
                module.herdr = fake
                code, out, err = run_module(module, _spawn_argv(run, *extra))
                T.assertEqual(code, 0, err)
                payload = json.loads(out)
                identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
                T.assertFalse(any((call[:2] == ('worktree', 'create') for call in fake.calls)))
                create = next(call for call in fake.calls if call[:2] == ("workspace", "create"))
                T.assertEqual(create[create.index('--cwd') + 1], accepted)
                T.assertEqual(payload['repo'], identity['repo'])
                T.assertEqual(identity['repo'], accepted)
                T.assertEqual(git(primary, 'rev-parse', 'HEAD'), advanced)
                T.assertEqual(git(linked, 'rev-parse', 'HEAD'), base)
    finally:
        module.herdr = original


def check_spawn_linked_create_failure_resumes_one_create() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary, linked, base, advanced = linked_pair(root)
            accepted = str(linked.resolve())
            run = root / "run-abc"
            attempt = plant_run_files(run, node="W", base_sha=base, nodes=_writer_nodes(), repo=accepted)
            events = (run / "events.jsonl").read_bytes()
            graph = (run / "graph.json").read_bytes()
            fake = source_checked_herdr(primary, root / "workers", fail_creates=1)
            module.herdr = fake
            code, out, err = run_module(module, _node_argv(run, 'W'))
            T.assertEqual(code, 1, err)
            T.assertEqual(out, "")
            T.assertIn('boom', err)
            intent_path = attempt / "launch-intent.json"
            intent_bytes = intent_path.read_bytes()
            worker_id = json.loads(intent_bytes)["worker_id"]
            T.assertFalse((attempt / 'dispatch-identity.json').exists())
            T.assertEqual(fake.created, [])
            code, out, err = run_module(module, _node_argv(run, "W", "--resume"))
            T.assertEqual(code, 0, err)
            T.assertEqual(intent_path.read_bytes(), intent_bytes)
            T.assertEqual((run / 'events.jsonl').read_bytes(), events)
            T.assertEqual((run / 'graph.json').read_bytes(), graph)
            payload = json.loads(out)
            identity = json.loads((attempt / "dispatch-identity.json").read_text(encoding="utf-8"))
            T.assertEqual(payload['worker_id'], identity['worker_id'])
            T.assertEqual(identity['worker_id'], worker_id)
            creates = [call for call in fake.calls if call[:2] == ("worktree", "create")]
            T.assertEqual(len(creates), 2)
            T.assertEqual(len(fake.created), 1)
            for call in creates:
                T.assertEqual(Path(call[call.index('--cwd') + 1]).resolve(), primary.resolve())
                T.assertEqual(call[call.index('--base') + 1], base)
                T.assertNotEqual(base, advanced)
            T.assertEqual(git(linked, 'rev-parse', 'HEAD'), base)
            T.assertTrue(linked.is_dir())
            T.assertTrue(primary.is_dir())
            _assert_frozen_writer(run, attempt, payload, identity, accepted=accepted, primary=primary, base=base, advanced=advanced)
    finally:
        module.herdr = original


def check_spawn_refuses_protected_creation_sources() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            primary, linked, base, advanced = linked_pair(root)
            accepted = str(linked.resolve())
            for name, steal in (("accepted", accepted), ("source", str(primary.resolve()))):
                run = root / name / "run-abc"
                attempt = plant_run_files(run, node="W", base_sha=base, nodes=_writer_nodes(), repo=accepted)
                before = (git(primary, "rev-parse", "HEAD"), git(linked, "rev-parse", "HEAD"))
                fake = source_checked_herdr(primary, root / "workers" / name, steal=steal)
                module.herdr = fake
                code, out, err = run_module(module, _node_argv(run, 'W'))
                T.assertEqual(code, 1, err)
                T.assertEqual(out, "")
                T.assertIn("worktree", err)
                T.assertIn("accepted repository or its creation source", err)
                T.assertFalse((attempt / 'dispatch-identity.json').exists())
                T.assertTrue(any((call[:2] == ('worktree', 'create') for call in fake.calls)))
                T.assertFalse(any((call[:2] in (('agent', 'start'), ('worktree', 'remove'), ('workspace', 'close')) for call in fake.calls)))
                T.assertTrue(primary.is_dir())
                T.assertTrue(linked.is_dir())
                T.assertEqual((git(primary, 'rev-parse', 'HEAD'), git(linked, 'rev-parse', 'HEAD')), before)
                T.assertEqual(before, (advanced, base))
    finally:
        module.herdr = original


class _ScriptedGit:
    def __init__(self, steps: list[tuple[int, str, str]]):
        self.steps = list(steps)

    def __call__(self, argv, **kwargs):
        if not self.steps:
            raise AssertionError(argv)
        code, out, err = self.steps.pop(0)
        return subprocess.CompletedProcess(argv, code, out, err)


def check_spawn_creation_source_metadata_fail_closed() -> None:
    module = load_script("spawn-agent.py")
    original_herdr, original_run = module.herdr, ioutil.run_cmd
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            empty = root / "empty"
            empty.mkdir()
            run = root / "real" / "run-abc"
            attempt = plant_run_files(run, node="W", base_sha="deadbeef", nodes=_writer_nodes(), repo=str(empty.resolve()))
            fake = spawn_herdr()
            module.herdr = fake
            code, _, err = run_module(module, _node_argv(run, "W"))
            T.assertEqual(code, 3, err)
            T.assertEqual(fake.calls, [])
            T.assertTrue(empty.is_dir())
            T.assertFalse((attempt / 'dispatch-identity.json').exists())

            anchor, other, decoy = root / "anchor", root / "other", root / "decoy"
            for path in (anchor, other, decoy):
                path.mkdir()
            differ = f"{anchor.resolve()}\n{other.resolve()}\n"
            cases = [
                [(0, "relative\nrelative\n", "")],
                [(0, f"{anchor.resolve()}\n", "")],
                [(0, differ, ""), (0, "worktree relative/path\0\0", "")],
                [
                    (0, differ, ""),
                    (0, f"worktree {decoy.resolve()}\0HEAD abc\0\0", ""),
                    (0, f"{decoy.resolve()}\n{decoy.resolve()}\n", ""),
                ],
            ]
            for index, steps in enumerate(cases):
                case_run = root / f"bad-{index}" / "run-abc"
                plant_run_files(case_run, node="W", base_sha="deadbeef", nodes=_writer_nodes(), repo=str(anchor.resolve()))
                script = _ScriptedGit(steps)
                ioutil.run_cmd = script
                fake = spawn_herdr()
                module.herdr = fake
                code, _, err = run_module(module, _node_argv(case_run, "W"))
                T.assertEqual(code, 3, err)
                T.assertEqual(fake.calls, [])
                T.assertEqual(script.steps, [])
    finally:
        module.herdr = original_herdr
        ioutil.run_cmd = original_run


def check_prompt_single_delivery_uses_structured_errors() -> None:
    module = load_script("prompt-agent.py")
    original = module.herdr

    def delivery_herdr(failure, *, rescued=False):
        fake = pane_prompt_herdr(workspace_pane="w1:p1", agent_pane="w1:p6")
    
        def prompt(*args):
            if failure is not None:
                if hasattr(failure, "code"):
                    failure.args = (f"herdr {' '.join(args)} failed: {failure.code}",)
                raise failure
            return {}
    
        fake.routes.update({
            ("agent", "prompt"): prompt,
            ("agent", "wait"): {} if rescued else _herdr_error("timeout"),
        })
        return fake

    try:
        with tempfile.TemporaryDirectory() as td:
            run = Path(td) / "run-id"
            plant_run_files(run, node="P", nodes=pane_nodes())
            write_events(run, [
                {"type": "session_run_bound", **DEFAULT_HOLDER},
                {"type": "ready", "node": "P", "from": "planned", "to": "ready", "attempt": 1},
                dispatch_intent_event("P", 1),
                {"type": "dispatched", "node": "P", "attempt": 1,
                 "handle": {"name": "worker", "workspace": "ws-live", "pane": "w1:p6", "agent": "agent-live"},
                 "model": {"requested_role": "slow", "requested_effort": "low", "resolved": "provider/live",
                           "thinking": "high", "effort": "medium"}},
                {"type": "blocked", "node": "P", "from": "running", "to": "blocked", "attempt": 1,
                 "reason": "approval requested"},
            ])
            before = (run / "events.jsonl").read_bytes()
            for contextual in (False, True):
                context = ["--run", str(run), "--node", "P", "--attempt", "1", *auth_args()] if contextual else []
                for text in ("agent_blocked", "agent_prompt_stalled", "timeout"):
                    for error_code in ("agent_not_found", "agent_not_ready", "agent_blocked", "agent_prompt_stalled", "timeout", None):
                        failure = _herdr_error(error_code) if error_code else RuntimeError(f"uncoded {text}")
                        fake = delivery_herdr(failure)
                        module.herdr = fake
                        code, out, err = run_module(module, ['prompt-agent.py', 'worker', *context, '--prompt', text])
                        if error_code == "agent_blocked":
                            T.assertEqual(code, 4)
                            T.assertEqual(json.loads(out), {'name': 'worker', 'prompted': False, 'blocked': True})
                        else:
                            T.assertEqual(code, 1)
                            T.assertFalse(out)
                            T.assertTrue(err.strip())
                        T.assertEqual(len([c for c in fake.calls if c[:2] == ('agent', 'prompt')]), 1)
                        T.assertEqual(len([c for c in fake.calls if c[:2] == ('agent', 'send-keys')]), error_code in {'agent_prompt_stalled', 'timeout'})
                        T.assertEqual((run / 'events.jsonl').read_bytes(), before)
                for failure in (None, _herdr_error("agent_prompt_stalled")):
                    fake = delivery_herdr(failure, rescued=True)
                    module.herdr = fake
                    code, out, err = run_module(module, ["prompt-agent.py", "worker", *context, "--prompt", "work"])
                    T.assertEqual(code, 0, err)
                    payload = json.loads(out)
                    T.assertIs(payload['prompted'], True)
                    T.assertNotIn('retries', payload)
                    T.assertEqual('event' in payload, contextual)
                    if contextual:
                        T.assertEqual(payload['event']['type'], 'attempt_started')
                    T.assertEqual(len([c for c in fake.calls if c[:2] == ('agent', 'prompt')]), 1)
                    T.assertIn('--wait', next((c for c in fake.calls if c[:2] == ('agent', 'prompt'))))
    finally:
        module.herdr = original


def check_spawn_delivery_classifies_only_structured_errors() -> None:
    module = load_script("spawn-agent.py")
    original = module.herdr
    try:
        for text in ("agent_blocked", "agent_prompt_stalled", "timeout"):
            for error_code in ("agent_not_found", "agent_blocked", "agent_not_ready", None):
                failure = _herdr_error(error_code, text) if error_code else RuntimeError(text)
                fake = spawn_herdr(prompt_error=failure)
                module.herdr = fake
                code, out, err = run_module(module, ['spawn-agent.py', 'worker', '--prompt', text])
                if error_code in {"agent_not_found", None}:
                    T.assertEqual(code, 1, err)
                    T.assertEqual(out, "")
                    T.assertIn("workspace ws", err)
                    T.assertIn("pane pane", err)
                else:
                    T.assertEqual(code, 0, err)
                    payload = json.loads(out)
                    T.assertIs(payload["prompted"], False)
                    T.assertIs(payload["blocked"], True)
                T.assertEqual(len([c for c in fake.calls if c[:2] == ('agent', 'prompt')]), 1)
                T.assertFalse(any((c[:2] == ('workspace', 'close') for c in fake.calls)))
    finally:
        module.herdr = original


def check_spawn_first_busy_retry_sleeps_quarter_second() -> None:
    from unittest.mock import patch

    module = load_script("spawn-agent.py")
    fake = spawn_herdr()
    success = fake.routes[("agent", "start")]
    starts = 0

    def start(*args):
        nonlocal starts
        starts += 1
        if starts == 1:
            raise _herdr_error("agent_pane_busy")
        return success(*args) if callable(success) else success

    fake.routes[("agent", "start")] = start
    sleeps = []
    with patch.object(module, "herdr", fake), patch.object(module.time, "sleep", sleeps.append):
        code, out, err = run_module(module, [
            "spawn-agent.py", "worker", "--retries", "2", "--retry-delay", "2",
        ])
    T.assertEqual(code, 0, err)
    T.assertEqual(starts, 2)
    T.assertEqual(sleeps, [0.25])
    T.assertIs(json.loads(out)["blocked"], False)


class ExecutorDispatchTests(unittest.TestCase):
    """Holder for the module-level check_* functions; unittest does not collect bare functions."""


for _name, _fn in sorted(globals().items()):
    if _name.startswith("check_") and callable(_fn):
        setattr(ExecutorDispatchTests, "test_" + _name[len("check_"):], (lambda fn: lambda self: fn())(_fn))


if __name__ == "__main__":
    unittest.main()
