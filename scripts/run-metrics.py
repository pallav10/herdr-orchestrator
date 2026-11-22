#!/usr/bin/env python3
"""Compute post-run duration, retry, and optional token/cost metrics from events.jsonl.

Usage: run-metrics.py --run RUN [--write]

Read-only replay of `<run>/events.jsonl` and attempt artifacts. With `--write`,
atomically writes `<run>/metrics.json`. Never fabricates token/cost numbers
when OMP session JSONL logs are absent or lack usage records.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
from pathlib import Path
from typing import Any

import cli_common
import graph_model
import ioutil
import ledger

TERMINAL_EVENTS = frozenset({"succeeded", "failed", "interrupted", "skipped", "invalidated"})
ACTIVE_START_EVENTS = frozenset({"dispatched", "attempt_started"})
SESSION_KEY_CANDIDATES = ("session_path", "agent_session", "session_jsonl")


def _parse_ts(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def _round_sec(value: float | None) -> float | None:
    return None if value is None else round(max(0.0, value), 6)


def _extract_session_path(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in SESSION_KEY_CANDIDATES:
        val = payload.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            inner = val.get("value")
            if isinstance(inner, str) and inner:
                return inner
    return None


def _find_attempt_session_path(run_dir: Path, node_id: str, attempt: int) -> Path | None:
    try:
        adir = ledger.attempt_dir(run_dir, node_id, attempt)
    except ioutil.RunError:
        return None
    for name in ("summary.json", "dispatch-identity.json"):
        path = adir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        candidate = _extract_session_path(data)
        if candidate:
            p = Path(candidate).expanduser()
            if p.is_file():
                return p
    return None


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _parse_session_usage(session_path: Path) -> dict[str, Any] | None:
    """Parse optional OMP session JSONL usage/cost entries; return None if no usage data found."""
    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    total_tokens = 0
    cost_usd = 0.0
    has_tokens = False
    has_cost = False
    try:
        with session_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                msg = entry.get("message") if isinstance(entry.get("message"), dict) else entry
                usage = msg.get("usage") if isinstance(msg.get("usage"), dict) else None
                if usage is None:
                    continue
                inp = _num(usage.get("input"))
                if inp is None:
                    inp = _num(usage.get("input_tokens"))
                out = _num(usage.get("output"))
                if out is None:
                    out = _num(usage.get("output_tokens"))
                cr = _num(usage.get("cacheRead"))
                if cr is None:
                    cr = _num(usage.get("cache_read_input_tokens"))
                cw = _num(usage.get("cacheWrite"))
                if cw is None:
                    cw = _num(usage.get("cache_creation_input_tokens"))
                tot = _num(usage.get("totalTokens"))
                if tot is None:
                    tot = _num(usage.get("total_tokens"))
                if any(v is not None for v in (inp, out, cr, cw, tot)):
                    has_tokens = True
                    input_tokens += int(inp or 0)
                    output_tokens += int(out or 0)
                    cache_read_tokens += int(cr or 0)
                    cache_write_tokens += int(cw or 0)
                    total_tokens += int(tot) if tot is not None else int((inp or 0) + (out or 0) + (cr or 0) + (cw or 0))
                cost_obj = usage.get("cost")
                if isinstance(cost_obj, dict):
                    c = _num(cost_obj.get("total"))
                    if c is not None:
                        has_cost = True
                        cost_usd += c
                else:
                    c = _num(cost_obj)
                    if c is not None:
                        has_cost = True
                        cost_usd += c
    except OSError:
        return None
    if not has_tokens and not has_cost:
        return None
    return {
        "input_tokens": input_tokens if has_tokens else None,
        "output_tokens": output_tokens if has_tokens else None,
        "cache_read_tokens": cache_read_tokens if has_tokens else None,
        "cache_write_tokens": cache_write_tokens if has_tokens else None,
        "total_tokens": total_tokens if has_tokens else None,
        "cost_usd": round(cost_usd, 6) if has_cost else None,
    }


def compute_metrics(run_dir: str | Path) -> dict[str, Any]:
    run_path = Path(ioutil.canonical_path(run_dir))
    run_info = ledger.load_run(run_path)
    snap = ledger.replay(run_path)
    events = snap["events"]
    state_nodes = snap["state"].get("nodes") or {}

    all_ts = [ts for ev in events if (ts := _parse_ts(ev.get("ts"))) is not None]
    total_wall_sec = _round_sec(max(all_ts) - min(all_ts)) if len(all_ts) >= 2 else (0.0 if len(all_ts) == 1 else None)

    node_events: dict[str, list[dict[str, Any]]] = {nid: [] for nid in state_nodes}
    for ev in events:
        nid = ev.get("node")
        if isinstance(nid, str) and nid:
            node_events.setdefault(nid, []).append(ev)

    nodes_out: dict[str, Any] = {}
    attempts_total = 0
    retries_total = 0
    attempts_with_tokens = 0

    for nid in sorted(node_events):
        evs = node_events[nid]
        by_attempt: dict[int, list[dict[str, Any]]] = {}
        for ev in evs:
            raw_att = ev.get("attempt")
            if isinstance(raw_att, int) and not isinstance(raw_att, bool) and raw_att >= 1:
                by_attempt.setdefault(raw_att, []).append(ev)

        attempt_metrics: list[dict[str, Any]] = []
        node_active = 0.0
        node_wait = 0.0
        has_active = False
        has_wait = False
        failures = 0
        blocks = 0
        node_ts = [ts for ev in evs if (ts := _parse_ts(ev.get("ts"))) is not None]
        node_wall = _round_sec(max(node_ts) - min(node_ts)) if len(node_ts) >= 2 else (0.0 if len(node_ts) == 1 else None)

        for att in sorted(by_attempt):
            aevs = by_attempt[att]
            ats = [ts for ev in aevs if (ts := _parse_ts(ev.get("ts"))) is not None]
            att_wall = _round_sec(max(ats) - min(ats)) if len(ats) >= 2 else (0.0 if len(ats) == 1 else None)

            active_sec = 0.0
            wait_sec = 0.0
            att_has_active = False
            att_has_wait = False
            mode: str | None = None  # "active" | "wait"
            mode_start: float | None = None
            att_outcome: str | None = None
            att_blocks = 0

            for ev in aevs:
                typ = ev.get("type")
                ts = _parse_ts(ev.get("ts"))
                if typ == "blocked":
                    blocks += 1
                    att_blocks += 1
                if typ in {"failed", "interrupted"}:
                    failures += 1
                if typ in TERMINAL_EVENTS:
                    att_outcome = typ

                if ts is not None:
                    if mode is not None and mode_start is not None:
                        delta = max(0.0, ts - mode_start)
                        if mode == "active":
                            active_sec += delta
                            att_has_active = True
                        elif mode == "wait":
                            wait_sec += delta
                            att_has_wait = True
                    if typ in ACTIVE_START_EVENTS:
                        mode = "active"
                        mode_start = ts
                    elif typ in {"ready", "dispatch_intent", "blocked"}:
                        mode = "wait"
                        mode_start = ts
                    elif typ in TERMINAL_EVENTS:
                        mode = None
                        mode_start = None

            if att_has_active:
                node_active += active_sec
                has_active = True
            if att_has_wait:
                node_wait += wait_sec
                has_wait = True

            session_path = _find_attempt_session_path(run_path, nid, att)
            usage = _parse_session_usage(session_path) if session_path is not None else None
            if usage is not None:
                attempts_with_tokens += 1

            attempt_metrics.append({
                "attempt": att,
                "outcome": att_outcome,
                "duration_sec": att_wall,
                "active_sec": _round_sec(active_sec) if att_has_active else None,
                "wait_sec": _round_sec(wait_sec) if att_has_wait else None,
                "blocks": att_blocks,
                "usage": usage,
            })

        att_count = len(by_attempt)
        retry_count = max(0, att_count - 1)
        attempts_total += att_count
        retries_total += retry_count

        state_node = state_nodes.get(nid) or {}
        nodes_out[nid] = {
            "state": state_node.get("state"),
            "wall_sec": node_wall,
            "active_sec": _round_sec(node_active) if has_active else None,
            "wait_sec": _round_sec(node_wait) if has_wait else None,
            "attempts": attempt_metrics,
            "attempt_count": att_count,
            "retries": retry_count,
            "failures": failures,
            "blocks": blocks,
        }

    token_coverage = round(attempts_with_tokens / attempts_total, 4) if attempts_total > 0 else 0.0

    return {
        "run_id": run_info.get("id") or run_path.name,
        "total_wall_sec": total_wall_sec,
        "node_count": len(nodes_out),
        "attempts_total": attempts_total,
        "retries_total": retries_total,
        "nodes": nodes_out,
        "token_coverage": token_coverage,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="canonical absolute path to run directory")
    parser.add_argument("--write", action="store_true", help="atomically write <run>/metrics.json")
    args = parser.parse_args()

    run_path = Path(ioutil.canonical_path(args.run))
    metrics = compute_metrics(run_path)
    if args.write:
        ioutil.atomic_json(run_path / "metrics.json", metrics)
    print(json.dumps(metrics))
    return 0


if __name__ == "__main__":
    cli_common.run(main)
