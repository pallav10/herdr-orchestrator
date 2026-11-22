#!/usr/bin/env python3
"""Selectively invalidate a succeeded node (and optional transitive dependents) for rerun."""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import cli_common
import graph_model
import ioutil
import ledger
from cli_common import holder_from_args


def _transitive_dependents_topo(graph: dict[str, Any], target: str) -> list[str]:
    """Return [target, ...transitive dependents] in topological order."""
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), dict) else {}
    children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node_id, plan in nodes.items():
        for dep in plan.get("depends_on") or []:
            if dep in children:
                children[dep].append(node_id)

    reachable: set[str] = {target}
    queue: deque[str] = deque([target])
    while queue:
        current = queue.popleft()
        for child in sorted(children.get(current, [])):
            if child not in reachable:
                reachable.add(child)
                queue.append(child)

    indegree: dict[str, int] = {node_id: 0 for node_id in reachable}
    for node_id in reachable:
        for dep in (nodes.get(node_id) or {}).get("depends_on") or []:
            if dep in reachable:
                indegree[node_id] += 1

    ready = deque(sorted(node_id for node_id, deg in indegree.items() if deg == 0))
    ordered: list[str] = []
    while ready:
        current = ready.popleft()
        ordered.append(current)
        for child in sorted(children.get(current, [])):
            if child in indegree:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
    return ordered


def _resolve_feedback_text(raw: str | None) -> str | None:
    if raw is None:
        return None
    candidate = Path(raw)
    try:
        is_file = candidate.is_file()
    except OSError:
        return raw
    if is_file:
        return candidate.read_text(encoding="utf-8")
    return raw


def invalidate_node(
    run_dir: str | Path,
    *,
    node_id: str,
    reason: str,
    holder: dict[str, str],
    feedback: str | None = None,
    cascade: bool = True,
    retry: bool = False,
    lock_timeout: float = 120,
) -> dict[str, Any]:
    """Invalidate target node (and optional succeeded dependents), optionally retrying and writing feedback."""
    if not isinstance(reason, str) or not reason.strip():
        raise ioutil.UsageError("--reason must be non-empty")

    run_path = Path(ioutil.canonical_path(run_dir))
    feedback_text = _resolve_feedback_text(feedback)

    with ledger.with_lock(run_path, lock_timeout=lock_timeout, node=node_id):
        snap, graph = ledger._replay(run_path)
        graph_model.require_active_holder(snap["controller"], holder, "invalidate-node")

        nodes = snap["state"].get("nodes") or {}
        if node_id not in nodes:
            raise ioutil.PreconditionError(f"unknown node {node_id!r}")
        if nodes[node_id].get("state") != "succeeded":
            raise ioutil.PreconditionError(
                f"target node {node_id!r} must be succeeded (got {nodes[node_id].get('state')!r})"
            )

        scope = _transitive_dependents_topo(graph, node_id) if cascade else [node_id]
        to_invalidate = [nid for nid in scope if nodes.get(nid, {}).get("state") == "succeeded"]

        events: list[dict[str, Any]] = []
        invalidated: list[str] = []
        retried: list[str] = []

        for nid in to_invalidate:
            node = nodes[nid]
            attempt = int(node.get("attempt", 0))
            events.append(
                {
                    "type": "invalidated",
                    "node": nid,
                    "from": "succeeded",
                    "to": "invalidated",
                    "attempt": attempt,
                    "reason": reason,
                }
            )
            invalidated.append(nid)

            plan = (graph.get("nodes") or {}).get(nid) or {}
            policy_allows = graph_model._retry_allows(plan, "invalidated", attempt)
            if nid == node_id and (policy_allows or retry):
                retry_event: dict[str, Any] = {
                    "type": "retried",
                    "node": nid,
                    "from": "invalidated",
                    "to": "planned",
                    "attempt": attempt,
                    "next_attempt": attempt + 1,
                    "reason": reason,
                }
                if retry and not policy_allows:
                    retry_event["force_retry"] = True
                events.append(retry_event)
                retried.append(nid)
            elif nid != node_id and policy_allows:
                events.append(
                    {
                        "type": "retried",
                        "node": nid,
                        "from": "invalidated",
                        "to": "planned",
                        "attempt": attempt,
                        "next_attempt": attempt + 1,
                        "reason": reason,
                    }
                )
                retried.append(nid)

        ledger.append_events_locked(run_path, events, holder=holder)

        feedback_path: str | None = None
        if feedback_text is not None and node_id in retried:
            next_attempt = int(nodes[node_id].get("attempt", 0)) + 1
            dest = ledger.attempt_dir(run_path, node_id, next_attempt) / "feedback.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            ioutil.atomic_text(dest, feedback_text)
            feedback_path = str(dest)

    return {
        "target": node_id,
        "invalidated": invalidated,
        "retried": retried,
        "feedback_path": feedback_path,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        usage=(
            "invalidate-node.py --run RUN --node NODE --reason TEXT "
            "--session S --holder-workspace W --holder-pane P "
            "[--feedback FILE_OR_TEXT] [--cascade|--no-cascade] [--retry] [--lock-timeout SECONDS]"
        ),
    )
    parser.add_argument("--run", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--holder-workspace", required=True)
    parser.add_argument("--holder-pane", required=True)
    parser.add_argument("--feedback", default=None, help="feedback text or path to a feedback file")
    parser.add_argument("--cascade", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--retry",
        action="store_true",
        help="append retried for target node even when retry policy does not list invalidated",
    )
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    holder = holder_from_args(args)
    result = invalidate_node(
        args.run,
        node_id=args.node,
        reason=args.reason,
        holder=holder,
        feedback=args.feedback,
        cascade=args.cascade,
        retry=args.retry,
        lock_timeout=args.lock_timeout,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    cli_common.run(main)
