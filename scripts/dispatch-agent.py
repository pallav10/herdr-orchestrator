#!/usr/bin/env python3
"""Dispatch a ready graph node: intent -> spawn --resume -> dispatched.

Usage: dispatch-agent.py --run RUN --node N [--attempt A] --session S --holder-workspace W --holder-pane P --epoch E (--prompt-file F | --prompt TEXT) [--name ALIAS] [--model-role R | --model M] [--requested-effort E] [--plan P] [--kind K] [--branch B] [--path P] [--orchestrate|--no-orchestrate] [--retries N] [--retry-delay S] [--lock-timeout SECONDS]

Does, in one idempotent step:
  1. refuse when --epoch does not match the controller epoch
  2. reconcile readiness for the holder
  3. ready node: append dispatch_intent when missing, spawn-agent.py --resume when
     launch-complete.json is missing, then append dispatched from the identity
     (liveness.herdr_ready_dispatch_candidate)
  4. planned node, or blocked node without a dispatch identity: exit 3 with the node reason
  5. any other state for the current attempt is authoritative: no spawn, no launch record
     required (legacy ledgers have none); prompted/blocked are null without a record
  6. running attempt whose launch record is blocked and has no blocked event after
     dispatched: append blocked once (crash between dispatched and blocked replayed)

Prints one JSON line:
  {"node","attempt","agent","workspace","worktree","model","state","prompted","blocked"}
Exit codes: 0 ok, 1 runtime, 2 usage, 3 precondition.
"""

from __future__ import annotations

import argparse
import json

import sys
from pathlib import Path
from typing import Any


import graph_model
import ioutil
import cli_common

CLI_EXTRA_CODES = {RuntimeError: 1}
import ledger
import liveness
import scheduler
from graph_model import AGENT_NAME_RE
from cli_common import SCRIPTS, holder_from_args, run_child





def node_alias(node_id: str) -> str:
    slug = ioutil.slug(node_id, limit=None, fallback="", allowed="abcdefghijklmnopqrstuvwxyz0123456789_-", trim_after_limit=False)
    if not slug or slug[0] not in "abcdefghijklmnopqrstuvwxyz":
        slug = "n-" + slug
    return slug[:32]


