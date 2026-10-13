"""Lifecycle scheduling, invalidation, notifications, and reconciliation."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any
from ioutil import PreconditionError, _read_json, canonical_path
from graph_model import ATTEMPT_TERMINAL, STATES, _condition_true, _retry_allows, current_artifact, holder_exact
from ledger import _EVENT_TO, _apply_event, _load_graph_revision, _materialize_locked, _repair_dispatch_snapshots_locked, _replay, append_event_locked, append_events_locked, attempt_dir, with_lock
from liveness import _live_herdr_records, maybe_interrupt_herdr, maybe_interrupt_native, native_quiesced
from promotion import _recover_promotion_locked


def _computed_lifecycle_events(state: dict[str, Any], graph: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    nodes = state["nodes"]
    plans = graph.get("nodes", {})
    dispatch = state.get("dispatch", False)
    for node_id in sorted(nodes):
        if node_id not in plans:
            continue
        node = nodes[node_id]
        cur = node.get("state")
        if cur in {"running", "succeeded", "failed", "interrupted", "skipped", "invalidated"}:
            continue
        if cur == "blocked" and not str(node.get("reason", "")).startswith(("dep ", "input ")):
            continue
        plan = plans.get(node_id, {})
        attempt = int(node.get("attempt", 0))
        for inp in plan.get("inputs", []) or []:
            if inp.get("required", True) and nodes.get(inp["from"], {}).get("state") == "skipped":
                reason = f"input {inp['alias']} missing"
                if cur != "blocked" or node.get("reason") != reason:
                    events.append({"type": "blocked", "node": node_id, "from": cur, "to": "blocked", "attempt": attempt, "reason": reason})
                break
        else:
            deps = plan.get("depends_on", []) or []
            failed = next((dep for dep in deps if nodes.get(dep, {}).get("state") in {"failed", "interrupted"}), None)
            if failed:
                reason = f"dep {failed} is {nodes[failed]['state']}"
                if cur != "blocked" or node.get("reason") != reason:
                    events.append({"type": "blocked", "node": node_id, "from": cur, "to": "blocked", "attempt": attempt, "reason": reason})
                continue
            if not all(nodes.get(dep, {}).get("state") == "succeeded" for dep in deps):
                if cur in {"blocked", "ready"}:
                    events.append({"type": "planned", "node": node_id, "from": cur, "to": "planned", "attempt": attempt, "reason": "dependencies pending"})
                continue
            for inp in plan.get("inputs", []) or []:
                if inp.get("required", True):
                    art = current_artifact(state, inp["from"], inp["output"])
                    if not art or not art.get("path") or not art.get("hash"):
                        reason = f"input {inp['alias']} missing"
                        if cur != "blocked" or node.get("reason") != reason:
                            events.append({"type": "blocked", "node": node_id, "from": cur, "to": "blocked", "attempt": attempt, "reason": reason})
                        break
            else:
                if not _condition_true(plan.get("condition")):
                    if cur != "skipped":
                        events.append({"type": "skipped", "node": node_id, "from": cur, "to": "skipped", "attempt": attempt, "reason": "condition false"})
                elif dispatch and cur != "ready":
                    events.append({"type": "ready", "node": node_id, "from": cur, "to": "ready", "attempt": attempt})
    return events


def _invalidation_policy_events(graph: dict[str, Any], node_id: str, attempt: int) -> list[dict[str, Any]]:
    if _retry_allows(graph.get("nodes", {}).get(node_id, {}), "invalidated", attempt):
        return [{"type": "retried", "node": node_id, "from": "invalidated", "to": "planned", "attempt": attempt, "next_attempt": attempt + 1, "reason": "invalidated digest"}]
    return [{"type": "blocked", "node": node_id, "from": "invalidated", "to": "blocked", "attempt": attempt, "reason": "invalidated digest"}]


def invalidate_if_stale(state: dict[str, Any], graph: dict[str, Any]) -> list[dict[str, Any]]:
    run_dir = Path(state["run"])
    events: list[dict[str, Any]] = []
    for node_id, node in state.get("nodes", {}).items():
        attempt = int(node.get("attempt", 0))
        if node.get("state") == "invalidated":
            events.extend(_invalidation_policy_events(graph, node_id, attempt))
            continue
        if node.get("state") != "succeeded":
            continue
        try:
            consumed_path = attempt_dir(run_dir, node_id, attempt) / "consumed_artifacts.json"
        except PreconditionError:
            # Unaddressable attempt dir (bad node id / attempt 0): there is no consumed
            # receipt to compare, so the node cannot be shown stale. Same outcome as a
            # missing consumed_artifacts.json below.
            continue
        if not consumed_path.exists():
            continue

        raw_consumed = _read_json(consumed_path)
        consumed = raw_consumed.get("consumed", []) if isinstance(raw_consumed, dict) else raw_consumed
        for item in consumed:
            art = current_artifact(state, item.get("producer"), item.get("output"))
            if not art:
                continue
            new_hash = art.get("hash")
            if new_hash == item.get("hash"):
                continue
            events.append({"type": "invalidated", "node": node_id, "from": "succeeded", "to": "invalidated", "old_hash": item.get("hash"), "new_hash": new_hash, "producer": item.get("producer"), "output": item.get("output"), "attempt": attempt})
            events.extend(_invalidation_policy_events(graph, node_id, attempt))
            break
    return events


def _event_key(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        event.get("type"),
        event.get("node"),
        event.get("attempt"),
        event.get("next_attempt"),
        event.get("from"),
        event.get("to"),
        event.get("old_hash"),
        event.get("new_hash"),
        event.get("producer"),
        event.get("output"),
        event.get("reason"),
        event.get("ref"),
        event.get("expected_old"),
        event.get("new"),
        event.get("key"),
    )


def _same_state_repeat(nodes: dict[str, Any], candidate: dict[str, Any]) -> bool:
    node_id = candidate.get("node")
    if not isinstance(node_id, str):
        return False
    node = nodes.get(node_id) or {}
    typ = candidate.get("type")
    target = candidate.get("to") if candidate.get("to") in STATES else _EVENT_TO.get(typ)
    if target is None or node.get("state") != target:
        return False
    if target == "blocked" and candidate.get("reason") != node.get("reason"):
        return False
    return True


def _all_done_occurrence(snap: dict[str, Any], graph: dict[str, Any] | None = None) -> int | None:
    terminal = ATTEMPT_TERMINAL
    nodes = snap["state"].get("nodes") or {}
    active_now = graph.get("nodes") if isinstance(graph, dict) else None
    if active_now is None:
        active_now = nodes
    if not active_now or not all((nodes.get(node_id) or {}).get("state") in terminal for node_id in active_now):
        return None
    events = snap.get("events") or []
    if any(event.get("type") == "graph_accepted" and event.get("revision") for event in events):
        active: set[str] = set()
        node_state: dict[str, str] = {}
    else:
        active = set(active_now)
        node_state = {node_id: "planned" for node_id in active}
    pending = {node_id for node_id in active if node_state.get(node_id, "planned") not in terminal}
    occurrence = 0
    all_done = bool(active) and not pending
    if all_done:
        occurrence = 1
    run_dir = Path(snap["state"]["run"]) if snap["state"].get("run") else None
    for event in events:
        typ = event.get("type")
        if typ == "graph_accepted" and event.get("revision") and run_dir is not None:
            revised = _load_graph_revision(run_dir, event["revision"]).get("graph") or {}
            active = set(revised.get("nodes") or {})
            for node_id in active:
                node_state.setdefault(node_id, "planned")
            pending = {node_id for node_id in active if node_state.get(node_id, "planned") not in terminal}
        else:
            target = event.get("to") if event.get("to") in STATES else _EVENT_TO.get(typ)
            node_id = event.get("node")
            if not (isinstance(node_id, str) and target):
                continue
            node_state[node_id] = target
            if node_id not in active:
                continue
            if target in terminal:
                pending.discard(node_id)
            else:
                pending.add(node_id)
        now = bool(active) and not pending
        if now and not all_done:
            occurrence += 1
        all_done = now
    return occurrence or 1


def _notification_edge_key(event: dict[str, Any], sequence: int) -> str | None:
    state = event.get("to") if event.get("to") in STATES else event.get("type")
    if state in {"blocked", "failed"} and event.get("node"):
        return f"edge:{event.get('epoch', 1)}:{sequence}:{state}:{event['node']}:{event.get('attempt', 0)}"
    return None


def _notification_targets(snap: dict[str, Any], graph: dict[str, Any] | None = None) -> list[str]:
    notified = {event.get("key") for event in snap["events"] if event.get("type") == "notification_sent"}
    targets: list[str] = []
    for sequence, event in enumerate(snap["events"], 1):
        key = _notification_edge_key(event, sequence)
        if key and key not in notified:
            targets.append(key)
    occurrence = _all_done_occurrence(snap, graph)
    if occurrence is not None:
        all_done = f"all-done:{occurrence}"
        if all_done not in notified:
            targets.append(all_done)
    return targets


def _notify(run_dir: Path, snap: dict[str, Any], *, holder=None, graph: dict[str, Any] | None = None) -> tuple[list[str], dict[str, Any]]:
    targets = _notification_targets(snap, graph)
    if targets:
        snap = append_events_locked(run_dir, [{"type": "notification_sent", "key": key} for key in targets], holder=holder)
    return targets, snap


def _append_candidates_locked(run_dir: Path, snap: dict[str, Any], graph: dict[str, Any], candidates: Iterable[dict[str, Any]], *, holder=None) -> dict[str, Any] | None:
    seen: set[tuple] = set()
    pending: list[dict[str, Any]] = []
    nodes = snap["state"].get("nodes") or {}
    for candidate in candidates:
        key = _event_key(candidate)
        if key in seen or _same_state_repeat(nodes, candidate):
            continue
        node = nodes.get(candidate.get("node")) or {}
        if candidate.get("from") is not None and candidate["from"] != node.get("state"):
            continue
        if candidate.get("attempt") is not None and candidate["attempt"] != node.get("attempt", 0):
            continue
        _apply_event(snap["state"], snap["controller"], graph, candidate)
        seen.add(key)
        pending.append(candidate)
    if pending:
        return append_events_locked(run_dir, pending, holder=holder)
    return None


def reconcile(run_dir: str | Path, *, event: dict[str, Any] | None = None, notify: bool = False, require_native_quiesced: bool = False, current_session: str | None = None, live_native_ids: list[str] | None = None, native_inventory_complete: bool = False, holder: dict[str, str] | None = None, repo: str | Path | None = None, live_herdr: list[dict[str, Any]] | None = None, lock_timeout=120) -> dict[str, Any]:
    """Return {state, controller, notified}."""
    run_dir = Path(canonical_path(run_dir))
    notified: list[str] = []
    _live_herdr_records(live_herdr)
    if holder is None:
        if event is not None or notify or repo is not None:
            raise PreconditionError("mutating reconcile requires exact current holder")
        with with_lock(run_dir, lock_timeout=lock_timeout):
            snap, graph = _replay(run_dir)
            if require_native_quiesced and not native_quiesced(snap["state"]):
                raise PreconditionError("native executors are not quiesced")
            _materialize_locked(run_dir, snap, graph)
            return {"state": snap["state"], "controller": snap["controller"], "notified": notified}
    with with_lock(run_dir, lock_timeout=lock_timeout, node=event.get("node") if event else None):
        snap, graph = _replay(run_dir)
        if not holder_exact(snap["controller"], holder):
            raise PreconditionError("mutating reconcile requires exact current holder")
        if repo:
            _recover_promotion_locked(run_dir, repo, holder=holder)
            snap, graph = _replay(run_dir)
        if event:
            append_event_locked(run_dir, event, holder=holder)
            snap, graph = _replay(run_dir)
        while True:
            _repair_dispatch_snapshots_locked(run_dir, snap)
            if snap.get("truncated_tail"):
                break

            def candidates() -> Iterable[dict[str, Any]]:
                # Generate each group after earlier candidates update the projection.
                yield from maybe_interrupt_native(snap["state"], current_session, live_native_ids, native_inventory_complete)
                yield from maybe_interrupt_herdr(snap["state"], live_herdr)
                yield from invalidate_if_stale(snap["state"], graph)
                yield from _computed_lifecycle_events(snap["state"], graph)

            refreshed = _append_candidates_locked(run_dir, snap, graph, candidates(), holder=holder)
            if refreshed is None:
                break
            snap = refreshed
        if notify:
            notified, snap = _notify(run_dir, snap, holder=holder, graph=graph)
        _materialize_locked(run_dir, snap, graph)
        if require_native_quiesced and not native_quiesced(snap["state"]):
            raise PreconditionError("native executors are not quiesced")
        return {"state": snap["state"], "controller": snap["controller"], "notified": notified}
