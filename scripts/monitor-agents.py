#!/usr/bin/env python3
"""Poll `herdr agent list` until every named agent settles.

Usage: monitor-agents.py NAME [NAME ...] [--interval SECS] [--min-interval SECS]
 [--grace SECS] [--timeout SECS] [--notify-pane PANE_ID]
 [--notify-on final|edge] [--label TEXT] [--stale SECS] [--every-poll]

Prints JSON lines when states, pending, or stale agents change; --every-poll
prints every poll. Cadence starts/resets at --min-interval (2 seconds) and
doubles unchanged up to --interval (30 seconds). Poll rows contain
{"t": epoch, "states": {name: status}, "pending": [...],
 "last_activity": {name: epoch|null}, "idle_for": {name: secs|null}}.
Up to three consecutive transient agent-list failures are retried.
Exit codes: 0 = all named agents settled (idle/done/blocked),
            1 = runtime failure (herdr unreachable or unexpected output),
            2 = timeout with agents still unsettled,
            3 = a named agent vanished (released/exited) before settling.

Designed to run in the BACKGROUND so the orchestrator pane stays free. Two wake
channels exist; use at least one, otherwise settlement is only visible by reading
the JSONL file:
  * launch it as a harness async job whose exit auto-delivers (preferred), or
  * `--notify-pane <controller pane id>`: on the terminal outcome (and, with
    `--notify-on edge`, on every agent's first settled observation) the monitor
    types one line into that pane via `herdr pane run`, which the controller
    receives as a user message. `--label` prefixes the line (run id, batch name).
Settled Herdr states are observation only, never run success.
"""

from __future__ import annotations

import argparse
import errno
import json
import subprocess
import sys
import time
from pathlib import Path

import ioutil
import cli_common

CLI_EXTRA_CODES = {RuntimeError: 1}
from herdr_cli import herdr

from graph_model import SETTLED


def agent_states() -> dict[str, dict]:
    payload = herdr("agent", "list")
    try:
        agents = payload["result"]["agents"]
        if not isinstance(agents, list):
            raise TypeError("agents must be a list")
        result = {}
        for a in agents:
            if not isinstance(a, dict):
                raise TypeError("agent must be an object")
            name = a.get("name")
            if not name:
                continue
            status = a["agent_status"]
            if not isinstance(name, str) or not isinstance(status, str):
                raise TypeError("agent name and status must be strings")
            sess = a.get("agent_session")
            value = sess.get("value") if isinstance(sess, dict) else None
            result[name] = {
                "status": status,
                "session": value if isinstance(value, str) else None,
            }
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"herdr agent list returned unexpected output: {str(payload)[:200]}") from exc
    return result


def last_activity(session: str | None) -> float | None:
    """Newest mtime of the session file and its sibling *.jsonl logs.

    Every OSError below (session file gone, log dir unreadable, log rotated
    mid-glob) means "no activity signal available" for this poll, not a fault:
    the monitor loop must keep running, so each is absorbed and reported as None.
    """
    if not session:
        return None
    mtimes = []
    path = Path(session)
    try:
        mtimes.append(path.stat().st_mtime)
    except OSError:
        pass
    try:
        for child in path.with_suffix("").glob("*.jsonl"):
            try:
                mtimes.append(child.stat().st_mtime)
            except OSError:
                pass
    except OSError:
        pass
    return max(mtimes) if mtimes else None


def notify(pane: str | None, label: str, text: str) -> None:
    if not pane:
        return
    line = f"[monitor{(' ' + label) if label else ''}] {text}"
    # Notification is best-effort: a dead/renamed pane or a wedged herdr must never
    # kill the monitor loop, so every failure is reported on stdout and swallowed.
    try:
        herdr("pane", "run", pane, line)
    except (ioutil.RunError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"notify_failed": pane, "detail": f"{type(exc).__name__}: {exc}"[:200]}), flush=True)


