#!/usr/bin/env python3
"""Claude Code statusLine/hook producer for herdr-orchestrator context snapshots.

Reads Claude Code's JSON hook payload from stdin (containing `context_window`
and `model`) and atomically writes a schema_version=2 context snapshot under
`$XDG_STATE_HOME/herdr-orchestrator/context/<sha256(workspace + '\\n' + pane)>.json`
with mode 0600 inside a mode 0700 directory.

Never raises or prints errors to stdout so it cannot disrupt Claude Code.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import harnesses  # noqa: E402
import herdr_cli  # noqa: E402


def _write_snapshot(workspace: str, pane: str, payload: dict[str, Any]) -> Path:
    directory = harnesses.state_dir("context")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(directory)
    if not os.path.isdir(directory) or os.path.islink(directory) or st.st_uid != os.getuid():
        raise OSError("unsafe context snapshot directory")
    os.chmod(directory, 0o700)
    digest = hashlib.sha256(f"{workspace}\n{pane}".encode("utf-8")).hexdigest()
    destination = directory / f"{digest}.json"
    tmp = directory / f".{digest}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, destination)
    return destination


def build_snapshot(hook_data: dict[str, Any], *, workspace: str, pane: str, pid: int, now_ms: int | None = None) -> dict[str, Any]:
    cw = hook_data.get("context_window") if isinstance(hook_data.get("context_window"), dict) else {}
    window = cw.get("context_window_size") or cw.get("context_window")
    tokens = cw.get("total_input_tokens")
    if tokens is None and isinstance(cw.get("current_usage"), dict):
        cur = cw["current_usage"]
        parts = [cur.get(k) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
        if any(isinstance(v, (int, float)) for v in parts):
            tokens = sum(int(v) for v in parts if isinstance(v, (int, float)))
    if tokens is None and isinstance(cw.get("used_percentage"), (int, float)) and isinstance(window, (int, float)) and window > 0:
        tokens = int(round((float(cw["used_percentage"]) / 100.0) * float(window)))

    raw_model = hook_data.get("model")
    if isinstance(raw_model, dict):
        model_id = str(raw_model.get("id") or raw_model.get("display_name") or "unknown")
    elif isinstance(raw_model, str) and raw_model.strip():
        model_id = raw_model.strip()
    else:
        model_id = "unknown"

    available = isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and isinstance(window, (int, float)) and not isinstance(window, bool) and window > 0
    payload: dict[str, Any] = {
        "schema_version": 2,
        "harness": "claude",
        "source": harnesses.get_spec("claude").context_source,
        "workspace": workspace,
        "pane": pane,
        "pid": pid,
        "observed_at_ms": int(time.time() * 1000) if now_ms is None else now_ms,
        "available": bool(available),
        "model": {"provider": "anthropic", "id": model_id},
        "context_usage": {"tokens": int(tokens) if available else 0, "context_window": int(window) if available else 1},
    }
    session_id = hook_data.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        payload["session_id"] = session_id.strip()
    transcript = hook_data.get("transcript_path")
    if isinstance(transcript, str) and transcript.strip() and os.path.isfile(transcript):
        payload["session_file"] = os.path.realpath(transcript)
    return payload


def main() -> int:
    workspace, pane = herdr_cli.current_holder_env()
    if not workspace or not pane:
        return 0
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            return 0
        # Parent PID is the Claude Code foreground process invoking this hook.
        pid = os.getppid()
        snapshot = build_snapshot(data, workspace=workspace, pane=pane, pid=pid)
        _write_snapshot(workspace, pane, snapshot)
    except Exception:
        # Export failures must never disrupt the host harness.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
