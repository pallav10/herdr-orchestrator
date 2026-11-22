#!/usr/bin/env python3
"""Safely prune orchestrator-created worktrees and promotion ref after run completion.

Usage: prune-run.py --run RUN --repo REPO --session S --holder-workspace W --holder-pane P [--dry-run] [--force-undelivered] [--lock-timeout SECONDS]

Fail-closed safety gates:
  1. Requires exact holder of the run's active lease.
  2. Refuses unless every graph node is terminal (succeeded/skipped/failed/interrupted)
     and no active worker is live.
  3. Refuses if `promotion_ref` has unmerged commits unless delivery succeeded or
     `--force-undelivered` is passed.
  4. Refuses to remove any dirty worktree (`git status --porcelain` non-empty).
  5. Never touches the primary checkout or primary branch (`main`/`master`).

Removes only orchestrator-created linked worktrees recorded in dispatch identities
and `refs/heads/herdr/<run-id>`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import cli_common
import graph_model
import ioutil
import ledger
import promotion
from ioutil import PreconditionError, RunError, UsageError, canonical_path

TERMINAL_NODE_STATES = frozenset({"succeeded", "skipped", "failed", "interrupted"})
PROTECTED_BRANCH_REFS = frozenset({"refs/heads/main", "refs/heads/master"})


def _primary_checkout(repo_abs: str) -> str:
    """Resolve the primary checkout path for `repo_abs`."""
    try:
        return canonical_path(ioutil.worktree_creation_source(repo_abs))
    except RunError:
        return repo_abs


def _list_git_worktrees(repo_abs: str) -> set[str]:
    proc = ioutil.run_cmd(
        ["git", "-C", repo_abs, "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RunError(f"git worktree list --porcelain failed: {detail}")
    paths: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            raw = line[len("worktree "):].strip()
            if raw:
                paths.add(canonical_path(raw))
    return paths


def _collect_recorded_worktrees_and_branches(
    run_dir: Path, graph: dict[str, Any], events: list[dict[str, Any]]
) -> tuple[list[str], set[str]]:
    """Collect unique canonical worktree paths and run-owned branch refs from dispatch identities/events."""
    found: list[str] = []
    seen: set[str] = set()
    branches: set[str] = set()

    def _add(raw_wt: object, raw_branch: object) -> None:
        if isinstance(raw_wt, str) and raw_wt.strip():
            cand = canonical_path(raw_wt)
            if cand not in seen:
                seen.add(cand)
                found.append(cand)
        if isinstance(raw_branch, str) and raw_branch.strip():
            ref = raw_branch if raw_branch.startswith("refs/heads/") else f"refs/heads/{raw_branch}"
            branches.add(ref)

    artifacts_root = run_dir / "artifacts"
    if artifacts_root.is_dir():
        for node_id in sorted((graph.get("nodes") or {}).keys()):
            node_dir = artifacts_root / node_id
            if not node_dir.is_dir():
                continue
            for attempt_entry in sorted(node_dir.iterdir(), key=lambda p: p.name):
                if not attempt_entry.is_dir() or not attempt_entry.name.isdigit():
                    continue
                ident = ledger.load_dispatch_identity(attempt_entry)
                if isinstance(ident, dict):
                    _add(ident.get("worktree_path"), ident.get("branch"))

    for ev in events:
        if ev.get("type") == "dispatched" and isinstance(ev.get("handle"), dict):
            h = ev["handle"]
            _add(h.get("worktree_path"), h.get("branch"))

    return found, branches


def _is_dirty_worktree(wt_path: str) -> bool:
    proc = ioutil.run_cmd(
        ["git", "-C", wt_path, "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise PreconditionError(f"cannot inspect worktree {wt_path}: {detail}")
    return bool(proc.stdout.strip())


def _is_ancestor(repo_abs: str, ancestor_sha: str, target_ref: str) -> bool:
    proc = ioutil.run_cmd(
        ["git", "-C", repo_abs, "merge-base", "--is-ancestor", ancestor_sha, target_ref],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _promotion_ref_delivered_or_merged(
    repo_abs: str,
    run_info: dict[str, Any],
    promo_ref: str,
    promo_sha: str,
    excluded_refs: set[str],
) -> bool:
    """True if promo_sha equals base_sha, matches the default delivery branch, or is merged into a non-run branch."""
    base_sha = run_info.get("base_sha")
    if isinstance(base_sha, str) and base_sha and promo_sha == base_sha:
        return True

    job = run_info.get("job") or run_info.get("id") or ""
    if job:
        try:
            default_ref = promotion.default_delivery_branch(job)
        except RunError:
            default_ref = None
        if default_ref and default_ref != promo_ref:
            deliv_sha = ioutil._git_ref(repo_abs, default_ref)
            if deliv_sha and _is_ancestor(repo_abs, promo_sha, default_ref):
                return True

    proc = ioutil.run_cmd(
        ["git", "-C", repo_abs, "for-each-ref", "--format=%(refname)", "refs/heads/"],
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        for ref in proc.stdout.splitlines():
            ref = ref.strip()
            if not ref or ref == promo_ref or ref in excluded_refs or ref.startswith("refs/heads/herdr/"):
                continue
            if _is_ancestor(repo_abs, promo_sha, ref):
                return True

    return False


def _ensure_no_live_workers(run_dir: Path, state: dict[str, Any], events: list[dict[str, Any]]) -> None:
    nodes = state.get("nodes") or {}
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get("state") in graph_model.ACTIVE_STATES:
            raise PreconditionError(f"cannot prune while node {node_id} is active ({node.get('state')})")
        if node.get("state") == "ready" and graph_model.has_current_dispatch_intent(node):
            raise PreconditionError(f"cannot prune while node {node_id} has pending dispatch_intent")


def prune_run(
    run_dir: str | Path,
    *,
    repo: str | Path,
    holder: dict[str, str],
    dry_run: bool = False,
    force_undelivered: bool = False,
    lock_timeout: float = 120,
) -> dict[str, Any]:
    run_path = Path(canonical_path(run_dir))
    repo_abs = canonical_path(repo)

    with ledger.with_lock(run_path, lock_timeout=lock_timeout):
        snap = ledger.replay(run_path)
        if snap.get("truncated_tail"):
            raise PreconditionError("events.jsonl has truncated tail; repair before pruning")
        graph_model.require_active_holder(snap["controller"], holder, "prune-run")

        run_info = ledger.load_run(run_path)
        run_repo = run_info.get("repo")
        if not isinstance(run_repo, str) or canonical_path(run_repo) != repo_abs:
            raise PreconditionError("prune --repo does not match run repo")

        graph = ledger.load_graph(run_path)
        nodes = snap["state"].get("nodes") or {}
        graph_nodes = graph.get("nodes") or {}
        if not graph_nodes:
            raise PreconditionError("cannot prune run with empty graph")

        _ensure_no_live_workers(run_path, snap["state"], snap["events"])

        non_terminal = [
            f"{nid}({(nodes.get(nid) or {}).get('state')})"
            for nid in sorted(graph_nodes)
            if (nodes.get(nid) or {}).get("state") not in TERMINAL_NODE_STATES
        ]
        if non_terminal:
            raise PreconditionError(f"cannot prune with non-terminal nodes: {', '.join(non_terminal)}")

        primary = _primary_checkout(repo_abs)
        git_worktrees = _list_git_worktrees(repo_abs)
        recorded_worktrees, recorded_branches = _collect_recorded_worktrees_and_branches(run_path, graph, snap["events"])

        # Validate promotion ref gate before any mutation.
        run_id = run_info.get("id") or run_path.name
        expected_promo_ref = f"refs/heads/herdr/{run_id}"
        promo_ref = run_info.get("promotion_ref")
        refs_to_remove: list[str] = []
        skipped: list[dict[str, str]] = []

        if isinstance(promo_ref, str) and promo_ref:
            if promo_ref in PROTECTED_BRANCH_REFS or promo_ref != expected_promo_ref:
                skipped.append({"target": promo_ref, "reason": "not run-owned herdr promotion ref"})
            else:
                promo_sha = ioutil._git_ref(repo_abs, promo_ref)
                if promo_sha is None:
                    skipped.append({"target": promo_ref, "reason": "ref absent"})
                else:
                    if ledger._promotion_ref_checked_out(repo_abs, promo_ref):
                        raise PreconditionError(f"cannot prune checked-out promotion ref {promo_ref}")
                    if not force_undelivered and not _promotion_ref_delivered_or_merged(
                        repo_abs, run_info, promo_ref, promo_sha, recorded_branches
                    ):
                        raise PreconditionError(
                            f"promotion ref {promo_ref} ({promo_sha[:12]}) has unmerged commits; "
                            "deliver first or pass --force-undelivered"
                        )
                    refs_to_remove.append(promo_ref)

        # Validate every recorded worktree before removing anything (all-or-none preflight).
        worktrees_to_remove: list[str] = []
        for wt in recorded_worktrees:
            if wt == primary or wt == repo_abs:
                skipped.append({"target": wt, "reason": "primary checkout protected"})
                continue
            if wt not in git_worktrees or not Path(wt).exists():
                skipped.append({"target": wt, "reason": "worktree absent"})
                continue
            if not ioutil.is_worktree_of(wt, primary):
                skipped.append({"target": wt, "reason": "not a linked worktree of repo"})
                continue
            if _is_dirty_worktree(wt):
                raise PreconditionError(f"worktree is dirty: {wt}")
            worktrees_to_remove.append(wt)

        removed_worktrees: list[str] = []
        removed_refs: list[str] = []

        if not dry_run:
            for wt in worktrees_to_remove:
                proc = ioutil.run_cmd(
                    ["git", "-C", primary, "worktree", "remove", wt],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout).strip()
                    raise RunError(f"git worktree remove {wt} failed: {detail}")
                removed_worktrees.append(wt)

            for ref in refs_to_remove:
                proc = ioutil.run_cmd(
                    ["git", "-C", primary, "update-ref", "-d", ref],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode != 0:
                    detail = (proc.stderr or proc.stdout).strip()
                    raise RunError(f"git update-ref -d {ref} failed: {detail}")
                removed_refs.append(ref)
        else:
            removed_worktrees = list(worktrees_to_remove)
            removed_refs = list(refs_to_remove)

        return {
            "dry_run": bool(dry_run),
            "removed_worktrees": removed_worktrees,
            "removed_refs": removed_refs,
            "skipped": skipped,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="canonical absolute path to run directory")
    parser.add_argument("--repo", required=True, help="canonical absolute path to target git repository")
    parser.add_argument("--session", required=True, help="canonical absolute path to holder session directory")
    parser.add_argument("--holder-workspace", required=True, help="controller holder workspace id")
    parser.add_argument("--holder-pane", required=True, help="controller holder pane id")
    parser.add_argument("--dry-run", action="store_true", help="validate and report targets without removing")
    parser.add_argument("--force-undelivered", action="store_true", help="allow deleting promotion ref with unmerged commits")
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    holder = cli_common.holder_from_args(args, parser, context="--run", required_message="prune-run requires --session, --holder-workspace, and --holder-pane", nonempty=True)
    if holder is None:
        raise UsageError("prune-run requires holder identity")

    result = prune_run(
        args.run,
        repo=args.repo,
        holder=holder,
        dry_run=args.dry_run,
        force_undelivered=args.force_undelivered,
        lock_timeout=args.lock_timeout,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    cli_common.run(main)
