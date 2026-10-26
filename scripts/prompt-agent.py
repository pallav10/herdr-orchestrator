#!/usr/bin/env python3
"""Prompt a Herdr agent with confirmed single delivery.

Usage: prompt-agent.py NAME (--prompt TEXT | --prompt-file PATH)
                      [--orchestrate | --no-orchestrate]
                      [--run PATH --node ID --attempt N]
                      [--lock-timeout SECONDS]

Prints one JSON line:
  {"name","prompted":true} on success
  {"name","prompted":false,"blocked":true} when the agent is already at an
  approval/question dialog (`agent_blocked`); inspect before sending keys.
--run/--node/--attempt are all-or-none. Together they load the replayed
runtime node: a ready pane attempt after dispatch_intent, or continuation
of a blocked live herdr-agent. Exact recorded workspace/pane/agent and
run/node/attempt tokens are required; herdr-agent also needs
provenance=orchestrator. Pane may keep user provenance. Initial herdr-agent
launch and wrong state/attempt are refused before metadata or prompt
mutation. Shared prepare_dispatch_prompt is injected; pane stamps only
run/node/attempt (never provenance).
Initial pane dispatch persists launch-intent.json before metadata or prompt
mutation, freezing the accepted plan even if delivery fails.
Dispatched text goes through prepare_prompt; a leading bare `orchestrate`
line is on by default. Override with --orchestrate / --no-orchestrate.
Delivery is confirmed, not assumed: the prompt is submitted with
`--wait --until working`, a composer left holding it unsubmitted is submitted
with Enter, and an agent that never leaves idle is a failure, not `prompted`.
Exit codes: 0 ok, 1 runtime failure, 2 bad arguments, 3 precondition, 4 agent_blocked.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any


import artifacts
import authz
import graph_model
import ioutil
import cli_common

CLI_EXTRA_CODES = {RuntimeError: 1}
import ledger
from herdr_cli import deliver_prompt, herdr, runtime_agent_id, stamp_workspace_tokens
from prompt_contract import prepare_prompt
from graph_model import AGENT_NAME_RE
from cli_common import agent_pane_id, agent_workspace_id, run_context, token_value, validate_auth_args, workspace_pane_id
from liveness import recorded_herdr_identity
from routing import routing_for


def load_prompt(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    if bool(args.prompt) == bool(args.prompt_file):
        parser.error("exactly one of --prompt or --prompt-file is required")
    prompt = args.prompt
    if args.prompt_file:
        try:
            with open(args.prompt_file, encoding="utf-8") as fh:
                prompt = fh.read().strip()
        except OSError as exc:
            parser.error(f"cannot read --prompt-file: {exc}")
        if not prompt:
            parser.error(f"--prompt-file {args.prompt_file} is empty")
    if not prompt or not prompt.strip():
        parser.error("prompt is empty")
    return prompt.strip()


def continuation_event(node_id: str, attempt: str, node: dict[str, Any], live: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any]:
    handle = node.get("handle") if isinstance(node.get("handle"), dict) else {}
    tokens = live.get("tokens") if isinstance(live.get("tokens"), dict) else {}
    live_tokens = {key: tokens[key] for key in ("provenance", "run", "node", "attempt") if key in tokens}
    recorded = node.get("model") if isinstance(node.get("model"), dict) else {}
    observed = graph_model.validate_continuation_routing(recorded, routing)
    live_identity_payload = {
        "workspace": live["workspace"],
        "pane": live["pane"],
        "agent": live.get("agent"),
        "tokens": live_tokens,
        "routing": observed,
    }
    if live.get("identity_kind"):
        live_identity_payload["identity_kind"] = live["identity_kind"]
    if live.get("worker_id"):
        live_identity_payload["worker_id"] = live["worker_id"]
    return {
        "type": "attempt_started",
        "node": node_id,
        "attempt": int(attempt),
        "from": "blocked",
        "to": "running",
        "handle": handle,
        "model": graph_model.validate_model(recorded),
        "live_identity": live_identity_payload,
    }


def match_recorded_identity(
    recorded: dict[str, Any],
    live: dict[str, Any],
    parser: argparse.ArgumentParser,
    *,
    required: bool,
) -> None:
    for key in ("workspace", "pane", "agent"):
        stored = recorded.get(key)
        if stored is None:
            if required:
                parser.error(f"recorded {key} missing")
            continue
        if stored != live[key]:
            parser.error(f"graph handle {key} does not match live identity")
    tokens = recorded.get("tokens") if isinstance(recorded.get("tokens"), dict) else {}
    live_tokens = live.get("tokens") if isinstance(live.get("tokens"), dict) else {}
    for key in ("run", "node", "attempt"):
        expected = tokens.get(key)
        if expected is None:
            if required:
                parser.error(f"recorded {key} token missing")
            continue
        if token_value(str(expected)) != token_value(str(live_tokens.get(key, ""))):
            parser.error(f"recorded {key} token does not match live identity")


def load_contextual_node(
    run: Path, node_id: str, attempt: str, name: str, parser: argparse.ArgumentParser
) -> tuple[dict, dict, str]:
    try:
        loaded = ledger.load_run(run)
    except (ioutil.UsageError, ioutil.PreconditionError) as exc:
        parser.error(str(exc))
    try:
        graph = ledger.load_accepted_graph(run)
    except (ioutil.UsageError, ioutil.PreconditionError, ioutil.RunError) as exc:
        if not (run / "graph.json").is_file():
            parser.error(f"accepted graph missing: {run / 'graph.json'}")
        parser.error(f"cannot read accepted graph: {exc}")
    try:
        graph_model.validate_graph(graph)
    except ioutil.PreconditionError as exc:
        parser.error(f"graph is not accepted: {exc}")
    if graph.get("run") != loaded.get("path"):
        parser.error("graph.run mismatch")
    if graph.get("base_sha") != loaded.get("base_sha"):
        parser.error("graph.base_sha mismatch")
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict) or not isinstance(nodes.get(node_id), dict):
        parser.error(f"graph missing node {node_id}")
    run_id = loaded.get("id")
    if not isinstance(run_id, str) or not run_id:
        parser.error("run.json missing id")
    try:
        snap = ledger.replay(run)
    except (ioutil.UsageError, ioutil.PreconditionError, ioutil.RunError) as exc:
        parser.error(str(exc))
    state = snap.get("state") if isinstance(snap.get("state"), dict) else {}
    runtime = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    if not isinstance(runtime.get(node_id), dict):
        parser.error(f"graph missing node {node_id}")
    node = runtime[node_id]
    executor = node.get("executor")
    node_state = node.get("state")
    try:
        current_attempt = int(node.get("attempt", 0))
    except (TypeError, ValueError):
        current_attempt = -1
    if current_attempt != int(attempt):
        parser.error(f"attempt {attempt} does not match current attempt {node.get('attempt')}")
    intent = node.get("dispatch_intent") if isinstance(node.get("dispatch_intent"), dict) else {}
    if executor == "pane" and node_state == "ready" and int(intent.get("attempt", -1)) == int(attempt):
        handle = node.get("handle") if isinstance(node.get("handle"), dict) else {}
        stored = handle.get("name")
        if not isinstance(stored, str) or not stored:
            parser.error(f"pane node {node_id} missing a stored handle name")
        if stored != name:
            parser.error(f"pane node {node_id} handle name {stored!r} does not match {name!r}")
        return loaded, node, "pane"
    if node_state == "blocked" and executor in {"pane", "herdr-agent"}:
        handle = node.get("handle") if isinstance(node.get("handle"), dict) else {}
        if graph_model.parse_herdr_identity(handle, executor, require_context=False) is None:
            parser.error(f"node {node_id} blocked continuation requires a recorded handle")
        reason = node.get("reason")
        if isinstance(reason, str) and reason.startswith("dep "):
            parser.error(f"node {node_id} is dependency-blocked")
        if executor == "pane":
            stored = handle.get("name")
            if isinstance(stored, str) and stored and stored != name:
                parser.error(f"pane node {node_id} handle name {stored!r} does not match {name!r}")
        return loaded, node, executor
    if executor == "herdr-agent":
        parser.error("herdr-agent initial launch requires spawn-agent")
    parser.error(f"node {node_id} is not a ready pane attempt or blocked herdr-agent/pane")


def live_herdr_raw(target: str, identity_kind: str) -> dict[str, Any]:
    payload = herdr("agent", "get", target)
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    agent = result.get("agent") if isinstance(result.get("agent"), dict) else result
    if not isinstance(agent, dict) or not agent:
        raise RuntimeError(f"live agent missing for {target}")
    workspace = agent_workspace_id(agent)
    if not workspace:
        raise ioutil.PreconditionError(f"live workspace missing for {target}")
    fetched = herdr("workspace", "get", workspace)
    ws_result = fetched.get("result") if isinstance(fetched.get("result"), dict) else {}
    workspace_obj = ws_result.get("workspace") if isinstance(ws_result.get("workspace"), dict) else {}
    if not isinstance(workspace_obj, dict) or not workspace_obj:
        raise RuntimeError(f"live workspace missing for {target}")
    agent_pane = agent_pane_id(agent)
    ws_pane = workspace_pane_id(workspace_obj)
    pane = agent_pane or ws_pane
    if not pane:
        raise ioutil.PreconditionError(f"live pane missing for {target}")
    tokens = workspace_obj.get("tokens")
    tokens = dict(tokens) if isinstance(tokens, dict) else {}
    identity: dict[str, Any] = {
        "identity_kind": identity_kind,
        "workspace": workspace,
        "pane": pane,
        "tokens": tokens,
        "payload": payload,
        "agent": None,
    }
    if identity_kind == graph_model.IDENTITY_MANAGED_NAME:
        name = agent.get("name")
        if not isinstance(name, str) or not name:
            raise ioutil.PreconditionError(f"live agent missing name for {target}")
        identity["worker_id"] = name
    else:
        identity["agent"] = runtime_agent_id(agent, target)
    return identity


def _verify_herdr_identity(run, node_id, attempt, node, loaded, parser):
    """Match the recorded managed identity against the live herdr agent."""
    recorded_raw = recorded_herdr_identity({"run": str(run)}, node_id, node, strict=True)
    parsed = graph_model.parse_herdr_identity(recorded_raw, "herdr-agent", require_context=True)
    if parsed is None:
        parser.error(f"node {node_id} recorded identity is invalid or incomplete")
    recorded_tokens = recorded_raw.get("tokens") if isinstance(recorded_raw.get("tokens"), dict) else {}
    if token_value(str(recorded_tokens.get("run", ""))) != token_value(loaded["id"]):
        parser.error("recorded run token does not match attempt")
    if token_value(str(recorded_tokens.get("node", ""))) != token_value(node_id):
        parser.error("recorded node token does not match attempt")
    if token_value(str(recorded_tokens.get("attempt", ""))) != token_value(attempt):
        parser.error("recorded attempt token does not match attempt")
    target = graph_model.herdr_command_target(recorded_raw, "herdr-agent")
    if not target:
        parser.error(f"node {node_id} recorded identity has no resolvable command target")
    identity = live_herdr_raw(target, parsed["identity_kind"])
    live_raw = {
        "identity_kind": identity["identity_kind"],
        "workspace": identity["workspace"],
        "pane": identity["pane"],
        "tokens": identity["tokens"],
    }
    if identity.get("worker_id"):
        live_raw["worker_id"] = identity["worker_id"]
    if identity.get("agent"):
        live_raw["agent"] = identity["agent"]
    if not graph_model.herdr_identity_matches(recorded_raw, live_raw, "herdr-agent"):
        parser.error(f"node {node_id} live identity does not match recorded identity")
    return target, identity


def _verify_pane_identity(name, run, node_id, node, parser):
    """Legacy pane/agent identity: the live handle must match the recorded one."""
    stored = (node.get("handle") or {}).get("name")
    if isinstance(stored, str) and stored and stored != name:
        parser.error("pane handle name does not match the current accepted plan")
    identity = live_herdr_raw(name, graph_model.IDENTITY_RUNTIME_ID)
    recorded = recorded_herdr_identity({"run": str(run)}, node_id, node, strict=True)
    match_recorded_identity(recorded, identity, parser, required=recorded["identity_file"])
    return name, identity


def _print_payload(args, identity, event, blocked) -> int:
    if blocked:
        payload = {"name": args.name, "prompted": False, "blocked": True}
        agent = {}
        if isinstance(identity, dict):
            raw = identity.get("payload")
            result = raw.get("result") if isinstance(raw, dict) and isinstance(raw.get("result"), dict) else raw
            if isinstance(result, dict):
                agent = result.get("agent") if isinstance(result.get("agent"), dict) else result
        if isinstance(agent, dict):
            for key in ("reason", "question"):
                if agent.get(key) is not None:
                    payload[key] = agent[key]
        print(json.dumps(payload))
        return 4
    if identity is None:
        print(json.dumps({"name": args.name, "prompted": True}))
        return 0
    payload = {
        "name": args.name,
        "workspace": identity["workspace"],
        "pane": identity["pane"],
        "agent": identity.get("agent"),
        "routing": identity["routing"],
        "tokens": identity["tokens"],
        "prompted": True,
        "run": identity["run"],
        "node": identity["node"],
        "attempt": identity["attempt"],
    }
    if identity.get("worker_id"):
        payload["worker_id"] = identity["worker_id"]
        payload["identity_kind"] = graph_model.IDENTITY_MANAGED_NAME
    if event is not None:
        payload["event"] = event
    print(json.dumps(payload))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--run", default=None)
    parser.add_argument("--node", default=None)
    parser.add_argument("--attempt", type=int, default=None)
    parser.add_argument("--session", default=None)
    parser.add_argument("--holder-workspace", default=None)
    parser.add_argument("--holder-pane", default=None)
    parser.add_argument("--epoch", type=int, default=None)
    orch = parser.add_mutually_exclusive_group()
    orch.add_argument("--orchestrate", action="store_true", dest="orchestrate")
    orch.add_argument("--no-orchestrate", action="store_false", dest="orchestrate")
    parser.set_defaults(orchestrate=True)
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    if not AGENT_NAME_RE.match(args.name):
        parser.error(f"name {args.name!r} must match {AGENT_NAME_RE.pattern}")

    context = run_context(args, parser)
    holder, epoch = validate_auth_args(args, parser, context)
    prompt = load_prompt(parser, args)
    identity = None
    event = None
    blocked = False
    continuation = False
    if context is not None:
        run, node_id, attempt = context
        loaded, node, kind = load_contextual_node(run, node_id, attempt, args.name, parser)
        continuation = node.get("state") == "blocked"
        try:
            prompt = artifacts.prepare_dispatch_prompt(run, node_id, attempt, prompt or "")
        except (ioutil.UsageError, ioutil.PreconditionError) as exc:
            parser.error(str(exc))
        prompt = prepare_prompt(prompt, orchestrate=args.orchestrate)
        try:
            with authz.dispatch_guard(
                run, node_id, int(attempt), holder=holder, epoch=epoch, continuation=continuation,
                lock_timeout=args.lock_timeout,
            ) as guarded:
                node = guarded["state"]["nodes"][node_id]
                if node.get("executor") != kind:
                    raise ioutil.PreconditionError("executor changed before dispatch; reload the accepted plan")
                if kind == "herdr-agent":
                    target, identity = _verify_herdr_identity(run, node_id, attempt, node, loaded, parser)
                else:
                    target, identity = _verify_pane_identity(args.name, run, node_id, node, parser)
                tokens = dict(identity["tokens"])
                tokens["run"] = token_value(loaded["id"])
                tokens["node"] = token_value(node_id)
                tokens["attempt"] = token_value(attempt)
                model = node.get("model") if isinstance(node.get("model"), dict) else {}
                identity.update({
                    "run": str(run),
                    "node": node_id,
                    "attempt": attempt,
                    "run_id": loaded["id"],
                    "routing": routing_for(
                        [identity["payload"]],
                        requested_model=model.get("requested_model"),
                        requested_role=model.get("requested_role"),
                        requested_effort=model.get("requested_effort"),
                    ),
                    "tokens": tokens,
                })
                candidate = None
                if continuation:
                    candidate = continuation_event(node_id, attempt, node, identity, identity["routing"])
                    identity["routing"] = candidate["live_identity"]["routing"]
                if kind == "pane" and not continuation:
                    intent_path = ledger.attempt_dir(run, node_id, int(attempt)) / "launch-intent.json"
                    if not intent_path.exists():
                        try:
                            ioutil.atomic_json(intent_path, {
                                "executor": "pane", "run": str(run), "node": node_id,
                                "attempt": int(attempt), "name": target,
                                "workspace": identity["workspace"], "pane": identity["pane"],
                                "agent": identity["agent"],
                            })
                        except OSError as exc:
                            raise ioutil.RunError(f"cannot persist pane launch intent {intent_path}: {exc}") from exc
                stamp_workspace_tokens(
                    identity["workspace"], identity["run_id"], identity["node"], identity["attempt"],
                    invoke=herdr,
                )
                try:
                    deliver_prompt(herdr, target, prompt)
                except RuntimeError as exc:
                    if getattr(exc, "code", None) != "agent_blocked":
                        raise
                    blocked = True
                if continuation and not blocked:
                    event = candidate
        except ioutil.UsageError as exc:
            parser.error(str(exc))
    else:
        prompt = prepare_prompt(prompt, orchestrate=args.orchestrate)
        try:
            deliver_prompt(herdr, args.name, prompt)
        except RuntimeError as exc:
            if getattr(exc, "code", None) != "agent_blocked":
                raise
            blocked = True
    return _print_payload(args, identity, event, blocked)


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
