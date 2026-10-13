#!/usr/bin/env python3
"""Reconcile a run, printing compact controller state unless --full is supplied."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


import graph_model
import ioutil
import cli_common

CLI_EXTRA_CODES = {RuntimeError: 1}
import ledger
import scheduler
from herdr_cli import herdr
from cli_common import agent_pane_id, agent_workspace_id, workspace_pane_id
from ioutil import first_str


def _exact_agent_id(agent: dict[str, Any]) -> str | None:
    return first_str(agent, ("agent_id", "id"))


def _optional_tokens(container: dict[str, Any], label: str) -> dict[str, Any]:
    if "tokens" not in container:
        return {}
    tokens = container["tokens"]
    if isinstance(tokens, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in tokens.items()
    ):
        return tokens
    raise ioutil.RunError(f"herdr {label} returned unexpected output")


def _workspace_record(workspace_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fetched = herdr("workspace", "get", workspace_id)
    result = fetched.get("result") if isinstance(fetched.get("result"), dict) else {}
    workspace = result.get("workspace")
    if not isinstance(workspace, dict):
        raise ioutil.RunError(f"herdr workspace get {workspace_id} returned unexpected output")
    if workspace.get("workspace_id") != workspace_id:
        raise ioutil.RunError(f"herdr workspace get {workspace_id} returned unexpected output")
    tokens = _optional_tokens(workspace, f"workspace get {workspace_id}")
    return workspace, tokens


def _recorded_herdr_identities(run, *, lock_timeout=120) -> list[tuple[dict[str, Any], str]]:
    identities: list[tuple[dict, str]] = []
    if not run:
        return identities
    with ledger.with_lock(run, lock_timeout=lock_timeout):
        snap = ledger.replay(run)
        state = snap["state"]
        for node_id, node in state.get("nodes", {}).items():
            executor = node.get("executor")
            if executor not in graph_model.HERDR_EXECUTORS:
                continue
            recorded = ledger.recorded_herdr_identity(state, node_id, node)
            identities.append((recorded, executor))
    return identities


def _surfaces_of(identities: list[tuple[dict[str, Any], str]]) -> set[tuple[str, str]]:
    surfaces: set[tuple[str, str]] = set()
    for recorded, _executor in identities:
        rws, rpane = recorded.get("workspace"), recorded.get("pane")
        if isinstance(rws, str) and rws and isinstance(rpane, str) and rpane:
            surfaces.add((rws, rpane))
    return surfaces


def _recorded_herdr_surfaces(run, *, lock_timeout=120) -> set[tuple[str, str]]:
    return _surfaces_of(_recorded_herdr_identities(run, lock_timeout=lock_timeout))


def _with_status_fields(record: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    for key in ("agent_status", "reason", "question"):
        if key in agent and agent[key] is not None:
            record[key] = agent[key]
    return record


def observe_live_herdr(run=None, *, lock_timeout=120) -> list[dict[str, Any]] | None:
    try:
        payload = herdr("agent", "list")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else None
        agents = result.get("agents") if result else None
        if not isinstance(agents, list):
            raise ioutil.RunError("herdr agent list returned unexpected output")
        cache: dict[str, tuple[dict, dict]] = {}
        recorded_identities = _recorded_herdr_identities(run, lock_timeout=lock_timeout) if run else None
        recorded_surfaces = _surfaces_of(recorded_identities or [])
        records = []
        for agent in agents:
            if not isinstance(agent, dict):
                raise ioutil.RunError("herdr agent list returned unexpected output")
            workspace_id = agent_workspace_id(agent)
            if not workspace_id:
                raise ioutil.RunError("herdr agent list returned unexpected output")
            if run:
                agent_id = _exact_agent_id(agent)
                worker_id = first_str(agent.get("name"))
                matches_recorded = any(
                    (
                        recorded.get("identity_kind") in (None, graph_model.IDENTITY_RUNTIME_ID)
                        and agent_id
                        and recorded.get("agent") == agent_id
                    )
                    or (
                        not agent_id
                        and recorded.get("identity_kind") == graph_model.IDENTITY_MANAGED_NAME
                        and worker_id
                        and recorded.get("worker_id") == worker_id
                    )
                    for recorded, _executor in recorded_identities
                )
                if not matches_recorded:
                    pane = agent_pane_id(agent)
                    if any(
                        workspace_id == rws and (not pane or pane == rpane)
                        for rws, rpane in recorded_surfaces
                    ):
                        raise ioutil.RunError("herdr agent list returned unexpected output")
                    continue
            if workspace_id not in cache:
                cache[workspace_id] = _workspace_record(workspace_id)
            workspace, tokens = cache[workspace_id]
            pane = agent_pane_id(agent) or workspace_pane_id(workspace)
            if not pane:
                raise ioutil.RunError("herdr agent list returned unexpected output")
            agent_tokens = _optional_tokens(agent, "agent list")
            agent_id = _exact_agent_id(agent)
            filtered_tokens = {
                key: tokens[key]
                for key in ("provenance", "run", "node", "attempt")
                if key in tokens
            }
            if agent_id:
                records.append(_with_status_fields({
                    "workspace": workspace_id,
                    "pane": pane,
                    "agent": agent_id,
                    "tokens": filtered_tokens,
                }, agent))
                continue
            if recorded_identities is None:
                recorded_identities = _recorded_herdr_identities(run, lock_timeout=lock_timeout)
            worker_id = first_str(agent.get("name"))
            if worker_id and graph_model.WORKER_ID_RE.fullmatch(worker_id):
                candidate = {
                    "identity_kind": graph_model.IDENTITY_MANAGED_NAME,
                    "worker_id": worker_id,
                    "workspace": workspace_id,
                    "pane": pane,
                    "tokens": filtered_tokens,
                }
                if any(
                    graph_model.herdr_identity_matches(recorded, candidate, executor)
                    for recorded, executor in recorded_identities
                ):
                    records.append(_with_status_fields(candidate, agent))
                    continue
            # Scoped inventories also contain controllers and retained workers.
            # Recorded surfaces below still require an exact live identity.
            if not run and (agent_tokens or tokens):
                raise ioutil.RunError("herdr agent list returned unexpected output")
            if (workspace_id, pane) in _surfaces_of(recorded_identities):
                raise ioutil.RunError("herdr agent list returned unexpected output")
        return records
    except ioutil.RunError as exc:
        if getattr(exc, "code", None) == "server_not_running":
            return None
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--event")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--full", action="store_true", help="Print the complete scheduler result")
    parser.add_argument("--require-native-quiesced", action="store_true")
    parser.add_argument("--session", dest="current_session")
    parser.add_argument("--holder-workspace")
    parser.add_argument("--holder-pane")
    parser.add_argument("--repo")
    parser.add_argument("--live-native-id", action="append", default=None)
    parser.add_argument("--native-inventory-complete", action="store_true")
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    try:
        event = json.loads(args.event) if args.event else None
    except json.JSONDecodeError as exc:
        raise ioutil.UsageError(f"--event is not valid JSON: {exc}") from exc

    holder_values = (args.holder_workspace, args.holder_pane, args.current_session)
    if args.native_inventory_complete and not args.current_session:
        raise ioutil.UsageError("--native-inventory-complete requires --session")
    if args.live_native_id is not None and not args.native_inventory_complete:
        raise ioutil.UsageError("--live-native-id requires --native-inventory-complete")
    needs_holder = (
        args.event is not None
        or args.notify
        or args.repo is not None
        or args.current_session
        or args.require_native_quiesced
    )
    if needs_holder and not all(holder_values):
        raise ioutil.UsageError(
            "--holder-workspace, --holder-pane, and --session must be supplied together"
        )

    holder = None
    if all(holder_values):
        holder = {
            "workspace": args.holder_workspace,
            "pane": args.holder_pane,
            "session": args.current_session,
        }
    live_herdr = None
    live_native_ids = None
    if args.current_session:
        live_herdr = observe_live_herdr(run=args.run, lock_timeout=args.lock_timeout)
        if args.native_inventory_complete:
            live_native_ids = args.live_native_id if args.live_native_id is not None else []
    result = scheduler.reconcile(
        args.run,
        event=event,
        notify=args.notify,
        require_native_quiesced=args.require_native_quiesced,
        current_session=args.current_session,
        live_herdr=live_herdr,
        live_native_ids=live_native_ids,
        native_inventory_complete=args.native_inventory_complete,
        holder=holder,
        repo=args.repo,
        lock_timeout=args.lock_timeout,
    )
    if not args.full:
        controller, state = result["controller"], result["state"]
        result = {
            "epoch": controller["epoch"],
            "dispatch": controller["dispatch"],
            "lease": controller["lease"],
            "holder": {key: controller.get(key) for key in graph_model.HOLDER_KEYS},
            "nodes": {
                node_id: {
                    key: node[key]
                    for key in ("state", "attempt", "reason", "question")
                    if key in node
                }
                for node_id, node in state["nodes"].items()
            },
            "notified": result["notified"],
        }
        if state.get("truncated_tail"):
            result["truncated_tail"] = True
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