def summarize(named: dict[str, str]) -> str:
    counts: dict[str, int] = {}
    for state in named.values():
        counts[state] = counts.get(state, 0) + 1
    return " ".join(f"{k}:{v}" for k, v in sorted(counts.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="+", help="live agent names to watch")
    parser.add_argument("--interval", type=float, default=30.0, help="maximum poll interval")
    parser.add_argument("--min-interval", type=float, default=2.0, help="initial/reset poll interval")
    parser.add_argument("--grace", type=float, default=8.0,
                        help="settled states observed within the first GRACE seconds stay pending "
                             "(covers the prompt->working transition window)")
    parser.add_argument("--timeout", type=float, default=3600.0)
    parser.add_argument("--notify-pane", default=None, help="controller pane id to wake (e.g. w1:p1)")
    parser.add_argument("--notify-on", choices=("final", "edge"), default="final",
                        help="final: one line at exit; edge: also one line per agent's first settled observation")
    parser.add_argument("--label", default="", help="short prefix for notification lines (run id, batch name)")
    parser.add_argument("--stale", type=float, default=None,
                        help="emit STALE/ACTIVE when an unsettled agent is idle this many seconds")
    parser.add_argument("--every-poll", action="store_true",
                        help="print regular poll lines even when states are unchanged")
    args = parser.parse_args()

    start = time.time()
    deadline = start + args.timeout
    announced: set[str] = set()
    prev_stale: set[str] = set()
    prev_key = None
    interval = min(args.min_interval, args.interval)
    failures = 0
    while True:
        try:
            raw = agent_states()
        except (ioutil.RunError, OSError, subprocess.TimeoutExpired) as exc:
            transient = (
                isinstance(exc, subprocess.TimeoutExpired)
                or isinstance(exc, OSError) and exc.errno in (errno.ECONNRESET, errno.ECONNREFUSED, errno.ETIMEDOUT, errno.EPIPE)
                or isinstance(exc, ioutil.RunError) and getattr(exc, "code", None) in ("timeout", "connection_reset", "connection_refused")
            )
            failures += 1
            if not transient or failures > 3 or time.time() > deadline:
                if isinstance(exc, ioutil.RunError):
                    raise
                raise RuntimeError(str(exc)) from exc
            time.sleep(interval)
            continue
        failures = 0
        now = time.time()
        named = {}
        sessions = {}
        activity = {}
        idle_for = {}
        for n in args.names:
            info = raw.get(n)
            if info is None:
                named[n] = "gone"
                sessions[n] = None
            else:
                named[n] = info["status"]
                sessions[n] = info.get("session")
            act = last_activity(sessions[n])
            activity[n] = int(act) if act is not None else None
            idle_for[n] = int(now - act) if act is not None else None
        in_grace = (now - start) < args.grace
        pending = sorted(
            n for n, s in named.items()
            if (s not in SETTLED and s != "gone") or (in_grace and s in SETTLED)
        )
        stale = set()
        if args.stale is not None:
            for n, s in named.items():
                idle_n = idle_for[n]
                if s not in SETTLED and s != "gone" and idle_n is not None and idle_n >= args.stale:
                    stale.add(n)
        key = (named, pending, sorted(stale))
        changed = prev_key is None or key != prev_key
        interval = min(args.min_interval, args.interval) if changed else min(interval * 2, args.interval)
        if args.every_poll or changed:
            print(json.dumps({
                "t": int(now),
                "states": named,
                "pending": pending,
                "last_activity": activity,
                "idle_for": idle_for,
            }), flush=True)
        prev_key = key
        if args.stale is not None:
            for name in sorted(stale - prev_stale):
                print(json.dumps({
                    "t": int(now),
                    "STALE": name,
                    "idle_for": idle_for[name],
                    "session": sessions[name],
                }), flush=True)
                notify(args.notify_pane, args.label, f"{name} STALE idle {idle_for[name]}s")
            for name in sorted(prev_stale - stale):
                print(json.dumps({"t": int(now), "ACTIVE": name}), flush=True)
            prev_stale = stale
        if args.notify_on == "edge" and not in_grace:
            for name, state in named.items():
                if name not in announced and (state in SETTLED or state == "gone"):
                    announced.add(name)
                    notify(args.notify_pane, args.label, f"{name} {state}; pending {len(pending)}/{len(named)}")
        if not pending:
            ok = all(s in SETTLED for s in named.values())
            notify(args.notify_pane, args.label,
                   f"{'all settled' if ok else 'agent vanished'} ({summarize(named)}); collect now")
            return 0 if ok else 3
        if now > deadline:
            print(json.dumps({"timeout": True, "states": named}), flush=True)
            notify(args.notify_pane, args.label, f"timeout after {int(args.timeout)}s; still pending: {', '.join(pending)}")
            return 2
        time.sleep(interval)


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
