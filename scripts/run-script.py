#!/usr/bin/env python3
"""Execute a deterministic `script` graph node directly with no LLM pane.

Records `dispatch_intent` and `dispatched` under holder/epoch authorization,
runs the node's command with timeout and bounded stdout/stderr capture, writes
a hashed `script-result.json` receipt (plus declared `summary` outputs on
success), and records terminal `succeeded` or `failed` in the ledger.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import cli_common
import graph_model
import ioutil
import ledger
import scheduler

DEFAULT_TIMEOUT_SEC = 300
MAX_STREAM_BYTES = 8 * 1024
CLI_EXTRA_CODES = {RuntimeError: 1}


def _bound_text(text: str | None, max_bytes: int = MAX_STREAM_BYTES) -> str:
    """Keep up to max_bytes of UTF-8 text (head + tail split when truncated)."""
    if not text:
        return ""
    raw = text.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return text
    marker = b"\n...[truncated]...\n"
    budget = max(0, max_bytes - len(marker))
    head_len = budget // 2
    tail_len = budget - head_len
    head = raw[:head_len].decode("utf-8", errors="ignore")
    tail = raw[len(raw) - tail_len :].decode("utf-8", errors="ignore") if tail_len else ""
    return f"{head}\n...[truncated]...\n{tail}"


def _resolve_command(raw_command: Any) -> tuple[list[str], bool]:
    if isinstance(raw_command, list):
        return [str(part) for part in raw_command], False
    if isinstance(raw_command, str):
        return shlex.split(raw_command), False
    raise ioutil.PreconditionError("script node command must be str or list[str]")


def _resolve_cwd(run_dir: Path, plan: dict[str, Any], cli_cwd: str | None) -> str:
    raw = cli_cwd or plan.get("cwd")
    if isinstance(raw, str) and raw.strip():
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            run_meta = ledger.load_run(run_dir)
            base = run_meta.get("repo")
            if isinstance(base, str) and base:
                candidate = Path(base) / candidate
        resolved = Path(ioutil.canonical_path(candidate))
    else:
        run_meta = ledger.load_run(run_dir)
        repo = run_meta.get("repo")
        if not isinstance(repo, str) or not repo:
            raise ioutil.PreconditionError("cannot resolve script cwd: run has no repo and no --cwd/node.cwd given")
        resolved = Path(ioutil.canonical_path(repo))
    if not resolved.is_dir():
        raise ioutil.PreconditionError(f"script cwd does not exist: {resolved}")
    return str(resolved)


def _summary_artifacts(
    attempt_path: Path,
    graph: dict[str, Any],
    node_id: str,
    receipt: dict[str, Any],
    receipt_path: Path,
    receipt_hash: str,
) -> list[dict[str, Any]]:
    specs = graph_model.output_specs(graph, node_id)
    artifacts: list[dict[str, Any]] = []
    for name, out_type in specs.items():
        if out_type != "summary":
            raise ioutil.PreconditionError(
                f"script node {node_id} declares unsupported output type {out_type!r} for {name!r}"
            )
        filename = ledger._artifact_file_name(out_type)
        out_path = attempt_path / filename
        payload = {
            "node": node_id,
            "output": name,
            "type": out_type,
            "status": "succeeded",
            "exit_code": receipt["exit_code"],
            "duration_sec": receipt["duration_sec"],
            "command": receipt["command"],
            "cwd": receipt["cwd"],
            "script_receipt": {"path": str(receipt_path), "hash": receipt_hash},
            "stdout": receipt["stdout"],
            "stderr": receipt["stderr"],
        }
        ioutil.atomic_json(out_path, payload)
        artifacts.append(
            {
                "name": name,
                "type": out_type,
                "path": str(out_path),
                "hash": ioutil.sha256_file(out_path),
            }
        )
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        usage="run-script.py --run RUN --node NODE [--attempt A] --session S --holder-workspace W --holder-pane P --epoch E [--cwd DIR] [--lock-timeout SECONDS]",
    )
    parser.add_argument("--run", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--attempt", type=int, default=None)
    parser.add_argument("--session", required=True)
    parser.add_argument("--holder-workspace", required=True)
    parser.add_argument("--holder-pane", required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--cwd", default=None)
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    holder = cli_common.holder_from_args(args)
    run_dir = Path(ioutil.canonical_path(args.run))
    node_id = args.node

    snap = ledger.replay(run_dir)
    controller_epoch = int(snap["controller"]["epoch"])
    if controller_epoch != args.epoch:
        raise ioutil.PreconditionError(
            f"epoch {args.epoch} does not match controller epoch {controller_epoch}"
        )

    result = scheduler.reconcile(run_dir, holder=holder, lock_timeout=args.lock_timeout)
    state = result["state"]
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        raise ioutil.PreconditionError(f"unknown node {node_id!r}")

    graph = ledger.load_graph(run_dir)
    plan = graph.get("nodes", {}).get(node_id)
    if not isinstance(plan, dict):
        raise ioutil.PreconditionError(f"node {node_id!r} is absent from the current graph")
    if plan.get("executor") != "script":
        raise ioutil.PreconditionError(f"node {node_id} executor must be script, got {plan.get('executor')!r}")

    if node.get("state") != "ready":
        raise ioutil.PreconditionError(node.get("reason") or f"node {node_id} is {node.get('state')}, expected ready")

    current = int(node.get("attempt") or 0)
    expected = max(1, current)
    if args.attempt is not None and args.attempt != expected:
        raise ioutil.PreconditionError(
            f"attempt {args.attempt} does not match current attempt {expected}"
        )
    attempt = expected if args.attempt is None else args.attempt

    argv, _ = _resolve_command(plan.get("command"))
    cwd = _resolve_cwd(run_dir, plan, args.cwd)
    timeout_sec = int(plan.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)

    if not graph_model.has_current_dispatch_intent(node):
        ledger.append_event(
            run_dir,
            {"type": "dispatch_intent", "node": node_id, "attempt": attempt, "epoch": args.epoch},
            holder=holder,
            lock_timeout=args.lock_timeout,
        )

    ledger.append_event(
        run_dir,
        {
            "type": "dispatched",
            "node": node_id,
            "attempt": attempt,
            "epoch": args.epoch,
            "handle": {"executor": "script", "command": argv, "cwd": cwd},
            "model": {key: None for key in graph_model.MODEL_KEYS},
        },
        holder=holder,
        lock_timeout=args.lock_timeout,
    )

    attempt_path = ledger.attempt_dir(run_dir, node_id, attempt)
    attempt_path.mkdir(parents=True, exist_ok=True)
    receipt_path = attempt_path / "script-result.json"

    started = time.monotonic()
    timed_out = False
    exit_code: int | None = None
    stdout_text = ""
    stderr_text = ""
    exec_error: str | None = None

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout_sec,
            capture_output=True,
            text=True,
            check=False,
        )
        exit_code = int(proc.returncode)
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_raw = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout.decode("utf-8", errors="replace") if exc.stdout else "")
        stderr_raw = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr.decode("utf-8", errors="replace") if exc.stderr else "")
        stdout_text = stdout_raw
        stderr_text = stderr_raw
        exec_error = f"script timed out after {timeout_sec}s"
    except OSError as exc:
        exec_error = f"script execution error: {exc}"

    duration_sec = round(time.monotonic() - started, 6)
    receipt_payload: dict[str, Any] = {
        "node": node_id,
        "attempt": attempt,
        "command": argv,
        "cwd": cwd,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_sec": duration_sec,
        "stdout": _bound_text(stdout_text),
        "stderr": _bound_text(stderr_text),
    }
    if exec_error is not None:
        receipt_payload["error"] = exec_error

    ioutil.atomic_json(receipt_path, receipt_payload)
    receipt_hash = ioutil.sha256_file(receipt_path)
    receipt_ref = {"path": str(receipt_path), "hash": receipt_hash}

    if exit_code == 0 and not timed_out and exec_error is None:
        artifacts = _summary_artifacts(attempt_path, graph, node_id, receipt_payload, receipt_path, receipt_hash)
        appended = ledger.append_event(
            run_dir,
            {
                "type": "succeeded",
                "node": node_id,
                "attempt": attempt,
                "epoch": args.epoch,
                "artifacts": artifacts,
            },
            holder=holder,
            lock_timeout=args.lock_timeout,
        )
        final_state = appended["state"]["nodes"][node_id]["state"]
    else:
        if timed_out:
            reason = f"script_timeout: exceeded {timeout_sec}s"
        elif exec_error is not None:
            reason = f"script_error: {exec_error}"
        else:
            reason = f"script_exit_nonzero: exit_code={exit_code}"
        appended = ledger.append_event(
            run_dir,
            {
                "type": "failed",
                "node": node_id,
                "attempt": attempt,
                "epoch": args.epoch,
                "reason": reason,
            },
            holder=holder,
            lock_timeout=args.lock_timeout,
        )
        final_state = appended["state"]["nodes"][node_id]["state"]

    print(
        json.dumps(
            {
                "node": node_id,
                "attempt": attempt,
                "exit_code": exit_code,
                "duration_sec": duration_sec,
                "state": final_state,
                "receipt": receipt_ref,
            }
        )
    )
    return 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
