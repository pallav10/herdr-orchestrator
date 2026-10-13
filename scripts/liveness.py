"""Native and Herdr inventory reconciliation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import graph_model
from ioutil import PreconditionError, UsageError
from graph_model import ATTEMPT_TERMINAL, HERDR_EXECUTORS, _herdr_token, has_current_dispatch_intent, herdr_identity_matches, parse_herdr_identity, validate_model
from ledger import attempt_dir, load_dispatch_identity, recorded_herdr_identity


def native_quiesced(state: dict[str, Any], executors=graph_model.NATIVE_EXECUTORS) -> bool:
    terminal = ATTEMPT_TERMINAL
    for node in state.get("nodes", {}).values():
        if node.get("executor") not in executors:
            continue
        if node.get("state") in graph_model.ACTIVE_STATES:
            return False
        if node.get("state") not in terminal and node.get("dispatch_intent"):
            return False
    return True


def _native_handle_id(handle: dict[str, Any]) -> str | None:
    for key in ("job", "job_id", "runId"):
        value = handle.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _observable_orphan(node: dict[str, Any]) -> bool:
    state = node.get("state")
    return state in graph_model.ACTIVE_STATES or (state == "ready" and has_current_dispatch_intent(node))


def maybe_interrupt_native(state: dict[str, Any], current_session: str | None, live_native_ids: list[str] | None = None, native_inventory_complete: bool = False) -> list[dict[str, Any]]:
    if not current_session:
        return []
    live = {str(item) for item in (live_native_ids or [])}
    events = []
    for node_id, node in state.get("nodes", {}).items():
        if node.get("executor") not in graph_model.NATIVE_EXECUTORS or not _observable_orphan(node):
            continue
        handle = node.get("handle") if isinstance(node.get("handle"), dict) else {}
        native_id = _native_handle_id(handle)
        reason = None
        if node.get("state") in graph_model.ACTIVE_STATES:
            if handle.get("spawning_session") != current_session:
                reason = "native executor not owned by current session"
            elif native_inventory_complete and (not native_id or native_id not in live):
                reason = "native executor vanished from current session"
        elif native_inventory_complete and (not native_id or native_id not in live):
            reason = "native executor vanished from current session"
        if reason:
            events.append({"type": "interrupted", "node": node_id, "from": node.get("state"), "to": "interrupted", "attempt": node.get("attempt", 0), "reason": reason})
    return events


def _herdr_live_triple(record: dict[str, Any], executor: str) -> tuple[str | None, str | None, str | None] | None:
    parsed = parse_herdr_identity(record, executor, require_context=False)
    return None if parsed is None else (parsed["workspace"], parsed["pane"], parsed["target"])


def _herdr_live_surface(record: dict[str, Any]) -> tuple[str, str] | None:
    if not isinstance(record, dict):
        return None
    values = (record.get("workspace"), record.get("pane"))
    if not all(isinstance(value, str) and value for value in values):
        return None
    return values


def _herdr_stamp_complete(record: dict[str, Any]) -> bool:
    return all(isinstance(_herdr_token(record, key), str) and _herdr_token(record, key) for key in ("run", "node", "attempt"))


def _live_herdr_records(live_herdr: object) -> list[dict[str, Any]] | None:
    if live_herdr is None:
        return None
    if not isinstance(live_herdr, list) or any(not isinstance(item, dict) for item in live_herdr):
        raise UsageError("live_herdr must be a list of identity records")
    return live_herdr


def herdr_ready_dispatch_candidate(state: dict[str, Any], node_id: str, node: dict[str, Any]) -> dict[str, Any] | None:
    if node.get("executor") != "herdr-agent" or node.get("state") != "ready" or not has_current_dispatch_intent(node):
        return None
    run_dir = state.get("run")
    attempt = int(node.get("attempt") or 0)
    if not run_dir or attempt < 1:
        return None
    identity = load_dispatch_identity(attempt_dir(Path(run_dir), node_id, attempt))
    if not isinstance(identity, dict):
        return None
    handle = {}
    for key in ("workspace", "pane", "agent", "identity_kind", "worker_id", "repo", "worktree_path", "branch", "base"):
        value = identity.get(key)
        if isinstance(value, str) and value:
            handle[key] = value
    if parse_herdr_identity(handle, "herdr-agent", require_context=False) is None:
        return None
    tokens = identity.get("tokens")
    if isinstance(tokens, dict):
        handle["tokens"] = tokens
    routing = identity.get("routing")
    if not isinstance(routing, dict):
        return None
    try:
        model = validate_model(routing)
    except PreconditionError:
        return None
    return {"type": "dispatched", "node": node_id, "attempt": attempt, "handle": handle, "model": model}


def maybe_interrupt_herdr(state: dict[str, Any], live_herdr: object) -> list[dict[str, Any]]:
    records = _live_herdr_records(live_herdr)
    if records is None:
        return []
    events = []
    for node_id, node in state.get("nodes", {}).items():
        executor = node.get("executor")
        if executor not in HERDR_EXECUTORS or not _observable_orphan(node):
            continue
        recorded = recorded_herdr_identity(state, node_id, node)
        matched = None
        if parse_herdr_identity(recorded, executor, require_context=True) is not None:
            for record in records:
                if herdr_identity_matches(recorded, record, executor):
                    matched = record
                    break
        if matched is not None:
            recovered = herdr_ready_dispatch_candidate(state, node_id, node)
            if recovered:
                events.append(recovered)
            elif node.get("state") == "running" and matched.get("agent_status") == "blocked":
                reason = matched.get("reason") or matched.get("question") or "blocked"
                blocked = {"type": "blocked", "node": node_id, "from": "running", "to": "blocked", "attempt": node.get("attempt", 0), "reason": reason}
                if "question" in matched:
                    blocked["question"] = matched.get("question")
                events.append(blocked)
            continue
        recorded_triple = _herdr_live_triple(recorded, executor)
        if recorded_triple is not None and any(
            _herdr_live_triple(record, executor) == recorded_triple and not _herdr_stamp_complete(record)
            for record in records
        ):
            continue
        recorded_surface = _herdr_live_surface(recorded)
        if (
            recorded_surface is not None
            and parse_herdr_identity(recorded, executor, require_context=False) is None
            and any(_herdr_live_surface(record) == recorded_surface for record in records)
        ):
            continue
        events.append({"type": "interrupted", "node": node_id, "from": node.get("state"), "to": "interrupted", "attempt": node.get("attempt", 0), "reason": "herdr identity not live"})
    return events
