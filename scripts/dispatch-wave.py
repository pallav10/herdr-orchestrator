#!/usr/bin/env python3
"""Dispatch ready wave nodes ranked by critical-path depth and downstream fan-out.

Reconciles readiness, selects ready `herdr-agent` and `script` nodes, ranks
them by (critical_path_depth desc, downstream_fanout desc, node_id asc),
enforces `--max-parallel` (including already-running nodes), and dispatches
via `dispatch-agent.py` or `run-script.py`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import cli_common
import graph_model
import ioutil
import ledger
import scheduler

WAVE_EXECUTORS = frozenset({"herdr-agent", "script"})
CLI_EXTRA_CODES = {RuntimeError: 1}


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {raw!r}") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def rank_nodes(graph: dict[str, Any], node_ids: list[str]) -> list[dict[str, Any]]:
    """Rank node_ids by (critical_path_depth desc, downstream_fanout desc, node_id asc).

    critical_path_depth: longest directed path length (in edges) from node to any sink.
    downstream_fanout: count of unique transitive dependents of node.
    """
    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), dict) else {}
    dependents: dict[str, list[str]] = {nid: [] for nid in nodes}
    for nid, plan in nodes.items():
        if not isinstance(plan, dict):
            continue
        for dep in plan.get("depends_on") or []:
            if dep in dependents:
                dependents[dep].append(nid)

    depth_memo: dict[str, int] = {}
    fanout_memo: dict[str, set[str]] = {}

    def critical_depth(nid: str) -> int:
        if nid not in depth_memo:
            children = dependents.get(nid, [])
            depth_memo[nid] = 0 if not children else 1 + max(critical_depth(child) for child in children)
        return depth_memo[nid]

    def transitive_dependents(nid: str) -> set[str]:
        if nid not in fanout_memo:
            closure: set[str] = set()
            for child in dependents.get(nid, []):
                closure.add(child)
                closure.update(transitive_dependents(child))
            fanout_memo[nid] = closure
        return fanout_memo[nid]

    scored = [
        {
            "node": nid,
            "executor": (nodes.get(nid) or {}).get("executor"),
            "critical_path_depth": critical_depth(nid),
            "downstream_fanout": len(transitive_dependents(nid)),
        }
        for nid in node_ids
    ]
    scored.sort(key=lambda item: (-item["critical_path_depth"], -item["downstream_fanout"], item["node"]))
    return scored


def _find_prompt_file(prompts_dir: Path, node_id: str) -> Path | None:
    for ext in (".md", ".txt"):
        candidate = prompts_dir / f"{node_id}{ext}"
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        usage="dispatch-wave.py --run RUN --session S --holder-workspace W --holder-pane P --epoch E --prompts-dir DIR [--max-parallel N] [--dry-run] [--lock-timeout SECONDS]",
    )
    parser.add_argument("--run", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--holder-workspace", required=True)
    parser.add_argument("--holder-pane", required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--prompts-dir", required=True)
    parser.add_argument("--max-parallel", type=_positive_int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    holder = cli_common.holder_from_args(args)
    run_dir = Path(ioutil.canonical_path(args.run))
    prompts_dir = Path(ioutil.canonical_path(args.prompts_dir))
    if not prompts_dir.is_dir():
        raise ioutil.PreconditionError(f"prompts directory does not exist: {prompts_dir}")

    snap = ledger.replay(run_dir)
    graph_model.require_active_holder(snap["controller"], holder, "dispatch-wave")
    controller_epoch = int(snap["controller"]["epoch"])
    if controller_epoch != args.epoch:
        raise ioutil.PreconditionError(
            f"epoch {args.epoch} does not match controller epoch {controller_epoch}"
        )

    graph = ledger.load_graph(run_dir)
    if args.dry_run:
        # Compute readiness without mutating the ledger.
        state = snap["state"]
        nodes_state = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        pending_ready = {
            ev["node"]
            for ev in scheduler._computed_lifecycle_events(state, graph)
            if ev.get("type") == "ready" and isinstance(ev.get("node"), str)
        }
        ready_ids = [
            nid
            for nid, node in nodes_state.items()
            if isinstance(node, dict)
            and (node.get("state") == "ready" or nid in pending_ready)
            and (graph.get("nodes", {}).get(nid) or {}).get("executor") in WAVE_EXECUTORS
        ]
        running_count = sum(
            1 for node in nodes_state.values() if isinstance(node, dict) and node.get("state") == "running"
        )
    else:
        reconciled = scheduler.reconcile(run_dir, holder=holder, lock_timeout=args.lock_timeout)
        state = reconciled["state"]
        nodes_state = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
        ready_ids = [
            nid
            for nid, node in nodes_state.items()
            if isinstance(node, dict)
            and node.get("state") == "ready"
            and (graph.get("nodes", {}).get(nid) or {}).get("executor") in WAVE_EXECUTORS
        ]
        running_count = sum(
            1 for node in nodes_state.values() if isinstance(node, dict) and node.get("state") == "running"
        )

    ranked = rank_nodes(graph, ready_ids)
    slots = None if args.max_parallel is None else max(0, args.max_parallel - running_count)

    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for entry in ranked:
        nid = entry["node"]
        executor = entry["executor"]
        if slots is not None and len(planned) >= slots:
            skipped.append({**entry, "reason": "max_parallel"})
            continue
        if executor == "herdr-agent":
            prompt_file = _find_prompt_file(prompts_dir, nid)
            if prompt_file is None:
                skipped.append({**entry, "reason": f"missing prompt file ({nid}.md or {nid}.txt)"})
                continue
            planned.append({**entry, "prompt_file": str(prompt_file)})
        else:
            planned.append(entry)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "running": running_count,
                    "max_parallel": args.max_parallel,
                    "planned": planned,
                    "skipped": skipped,
                }
            )
        )
        return 0

    dispatched: list[dict[str, Any]] = []
    herdr_targets: list[str] = []

    for entry in planned:
        nid = entry["node"]
        executor = entry["executor"]
        if executor == "herdr-agent":
            argv = [
                sys.executable,
                str(cli_common.SCRIPTS / "dispatch-agent.py"),
                "--run", str(run_dir),
                "--node", nid,
                "--session", holder["session"],
                "--holder-workspace", holder["workspace"],
                "--holder-pane", holder["pane"],
                "--epoch", str(args.epoch),
                "--prompt-file", entry["prompt_file"],
                "--lock-timeout", str(args.lock_timeout),
            ]
            res = cli_common.run_child(argv, "dispatch-agent.py")
            dispatched.append({**entry, "result": res})
            target = res.get("worker_id") or res.get("agent")
            if isinstance(target, str) and target:
                herdr_targets.append(target)
        elif executor == "script":
            argv = [
                sys.executable,
                str(cli_common.SCRIPTS / "run-script.py"),
                "--run", str(run_dir),
                "--node", nid,
                "--session", holder["session"],
                "--holder-workspace", holder["workspace"],
                "--holder-pane", holder["pane"],
                "--epoch", str(args.epoch),
                "--lock-timeout", str(args.lock_timeout),
            ]
            res = cli_common.run_child(argv, "run-script.py")
            dispatched.append({**entry, "result": res})

    run_meta = ledger.load_run(run_dir)
    run_label = run_meta.get("id") or run_dir.name
    monitor_cmd: list[str] = []
    if herdr_targets:
        monitor_cmd = [
            sys.executable,
            str(cli_common.SCRIPTS / "monitor-agents.py"),
            *herdr_targets,
            "--notify-pane", holder["pane"],
            "--label", str(run_label),
        ]

    print(
        json.dumps(
            {
                "dispatched": dispatched,
                "skipped": skipped,
                "monitor_cmd": monitor_cmd,
            }
        )
    )
    return 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
