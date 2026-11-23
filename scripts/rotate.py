#!/usr/bin/env python3
"""Spawn and verify a successor orchestrator without retiring the predecessor.

Usage: rotate.py --session PATH [--label successor] [--model-role default]
                  [--timeout 300] [--lock-timeout SECONDS]

--session names the predecessor session directory (required). Preconditions
(exit 3): that session resolves, HANDOFF.md and NEXT-SESSION-PROMPT.md exist,
and session.json names an existing canonical run. Measured current context usage must then satisfy
tokens * 2 >= context_window (exactly 50% accepted). Unknown, low, stale, or
invalid usage exits 3 before drain, reconcile, spawn, or prompt. There is no
bypass. It then marks the run lease draining, resumes an identical in-progress
drain, waits until native task/workflow work is quiesced, and fails closed
without spawning if native work stays live or ambiguous through --timeout.
The successor acknowledges with ack-handoff.py and its exact nonce; rotate polls successor workspace tokens until handoff-ack matches.
Prints one JSON line with successor, predecessor, predecessor session id, run,
and context_usage.
Exit codes: 0 success, 1 runtime failure, 2 usage, 3 domain condition.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex

import sys
import time
import uuid
from typing import Any

import graph_model
import ioutil
import cli_common
import authz

CLI_EXTRA_CODES = {RuntimeError: 1}
import ledger
import liveness
import scheduler
import session_docs
import context_usage
import run_store

from herdr_cli import current_holder_env, herdr
from graph_model import AGENT_NAME_RE, LEASE_ACTIVE, LEASE_DRAINING, PROVENANCE_ORCHESTRATOR
from cli_common import SCRIPTS, run_child
import reconcile


def _print_context_refusal(exc: context_usage.ContextUsageError) -> None:
    print(
        json.dumps(
            {
                "fraction": exc.fraction,
                "source": exc.source,
                "reason": exc.reason,
                "guidance": exc.guidance,
            }
        ),
        file=sys.stderr,
    )

def validate_spawn_output(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("name", "workspace", "pane"):
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"spawn-agent.py output missing {key!r}")
    return payload


def _string_dict(raw: object) -> dict[str, str]:
    found: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and isinstance(value, str):
                found[key] = value
    return found


def predecessor_meta(session: Path) -> dict[str, Any]:
    workspace, pane = current_holder_env()
    session_id = ioutil.canonical_path(session)
    session_docs.holder_identity(session, {"workspace": workspace, "pane": pane, "session": session_id})
    provenance = "user-owned"
    fetched = herdr("workspace", "get", workspace).get("result", {})
    raw_ws = fetched.get("workspace")
    ws = raw_ws if isinstance(raw_ws, dict) else {}
    tokens = _string_dict(ws.get("tokens"))
    if tokens.get("provenance") == PROVENANCE_ORCHESTRATOR:
        provenance = PROVENANCE_ORCHESTRATOR
    return {
        "workspace": workspace,
        "pane": pane,
        "session": session_id,
        "provenance": provenance,
        "retire": "close" if provenance == PROVENANCE_ORCHESTRATOR else "stand-down",
    }


def check_docs(session: Path) -> None:
    missing = [str(session / name) for name in session_docs.BOOTSTRAP_NAMES if not (session / name).is_file()]
    if missing:
        raise ioutil.PreconditionError(f"missing docs: {', '.join(missing)}")

def start_lease_drain(run: Path, holder: dict[str, str], *, handoff: str | None = None, lock_timeout=120) -> dict[str, Any]:
    with ledger.with_lock(run, lock_timeout=lock_timeout):
        snap = ledger.replay(run)
        controller = snap["controller"]
        if controller.get("lease") == LEASE_DRAINING and graph_model.holder_exact(controller, holder):
            return snap
        payload: dict[str, Any] = {"type": "lease_drain_started", **holder}
        if isinstance(handoff, str) and handoff.strip():
            payload["handoff"] = handoff.strip()
        return ledger.append_event_locked(
            run,
            payload,
            holder=holder,
        )

def ensure_native_quiesced(run: Path, holder: dict[str, str], timeout: int, *, lock_timeout=120) -> None:
    deadline = time.time() + timeout
    first = True
    previous_live = None
    while True:
        live_herdr = reconcile.observe_live_herdr(run=run, lock_timeout=lock_timeout)
        live_set = None if live_herdr is None else tuple(sorted(json.dumps(item, sort_keys=True) for item in live_herdr))
        if first or live_set != previous_live:
            scheduler.reconcile(
                run, current_session=holder["session"], holder=holder,
                live_herdr=live_herdr, live_native_ids=None,
                native_inventory_complete=False, lock_timeout=lock_timeout,
            )
        first = False
        previous_live = live_set
        state = ledger.replay(run)["state"]
        unknown = live_herdr is None and not liveness.native_quiesced(state, graph_model.HERDR_EXECUTORS)
        if liveness.native_quiesced(state) and not unknown:
            result = scheduler.reconcile(
                run, current_session=holder["session"], holder=holder,
                live_herdr=live_herdr, live_native_ids=None,
                native_inventory_complete=False, lock_timeout=lock_timeout,
            )
            state = result["state"]
            if liveness.native_quiesced(state) and (live_herdr is not None or liveness.native_quiesced(state, graph_model.HERDR_EXECUTORS)):
                return
        remaining = deadline - time.time()
        if remaining <= 0:
            if unknown:
                raise ioutil.PreconditionError("active Herdr executors cannot be observed")
            raise ioutil.PreconditionError("native executors are not quiesced")
        time.sleep(min(1.0, remaining))

def successor_transfer_matches(run: Path, successor: dict[str, Any], nonce: str, tokens: dict[str, str], *, lock_timeout=120) -> bool:
    with ledger.with_lock(run, lock_timeout=lock_timeout):
        snap = ledger.replay(run)
    controller = snap["controller"]
    controller_session = controller.get("session")
    if (
        controller.get("lease") != LEASE_ACTIVE
        or controller.get("workspace") != successor["workspace"]
        or controller.get("pane") != successor["pane"]
    ):
        return False
    if tokens.get("handoff-ack") != nonce:
        return False
    token_session = tokens.get("session")
    if token_session not in {
        controller_session,
        Path(str(controller_session)).name,
    }:
        return False
    for event in reversed(snap["events"]):
        if event.get("type") != "lease_transfer" or event.get("nonce") != nonce:
            continue
        return (
            event.get("workspace") == successor["workspace"]
            and event.get("pane") == successor["pane"]
            and event.get("session") == controller_session
        )
    return False


def successor_prompt(session: Path, ack_script: Path, nonce: str, run: Path, *, lock_timeout=120) -> str:
    reconcile_script = ack_script.with_name("reconcile.py")
    run_abs = str(run)
    return (
        "You are the successor orchestrator pane. Ground first, then take over dispatch.\n"
        "1) Read these docs in order:\n"
        f"   - {session / 'NEXT-SESSION-PROMPT.md'}\n"
        f"   - {session / 'HANDOFF.md'}\n"
        f"   - {session / session_docs.META_NAME} run: {run_abs}\n"
        "2) Run this acknowledgment command first; it prints JSON with absolute successor "
        "session, session_id, workspace, and run:\n"
        f"   python3 {ack_script} --nonce {nonce} --session {shlex.quote(str(session))} --lock-timeout {lock_timeout}\n"
        "3) Reconcile the acknowledgment JSON's canonical run and concrete successor session with this pane's holder identity:\n"
        f"   python3 {reconcile_script} --run <ack JSON run> --session <ack JSON session> "
        f"--holder-workspace \"$HERDR_WORKSPACE_ID\" --holder-pane \"$HERDR_PANE_ID\" --lock-timeout {lock_timeout}\n"
        "The nonce is "
        f"{nonce}. Ack rolls your own session directory bound to run {run_abs}; write only there\n"
        "from then on, and never follow docs/sessions/latest.\n"
        "4) Confirm ack succeeded. Reattach live Herdr workers by workspace, pane, agent, and "
        "tokens; do not redispatch them. Then continue orchestration."
    )

def predecessor_meta_for_run(run: Path) -> dict[str, Any]:
    workspace, pane = current_holder_env()
    if not workspace or not pane:
        raise ioutil.PreconditionError("HERDR_WORKSPACE_ID and HERDR_PANE_ID (or HC_TERMINAL_ID) are required")
    snap = ledger.replay(run)
    controller = snap["controller"]
    session_id = controller.get("session")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ioutil.PreconditionError("run has no bound controller session")
    holder = {"workspace": workspace, "pane": pane, "session": session_id}
    if not graph_model.holder_exact(controller, holder):
        raise ioutil.PreconditionError("rotate --run requires the exact active controller holder")
    provenance = "user-owned"
    fetched = herdr("workspace", "get", workspace).get("result", {})
    raw_ws = fetched.get("workspace")
    ws = raw_ws if isinstance(raw_ws, dict) else {}
    tokens = _string_dict(ws.get("tokens"))
    if tokens.get("provenance") == PROVENANCE_ORCHESTRATOR:
        provenance = PROVENANCE_ORCHESTRATOR
    return {
        "workspace": workspace,
        "pane": pane,
        "session": session_id,
        "provenance": provenance,
        "retire": "close" if provenance == PROVENANCE_ORCHESTRATOR else "stand-down",
    }


def synthesize_successor_prompt(run: Path, ack_script: Path, nonce: str, *, lock_timeout=120) -> str:
    """Synthesize a self-contained successor bootstrap prompt directly from run.db state and notes."""
    ho_script = ack_script.with_name("ho.py")
    snap = ledger.replay(run)
    run_doc = ledger.load_run(run)
    notes = ledger.list_notes(run)
    brief = run_store.synthesize_brief(
        run,
        run=run_doc,
        state=snap["state"],
        controller=snap["controller"],
        notes=notes,
    )
    run_abs = str(run)
    return (
        "You are the successor orchestrator pane. Ground first, then take over dispatch.\n\n"
        "1) Acknowledge the controller lease transfer first:\n"
        f"   python3 {ack_script} --nonce {nonce} --run {shlex.quote(run_abs)} --lock-timeout {lock_timeout}\n"
        "2) Reconcile and inspect next actions using the unified CLI:\n"
        f"   python3 {ho_script} next --reconcile --run {shlex.quote(run_abs)}\n"
        "   (Re-print this brief anytime with: "
        f"python3 {ho_script} brief --md --run {shlex.quote(run_abs)})\n"
        "3) Confirm ack succeeded. Reattach live Herdr workers by recorded identity and tokens; "
        "do not redispatch them. Continue orchestration.\n\n"
        "--- Synthesized Run Brief (authoritative from run.db) ---\n"
        f"{brief['markdown']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="successor")
    parser.add_argument("--kind", default=None, help="successor harness kind (defaults to predecessor/env/omp)")
    parser.add_argument("--model-role", default="default")
    parser.add_argument("--model", default=None, help="explicit successor model (overrides --model-role)")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--session", default=None, help="legacy predecessor session directory")
    target.add_argument("--run", default=None, help="canonical run directory (zero-md SQLite rotation)")
    parser.add_argument("--focus", default=None, help="handoff summary / current focus recorded on drain")
    parser.add_argument("--timeout", type=int, default=300)
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    if not AGENT_NAME_RE.match(args.label):
        parser.error(f"--label {args.label!r} must match {AGENT_NAME_RE.pattern}")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")

    spawn_script = SCRIPTS / "spawn-agent.py"
    ack_script = SCRIPTS / "ack-handoff.py"
    if not spawn_script.is_file():
        raise RuntimeError(f"missing script: {spawn_script}")
    if not ack_script.is_file():
        raise RuntimeError(f"missing script: {ack_script}")

    if args.run:
        run = Path(ioutil.canonical_path(args.run))
        if not run_store.has_db(run) and not (run / "run.json").is_file():
            raise ioutil.PreconditionError(f"run not found: {run}")
        session = None
        predecessor = predecessor_meta_for_run(run)
        holder = {key: predecessor[key] for key in authz.HOLDER_KEYS}
        try:
            usage = context_usage.require_context_usage(None, holder)
        except context_usage.ContextUsageError as exc:
            _print_context_refusal(exc)
            return 3
        start_lease_drain(run, holder, handoff=args.focus, lock_timeout=args.lock_timeout)
        ensure_native_quiesced(run, holder, args.timeout, lock_timeout=args.lock_timeout)
        nonce = uuid.uuid4().hex
        prompt = synthesize_successor_prompt(run, ack_script.resolve(), nonce, lock_timeout=args.lock_timeout)
        session_label = predecessor["session"]
    else:
        try:
            session = session_docs.load_session(Path(args.session))
        except RuntimeError as exc:
            raise ioutil.PreconditionError(str(exc)) from exc
        check_docs(session)
        run = session_docs.session_run(session)
        predecessor = predecessor_meta(session)
        holder = {key: predecessor[key] for key in authz.HOLDER_KEYS}
        try:
            usage = context_usage.require_context_usage(session, holder)
        except context_usage.ContextUsageError as exc:
            _print_context_refusal(exc)
            return 3
        start_lease_drain(run, holder, handoff=args.focus, lock_timeout=args.lock_timeout)
        ensure_native_quiesced(run, holder, args.timeout, lock_timeout=args.lock_timeout)
        nonce = uuid.uuid4().hex
        prompt = successor_prompt(session, ack_script.resolve(), nonce, run, lock_timeout=args.lock_timeout)
        session_label = session.name

    import harnesses
    succ_kind = harnesses.default_kind(explicit=args.kind, detected_kind=usage.get("harness"))
    cmd = [
        sys.executable,
        str(spawn_script),
        args.label,
        "--kind",
        succ_kind,
    ]
    if args.model:
        cmd += ["--model", args.model]
    elif args.model_role:
        cmd += ["--model-role", args.model_role]
    if harnesses.get_spec(succ_kind).orchestrate_default:
        cmd.append("--orchestrate")
    cmd += [
        "--prompt",
        prompt,
        "--lock-timeout", str(args.lock_timeout),
    ]
    spawned = validate_spawn_output(run_child(cmd, "spawn-agent.py"))
    successor = {
        "name": spawned["name"],
        "workspace": spawned["workspace"],
        "pane": spawned["pane"],
    }

    deadline = time.time() + args.timeout
    acked = False
    while time.time() <= deadline:
        fetched = herdr("workspace", "get", successor["workspace"]).get("result", {})
        raw_ws = fetched.get("workspace")
        ws = raw_ws if isinstance(raw_ws, dict) else {}
        tokens = _string_dict(ws.get("tokens"))
        if successor_transfer_matches(run, successor, nonce, tokens, lock_timeout=args.lock_timeout):
            acked = True
            break
        if tokens.get("handoff-ack") == nonce:
            raise ioutil.PreconditionError("handoff ack does not match spawned successor")
        time.sleep(5.0)

    result = {
        "successor": successor,
        "acked": acked,
        "nonce": nonce,
        "predecessor": predecessor,
        "session": session_label,
        "run": str(run),
        "context_usage": usage,
    }
    print(json.dumps(result))
    return 0 if acked else 3


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
