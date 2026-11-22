#!/usr/bin/env python3
"""Read-only replay and timeline inspector for a herdr-orchestrator run.

Usage: inspect-run.py --run RUN [--format json|text] [--node NODE]

Replays events.jsonl without acquiring run.lock or mutating any caches.
Exit codes: 0 ok, 1 runtime error, 2 usage error, 3 precondition error.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

import cli_common
import graph_model
import ioutil
import ledger

CLI_EXTRA_CODES = {RuntimeError: 1}


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _node_duration_sec(events: list[dict[str, Any]], node_id: str, attempt: int) -> float | None:
    if attempt <= 0:
        return None
    start_dt: datetime | None = None
    fallback_start: datetime | None = None
    end_dt: datetime | None = None
    for event in ledger.events_for_attempt(events, node_id, attempt):
        typ = event.get("type")
        ts = _parse_iso(event.get("t"))
        if ts is None:
            continue
        if typ == "dispatched" and start_dt is None:
            start_dt = ts
        elif typ == "attempt_started" and fallback_start is None:
            fallback_start = ts
        elif typ in graph_model.ATTEMPT_TERMINAL or event.get("to") in graph_model.ATTEMPT_TERMINAL:
            end_dt = ts
    begin = start_dt or fallback_start
    if begin is None or end_dt is None:
        return None
    return round(max(0.0, (end_dt - begin).total_seconds()), 3)


def _read_dispatch_identity_safe(run_dir: Path, node_id: str, attempt: int) -> dict[str, Any]:
    if attempt <= 0:
        return {}
    try:
        path = ledger.attempt_dir(run_dir, node_id, attempt) / "dispatch-identity.json"
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, ioutil.RunError):
        return {}


def _has_integration_receipt(run_dir: Path, node_id: str, attempt: int) -> bool:
    if attempt <= 0:
        return False
    try:
        return (ledger.attempt_dir(run_dir, node_id, attempt) / "integration.json").is_file()
    except (OSError, ioutil.RunError):
        return False


def _read_lock_diagnostics(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.lock.owner"
    if not path.exists():
        return {"present": False, "unverified": True, "summary": None, "owner": None}
    owner: dict[str, Any] | None = None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            owner = parsed
    except (OSError, ValueError):
        owner = None
    return {
        "present": True,
        "unverified": True,
        "summary": ledger.read_lock_owner(path),
        "owner": owner,
    }


def _summarize_node(
    run_dir: Path,
    events: list[dict[str, Any]],
    node_id: str,
    node: dict[str, Any],
) -> dict[str, Any]:
    attempt = int(node.get("attempt", 0) or 0)
    handle = node.get("handle") if isinstance(node.get("handle"), dict) else {}
    identity = _read_dispatch_identity_safe(run_dir, node_id, attempt)
    worker_id = handle.get("worker_id") or identity.get("worker_id")
    worktree = handle.get("worktree_path") or handle.get("worktree") or identity.get("worktree_path")
    artifacts = node.get("artifacts") if isinstance(node.get("artifacts"), list) else []
    return {
        "state": node.get("state"),
        "attempt": attempt,
        "executor": node.get("executor"),
        "duration_sec": _node_duration_sec(events, node_id, attempt),
        "reason": node.get("reason"),
        "question": node.get("question"),
        "worker_id": worker_id if isinstance(worker_id, str) and worker_id else None,
        "worktree": worktree if isinstance(worktree, str) and worktree else None,
        "artifact_count": len(artifacts),
    }


def _infer_next_actions(
    run_dir: Path,
    run_doc: dict[str, Any],
    snap: dict[str, Any],
    node_ids: list[str],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    controller = snap["controller"]
    nodes = snap["state"].get("nodes", {})
    graph = ledger.load_graph(run_dir)

    if snap.get("truncated_tail"):
        actions.append({
            "action": "repair",
            "node": None,
            "detail": "events.jsonl has a truncated tail; inspect and repair before mutating",
        })
    if controller.get("lease") == graph_model.LEASE_DRAINING:
        actions.append({
            "action": "handoff",
            "node": None,
            "detail": "controller lease is draining; complete ack-handoff.py before dispatching",
        })

    repo = run_doc.get("repo") or "<REPO>"
    for node_id in node_ids:
        node = nodes[node_id]
        state = node.get("state")
        attempt = int(node.get("attempt", 0) or 0)
        executor = node.get("executor")
        integrates = bool(node.get("integrates")) or graph_model.declares_output(
            ((graph.get("nodes") or {}).get(node_id) or {}).get("outputs") or [], "integration"
        )
        handle = node.get("handle") if isinstance(node.get("handle"), dict) else {}

        if state == "ready":
            if executor in graph_model.HERDR_EXECUTORS:
                detail = f"dispatch-agent.py --run {run_dir} --node {node_id}"
            elif executor == "script":
                detail = f"run-script.py --run {run_dir} --node {node_id}"
            else:
                detail = f"dispatch native {executor} node {node_id}"
            actions.append({"action": "dispatch", "node": node_id, "detail": detail})
        elif state == "running":
            if integrates and node.get("promotion_intent") and not node.get("promoted"):
                actions.append({
                    "action": "recover_promotion",
                    "node": node_id,
                    "detail": f"reconcile.py --run {run_dir} --repo {repo}",
                })
            elif integrates and _has_integration_receipt(run_dir, node_id, attempt) and not node.get("promoted"):
                actions.append({
                    "action": "promote",
                    "node": node_id,
                    "detail": f"run_state.promote() for integrator {node_id} attempt {attempt}",
                })
            elif executor in graph_model.HERDR_EXECUTORS:
                actions.append({
                    "action": "monitor",
                    "node": node_id,
                    "detail": f"monitor-agents.py or finish-agent.py for {node_id} attempt {attempt}",
                })
            else:
                actions.append({
                    "action": "monitor",
                    "node": node_id,
                    "detail": f"await native {executor} node {node_id} and reconcile",
                })
        elif state == "blocked":
            detail = node.get("question") or node.get("reason") or f"prompt-agent.py to continue blocked node {node_id}"
            actions.append({"action": "unblock", "node": node_id, "detail": str(detail)})
        elif state == "succeeded":
            if integrates and not node.get("promoted"):
                actions.append({
                    "action": "promote",
                    "node": node_id,
                    "detail": f"run_state.promote() for integrator {node_id} attempt {attempt}",
                })
            if executor == "herdr-agent" and (handle.get("workspace") or handle.get("worker_id") or handle.get("agent")):
                actions.append({
                    "action": "retire",
                    "node": node_id,
                    "detail": f"retire-agents.py for terminal herdr-agent {node_id}",
                })
        elif state in {"failed", "interrupted", "invalidated"}:
            detail = node.get("reason") or f"inspect {state} node {node_id} and retry or replan"
            actions.append({"action": "retry_or_replan", "node": node_id, "detail": str(detail)})

    return actions


def inspect_run(run_path: str | Path, *, node_filter: str | None = None) -> dict[str, Any]:
    run_dir = Path(ioutil.canonical_path(run_path))
    snap = ledger.replay(run_dir)
    run_doc = ledger.load_run(run_dir)
    controller = snap["controller"]
    nodes = snap["state"].get("nodes", {})

    if node_filter is not None:
        if node_filter not in nodes:
            raise ioutil.UsageError(f"unknown node: {node_filter}")
        node_ids = [node_filter]
    else:
        node_ids = sorted(nodes)

    node_summaries = {
        node_id: _summarize_node(run_dir, snap["events"], node_id, nodes[node_id])
        for node_id in node_ids
    }
    return {
        "run": {
            "id": run_doc.get("id"),
            "job": run_doc.get("job"),
            "repo": run_doc.get("repo"),
            "base_sha": run_doc.get("base_sha"),
            "promotion_ref": run_doc.get("promotion_ref"),
            "epoch": controller.get("epoch"),
            "lease": controller.get("lease"),
            "holder": {key: controller.get(key) for key in graph_model.HOLDER_KEYS},
            "truncated_tail": bool(snap.get("truncated_tail")),
        },
        "nodes": node_summaries,
        "lock": _read_lock_diagnostics(run_dir),
        "next_actions": _infer_next_actions(run_dir, run_doc, snap, node_ids),
    }


def _format_text(report: dict[str, Any]) -> str:
    run = report["run"]
    lines = [
        f"Run: {run.get('id') or '-'}  job={run.get('job') or '-'}  epoch={run.get('epoch')}  lease={run.get('lease')}",
        f"Repo: {run.get('repo') or '-'}  base={(run.get('base_sha') or '-')[:12]}  ref={run.get('promotion_ref') or '-'}",
    ]
    if run.get("truncated_tail"):
        lines.append("WARNING: events.jsonl has a truncated tail")
    lock = report["lock"]
    if lock.get("present"):
        lines.append(f"Lock: {lock.get('summary')}")

    headers = ("NODE", "STATE", "ATT", "EXECUTOR", "DUR(s)", "ARTIFACTS", "WORKER", "NOTES")
    rows: list[tuple[str, ...]] = []
    for node_id, info in report["nodes"].items():
        dur = "-" if info["duration_sec"] is None else f"{info['duration_sec']:.3f}"
        note = info.get("question") or info.get("reason") or "-"
        rows.append((
            node_id,
            str(info.get("state") or "-"),
            str(info.get("attempt", 0)),
            str(info.get("executor") or "-"),
            dur,
            str(info.get("artifact_count", 0)),
            str(info.get("worker_id") or "-"),
            str(note),
        ))

    lines.append("")
    widths = [
        max(len(headers[idx]), *(len(row[idx]) for row in rows)) if rows else len(headers[idx])
        for idx in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines.append(fmt.format(*headers))
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append(fmt.format(*row))

    lines.append("")
    lines.append("Next actions:")
    if report["next_actions"]:
        for item in report["next_actions"]:
            prefix = f"[{item['action']}]"
            target = f" {item['node']}:" if item.get("node") else ""
            lines.append(f"  - {prefix}{target} {item['detail']}")
    else:
        lines.append("  - none")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="canonical absolute path to run directory")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="output format (default: json)")
    parser.add_argument("--node", default=None, help="inspect a single node ID")
    args = parser.parse_args()

    report = inspect_run(args.run, node_filter=args.node)
    if args.format == "text":
        print(_format_text(report))
    else:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
