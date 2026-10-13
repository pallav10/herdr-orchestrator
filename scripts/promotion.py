"""Compare-and-swap promotion, recovery, and final branch delivery."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from ioutil import PreconditionError, RunError, _git_ref, _run_git, atomic_json, canonical_path, run_cmd, slug
from graph_model import _check_holder, require_active_holder
from ledger import _previous_promotion_sha, _promotion_artifacts, _refuse_checked_out_promotion_ref, append_event_locked, attempt_dir, load_graph, load_run, replay, with_lock




def delivery_branch_ref(branch) -> str:
    """refs/heads/<branch> for a caller-supplied delivery branch, validated as a git branch."""
    if not isinstance(branch, str) or not branch.strip():
        raise PreconditionError("delivery branch must be a non-empty string")
    name = branch.strip().removeprefix("refs/heads/")
    ref = "refs/heads/" + name
    if (
        name.startswith("refs/")
        or name != name.strip("/")
        or name.startswith("-")
        or "//" in name
        or len(ref) > 200
        or run_cmd(["git", "check-ref-format", ref], capture_output=True).returncode != 0
    ):
        raise PreconditionError(f"invalid delivery branch {branch!r}; use a git branch name like feature/short-change-name")
    return ref


def default_delivery_branch(job: str) -> str:
    """Descriptive default delivery branch for a job: refs/heads/feature/<change-slug>."""
    return delivery_branch_ref(f"feature/{slug(job, limit=60, fallback='change', word_boundary=True)}")


def _delivered_sha(snap: dict[str, Any]) -> str | None:
    """Final promoted commit of this run, taken from promoted+succeeded facts only."""
    nodes = snap["state"].get("nodes") or {}
    sha = None
    for event in snap["events"]:
        if event.get("type") != "promoted" or not event.get("new"):
            continue
        node = nodes.get(event.get("node")) or {}
        if node.get("state") == "succeeded" and int(node.get("attempt") or 0) == int(event.get("attempt") or 0):
            sha = event["new"]
    return sha


def _graph_work_complete(run_dir: Path, snap: dict[str, Any], graph: dict[str, Any] | None = None) -> bool:
    """True when every node of the accepted graph is terminal-good (succeeded or skipped)."""
    graph = graph if graph is not None else load_graph(run_dir)
    nodes = snap["state"].get("nodes") or {}
    ids = (graph.get("nodes") or {}).keys()
    if not ids:
        return False
    return all((nodes.get(node_id) or {}).get("state") in {"succeeded", "skipped"} for node_id in ids)


def _deliver_branch_locked(run_dir: Path, repo_abs: str, snap: dict[str, Any], *, holder=None, branch: str | None = None, graph: dict[str, Any] | None = None) -> dict[str, Any]:
    controller = snap["controller"]
    require_active_holder(controller, holder, "deliver_branch")
    run = load_run(run_dir)
    if not isinstance(run.get("repo"), str) or canonical_path(run["repo"]) != repo_abs:
        raise PreconditionError("delivery repo does not match run repo")
    if not _graph_work_complete(run_dir, snap, graph):
        raise PreconditionError("run graph still has unfinished nodes; deliver after every node is succeeded or skipped")
    sha = _delivered_sha(snap)
    if not sha:
        raise PreconditionError("run has no promoted and succeeded integration commit to deliver")
    ref = delivery_branch_ref(branch) if branch is not None else default_delivery_branch(run.get("job") or run["id"])
    if ref == run.get("promotion_ref"):
        raise PreconditionError(f"delivery branch {ref} is the run-owned promotion ref; choose another name")
    current = _git_ref(repo_abs, ref)
    if current == sha:
        return {"delivery_branch": ref, "sha": sha, "created": False}
    if current is not None:
        raise PreconditionError(f"delivery branch {ref} already exists at {current}; deliver with branch=<another name>")
    proc = run_cmd(["git", "-C", repo_abs, "update-ref", ref, sha, ""], capture_output=True, text=True)
    if proc.returncode != 0:
        landed = _git_ref(repo_abs, ref)
        if landed == sha:
            return {"delivery_branch": ref, "sha": sha, "created": False}
        if landed is not None:
            raise PreconditionError(f"delivery branch {ref} already exists at {landed}; deliver with branch=<another name>")
        raise RunError(proc.stderr.strip() or proc.stdout.strip() or f"git update-ref {ref} failed")
    return {"delivery_branch": ref, "sha": sha, "created": True}


def deliver_branch(run_dir, *, repo, holder=None, branch: str | None = None, lock_timeout=120) -> dict[str, Any]:
    """Create the human-facing delivery branch at this run's final promoted commit.

    Terminal, idempotent, create-only: never moves or deletes an existing ref.
    Returns {"delivery_branch", "sha", "created"}.
    """
    run_dir = Path(canonical_path(run_dir))
    repo_abs = canonical_path(repo)
    with with_lock(run_dir, lock_timeout=lock_timeout):
        return _deliver_branch_locked(run_dir, repo_abs, replay(run_dir), holder=holder, branch=branch)


def _autodeliver_locked(run_dir: Path, repo_abs: str, graph: dict[str, Any], *, holder=None) -> dict[str, Any]:
    """Publish the final delivery branch only once the whole accepted graph is done."""
    snap = replay(run_dir)
    try:
        return _deliver_branch_locked(run_dir, repo_abs, snap, holder=holder, graph=graph)
    except (PreconditionError, RunError) as exc:
        # publication never invalidates a completed promotion; the caller reports the note
        return {"delivery_branch": None, "delivery_note": str(exc)}


def _finalize_promotion_locked(run_dir: Path, promoted: dict[str, Any], artifacts: list[dict[str, Any]], *, holder=None) -> dict[str, Any]:
    node = promoted["node"]
    attempt = int(promoted["attempt"])
    attempt_path = attempt_dir(run_dir, node, attempt)
    append_event_locked(run_dir, promoted, holder=holder)
    atomic_json(attempt_path / "promoted.json", {"ref": promoted["ref"], "old": promoted["expected_old"], "new": promoted["new"]})
    return append_event_locked(run_dir, {"type": "succeeded", "node": node, "from": "running", "to": "succeeded", "attempt": attempt, "artifacts": artifacts}, holder=holder)


def promote(run_dir: str | Path, *, node: str, attempt: int, new_sha: str, repo: str | Path, holder: dict[str, str] | None = None, lock_timeout=120) -> dict[str, Any]:
    """Return one of three shapes, all carrying {state, controller, event}:

    - blocked promotion (ref moved): {state, controller, event} only, no delivery keys.
    - promoted + delivery: plus {delivery_branch, sha, created}.
    - promoted, delivery skipped: plus {delivery_branch: None, delivery_note}.
    """
    run_dir = Path(canonical_path(run_dir))
    repo_abs = canonical_path(repo)
    with with_lock(run_dir, lock_timeout=lock_timeout, node=node):
        snap = replay(run_dir)
        graph = load_graph(run_dir)
        run = load_run(run_dir)
        ref = run["promotion_ref"]
        expected_old = _previous_promotion_sha(snap["events"], run["base_sha"])
        if not isinstance(ref, str) or not ref or not isinstance(expected_old, str) or not expected_old:
            raise PreconditionError("run has no accepted promotion ref")
        intent = {"type": "promotion_intent", "ref": ref, "expected_old": expected_old, "new": new_sha, "node": node, "attempt": int(attempt)}
        _check_holder(snap["controller"], intent, holder)
        if not isinstance(run.get("repo"), str) or canonical_path(run["repo"]) != repo_abs:
            raise PreconditionError("promotion repo does not match run repo")
        _refuse_checked_out_promotion_ref(repo_abs, ref)
        artifacts = _promotion_artifacts(run_dir, graph, node, int(attempt), snap["state"]["nodes"][node], new_sha)

        append_event_locked(run_dir, intent, holder=holder)
        proc = run_cmd(["git", "-C", repo_abs, "update-ref", ref, new_sha, expected_old], capture_output=True, text=True)
        if proc.returncode != 0:
            current = run_cmd(["git", "-C", repo_abs, "rev-parse", ref], capture_output=True, text=True).stdout.strip()
            if current != expected_old:
                return append_event_locked(run_dir, {"type": "blocked", "node": node, "attempt": int(attempt), "reason": "promotion ref changed", "ref": ref}, holder=holder)
            raise RunError(proc.stderr.strip() or proc.stdout.strip() or "git update-ref failed")
        promoted = {"type": "promoted", "ref": ref, "expected_old": expected_old, "new": new_sha, "node": node, "attempt": int(attempt)}
        result = _finalize_promotion_locked(run_dir, promoted, artifacts, holder=holder)
        return {**result, **_autodeliver_locked(run_dir, repo_abs, graph, holder=holder)}


def _pending_promotion(events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    pending = None
    promoted = None
    for event in events:
        if event.get("type") == "promotion_intent":
            pending = event
            promoted = None
        elif pending and event.get("node") == pending.get("node") and event.get("attempt") == pending.get("attempt"):
            if event.get("type") == "promoted":
                promoted = event
            elif event.get("type") in {"blocked", "succeeded"}:
                pending = None
                promoted = None
    return pending, promoted


def recover_promotion(run_dir, repo, *, holder=None, lock_timeout=120) -> None:
    """Return None."""
    run_dir = Path(canonical_path(run_dir))
    with with_lock(run_dir, lock_timeout=lock_timeout):
        _recover_promotion_locked(run_dir, repo, holder=holder)


def _ensure_delivery_locked(run_dir: Path, repo_abs: str, snap: dict[str, Any], graph: dict[str, Any], *, holder=None) -> None:
    """Publish the final branch for a finished run that never got one; no-op otherwise."""
    if not _graph_work_complete(run_dir, snap, graph) or not _delivered_sha(snap):
        return
    controller = snap["controller"]
    require_active_holder(controller, holder, "ensure_delivery")
    run = load_run(run_dir)
    if not isinstance(run.get("repo"), str) or canonical_path(run["repo"]) != repo_abs:
        raise PreconditionError("promotion repo does not match run repo")
    _autodeliver_locked(run_dir, repo_abs, graph, holder=holder)


def _recover_promotion_locked(run_dir: Path, repo, *, holder=None) -> None:
    repo_abs = canonical_path(repo)
    snap = replay(run_dir)
    graph = load_graph(run_dir)
    pending, already_promoted = _pending_promotion(snap["events"])
    if not pending:
        # a crash between the succeeded append and publication leaves no pending intent
        _ensure_delivery_locked(run_dir, repo_abs, snap, graph, holder=holder)
        return
    promoted = {"type": "promoted", "ref": pending["ref"], "expected_old": pending["expected_old"], "new": pending["new"], "node": pending["node"], "attempt": pending["attempt"]}
    _check_holder(snap["controller"], promoted, holder)
    run = load_run(run_dir)
    if not isinstance(run.get("repo"), str) or canonical_path(run["repo"]) != repo_abs:
        raise PreconditionError("promotion repo does not match run repo")
    _refuse_checked_out_promotion_ref(repo_abs, pending["ref"])
    ref = pending["ref"]
    current = run_cmd(["git", "-C", repo_abs, "rev-parse", ref], capture_output=True, text=True).stdout.strip()
    if already_promoted and current != pending["new"]:
        append_event_locked(run_dir, {"type": "blocked", "node": pending["node"], "attempt": int(pending["attempt"]), "reason": "promotion ref changed", "ref": ref}, holder=holder)
        return
    artifacts = _promotion_artifacts(
        run_dir,
        graph,
        pending["node"],
        int(pending["attempt"]),
        snap["state"]["nodes"][pending["node"]],
        pending["new"],
    )
    if already_promoted:
        atomic_json(attempt_dir(run_dir, pending["node"], int(pending["attempt"])) / "promoted.json", {"ref": promoted["ref"], "old": promoted["expected_old"], "new": promoted["new"]})
        append_event_locked(run_dir, {"type": "succeeded", "node": pending["node"], "from": "running", "to": "succeeded", "attempt": int(pending["attempt"]), "artifacts": artifacts}, holder=holder)
        _autodeliver_locked(run_dir, repo_abs, graph, holder=holder)
        return
    if current == pending["new"]:
        _finalize_promotion_locked(run_dir, promoted, artifacts, holder=holder)
        _autodeliver_locked(run_dir, repo_abs, graph, holder=holder)
    elif current == pending["expected_old"]:
        _run_git(repo_abs, "update-ref", ref, pending["new"], pending["expected_old"])
        _finalize_promotion_locked(run_dir, promoted, artifacts, holder=holder)
        _autodeliver_locked(run_dir, repo_abs, graph, holder=holder)
    else:
        append_event_locked(run_dir, {"type": "blocked", "node": pending["node"], "attempt": int(pending["attempt"]), "reason": "promotion ref changed", "ref": ref}, holder=holder)
