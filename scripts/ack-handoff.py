#!/usr/bin/env python3
"""Validate a named predecessor session, roll a successor session, stamp ack.

Usage: ack-handoff.py --nonce N --session PATH [--lock-timeout SECONDS]

--session is the concrete predecessor session directory (never a pointer):
  - HANDOFF.md and NEXT-SESSION-PROMPT.md exist
  - session.json binds an existing canonical run under the same docs root
Only after all checks pass, opens a fresh session carried forward from that
predecessor and bound to the same run, transfers the run lease, then stamps
current workspace metadata with:
  handoff-ack=<nonce>, session=<new session id>
A rerun with the same nonce after a successful transfer reuses that successor
and retries stamping; it does not open another session or append another transfer.
Exit codes: 0 ok, 1 runtime failure, 2 usage, 3 metadata/resource precondition or lease refusal.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import ioutil
import cli_common
import authz

CLI_EXTRA_CODES = {RuntimeError: 1}
import ledger
import session_docs
import run_store
import state_paths

from graph_model import LEASE_ACTIVE, LEASE_DRAINING
from cli_common import SCRIPTS
from herdr_cli import current_holder_env, herdr, stamp_workspace_tokens


def read_required(path: Path) -> str:
    if not path.is_file():
        raise ioutil.PreconditionError(f"missing required file: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ioutil.PreconditionError(f"cannot read {path}: {exc}") from exc

def predecessor_holder(controller: dict[str, Any], predecessor: Path, meta: dict[str, Any]) -> dict[str, Any]:
    if controller.get("lease") != LEASE_DRAINING:
        raise ioutil.PreconditionError("lease_transfer requires draining current holder")
    holder = {
        "workspace": meta.get("workspace"),
        "pane": meta.get("pane"),
        "session": ioutil.canonical_path(predecessor),
    }
    if any(
        not isinstance(holder.get(key), str) or not holder[key].strip()
        for key in authz.HOLDER_KEYS
    ):
        raise ioutil.PreconditionError("lease_transfer requires predecessor holder")
    for key in authz.HOLDER_KEYS:
        recorded = controller.get(key)
        if recorded is not None and holder[key] != recorded:
            raise ioutil.PreconditionError("lease_transfer requires predecessor holder")
    return holder


def transferred_successor(run: Path, predecessor: Path, nonce: str, workspace: str, pane: str) -> dict[str, Any] | None:
    snap = ledger.replay(run)
    controller = snap["controller"]
    # lease-state only: this inspects the *successor's* claim, so the caller is not the holder here
    if controller.get("lease") != LEASE_ACTIVE:
        return None
    transfers = [event for event in snap["events"] if event.get("type") == "lease_transfer"]
    if not transfers:
        return None
    latest = transfers[-1]
    if latest.get("nonce") != nonce:
        raise ioutil.PreconditionError("lease_transfer nonce mismatch")
    raw = latest.get("session")
    if not isinstance(raw, str) or not raw.strip():
        raise ioutil.PreconditionError("lease_transfer lineage mismatch")
    try:
        successor = session_docs.load_session(Path(raw))
    except RuntimeError as exc:
        raise ioutil.PreconditionError("lease_transfer lineage mismatch") from exc
    meta = session_docs.read_meta(successor)
    successor_session = ioutil.canonical_path(successor)
    if (
        controller.get("workspace") != workspace
        or controller.get("pane") != pane
        or controller.get("session") != successor_session
        or latest.get("workspace") != workspace
        or latest.get("pane") != pane
        or latest.get("session") != successor_session
        or meta.get("workspace") != workspace
        or meta.get("pane") != pane
        or meta.get("parent") != predecessor.name
        or meta.get("run") != str(run)
        or meta.get("id") != successor.name
    ):
        raise ioutil.PreconditionError("lease_transfer lineage mismatch")
    return {
        "session": successor_session,
        "id": successor.name,
        "parent": meta.get("parent"),
        "run": str(run),
    }

def _ack_run_mode(run: Path, nonce: str, workspace: str, pane: str, *, lock_timeout=120) -> dict[str, Any]:
    """Zero-md run-centric lease transfer (epoch-based session id, index.db updated)."""
    if not run_store.has_db(run) and not (run / "run.json").is_file():
        raise ioutil.PreconditionError(f"run not found: {run}")
    run_doc = ledger.load_run(run)
    with ledger.with_lock(run, lock_timeout=lock_timeout):
        snap = ledger.replay(run)
        controller = snap["controller"]
        if controller.get("lease") == LEASE_ACTIVE:
            transfers = [ev for ev in snap["events"] if ev.get("type") == "lease_transfer"]
            if not transfers:
                raise ioutil.PreconditionError("lease_transfer requires draining current holder")
            latest = transfers[-1]
            if latest.get("nonce") != nonce:
                raise ioutil.PreconditionError("lease_transfer nonce mismatch")
            if (
                controller.get("workspace") != workspace
                or controller.get("pane") != pane
                or latest.get("workspace") != workspace
                or latest.get("pane") != pane
            ):
                raise ioutil.PreconditionError("lease_transfer lineage mismatch")
            session_id = str(controller.get("session") or f"epoch-{controller.get('epoch', 1)}")
            parent = str(latest.get("parent_session") or "")
        else:
            if controller.get("lease") != LEASE_DRAINING:
                raise ioutil.PreconditionError("lease_transfer requires draining current holder")
            holder = {key: controller.get(key) for key in authz.HOLDER_KEYS}
            if any(not isinstance(holder.get(key), str) or not holder[key].strip() for key in authz.HOLDER_KEYS):
                raise ioutil.PreconditionError("lease_transfer requires predecessor holder")
            parent = str(holder["session"])
            next_epoch = int(controller.get("epoch", 1)) + 1
            session_id = f"epoch-{next_epoch}"
            ledger.append_event_locked(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": workspace,
                    "pane": pane,
                    "session": session_id,
                    "parent_session": parent,
                    "nonce": nonce,
                },
                holder=holder,
            )
        state_paths.bind_pane(
            workspace,
            pane,
            run_id=str(run_doc["id"]),
            run_dir=run,
            job=str(run_doc.get("job") or ""),
            session_id=session_id,
            repo_common_dir=run_doc.get("repo_common_dir"),
        )
        stamp_workspace_tokens(
            workspace,
            source="successor",
            tokens=(
                f"handoff-ack={nonce}",
                f"session={session_id}",
            ),
            invoke=herdr,
        )
    return {
        "acked": True,
        "workspace": workspace,
        "nonce": nonce,
        "session": session_id,
        "session_id": session_id,
        "parent": parent,
        "run": str(run),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nonce", required=True, help="nonce generated by rotate.py")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--session", default=None, help="legacy predecessor session directory")
    target.add_argument("--run", default=None, help="canonical run directory (zero-md SQLite ack)")
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    if not args.nonce.strip():
        parser.error("--nonce must be non-empty")
    if args.run:
        workspace, pane = current_holder_env()
        if not workspace:
            raise ioutil.PreconditionError("HERDR_WORKSPACE_ID (or HC_TERMINAL_ID) is not set")
        if not pane:
            raise ioutil.PreconditionError("HERDR_PANE_ID (or HC_TERMINAL_ID) is not set")
        run = Path(ioutil.canonical_path(args.run))
        result = _ack_run_mode(run, args.nonce, workspace, pane, lock_timeout=args.lock_timeout)
        print(json.dumps(result))
        return 0

    predecessor = session_docs.load_session(Path(args.session))
    root = predecessor.parents[1]
    meta = session_docs.read_meta(predecessor)
    job = meta.get("job") or Path.cwd().name
    handoff = predecessor / "HANDOFF.md"
    next_prompt = predecessor / "NEXT-SESSION-PROMPT.md"

    read_required(handoff)
    read_required(next_prompt)
    run = session_docs.session_run(predecessor)

    workspace, pane = current_holder_env()
    if not workspace:
        raise ioutil.PreconditionError("HERDR_WORKSPACE_ID (or HC_TERMINAL_ID) is not set")
    if not pane:
        raise ioutil.PreconditionError("HERDR_PANE_ID (or HC_TERMINAL_ID) is not set")

    with ledger.with_lock(run, lock_timeout=args.lock_timeout):
        recovered = transferred_successor(run, predecessor, args.nonce, workspace, pane)
        if recovered is None:
            holder = predecessor_holder(ledger.replay(run)["controller"], predecessor, meta)
            template_dir = SCRIPTS.parent / "references" / "templates"
            rolled = session_docs.open_session(
                root, job=job, template_dir=template_dir, parent=predecessor, run=str(run),
                defer_run_binding=True, lock_timeout=args.lock_timeout,
            )
            if rolled.get("run") != str(run):
                raise RuntimeError("successor session run mismatch")

            session_id = rolled.get("id")
            if not isinstance(session_id, str) or not session_id.strip():
                raise RuntimeError("successor session id missing")
            successor = session_docs.load_session(predecessor.parent / session_id)
            successor_session = ioutil.canonical_path(successor)
            parent = rolled["parent"]

            ledger.append_event_locked(
                run,
                {
                    "type": "lease_transfer",
                    "workspace": workspace,
                    "pane": pane,
                    "session": successor_session,
                    "nonce": args.nonce,
                },
                holder=holder,
            )
        else:
            successor_session = recovered["session"]
            session_id = recovered["id"]
            parent = recovered["parent"]

        stamp_workspace_tokens(
            workspace,
            source="successor",
            tokens=(
                f"handoff-ack={args.nonce}",
                f"session={session_id}",
            ),
            invoke=herdr,
        )

    print(json.dumps({
        "acked": True,
        "workspace": workspace,
        "nonce": args.nonce,
        "session": successor_session,
        "session_id": session_id,
        "parent": parent,
        "run": str(run),
    }))
    return 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
