#!/usr/bin/env python3
"""Single boundary for invoking the `herdr` CLI."""

from __future__ import annotations


import json
import subprocess
from pathlib import Path
from typing import Any
import ioutil
from cli_common import token_flag
from graph_model import PROVENANCE_ORCHESTRATOR


def runtime_agent_id(agent: dict[str, Any], target: str) -> str:
    """Require the server-issued identity used by user-owned pane dispatch."""
    agent_id = ioutil.first_str(agent, ("agent_id", "id"))
    if not agent_id:
        raise ioutil.PreconditionError(
            f"live agent missing stable id for {target}: user-owned pane dispatch "
            "requires server-issued agent_id/id; terminal_id, agent_session, names "
            "and pane addresses are not supported replacement-safe identities. "
            "Preflight with init-docs.py --pane TARGET before creating metadata. "
            "Restoring dispatch to this existing pane requires upstream Herdr "
            "agent-incarnation identity and identity-conditional prompt delivery; "
            "see references/identity-reattach.md#required-upstream-agent-incarnation-contract. "
            "Preflight refusal or creating a managed worker does not restore this pane."
        )
    return agent_id



TIMEOUT = ioutil.SUBPROCESS_TIMEOUT
READ_TIMEOUT = 10
# Longer than the 5000ms state-change window `herdr agent prompt --wait` enforces
# itself, so a stalled composer reports agent_prompt_stalled rather than timeout.
PROMPT_WAIT_MS = 15000
SESSIONS_DIR = Path.home() / ".config" / "herdr" / "sessions"
_UNCONFIRMED_CODES = ("agent_prompt_stalled", "timeout")


def _extract_error_code(stderr: str) -> str | None:
    raw = stderr.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    result = payload.get("result")
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
    return None


def running_sessions() -> list[str]:
    """Named sessions that currently have a server socket, sorted by name."""
    try:
        return sorted(sock.parent.name for sock in SESSIONS_DIR.glob("*/herdr.sock"))
    except OSError:
        return []


def _session_hint() -> str:
    # HERDR_SOCKET_PATH/HERDR_SESSION are not inherited by every caller (an Eval
    # kernel has neither), so "no server" usually means "wrong socket", not "no server".
    names = running_sessions()
    if not names:
        return ""
    return (
        f"; running named sessions: {', '.join(names)} "
        "(re-run with HERDR_SESSION=<name>, or herdr --session <name>)"
    )


def _run(args: tuple[str, ...]) -> subprocess.CompletedProcess:
    timeout = (
        READ_TIMEOUT
        if len(args) >= 2
        and args[0] in ("agent", "workspace", "pane")
        and args[1] in ("get", "list", "show")
        else TIMEOUT
    )
    try:
        proc = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ioutil.RunError("herdr not on PATH") from exc
    if proc.returncode != 0:
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        detail = err or out or f"rc={proc.returncode}"
        code = _extract_error_code(err) or _extract_error_code(out)
        if code == "server_not_running":
            detail += _session_hint()
        exc = ioutil.RunError(f"herdr {' '.join(args)} failed: {detail}")
        exc.code = code
        exc.args_tuple = args
        exc.returncode = proc.returncode
        exc.stdout = proc.stdout
        exc.stderr = proc.stderr
        raise exc
    return proc


def herdr(*args: str) -> dict[str, Any]:
    out = _run(args).stdout.strip()
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ioutil.RunError(f"herdr {' '.join(args)} returned non-JSON stdout: {out}") from exc
    if not isinstance(payload, dict):
        raise ioutil.RunError(f"herdr {' '.join(args)} returned unexpected output")
    return payload


def herdr_text(*args: str) -> str:
    """Raw stdout for subcommands that do not emit JSON (e.g. `agent read`)."""
    return _run(args).stdout


def _unconfirmed(exc: BaseException) -> bool:
    """True when herdr accepted the text but never observed the turn start."""
    return getattr(exc, "code", None) in _UNCONFIRMED_CODES


def deliver_prompt(call, target: str, text: str) -> None:
    """Submit a prompt through `call` and prove the agent picked it up.

    A bare `herdr agent prompt` exiting 0 only proves the CLI accepted the text:
    it can land in the composer as an unsubmitted attachment while the pane sits
    idle forever. `--wait --until working` makes the server observe the turn
    actually starting; `agent_prompt_stalled`/`timeout` means the composer still
    holds it, so submit the pending composer with Enter and observe again.
    Raises RunError with code `agent_not_ready` when the agent never leaves idle
    — an unconfirmed delivery is blocked, never dispatched.

    `call` is the caller's herdr invoker (`herdr_cli.herdr` in every script), so
    the whole sequence goes through one boundary.
    """
    wait = ("--until", "working", "--timeout", str(PROMPT_WAIT_MS))
    try:
        call("agent", "prompt", target, text, "--wait", *wait)
        return
    except RuntimeError as exc:
        if not _unconfirmed(exc):
            raise
    call("agent", "send-keys", target, "Enter")
    try:
        call("agent", "wait", target, *wait)
    except RuntimeError as exc:
        if not _unconfirmed(exc):
            raise
        unconfirmed = ioutil.RunError(
            f"agent {target} never left idle after its prompt was submitted: {exc}"
        )
        unconfirmed.code = "agent_not_ready"
        raise unconfirmed from exc


def stamp_workspace_tokens(
    ws_id: str,
    run_id: str | None = None,
    node: str | None = None,
    attempt_id: str | None = None,
    *,
    source: str = PROVENANCE_ORCHESTRATOR,
    tokens: tuple[str, ...] | list[str] = (),
    invoke=None,
) -> dict[str, Any]:
    """Stamp context and caller-specific tokens in one metadata operation."""
    argv = ["workspace", "report-metadata", ws_id, "--source", source]
    for token in tokens:
        argv.extend(("--token", token))
    for key, value in (("run", run_id), ("node", node), ("attempt", attempt_id)):
        if value is not None:
            argv.extend(("--token", token_flag(key, value)))
    return (invoke or herdr)(*argv)