def _passthrough(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    if args.model_role:
        out += ["--model-role", args.model_role]
    if args.model:
        out += ["--model", args.model]
    if args.requested_effort:
        out += ["--requested-effort", args.requested_effort]
    if args.plan:
        out += ["--plan", args.plan]
    if args.kind:
        out += ["--kind", args.kind]
    if args.cwd:
        out += ["--cwd", args.cwd]
    if args.branch:
        out += ["--branch", args.branch]
    if args.path:
        out += ["--path", args.path]
    if args.orchestrate is True:
        out.append("--orchestrate")
    elif args.orchestrate is False:
        out.append("--no-orchestrate")
    if args.retries is not None:
        out += ["--retries", str(args.retries)]
    if args.retry_delay is not None:
        out += ["--retry-delay", str(args.retry_delay)]
    return out


def _blocked_after_dispatch(run: Path, node_id: str, attempt: int) -> bool:
    seen_dispatch = False
    for event in ledger.events_for_attempt(ledger.replay(run)["events"], node_id, attempt):
        if event.get("type") == "dispatched":
            seen_dispatch = True
        elif seen_dispatch and event.get("type") == "blocked":
            return True
    return False


def _print_result(node_id: str, attempt: int, identity: dict[str, Any], state: str, prompted: dict[str, Any] | None = None, blocked: dict[str, Any] | None = None) -> None:
    routing = identity.get("routing") if isinstance(identity.get("routing"), dict) else {}
    worktree = identity.get("worktree_path")
    print(json.dumps({
        "node": node_id,
        "attempt": attempt,
        "agent": identity.get("worker_id") or identity.get("agent"),
        "workspace": identity.get("workspace"),
        "worktree": worktree if isinstance(worktree, str) and worktree else None,
        "model": routing.get("resolved"),
        "state": state,
        "prompted": prompted,
        "blocked": blocked,
    }))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        usage="dispatch-agent.py --run RUN --node N [--attempt A] --session S --holder-workspace W --holder-pane P --epoch E (--prompt-file F | --prompt TEXT) [--name ALIAS] [--model-role R | --model M] [--requested-effort E] [--plan P] [--kind K] [--cwd DIR] [--branch B] [--path P] [--orchestrate|--no-orchestrate] [--retries N] [--retry-delay S] [--lock-timeout SECONDS]",
    )
    parser.add_argument("--run", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--attempt", type=int, default=None)
    parser.add_argument("--session", required=True)
    parser.add_argument("--holder-workspace", required=True)
    parser.add_argument("--holder-pane", required=True)
    parser.add_argument("--epoch", type=int, required=True)
    prompts = parser.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--prompt-file", default=None)
    prompts.add_argument("--prompt", default=None)
    parser.add_argument("--name", default=None)
    models = parser.add_mutually_exclusive_group()
    models.add_argument("--model-role", default=None)
    models.add_argument("--model", default=None)
    parser.add_argument("--requested-effort", default=None)
    parser.add_argument("--plan", default=None)
    parser.add_argument("--kind", default=None)
    parser.add_argument("--cwd", default=None, help="workspace cwd for read-only (non-worktree) nodes")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--path", default=None)
    orch = parser.add_mutually_exclusive_group()
    orch.add_argument("--orchestrate", action="store_true", dest="orchestrate")
    orch.add_argument("--no-orchestrate", action="store_false", dest="orchestrate")
    parser.set_defaults(orchestrate=None)
    parser.add_argument("--retries", type=int, default=None)
    parser.add_argument("--retry-delay", type=float, default=None)
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    if args.name is not None and not AGENT_NAME_RE.match(args.name):
        parser.error(f"name {args.name!r} must match {AGENT_NAME_RE.pattern}")

    holder = holder_from_args(args)
    run = Path(ioutil.canonical_path(args.run))
    node_id = args.node

    snap = ledger.replay(run)
    controller_epoch = int(snap["controller"]["epoch"])
    if controller_epoch != args.epoch:
        raise ioutil.PreconditionError(
            f"epoch {args.epoch} does not match controller epoch {controller_epoch}"
        )
    result = scheduler.reconcile(run, holder=holder, lock_timeout=args.lock_timeout)
    state = result["state"]
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        raise ioutil.PreconditionError("unknown node")
    
    current = int(node.get("attempt") or 0)
    expected = max(1, current)
    if args.attempt is not None and args.attempt != expected:
        raise ioutil.PreconditionError(
            f"attempt {args.attempt} does not match current attempt {expected}"
        )
    attempt = expected if args.attempt is None else args.attempt
    attempt_dir = ledger.attempt_dir(run, node_id, attempt)
    identity_path = attempt_dir / "dispatch-identity.json"
    record_path = attempt_dir / "launch-complete.json"
    
    if node.get("state") == "ready":
        if not graph_model.has_current_dispatch_intent(node):
            ledger.append_event(
                run,
                {"type": "dispatch_intent", "node": node_id, "attempt": attempt, "epoch": args.epoch},
                holder=holder,
                lock_timeout=args.lock_timeout,
            )
        if not record_path.is_file():
            alias = args.name or node_alias(node_id)
            argv = [
                sys.executable,
                str(SCRIPTS / "spawn-agent.py"),
                alias,
                "--run", str(run),
                "--node", node_id,
                "--attempt", str(attempt),
                "--session", holder["session"],
                "--holder-workspace", holder["workspace"],
                "--holder-pane", holder["pane"],
                "--epoch", str(args.epoch),
                "--lock-timeout", str(args.lock_timeout),
            ]
            if args.prompt_file is not None:
                argv += ["--prompt-file", args.prompt_file]
            else:
                argv += ["--prompt", args.prompt]
            argv += _passthrough(args)
            argv.append("--resume")
            run_child(argv, "spawn-agent.py")
            if not record_path.is_file():
                raise ioutil.PreconditionError("launch record missing after spawn")
        state = ledger.replay(run)["state"]
        node = state["nodes"][node_id]
        if node.get("state") == "ready":
            event = liveness.herdr_ready_dispatch_candidate(state, node_id, node)
            if event is None:
                raise ioutil.PreconditionError("dispatch identity incomplete")
            event["epoch"] = args.epoch
            node = ledger.append_event(run, event, holder=holder, lock_timeout=args.lock_timeout)["state"]["nodes"][node_id]
    elif node.get("state") == "planned" or (node.get("state") == "blocked" and not identity_path.is_file()):
        raise ioutil.PreconditionError(node.get("reason") or f"node is {node.get('state')}")
    
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}
    if node.get("state") == "running" and record.get("blocked") and not _blocked_after_dispatch(run, node_id, attempt):
        reason = "agent_not_ready" if not record.get("prompted") else "agent_blocked"
        appended = ledger.append_event(
            run,
            {"type": "blocked", "node": node_id, "attempt": attempt, "epoch": args.epoch, "reason": reason},
            holder=holder,
            lock_timeout=args.lock_timeout,
        )
        node = appended["state"]["nodes"][node_id]
    
    identity = ledger.load_dispatch_identity(attempt_dir) or {}
    _print_result(
        node_id,
        attempt,
        identity,
        node.get("state"),
        prompted=record.get("prompted"),
        blocked=record.get("blocked"),
    )
    return 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
