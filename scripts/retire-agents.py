#!/usr/bin/env python3
"""Retire orchestrator-owned agents by closing their workspaces.

Usage: retire-agents.py NAME [NAME ...] [--collect] [--force-unsettled]
           [--run PATH --session S --holder-workspace W --holder-pane P] [--lock-timeout SECONDS]

With --run, closing a workspace requires the run's active lease and the exact holder triple.

Prints one JSON line:
  {"retired":[{"name","workspace","summary"?}],"refused":[{"name","reason"}]}
Exit codes: 0 all requested agents retired, 1 runtime failure,
            2 bad arguments, 3 one or more refused.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from typing import Any


import graph_model
import ioutil
import cli_common

CLI_EXTRA_CODES = {RuntimeError: 1}
import ledger
import receipts
from herdr_cli import herdr
from graph_model import AGENT_NAME_RE, ATTEMPT_TERMINAL, PROVENANCE_ORCHESTRATOR, SETTLED
from cli_common import SCRIPTS, holder_from_args, run_child, ChildFailed
from ioutil import first_str, load_json


def extract_collect_summary(payload: dict[str, Any], name: str) -> str:
    agents = payload.get("agents")
    if not isinstance(agents, list):
        raise RuntimeError(f"collect-agent.py response missing agents for {name}")
    for item in agents:
        if item.get("name") == name:
            return item.get("summary", "")
    raise RuntimeError(f"collect-agent.py response did not include {name}")





def attempt_directory(run_dir: Path, node_id: str, attempt: int) -> Path | None:
    try:
        return Path(ledger.attempt_dir(run_dir, node_id, attempt))
    except (OSError, ioutil.UsageError, ioutil.PreconditionError, ioutil.RunError):
        return None


def outputs_of_type(plan: dict[str, Any], output_type: str) -> list[str]:
    names: list[str] = []
    for out in plan.get("outputs", []) or []:
        if isinstance(out, dict) and out.get("type") == output_type and isinstance(out.get("name"), str) and out["name"]:
            names.append(out["name"])
    return names


def has_vcs_surface(plan: dict[str, Any]) -> bool:
    return bool(plan.get("worktree")) or graph_model.declares_output(plan.get("outputs") or [], "vcs", require_name=True)




def integration_consumers(graph: dict[str, Any], producer: str) -> list[str]:
    nodes = graph.get("nodes", {}) if isinstance(graph.get("nodes"), dict) else {}
    vcs_names = set(outputs_of_type(nodes.get(producer, {}), "vcs"))
    out: list[str] = []
    for node_id, plan in nodes.items():
        inputs = plan.get("inputs", []) or [] if isinstance(plan, dict) else []
        if any(
            isinstance(inp, dict) and inp.get("from") == producer and inp.get("output") in vcs_names
            for inp in inputs
        ):
            out.append(node_id)
    return out


def consumer_vcs_names(graph: dict[str, Any], consumer: str, producer: str) -> list[str]:
    nodes = graph.get("nodes", {}) if isinstance(graph.get("nodes"), dict) else {}
    vcs_names = set(outputs_of_type(nodes.get(producer, {}), "vcs"))
    names: list[str] = []
    for inp in nodes.get(consumer, {}).get("inputs", []) or []:
        if isinstance(inp, dict) and inp.get("from") == producer and inp.get("output") in vcs_names:
            names.append(inp["output"])
    return names


def parse_positive_attempt(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        attempt = value
        text = str(value)
    elif isinstance(value, str) and value.isdecimal():
        attempt = int(value)
        text = value
    else:
        return None
    if attempt < 1 or text != str(attempt):
        return None
    return attempt


def exact_attempt_for_tokens(tokens: dict[str, str], run_id: str, graph: dict[str, Any]) -> tuple[str, int] | None:
    if tokens.get("run") != run_id:
        return None
    nodes = graph.get("nodes", {})
    node_id = tokens.get("node")
    if not isinstance(nodes, dict) or not isinstance(node_id, str) or node_id not in nodes:
        return None
    attempt = parse_positive_attempt(tokens.get("attempt"))
    if attempt is None:
        return None
    return node_id, attempt


def has_contextual_tokens(tokens: dict[str, str]) -> bool:
    return any(tokens.get(key) not in (None, "") for key in ("run", "node", "attempt"))


def pane_never_retired(tokens: dict[str, str], run_id: str, graph: dict[str, Any]) -> bool:
    exact = exact_attempt_for_tokens(tokens, run_id, graph)
    if exact is None:
        return False
    plan = graph.get("nodes", {}).get(exact[0], {})
    return isinstance(plan, dict) and plan.get("executor") == "pane"


def executor_for_node(graph: dict[str, Any], node_id: str) -> str | None:
    nodes = graph.get("nodes", {}) if isinstance(graph.get("nodes"), dict) else {}
    plan = nodes.get(node_id)
    executor = plan.get("executor") if isinstance(plan, dict) else None
    return executor if isinstance(executor, str) and executor else None


def _identity_quad(raw: dict[str, Any], executor: str | None) -> tuple | None:
    parsed = graph_model.parse_herdr_identity(raw, executor, require_context=False)
    if parsed is None:
        return None
    return (parsed["identity_kind"], parsed["target"], parsed["workspace"], parsed["pane"])


def recorded_attempt_handle(events: list[dict[str, Any]], run_dir: Path, node_id: str, attempt: int, executor: str | None) -> dict[str, Any] | None:
    handle = None
    handle_quad = None
    for event in ledger.events_for_attempt(events, node_id, attempt, attempt_parser=parse_positive_attempt):
        if event.get("type") not in {"dispatched", "attempt_started"}:
            continue
        raw = event.get("handle") if isinstance(event.get("handle"), dict) else {}
        quad = _identity_quad(raw, executor)
        if quad is not None:
            handle, handle_quad = raw, quad
    identity = None
    identity_quad = None
    directory = attempt_directory(run_dir, node_id, attempt)
    if directory is not None:
        path = directory / "dispatch-identity.json"
        if path.is_file():
            try:
                data = load_json(path)
            except RuntimeError:
                return None
            if isinstance(data, dict):
                identity_quad = _identity_quad(data, executor)
                if identity_quad is None:
                    return None
                identity = data
    if handle_quad is not None and identity_quad is not None and handle_quad != identity_quad:
        return None
    return handle if handle is not None else identity


def retirement_indices(events: list[dict[str, Any]], run_dir: Path, graph: dict[str, Any], state: dict[str, Any]) -> tuple[dict, dict, set, dict]:
    attempt_events: dict[tuple[str, int], list[dict[str, Any]]] = {}
    terminal: set[tuple[str, int]] = set()
    for event in events:
        node_id = event.get("node")
        attempt = parse_positive_attempt(event.get("attempt"))
        if not isinstance(node_id, str) or attempt is None:
            continue
        key = (node_id, attempt)
        attempt_events.setdefault(key, []).append(event)
        if event.get("type") in ATTEMPT_TERMINAL:
            terminal.add(key)
    nodes = state.get("nodes", {}) if isinstance(state.get("nodes"), dict) else {}
    current = {
        (node_id, attempt)
        for node_id, node in nodes.items()
        if isinstance(node_id, str) and isinstance(node, dict)
        and (attempt := parse_positive_attempt(node.get("attempt"))) is not None
    }
    handles = {}
    claims: dict[tuple, set[tuple[str, int]]] = {}
    for node_id, attempt in attempt_events.keys() | current:
        key = (node_id, attempt)
        executor = executor_for_node(graph, node_id)
        handle = recorded_attempt_handle(attempt_events.get(key, []), run_dir, node_id, attempt, executor)
        handles[key] = handle
        if handle is not None and key in current and key not in terminal:
            quad = _identity_quad(handle, executor)
            claims.setdefault(quad, set()).add(key)
    return attempt_events, handles, terminal, claims


def live_identity_record(live: dict[str, Any], identity_kind: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "workspace": live.get("workspace"),
        "pane": live.get("pane"),
        "tokens": live.get("tokens"),
        "identity_kind": identity_kind,
    }
    if identity_kind == graph_model.IDENTITY_MANAGED_NAME:
        if live.get("live_name"):
            record["worker_id"] = live["live_name"]
    else:
        if live.get("agent"):
            record["agent"] = live["agent"]
    return record


def live_matches_recorded(
    live: dict[str, Any], tokens: dict[str, str], run_id: str, graph: dict[str, Any], handles: dict, claims: dict
) -> bool:
    exact = exact_attempt_for_tokens(tokens, run_id, graph)
    if exact is None:
        return False
    node_id, attempt = exact
    executor = executor_for_node(graph, node_id)
    recorded = handles.get((node_id, attempt))
    if recorded is None:
        return False
    recorded_parsed = graph_model.parse_herdr_identity(recorded, executor, require_context=False)
    if recorded_parsed is None:
        return False
    live_raw = live_identity_record(live, recorded_parsed["identity_kind"])
    if not graph_model.herdr_identity_matches(recorded, live_raw, executor):
        return False
    live_quad = _identity_quad(live_raw, executor)
    return live_quad is not None and not (claims.get(live_quad, set()) - {(node_id, attempt)})


def attempt_vcs_hash(run_dir: Path, node_id: str, attempt: int) -> str | None:
    directory = attempt_directory(run_dir, node_id, attempt)
    if directory is None:
        return None
    path = directory / "vcs.json"
    try:
        payload = load_json(path)
        if not isinstance(payload, dict):
            return None
        return ioutil.sha256_file(path)
    except (OSError, RuntimeError):
        return None


def artifactless_interrupted_attempt(events: dict, run_dir: Path, node_id: str, attempt: int) -> bool:
    directory = attempt_directory(run_dir, node_id, attempt)
    if directory is not None and (directory / "vcs.json").exists():
        return False
    return any(event.get("type") == "interrupted" for event in events.get((node_id, attempt), []))


def promoted_new(events: dict, node_id: str, attempt: int) -> str | None:
    new = None
    for event in events.get((node_id, attempt), []):
        if event.get("type") == "promoted":
            value = event.get("new")
            new = value if isinstance(value, str) and value else None
    return new


def integration_payload_ok(path: Path, consumed: list[dict[str, Any]], *, expected_sha: str | None, expected_base: str | None) -> bool:
    try:
        receipts.validate_integration_payload(path, consumed, expected_sha=expected_sha, expected_base=expected_base)
    except (OSError, ioutil.UsageError, ioutil.PreconditionError, ioutil.RunError):
        return False
    return True


def integrator_terminal_ok(run_dir: Path, graph: dict[str, Any], state: dict[str, Any], events: dict, run_info: dict[str, Any], node_id: str, attempt: int) -> bool:
    node = state.get("nodes", {}).get(node_id, {})
    if node.get("state") != "succeeded" or parse_positive_attempt(node.get("attempt")) != attempt:
        return False
    promoted = node.get("promoted")
    if not isinstance(promoted, dict) or parse_positive_attempt(promoted.get("attempt")) != attempt:
        return False
    try:
        receipts.validate_promoted_receipt(run_dir, events.get((node_id, attempt), []), node_id, attempt)
    except (OSError, ioutil.UsageError, ioutil.PreconditionError, ioutil.RunError):
        return False
    directory = attempt_directory(run_dir, node_id, attempt)
    if directory is None:
        return False
    path = directory / "integration.json"
    try:
        payload = load_json(path)
    except RuntimeError:
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("consumed"), list):
        return False
    if run_info.get("base_sha") is None:
        return False
    expected = promoted_new(events, node_id, attempt)
    if expected is None:
        return False
    try:
        consumed = ledger.vcs_typed_consumed(graph, ledger.read_consumed_snapshot(run_dir, node_id, attempt))
    except (OSError, ioutil.UsageError, ioutil.PreconditionError, ioutil.RunError):
        return False
    return integration_payload_ok(path, consumed, expected_sha=expected, expected_base=run_info.get("base_sha"))


def consumed_digest_ok(payload: dict[str, Any], producer: str, producer_attempt: int, digest: str, names: list[str]) -> bool:
    consumed = payload.get("consumed")
    if not isinstance(consumed, list) or not names:
        return False
    for name in names:
        if not any(
            isinstance(item, dict)
            and item.get("producer") == producer
            and parse_positive_attempt(item.get("producer_attempt", item.get("attempt"))) == producer_attempt
            and item.get("output") == name
            and item.get("hash") == digest
            for item in consumed
        ):
            return False
    return True


def integration_receipt_ok(
    run_dir: Path,
    graph: dict[str, Any],
    state: dict[str, Any],
    events: dict,
    run_info: dict[str, Any],
    consumer: str,
    producer: str,
    producer_attempt: int,
    digest: str,
) -> bool:
    node = state.get("nodes", {}).get(consumer, {})
    consumer_attempt = parse_positive_attempt(node.get("attempt"))
    if consumer_attempt is None or not integrator_terminal_ok(run_dir, graph, state, events, run_info, consumer, consumer_attempt):
        return False
    directory = attempt_directory(run_dir, consumer, consumer_attempt)
    if directory is None:
        return False
    try:
        payload = load_json(directory / "integration.json")
    except RuntimeError:
        return False
    if not isinstance(payload, dict):
        return False
    return consumed_digest_ok(payload, producer, producer_attempt, digest, consumer_vcs_names(graph, consumer, producer))


def nonintegration_consumption_ok(
    run_dir: Path,
    graph: dict[str, Any],
    state: dict[str, Any],
    events: dict,
    consumer: str,
    producer: str,
    producer_attempt: int,
    digest: str,
) -> bool:
    try:
        names = consumer_vcs_names(graph, consumer, producer)
        if not names or parse_positive_attempt(producer_attempt) != producer_attempt:
            return False
        nodes = state.get("nodes", {}) if isinstance(state, dict) and isinstance(state.get("nodes"), dict) else {}
        node = nodes.get(consumer)
        if not isinstance(node, dict) or node.get("state") != "succeeded":
            return False
        attempt = parse_positive_attempt(node.get("attempt"))
        if attempt is None or not any(
            event.get("type") == "succeeded"
            for event in events.get((consumer, attempt), [])
        ):
            return False
        snapshot = ledger.read_consumed_snapshot(run_dir, consumer, attempt)
        replayed = node.get("consumed_artifacts")
        if not isinstance(replayed, list) or snapshot != replayed:
            return False
        producer_node = nodes.get(producer)
        on_target = (
            isinstance(producer_node, dict)
            and producer_node.get("state") == "succeeded"
            and parse_positive_attempt(producer_node.get("attempt")) == producer_attempt
        )
        for name in names:
            rows = [
                item
                for item in ledger.vcs_typed_consumed(graph, snapshot)
                if item.get("producer") == producer and item.get("output") == name
            ]
            if not rows:
                return False
            for item in rows:
                if parse_positive_attempt(item.get("producer_attempt")) != producer_attempt or item.get("hash") != digest:
                    return False
                path = item.get("path")
                if not isinstance(path, str) or not path:
                    return False
                actual = ioutil.sha256_file(path)
                if actual != item.get("hash") or actual != digest:
                    return False
            art = graph_model.current_artifact(state, producer, name)
            if art is None:
                if on_target:
                    return False
            elif art.get("hash") != digest:
                return False
        return True
    except (OSError, ioutil.RunError, TypeError, ValueError, AttributeError):
        return False


def missing_integration_receipt(tokens: dict[str, str], run_info: dict[str, Any], run_dir: Path, graph: dict[str, Any], state: dict[str, Any], events: dict) -> bool:
    exact = exact_attempt_for_tokens(tokens, run_info.get("id", run_dir.name), graph)
    if exact is None:
        return True
    node_id, attempt = exact
    plan = graph.get("nodes", {}).get(node_id, {})
    if graph_model.declares_output(plan.get("outputs") or [], "integration", require_name=True):
        return not integrator_terminal_ok(run_dir, graph, state, events, run_info, node_id, attempt)
    if artifactless_interrupted_attempt(events, run_dir, node_id, attempt):
        return False
    if not has_vcs_surface(plan):
        return False
    consumers = integration_consumers(graph, node_id)
    if not consumers:
        return False
    digest = attempt_vcs_hash(run_dir, node_id, attempt)
    if not digest:
        return True
    nodes = graph.get("nodes", {}) if isinstance(graph.get("nodes"), dict) else {}
    for consumer in consumers:
        consumer_plan = nodes.get(consumer, {})
        if graph_model.declares_output(consumer_plan.get("outputs") or [] if isinstance(consumer_plan, dict) else [], "integration", require_name=True):
            ok = integration_receipt_ok(run_dir, graph, state, events, run_info, consumer, node_id, attempt, digest)
        else:
            ok = nonintegration_consumption_ok(run_dir, graph, state, events, consumer, node_id, attempt, digest)
        if not ok:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="+")
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--force-unsettled", action="store_true")
    parser.add_argument("--run", default=None)
    parser.add_argument("--session", default=None)
    parser.add_argument("--holder-workspace", default=None)
    parser.add_argument("--holder-pane", default=None)
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    holder = holder_from_args(
        args, parser, context="--run" if args.run else None,
        required_message="--session, --holder-workspace, and --holder-pane are required with --run",
        nonempty=True,
    )

    for name in args.names:
        if not AGENT_NAME_RE.match(name):
            parser.error(f"name {name!r} must match {AGENT_NAME_RE.pattern}")

    collect_script = str(SCRIPTS / "collect-agent.py")
    if args.collect and not Path(collect_script).is_file():
        raise RuntimeError(f"cannot read collect-agent.py: {collect_script}")

    refused: list[dict[str, str]] = []
    candidates: list[dict[str, object]] = []
    run_dir = graph = run_snapshot = run_events = run_info = None
    if args.run:
        run_info = ledger.load_run(args.run)
        run_dir = Path(run_info["dir"])
        lock = ledger.with_lock(run_dir, lock_timeout=args.lock_timeout)
    else:
        lock = contextlib.nullcontext()

    with lock:
        if args.run:
            graph = ledger.load_graph(run_dir)
            if not isinstance(graph, dict):
                raise RuntimeError("graph.json must be an object")
            replay = ledger.replay(run_dir)
            # closing a workspace is irreversible: only the active lease holder may do it
            graph_model.require_active_holder(replay["controller"], holder, "retire-agents workspace close")
            run_snapshot = replay["state"]
            run_events = replay["events"]
            run_events, run_handles, run_terminal, run_claims = retirement_indices(run_events, run_dir, graph, run_snapshot)
        for name in args.names:
            try:
                agent = herdr("agent", "get", name)["result"]["agent"]
            except RuntimeError as exc:
                if getattr(exc, "code", None) == "agent_not_found":
                    refused.append({"name": name, "reason": "agent_not_found"})
                    continue
                raise

            agent_state = agent.get("agent_status", "unknown")
            workspace = agent.get("workspace_id")
            if not workspace:
                refused.append({"name": name, "reason": "missing_workspace"})
                continue

            workspace_obj = herdr("workspace", "get", workspace)["result"]["workspace"]
            tokens = workspace_obj.get("tokens") or {}
            if tokens.get("provenance") != PROVENANCE_ORCHESTRATOR:
                refused.append({"name": name, "reason": "not_orchestrator_owned"})
                continue

            if has_contextual_tokens(tokens) and not args.run:
                refused.append({"name": name, "reason": "run_required"})
                continue
            if args.run and pane_never_retired(tokens, run_info.get("id", run_dir.name), graph):
                refused.append({"name": name, "reason": "pane_executor"})
                continue


            if agent_state not in SETTLED and not args.force_unsettled:
                refused.append({"name": name, "reason": f"unsettled:{agent_state}"})
                continue

            pane = first_str(agent, ("pane_id",)) or first_str(workspace_obj.get("root_pane"), ("pane_id",))
            agent_id = first_str(agent, ("agent_id", "id"))
            live_name = agent.get("name")
            candidates.append({
                "name": name,
                "workspace": workspace,
                "pane": pane,
                "agent": agent_id,
                "live_name": live_name if isinstance(live_name, str) and live_name else None,
                "tokens": tokens,
            })

        if args.run:
            gated: list[dict[str, object]] = []
            run_id = run_info.get("id", run_dir.name)
            for item in candidates:
                tokens = item["tokens"]
                exact = exact_attempt_for_tokens(tokens, run_id, graph)
                if not live_matches_recorded(item, tokens, run_id, graph, run_handles, run_claims):
                    refused.append({"name": item["name"], "reason": "identity_mismatch"})
                elif exact is None or exact not in run_terminal:
                    refused.append({"name": item["name"], "reason": "attempt_not_terminal"})
                elif missing_integration_receipt(tokens, run_info, run_dir, graph, run_snapshot, run_events):
                    refused.append({"name": item["name"], "reason": "missing_integration_receipt"})
                else:
                    gated.append(item)
            candidates = gated

        if args.collect:
            for item in candidates:
                name = item["name"]
                try:
                    payload = run_child([sys.executable, collect_script, name], "collect-agent.py")
                except ChildFailed as exc:
                    detail = exc.stderr.strip() or f"exit {exc.returncode}"
                    raise ioutil.RunError(f"collect-agent.py failed for {name}: {detail}") from exc
                item["summary"] = extract_collect_summary(payload, name)

        retired: list[dict[str, str]] = []
        for item in candidates:
            herdr("workspace", "close", item["workspace"])
            row: dict[str, str] = {"name": item["name"], "workspace": item["workspace"]}
            if "summary" in item:
                row["summary"] = item["summary"]
            retired.append(row)

        print(json.dumps({"retired": retired, "refused": refused}))
        return 3 if refused else 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
