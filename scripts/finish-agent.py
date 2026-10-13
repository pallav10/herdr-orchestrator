#!/usr/bin/env python3
"""Finish a dispatched attempt: ownership gates -> collect -> succeeded or promote.

Usage: finish-agent.py --run RUN --node N --attempt A --session S --holder-workspace W --holder-pane P [--since SHA] [--force] [--lock-timeout SECONDS]

Does, in one idempotent step:
  1. load identity and refuse a dirty tree or HEAD that does not descend from --since (default: merge-base of promotion_ref and HEAD, else identity base)
  2. classify changed files against dispatch_intent.owns (working-tree only for integrators)
  3. integrators: refuse when HEAD is still on the promoted line; write integration.json from vcs-typed consumed receipts
  4. collect-agent.py --publish-vcs
  5. refuse a node that declared a worktree and finished at vcs form none: it committed
     nothing, which for a writer means its prompt never ran (--force records it anyway)
  6. writers: append succeeded with declared-output artifacts; integrators: print the promote call (no succeeded)

--force re-collects instead of trusting the recorded receipts: it re-reads the pane,
re-derives vcs.json from the worktree's current HEAD, rewrites integration.json, and
appends an artifact_forced audit event under the run lock for every receipt it replaces.
It is the only way to correct a mis-collected attempt, so it also runs on an attempt
already recorded succeeded — the recorded succeeded event keeps its original hashes as
history and no second succeeded is appended. Re-recording a node's artifacts is
invalidated -> retried, not --force.

Prints one JSON line:
  {"node","attempt","state","vcs_form","artifacts":[...],"unowned":[...],"promote": <str|null>,"since": <sha|null>}
Exit codes: 0 ok, 1 runtime, 2 usage, 3 precondition.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import graph_model
import ioutil
import cli_common

from artifacts import consumed_items, overwrite_forced_artifact
CLI_EXTRA_CODES = {RuntimeError: 1}
import ledger
import receipts
from receipts import GitFacts, writer_path_findings

from cli_common import SCRIPTS, holder_from_args, run_child





def _git(worktree: str, *args: str, git_facts: GitFacts | None = None) -> subprocess.CompletedProcess:
    return git_facts.git(worktree, *args) if git_facts is not None else ioutil.run_cmd(["git", "-C", worktree, *args], capture_output=True, text=True)




def _consumed_entry(item: dict[str, Any]) -> dict[str, Any]:
    entry = {
        "producer": item["producer"],
        "producer_attempt": item.get("producer_attempt", item.get("attempt")),
        "output": item["output"],
        "hash": item["hash"],
    }
    alias = item.get("alias")
    if isinstance(alias, str) and alias:
        entry["alias"] = alias
    return entry


def _promote_string(run: str | Path, node_id: str, attempt: int, new_sha: str, repo: str | Path, holder: dict[str, str] | None, *, lock_timeout=120) -> str:
    run_s = ioutil.canonical_path(run)
    repo_s = ioutil.canonical_path(repo)
    holder_s = json.dumps(holder)
    return (
        f'run_state.promote("{run_s}", node="{node_id}", attempt={int(attempt)}, '
        f'new_sha="{new_sha}", repo="{repo_s}", holder={holder_s}, lock_timeout={lock_timeout})'
    )


def _vcs_form(attempt_dir: Path) -> str | None:
    path = attempt_dir / "vcs.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    result = data.get("result") if isinstance(data, dict) else None
    if isinstance(result, dict):
        return result.get("form")
    return None


def _print_result(node_id: str, attempt: int, state: str, vcs_form: str | None, artifacts: list[dict[str, Any]], unowned: list[dict[str, Any]], promote: str | None, since: str | None = None) -> None:
    print(json.dumps({
        "node": node_id,
        "attempt": attempt,
        "state": state,
        "vcs_form": vcs_form,
        "artifacts": artifacts,
        "unowned": unowned,
        "promote": promote,
        "since": since,
    }))


def _write_integration_receipt(run_doc: dict[str, Any], graph: dict[str, Any], attempt_dir: Path, wt: str | None, head: str, *, force: bool = False, holder=None, git_facts: GitFacts | None = None, lock_timeout=120) -> bool:
    """Write integration.json; returns True when it replaced a different existing receipt."""
    promo = run_doc.get("promotion_ref")
    if head == run_doc.get("base_sha") or (
        isinstance(promo, str)
        and promo
        and _git(wt, "merge-base", "--is-ancestor", "HEAD", promo, git_facts=git_facts).returncode == 0
    ):
        raise ioutil.PreconditionError("integrator has no commit beyond the promoted line")
    items = []
    consumed_path = attempt_dir / "consumed_artifacts.json"
    if consumed_path.is_file():
        items = consumed_items(consumed_path)
    payload = {
        "base_sha": run_doc.get("base_sha"),
        "commit": head,
        "head": head,
        "consumed": [_consumed_entry(item) for item in receipts.vcs_typed_consumed(graph, items)],
    }
    integration_path = attempt_dir / "integration.json"
    if integration_path.is_file():
        existing = json.loads(integration_path.read_text(encoding="utf-8"))
        if existing == payload:
            return False
        if not force:
            raise ioutil.PreconditionError("integration.json already exists with different content")
        overwrite_forced_artifact(Path(run_doc["path"]), attempt_dir.parent.name, int(attempt_dir.name), integration_path, json.dumps(payload, indent=1, sort_keys=True) + "\n", holder=holder, lock_timeout=lock_timeout)
        return True
    ioutil.atomic_text(integration_path, json.dumps(payload, indent=1, sort_keys=True) + "\n")
    return False


def _collect_declared_artifacts(identity: dict[str, Any], run: Path, node_id: str, attempt: int, graph: dict[str, Any], wt: str | None, args) -> list[dict[str, Any]]:
    """Run collect-agent.py for this attempt and keep only graph-declared artifacts."""
    target = identity.get("worker_id") or identity.get("agent")
    if not isinstance(target, str) or not target:
        raise ioutil.PreconditionError("dispatch identity missing worker_id/agent")
    argv = [
        sys.executable,
        str(SCRIPTS / "collect-agent.py"),
        target,
        "--run", str(run),
        "--node", node_id,
        "--attempt", str(attempt),
        "--session", args.session,
        "--holder-workspace", args.holder_workspace,
        "--holder-pane", args.holder_pane,
        "--lock-timeout", str(args.lock_timeout),
    ]
    if wt:
        argv.append("--publish-vcs")
    if args.force:
        argv.append("--force")
    result = run_child(argv, "collect-agent.py")
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list) or not agents or not isinstance(agents[0], dict):
        raise ioutil.PreconditionError("collect-agent.py returned no agents")
    artifacts = agents[0].get("artifacts") if isinstance(agents[0].get("artifacts"), list) else []
    declared = graph_model.output_specs(graph, node_id)
    return [row for row in artifacts if isinstance(row, dict) and graph_model.artifact_name(row) in declared]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        usage="finish-agent.py --run RUN --node N --attempt A --session S --holder-workspace W --holder-pane P [--since SHA] [--force] [--lock-timeout SECONDS]",
    )
    parser.add_argument("--run", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--since", default=None)
    parser.add_argument("--session", required=True)
    parser.add_argument("--holder-workspace", required=True)
    parser.add_argument("--holder-pane", required=True)
    parser.add_argument("--force", action="store_true",
                        help="re-collect the attempt: replace differing receipts, audit each as artifact_forced, and record a writer that committed nothing")
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    holder = holder_from_args(args)

    run = Path(ioutil.canonical_path(args.run))
    node_id = args.node
    attempt = args.attempt

    run_doc = ledger.load_run(run)
    graph = ledger.load_graph(run)
    snap = ledger.replay(run)
    # gate every write below (integration receipt, forced unlink, succeeded event), not just --force
    graph_model.require_active_holder(snap["controller"], holder, "finish-agent")
    node = (snap["state"].get("nodes") or {}).get(node_id)
    if not isinstance(node, dict):
        raise ioutil.PreconditionError("unknown node")
    if int(node.get("attempt") or 0) != attempt:
        raise ioutil.PreconditionError(
            f"attempt {attempt} does not match current attempt {node.get('attempt', 0)}"
        )
    attempt_dir = ledger.attempt_dir(run, node_id, attempt)
    identity = ledger.load_dispatch_identity(attempt_dir)
    if not isinstance(identity, dict):
        raise ioutil.PreconditionError("missing dispatch-identity.json")
    plan = (graph.get("nodes") or {}).get(node_id) or {}
    integrator = graph_model.declares_output(plan.get("outputs") or [], "integration")
    wt = identity.get("worktree_path")
    if not isinstance(wt, str) or not wt:
        if integrator or graph_model.declares_output(plan.get("outputs") or [], "vcs"):
            raise ioutil.PreconditionError("dispatch identity missing worktree_path")
        wt = None
    git_facts = GitFacts(wt) if wt else None
    head_proc = _git(wt, "rev-parse", "HEAD", git_facts=git_facts) if wt else None
    head = head_proc.stdout.strip() if head_proc is not None and head_proc.returncode == 0 else ""
    promote = None
    if integrator:
        promote = _promote_string(run, node_id, attempt, head, run_doc.get("repo") or identity.get("repo") or "", holder, lock_timeout=args.lock_timeout)
    
    since = None
    if wt:
        since = args.since
        if not since and run_doc.get("promotion_ref"):
            merge = _git(wt, "merge-base", run_doc["promotion_ref"], "HEAD", git_facts=git_facts)
            since = merge.stdout.strip() if merge.returncode == 0 else None
        since = since or identity.get("base")
    
    already_succeeded = node.get("state") == "succeeded"
    if already_succeeded and not args.force:
        artifacts = node.get("artifacts") if isinstance(node.get("artifacts"), list) else []
        _print_result(node_id, attempt, "succeeded", _vcs_form(attempt_dir), artifacts, [], promote, since)
        return 0
    
    if wt:
        if not isinstance(since, str) or not since:
            raise ioutil.PreconditionError("missing since/base sha")
    
        dirty = _git(wt, "status", "--porcelain", "--untracked-files=all", git_facts=git_facts)
        if dirty.returncode != 0:
            raise ioutil.PreconditionError(dirty.stderr.strip() or "git status failed")
        if dirty.stdout.strip():
            raise ioutil.PreconditionError("worktree is dirty")
        if head_proc.returncode != 0 or not head:
            raise ioutil.PreconditionError(head_proc.stderr.strip() or "git rev-parse HEAD failed")
        ancestor = _git(wt, "merge-base", "--is-ancestor", since, "HEAD", git_facts=git_facts)
        if ancestor.returncode != 0:
            raise ioutil.PreconditionError(f"HEAD does not descend from {since}")
    
        intent = node.get("dispatch_intent") if isinstance(node.get("dispatch_intent"), dict) else {}
        owns = intent.get("owns")
        if not isinstance(owns, list):
            plan = (graph.get("nodes") or {}).get(node_id) or {}
            owns = list(plan.get("owns") or [])
        unowned, overlaps, _ = writer_path_findings(wt, node_id, owns, integrates=integrator, since=since, git_facts=git_facts)
        if unowned or overlaps:
            offending = [*unowned, *(item["path"] for item in overlaps)]
            _print_result(node_id, attempt, node.get("state"), None, [], offending, None, since)
            raise ioutil.PreconditionError(f"unowned files: {', '.join(offending)}")
    
    if integrator:
        _write_integration_receipt(run_doc, graph, attempt_dir, wt, head, force=args.force, holder=holder, git_facts=git_facts, lock_timeout=args.lock_timeout)
    
    artifacts = _collect_declared_artifacts(identity, run, node_id, attempt, graph, wt, args)
    vcs_form = _vcs_form(attempt_dir)
    
    node_plan = (graph.get("nodes") or {}).get(node_id) or {}
    if vcs_form == "none" and node_plan.get("worktree") and not integrator and not args.force:
        # a node that asked for its own worktree and committed nothing produced nothing;
        # in practice its prompt never ran. Mirrors the integrator's commit requirement.
        _print_result(node_id, attempt, node.get("state"), vcs_form, artifacts, [], None, since)
        raise ioutil.PreconditionError(
            f"writer {node_id} declares a worktree but its HEAD is still the base "
            "(vcs form none): the attempt committed nothing. Inspect the pane and retry "
            "the node, or pass --force to record the empty result deliberately"
        )
    
    if integrator:
        _print_result(node_id, attempt, node.get("state"), vcs_form, artifacts, [], promote, since)
        return 0
    
    if already_succeeded:
        # `succeeded` requires a running attempt: --force refreshes and audits the
        # receipts of a terminal attempt, it never re-records the attempt itself.
        _print_result(node_id, attempt, "succeeded", vcs_form, artifacts, [], None, since)
        return 0
    
    appended = ledger.append_event(
        run,
        {
            "type": "succeeded",
            "node": node_id,
            "from": "running",
            "to": "succeeded",
            "attempt": attempt,
            "artifacts": artifacts,
        },
        holder=holder,
        lock_timeout=args.lock_timeout,
    )
    final = appended["state"]["nodes"][node_id]
    _print_result(node_id, attempt, final.get("state"), vcs_form, artifacts, [], None, since)
    return 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
