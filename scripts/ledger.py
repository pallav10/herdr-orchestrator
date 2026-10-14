"""Authoritative event ledger, replay, revisions, and immutable append validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import Iterable, Iterator
import contextlib
import datetime as _dt
import fcntl
import hashlib
import ioutil
import graph_model
import json
import os
import ownership
import re
import errno
import math
import sys
import tempfile
import time
import uuid
from ioutil import PreconditionError, RunError, UsageError, _git_ref, _now, _read_json, _run_git, slug, consumed_items, atomic_json, atomic_text, canonical_path, run_cmd, sha256_file
from graph_model import HERDR_EXECUTORS, HOLDER_KEYS, LEASE_ACTIVE, LEASE_DRAINING, MODEL_KEYS, NODE_ID_RE, RETRY_ON, RUNTIME_KEYS, SCHEMA_VERSION, STATES, _as_list, _attempt_int, _check_holder, _condition_true, _eligible_blocked_continuation, _holder_payload, _request_fields, _required_attempt, _retry_allows, artifact_name, current_artifact, has_current_dispatch_intent, herdr_command_target, herdr_identity_matches, holder_exact, output_specs, require_active_holder, validate_continuation_routing, validate_graph, validate_model

_EVENT_TO = {
    "dispatched": "running",
    "attempt_started": "running",
    "ready": "ready",
    "planned": "planned",
    "succeeded": "succeeded",
    "invalidated": "invalidated",
    "retried": "planned",
    "blocked": "blocked",
    "failed": "failed",
    "interrupted": "interrupted",
    "skipped": "skipped",
}


def claim_run(docs_root: str | Path, job: str, *, created: str | None = None) -> dict[str, Any]:
    docs_abs = Path(canonical_path(docs_root))
    root = docs_abs / "runs"
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise PreconditionError("docs/runs must be a real directory")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise PreconditionError("docs/runs must be a real directory")
    if created is None:
        dt = _dt.datetime.now(_dt.timezone.utc)
    elif isinstance(created, _dt.datetime):
        dt = created if created.tzinfo else created.replace(tzinfo=_dt.timezone.utc)
    else:
        dt = _dt.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    stamp = dt.strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}-{slug(str(job))}"
    for n in range(1, 1000):
        run_id = stem if n == 1 else f"{stem}-{n:03d}"
        run_dir = root / run_id
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            # Another pane claimed this run id in the same second; try the next suffix.
            continue
    else:
        raise RunError(f"cannot claim run id for {stem}")

    run_abs = canonical_path(run_dir)
    if Path(run_abs).parent != Path(canonical_path(root)):
        raise PreconditionError("run path escaped docs/runs")
    run = {
        "id": run_id,
        "job": str(job),
        "created": dt.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "schema_version": SCHEMA_VERSION,
        "path": run_abs,
        "base_sha": None,
        "promotion_ref": None,
        "repo": None,
    }
    controller = {"epoch": 1, "workspace": None, "pane": None, "session": None, "run": run_abs, "dispatch": False, "lease": LEASE_ACTIVE}
    state = {"schema_version": SCHEMA_VERSION, "run": run_abs, "epoch": 1, "nodes": {}, "notified": []}
    atomic_json(run_dir / "run.json", run)
    atomic_text(run_dir / "events.jsonl", "")
    atomic_json(run_dir / "graph.json", {"schema_version": SCHEMA_VERSION, "run": run_abs, "nodes": {}})
    atomic_json(run_dir / "controller.json", controller)
    atomic_json(run_dir / "state.json", state)
    atomic_text(run_dir / "status.md", derive_status_md(state, {"nodes": {}}))
    (run_dir / "artifacts").mkdir()
    return load_run(run_dir)


def load_run(path) -> dict[str, Any]:
    """Return the run document (no nodes)."""
    run_dir = Path(canonical_path(path))
    committed = _committed_graph_revision(run_dir)
    return _run_from_revision(run_dir, committed)


def _run_from_revision(run_dir: Path, committed: dict[str, Any] | None) -> dict[str, Any]:
    run = dict(committed["run"]) if committed is not None else _read_json(run_dir / "run.json")
    if run.get("path") != canonical_path(run_dir):
        raise PreconditionError(f"run.json.path mismatch: {run.get('path')!r}")
    run["dir"] = canonical_path(run_dir)
    return run


def bind_session_run(run_dir, holder=None, *, lock_timeout=120) -> dict[str, Any]:
    """Return {state, controller, event}."""
    holder = _holder_payload(holder)
    run_dir = Path(canonical_path(run_dir))
    with with_lock(run_dir, lock_timeout=lock_timeout):
        snap = replay(run_dir)
        controller = snap["controller"]
        bound = all(controller.get(key) is not None for key in HOLDER_KEYS)
        if bound:
            if not holder_exact(controller, holder):
                raise PreconditionError("run is bound to a different holder")
            return {"state": snap["state"], "controller": controller, "event": None}
        return append_event_locked(run_dir, {"type": "session_run_bound", **holder}, holder=holder)


def _revision_digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _revision_path(run_dir: Path, revision: str) -> Path:
    if not isinstance(revision, str) or not revision.startswith("sha256:"):
        raise PreconditionError("invalid graph revision")
    digest = revision.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PreconditionError("invalid graph revision")
    return run_dir / "graph-revisions" / f"{digest}.json"


def _load_graph_revision(run_dir: Path, revision: str) -> dict[str, Any]:
    payload = _read_json(_revision_path(run_dir, revision))
    if not isinstance(payload.get("run"), dict) or not isinstance(payload.get("graph"), dict) or _revision_digest(payload) != revision:
        raise PreconditionError("graph revision payload mismatch")
    return payload


def _committed_graph_revision(run_dir: Path, events: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    if events is None:
        events, _truncated = _events(run_dir)
    epoch = next((int(event["epoch"]) for event in events if "epoch" in event), 1)
    committed = None
    for event in events:
        if event.get("type") == "lease_transfer" and event.get("epoch") == epoch:
            epoch += 1
            continue
        if event.get("epoch", epoch) != epoch:
            continue
        if event.get("type") == "graph_accepted" and event.get("revision"):
            committed = _load_graph_revision(run_dir, event["revision"])
    return committed


def load_graph(run_dir: str | Path) -> dict[str, Any]:
    run_dir = Path(canonical_path(run_dir))
    committed = _committed_graph_revision(run_dir)
    return _graph_from_revision(run_dir, committed)


def _graph_from_revision(run_dir: Path, committed: dict[str, Any] | None) -> dict[str, Any]:
    graph = dict(committed["graph"]) if committed is not None else _read_json(run_dir / "graph.json")
    if not isinstance(graph, dict):
        raise PreconditionError("graph.json must be an object")
    graph.setdefault("nodes", {})
    return graph


def load_accepted_graph(run_dir: str | Path) -> dict[str, Any]:
    """Read the accepted graph through committed revisions, never its derived cache alone."""
    return load_graph(run_dir)


def load_accepted_node(run_dir: str | Path, node_id: str) -> dict[str, Any]:
    graph = load_accepted_graph(run_dir)
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict):
        raise PreconditionError("graph.nodes must be an object")
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        raise PreconditionError(f"unknown node {node_id!r}")
    return node


def events_for_attempt(events: Iterable[dict[str, Any]], node: str, attempt: int, *, attempt_parser=int) -> Iterator[dict[str, Any]]:
    """Yield matching events in ledger order using the caller's attempt policy."""
    for event in events:
        if event.get("node") == node and attempt_parser(event.get("attempt", -1)) == attempt:
            yield event




def _node_is_active(run_dir: Path, node: dict[str, Any]) -> bool:
    """Running/blocked, or ready with an intent that may already have had a side effect.

    Herdr executors flush launch-intent.json before any Herdr call, so its absence proves
    no launch; native task/workflow leave no such marker, so their intent alone is active.
    """
    state = node.get("state")
    if state in graph_model.ACTIVE_STATES:
        return True
    if state != "ready" or not has_current_dispatch_intent(node):
        return False
    if node.get("executor") not in HERDR_EXECUTORS:
        return True
    return (attempt_dir(run_dir, node["id"], int(node["attempt"])) / "launch-intent.json").exists()


def _validate_revision_against_runtime(run_dir: Path, state: dict[str, Any], graph: dict[str, Any], previous: dict[str, Any] | None = None) -> None:
    new_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), dict) else {}
    old_nodes = previous.get("nodes") if isinstance(previous, dict) and isinstance(previous.get("nodes"), dict) else {}
    for node_id, node in (state.get("nodes") or {}).items():
        if not isinstance(node, dict):
            continue
        candidate = new_nodes.get(node_id)
        if node.get("state") == "ready" and has_current_dispatch_intent(node) and isinstance(candidate, dict):
            # ownership.json is an immutable per-attempt snapshot of the intent's owns
            if list(_as_list(candidate.get("owns"), f"{node_id}.owns")) != list(node.get("dispatch_intent", {}).get("owns") or []):
                raise PreconditionError(f"cannot rewrite owns of node {node_id} with a pending dispatch_intent")
        if not _node_is_active(run_dir, node):
            continue
        if node_id not in new_nodes:
            raise PreconditionError(f"cannot remove active node {node_id}")
        prior = old_nodes.get(node_id)
        if not isinstance(prior, dict) or not isinstance(candidate, dict):
            raise PreconditionError(f"cannot rewrite active node {node_id}")
        # phase is metadata-only; every other accepted plan field stays frozen.
        if {key: value for key, value in prior.items() if key != "phase"} != {key: value for key, value in candidate.items() if key != "phase"}:
            raise PreconditionError(f"cannot rewrite active node {node_id}")


def _promotion_ref_checked_out(repo, ref: str) -> bool:
    proc = run_cmd(
        ["git", "-C", canonical_path(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RunError(f"git worktree list --porcelain failed: {detail}")
    for line in proc.stdout.splitlines():
        if line.startswith("branch ") and line[len("branch "):].strip() == ref:
            return True
    return False


def _refuse_checked_out_promotion_ref(repo, ref: str) -> None:
    if not isinstance(ref, str) or not ref:
        raise PreconditionError("run has no accepted promotion ref")
    if _promotion_ref_checked_out(repo, ref):
        raise PreconditionError("promotion ref is checked out")


def _materialize_graph_revision_locked(run_dir: Path, payload: dict[str, Any]) -> None:
    run = payload["run"]
    graph = payload["graph"]
    if payload.get("initialize_ref"):
        _refuse_checked_out_promotion_ref(run["repo"], run["promotion_ref"])
    atomic_json(run_dir / "run.json", run)
    atomic_json(run_dir / "graph.json", graph)
    if payload.get("initialize_ref"):
        repo = run["repo"]
        ref = run["promotion_ref"]
        if _git_ref(repo, ref) is None:
            events, _truncated = _events(run_dir)
            if any(event.get("type") == "promoted" and event.get("ref") == ref for event in events):
                raise PreconditionError("promoted ref is missing; refusing to reset it to base")
            _run_git(repo, "update-ref", ref, run["base_sha"])


def accept_graph(run_dir, graph: dict[str, Any], *, repo: str | Path, holder=None, lock_timeout=120) -> dict[str, Any]:
    """Return the accepted graph document (schema_version, run, base_sha, promotion_ref, nodes)."""
    run_dir = Path(canonical_path(run_dir))
    repo_abs = canonical_path(repo)
    with with_lock(run_dir, lock_timeout=lock_timeout):
        snap = replay(run_dir)
        controller = snap["controller"]
        require_active_holder(controller, holder, "accept_graph")
        run = load_run(run_dir)
        if "run" in graph and graph["run"] != run["path"]:
            raise PreconditionError("graph.run must match run path")
        validate_graph(graph)
        _validate_revision_against_runtime(run_dir, snap["state"], graph, load_graph(run_dir))

        first = not run.get("base_sha")
        recorded_repo = run.get("repo")
        if first:
            if recorded_repo is not None and (not isinstance(recorded_repo, str) or canonical_path(recorded_repo) != repo_abs):
                raise PreconditionError("run repo does not match accepted repo")
            accepted_repo = repo_abs
            base_sha = _run_git(repo_abs, "rev-parse", "HEAD")
            promotion_ref = f"refs/heads/herdr/{run['id']}"
        else:
            if not isinstance(recorded_repo, str) or canonical_path(recorded_repo) != repo_abs:
                raise PreconditionError("run repo does not match accepted repo")
            accepted_repo = canonical_path(recorded_repo)
            base_sha = run["base_sha"]
            promotion_ref = run.get("promotion_ref")
            if not isinstance(promotion_ref, str) or not promotion_ref:
                raise PreconditionError("run has no accepted promotion ref")
        graph_out = {"schema_version": SCHEMA_VERSION, "run": run["path"], "base_sha": base_sha, "promotion_ref": promotion_ref, "nodes": graph["nodes"]}
        run_out = {k: v for k, v in run.items() if k != "dir"}
        run_out.update({"repo": accepted_repo, "base_sha": base_sha, "promotion_ref": promotion_ref})
        if first:
            _refuse_checked_out_promotion_ref(repo_abs, promotion_ref)


        committed = _committed_graph_revision(run_dir)
        if committed is not None and committed["run"] == run_out and committed["graph"] == graph_out:
            _materialize_graph_revision_locked(run_dir, committed)
            _materialize_locked(run_dir, replay(run_dir), graph_out)
            return graph_out

        ref_before = _git_ref(repo_abs, promotion_ref)
        if first and ref_before not in (None, base_sha):
            raise PreconditionError("promotion ref already exists")
        staged = {"run": run_out, "graph": graph_out, "initialize_ref": first}
        revision = _revision_digest(staged)
        revision_path = _revision_path(run_dir, revision)
        revision_path.parent.mkdir(parents=True, exist_ok=True)
        if revision_path.exists():
            if _read_json(revision_path) != staged:
                raise PreconditionError("graph revision payload mismatch")
        else:
            atomic_json(revision_path, staged)
        append_event_locked(run_dir, {"type": "graph_accepted", "revision": revision}, holder=holder)
        _materialize_graph_revision_locked(run_dir, staged)
        _materialize_locked(run_dir, replay(run_dir), graph_out)
        return graph_out


def write_lock_owner(path: Path, owner: dict[str, Any]) -> None:
    """Disposable diagnostics: atomic replacement, deliberately no fsync."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".run.lock.owner.", delete=False) as fh:
            temporary = Path(fh.name)
            json.dump(owner, fh)
            fh.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_lock_owner(path: Path) -> str:
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(owner, dict):
            raise ValueError("owner is not an object")
        fields = []
        for key in ("pid", "script", "node", "since"):
            value = owner.get(key)
            if key == "node" and value is None:
                continue
            text = "unknown" if value is None else str(value)
            fields.append(f"{key}={text.replace(chr(10), ' ').replace(chr(13), ' ')}")
        return " ".join(fields) + " (diagnostic, unverified)"
    except (OSError, ValueError, TypeError):
        return "holder unknown/unverified (diagnostic, unverified)"


@contextlib.contextmanager
def with_lock(run_dir, *, lock_timeout=120, node=None) -> Iterator[None]:
    try:
        timeout = float(lock_timeout)
    except (TypeError, ValueError) as exc:
        raise UsageError("lock_timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise UsageError("lock_timeout must be a finite positive number")
    run_dir = Path(canonical_path(run_dir))
    run_dir.mkdir(parents=True, exist_ok=True)
    owner_path = run_dir / "run.lock.owner"
    deadline = time.monotonic() + timeout
    with open(run_dir / "run.lock", "a+", encoding="utf-8") as fh:
        while True:
            if time.monotonic() >= deadline:
                raise PreconditionError(
                    f"timed out acquiring {run_dir / 'run.lock'}: "
                    f"{read_lock_owner(owner_path)}"
                )
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EINTR):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PreconditionError(
                        f"timed out acquiring {run_dir / 'run.lock'}: "
                        f"{read_lock_owner(owner_path)}"
                    ) from None
                if exc.errno != errno.EINTR:
                    time.sleep(min(0.05, remaining))
        token = uuid.uuid4().hex
        original_error = None
        owner_written = False
        try:
            owner = {"pid": os.getpid(), "script": sys.argv[0], "node": node,
                     "since": _now(), "run": str(run_dir), "lock": "run.lock",
                     "mode": "exclusive", "epoch": None, "token": token}
            try:
                write_lock_owner(owner_path, owner)
                owner_written = True
            except (OSError, TypeError, ValueError) as exc:
                raise PreconditionError(f"cannot write {owner_path}: {exc}") from exc
            yield
        except BaseException as exc:
            original_error = exc
            raise
        finally:
            try:
                if owner_written:
                    try:
                        recorded = json.loads(owner_path.read_text(encoding="utf-8"))
                    except FileNotFoundError:
                        recorded = None
                    if isinstance(recorded, dict) and recorded.get("token") == token:
                        owner_path.unlink()
            except (OSError, ValueError, TypeError) as exc:
                message = f"cannot clean up {owner_path}: {exc}"
                if original_error is not None:
                    print(message.replace("\n", " "), file=sys.stderr)
                else:
                    raise PreconditionError(message) from exc
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def append_event(run_dir, event: dict[str, Any], *, holder=None, lock_timeout=120) -> dict[str, Any]:
    """Return {state, controller, event}."""
    if ioutil._DISPATCH_GUARD_DEPTH:
        raise PreconditionError("append_event cannot nest inside dispatch_guard")
    run_dir = Path(canonical_path(run_dir))
    with with_lock(run_dir, lock_timeout=lock_timeout, node=event.get("node") if isinstance(event, dict) else None):
        return append_event_locked(run_dir, event, holder=holder)


def append_event_locked(run_dir: Path, event: dict[str, Any], *, holder=None) -> dict[str, Any]:
    """Append one event, preserving the single-event result shape."""
    if ioutil._DISPATCH_GUARD_DEPTH:
        raise PreconditionError("append_event cannot nest inside dispatch_guard")
    if not isinstance(event, dict):
        raise UsageError("event must be an object")
    snap = append_events_locked(run_dir, [event], holder=holder)
    return {"state": snap["state"], "controller": snap["controller"], "event": snap["events"][-1]}


def append_events_locked(run_dir: Path, events: list[dict[str, Any]], *, holder=None) -> dict[str, Any]:
    """Validate the complete batch before writing; return the refreshed replay snapshot."""
    if ioutil._DISPATCH_GUARD_DEPTH:
        raise PreconditionError("append_event cannot nest inside dispatch_guard")
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise UsageError("events must be a list of objects")
    run_dir = Path(canonical_path(run_dir))
    snap, graph = _replay(run_dir)
    if snap.get("truncated_tail"):
        raise PreconditionError("events.jsonl has truncated tail; repair before appending")
    lines = []
    intents = []
    snap["state"]["notified"] = set(snap["state"]["notified"])
    for event in events:
        active_epoch = snap["controller"]["epoch"]
        out = dict(event)
        if out.get("type") == "lease_drain_started" and isinstance(holder, dict):
            for key in HOLDER_KEYS:
                if out.get(key) is None and holder.get(key) is not None:
                    out[key] = holder[key]
        if isinstance(out.get("artifacts"), list):
            out["artifacts"] = [({**art, "path": canonical_path(art["path"])} if isinstance(art, dict) and "path" in art else art) for art in out["artifacts"]]
        _check_holder(snap["controller"], out, holder)
        out.setdefault("t", _now())
        if "epoch" in out and out["epoch"] != active_epoch:
            raise PreconditionError(f"stale event epoch {out['epoch']} != {active_epoch}")
        out.setdefault("epoch", active_epoch)
        if not lines:
            # Preserve the full preflight sweep, after holder/epoch authorization.
            _repair_dispatch_snapshots_locked(run_dir, snap)
        _validate_event_append(run_dir, snap, graph, out)
        lines.append(json.dumps(out, sort_keys=True, separators=(",", ":")) + "\n")
        if out.get("type") == "graph_accepted" and out.get("revision"):
            graph = _graph_from_revision(run_dir, _load_graph_revision(run_dir, out["revision"]))
            _apply_graph_revision(snap["state"], graph)
        _apply_event(snap["state"], snap["controller"], graph, out)
        snap["state"]["epoch"] = snap["controller"]["epoch"]
        snap["state"]["dispatch"] = snap["controller"]["dispatch"]
        snap["events"].append(out)
        if out.get("type") == "dispatch_intent":
            intents.append((out["node"], out["attempt"]))
    if lines:
        with open(run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write("".join(lines))
            fh.flush()
            os.fsync(fh.fileno())
        # Never use the pre-write projection as post-write authority.
        snap, graph = _replay(run_dir)
        for intent in intents:
            _repair_dispatch_snapshots_locked(run_dir, snap, only=intent)
    else:
        snap["state"]["notified"] = sorted(snap["state"]["notified"])
    _materialize_locked(run_dir, snap, graph)
    return snap


def _events(run_dir: Path) -> tuple[list[dict[str, Any]], bool]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return [], False
    events: list[dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if idx == len(lines) - 1:
                return events, True
            raise PreconditionError(f"invalid JSON in events.jsonl line {idx + 1}: {exc}") from exc
    return events, False


def _initial_state(run: dict[str, Any], graph: dict[str, Any], epoch: int = 1) -> dict[str, Any]:
    nodes = {}
    for node_id, node in graph.get("nodes", {}).items():
        material = dict(node)
        material.update({"id": node_id, "executor": node.get("executor"), "state": "planned", "attempt": 0, "artifacts": []})
        nodes[node_id] = material
    return {"schema_version": SCHEMA_VERSION, "run": run["path"], "epoch": epoch, "nodes": nodes, "notified": []}


def _resolve_consumed(state: dict[str, Any], graph: dict[str, Any], node_id: str) -> list[dict[str, Any]]:
    consumed: list[dict[str, Any]] = []
    node = graph.get("nodes", {}).get(node_id, {})
    for inp in node.get("inputs", []) or []:
        art = current_artifact(state, inp["from"], inp["output"])
        if not art:
            continue
        consumed.append({
            "alias": inp["alias"],
            "producer": inp["from"],
            "producer_attempt": state["nodes"].get(inp["from"], {}).get("attempt", 0),
            "output": inp["output"],
            "path": canonical_path(art["path"]),
            "hash": art["hash"],
        })
    return consumed


def _complete_json(path: Path) -> Any | None:
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, OSError):
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _artifacts_root(run_dir: Path) -> Path:
    run_real = Path(canonical_path(run_dir))
    artifacts = Path(canonical_path(run_real / "artifacts"))
    if artifacts == run_real or not _relative_to(artifacts, run_real):
        raise PreconditionError("artifacts path escapes run")
    return artifacts


def _ensure_attempt_snapshot(run_dir: Path, node_id: str, attempt: int, consumed: list[dict[str, Any]], owns: list[str]) -> Path:
    attempt = _attempt_int(attempt, "attempt")
    if attempt < 1:
        raise PreconditionError("attempt must be >= 1")
    path = attempt_dir(run_dir, node_id, attempt)
    if path.exists() and not path.is_dir():
        raise PreconditionError(f"attempt path is not a directory: {path}")
    expected = {
        path / "consumed_artifacts.json": consumed if isinstance(consumed, list) else [],
        path / "ownership.json": {"slices": {node_id: owns if isinstance(owns, list) else []}},
    }
    for target, value in expected.items():
        target_real = Path(canonical_path(target))
        if target_real == path or not _relative_to(target_real, path):
            raise PreconditionError(f"attempt snapshot escapes attempt dir: {target}")
        if target.exists() and not target.is_file():
            raise PreconditionError(f"attempt snapshot is not a file: {target}")
        if not target.exists():
            continue
        actual = _complete_json(target)
        if actual is None or actual != value:
            raise PreconditionError(f"attempt snapshot is immutable: {target}")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise PreconditionError(f"attempt path is not a directory: {path}")
    for target, value in expected.items():
        if target.exists() and not target.is_file():
            raise PreconditionError(f"attempt snapshot is not a file: {target}")
        if not target.exists():
            atomic_json(target, value)
    return path


def _repair_dispatch_snapshots_locked(run_dir: Path, snap: dict[str, Any], only: tuple[str, int] | None = None) -> None:
    for event in snap.get("events") or []:
        if event.get("type") != "dispatch_intent":
            continue
        node_id = event.get("node")
        if not isinstance(node_id, str) or "attempt" not in event:
            continue
        if only is not None and (node_id, int(event["attempt"])) != only:
            continue
        consumed = event.get("consumed")
        owns = event.get("owns")
        if not isinstance(consumed, list) or not isinstance(owns, list):
            raise PreconditionError("dispatch_intent missing immutable snapshot data")
        _ensure_attempt_snapshot(run_dir, node_id, int(event["attempt"]), consumed, owns)


def _relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def attempt_dir(run_dir: Path, node_id: str, attempt: int) -> Path:
    if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
        raise PreconditionError(f"invalid node id {node_id!r}")
    attempt = _attempt_int(attempt, "attempt")
    run_real = Path(canonical_path(run_dir))
    artifacts = _artifacts_root(run_real)
    resolved = Path(canonical_path(artifacts / node_id / str(attempt)))
    if resolved == artifacts or not _relative_to(resolved, artifacts):
        raise PreconditionError(f"attempt path escapes artifacts: {node_id}")
    if not _relative_to(resolved, run_real):
        raise PreconditionError(f"attempt path escapes run: {node_id}")
    return resolved


def _artifact_file_name(name: str, typ: str) -> str:
    return "vcs.json" if typ == "vcs" else "integration.json" if typ == "integration" else f"{name}.json"


def read_consumed_snapshot(run_dir: Path, node_id: str, attempt: int) -> list[dict[str, Any]]:
    consumed = consumed_items(attempt_dir(run_dir, node_id, attempt) / "consumed_artifacts.json")
    for item in consumed:
        for key in ("alias", "producer", "output", "path", "hash"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise PreconditionError(f"consumed_artifacts.json entry missing {key}")
        if "producer_attempt" not in item:
            raise PreconditionError("consumed_artifacts.json entry missing producer_attempt")
    return consumed


def _authoritative_consumed(run_dir: Path, node_id: str, attempt: int, node: dict[str, Any]) -> list[dict[str, Any]]:
    intent = node.get("dispatch_intent") if isinstance(node.get("dispatch_intent"), dict) else {}
    consumed = intent.get("consumed")
    owns = intent.get("owns")
    if not isinstance(consumed, list) or not isinstance(owns, list):
        raise PreconditionError("dispatch_intent missing immutable snapshot data")
    _ensure_attempt_snapshot(run_dir, node_id, attempt, consumed, owns)
    for item in consumed:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("hash"), str):
            raise PreconditionError("dispatch_intent missing immutable snapshot data")
        if _file_hash(Path(item["path"])) != item["hash"]:
            raise PreconditionError("consumed artifact hash mismatch")
    return consumed


def _file_hash(path: Path) -> str:
    try:
        return sha256_file(path)
    except OSError as exc:
        raise PreconditionError(f"cannot hash {path}: {exc}") from exc


def _validate_artifact_records(run_dir: Path, graph: dict[str, Any], node_id: str, attempt: int, artifacts) -> list[dict[str, Any]]:
    if artifacts is None:
        artifacts = []
    if not isinstance(artifacts, list):
        raise PreconditionError("artifacts must be a list")
    specs = output_specs(graph, node_id)
    attempt_path = attempt_dir(run_dir, node_id, attempt)
    seen: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise PreconditionError("artifacts entries must be objects")
        name = artifact_name(artifact)
        if name not in specs:
            raise PreconditionError(f"undeclared output artifact {name!r}")
        if name in seen:
            raise PreconditionError(f"duplicate artifact for output {name}")
        if not isinstance(artifact.get("hash"), str) or not artifact["hash"].startswith("sha256:"):
            raise PreconditionError(f"artifact {name} missing sha256 hash")
        if not isinstance(artifact.get("path"), str):
            raise PreconditionError(f"artifact {name} missing path")
        path = Path(canonical_path(artifact["path"]))
        if not _relative_to(path, attempt_path):
            raise PreconditionError(f"artifact {name} path is not attempt-local")
        expected_name = _artifact_file_name(name, specs[name])
        if path.name != expected_name:
            raise PreconditionError(f"artifact {name} path must be {expected_name}")
        if _file_hash(path) != artifact["hash"]:
            raise PreconditionError(f"artifact {name} hash mismatch")
        typ = artifact.get("type", specs[name])
        if typ != specs[name]:
            raise PreconditionError(f"artifact {name} type mismatch")
        seen[name] = {**artifact, "name": name, "type": specs[name], "path": str(path), "hash": artifact["hash"]}
    missing = [name for name in specs if name not in seen]
    if missing:
        raise PreconditionError(f"missing declared output {missing[0]}")
    return [seen[name] for name in specs]


def load_dispatch_identity(attempt_dir: Path) -> dict[str, Any] | None:
    path = attempt_dir / "dispatch-identity.json"
    return _read_json(path) if path.is_file() else None


def writer_path_findings(repo_root: str, node_id: str, owns: list, *, integrates: bool, since: str | None, git_facts: ioutil.GitFacts | None = None):
    """Shared writer/integrator ownership classification at receipt boundaries."""
    if git_facts is not None:
        status = git_facts.git(repo_root, "status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0:
            raise PreconditionError(status.stderr.strip() or "git status failed")
        changed = set() if not status.stdout.strip() else set(ownership.git_changed_paths(repo_root))
    else:
        changed = set(ownership.git_changed_paths(repo_root))
    if not integrates and since:
        if git_facts is None:
            changed |= set(ownership.git_committed_paths(repo_root, since))
        else:
            diff = git_facts.git(repo_root, "diff", "--name-only", "-z", f"{since}..HEAD")
            if diff.returncode != 0:
                raise PreconditionError(diff.stderr.decode("utf-8", "surrogateescape").strip() or "git diff failed")
            changed.update(path.decode("utf-8", "surrogateescape") for path in diff.stdout.split(b"\0") if path)
    return ownership.partition_paths_by_slice(sorted(changed), repo_root, {node_id: owns})


def _validate_writer_gates(run_dir: Path, node_id: str, attempt: int, node: dict[str, Any], vcs_path: Path, *, git_facts: ioutil.GitFacts | None = None, since: str | None = None) -> None:
    identity = load_dispatch_identity(attempt_dir(run_dir, node_id, attempt))
    if not isinstance(identity, dict):
        raise PreconditionError("writer success requires dispatch-identity.json")
    run = load_run(run_dir)
    handle = node.get("handle") if isinstance(node.get("handle"), dict) else {}
    model = node.get("model")
    intent = node.get("dispatch_intent") if isinstance(node.get("dispatch_intent"), dict) else {}
    tokens = identity.get("tokens") if isinstance(identity.get("tokens"), dict) else {}
    if not isinstance(identity.get("run"), str) or canonical_path(identity["run"]) != run["path"]:
        raise PreconditionError("dispatch-identity.json does not match attempt")
    expected = {"run_id": run["id"], "node": node_id, "attempt": str(attempt)}
    if any(str(identity.get(key)) != value for key, value in expected.items()):
        raise PreconditionError("dispatch-identity.json does not match attempt")
    # Handles recorded by older spawn-agent builds omit `repo`; the accepted run repo is the same fact.
    expected_handle = {key: handle.get(key) for key in ("workspace", "pane", "worktree_path", "branch", "base")}
    expected_handle["repo"] = handle.get("repo") or run.get("repo")
    for key, value in expected_handle.items():
        if not isinstance(value, str) or not value or identity.get(key) != value:
            raise PreconditionError(f"dispatch-identity handle does not match dispatched event ({key})")
    executor = node.get("executor")
    identity_target = herdr_command_target(identity, executor)
    handle_target = herdr_command_target(handle, executor)
    if identity_target is None or handle_target is None or identity_target != handle_target:
        raise PreconditionError("dispatch-identity handle does not match dispatched event")
    if validate_model(identity.get("routing")) != validate_model(model):
        raise PreconditionError("dispatch-identity routing does not match dispatched model")

    if not isinstance(run.get("repo"), str) or canonical_path(identity["repo"]) != canonical_path(run["repo"]):
        raise PreconditionError("dispatch-identity repo does not match run repo")

    vcs = _read_json(vcs_path)
    if not isinstance(vcs, dict):
        raise PreconditionError("vcs.json must be an object")
    if vcs.get("base_sha") != run.get("base_sha"):
        raise PreconditionError("vcs.base_sha does not match run base")
    if canonical_path(vcs.get("repo")) != canonical_path(identity["repo"]):
        raise PreconditionError("vcs repo does not match dispatch identity")
    if canonical_path(vcs.get("worktree")) != canonical_path(identity["worktree_path"]):
        raise PreconditionError("vcs worktree does not match dispatch identity")
    if vcs.get("branch") != identity["branch"]:
        raise PreconditionError("vcs branch does not match dispatch identity")
    if vcs.get("base_sha") != identity["base"]:
        raise PreconditionError("vcs base_sha does not match dispatch identity")

    owns = intent.get("owns")
    if not isinstance(owns, list):
        raise PreconditionError("dispatch_intent missing owns")
    expected_ownership = {"slices": {node_id: owns}}
    if _read_json(attempt_dir(run_dir, node_id, attempt) / "ownership.json") != expected_ownership:
        raise PreconditionError("ownership.json does not match dispatch_intent")
    repo_root = canonical_path(vcs["worktree"])
    integrates = any(isinstance(o, dict) and o.get("type") == "integration" for o in (node.get("outputs") or []))
    committed = not integrates and isinstance(vcs.get("result"), dict) and vcs["result"].get("form") == "commit"
    baseline = since if committed else None
    if committed and not baseline:
        if git_facts is None:
            baseline = ownership.resolve_diff_base(repo_root, run.get("promotion_ref"), vcs["base_sha"])
        else:
            promotion_ref = run.get("promotion_ref")
            if promotion_ref:
                merge = git_facts.git(repo_root, "merge-base", promotion_ref, "HEAD")
                baseline = merge.stdout.strip() if merge.returncode == 0 else None
            baseline = baseline or vcs["base_sha"]
    unowned, overlaps, _by_slice = writer_path_findings(repo_root, node_id, owns, integrates=integrates, since=baseline, git_facts=git_facts)
    if unowned or overlaps:
        offending = [*unowned, *(item["path"] for item in overlaps)]
        raise PreconditionError(f"ownership check failed: {', '.join(offending[:5])}")


def validate_vcs_payload(path: Path, *, expected_sha: str | None = None, require_commit: bool = False, attempt_dir: Path | None = None, promotion_ref=None, git_facts: ioutil.GitFacts | None = None) -> str | None:
    data = _read_json(path)
    def git(worktree, *args):
        if git_facts is not None:
            return git_facts.git(worktree, *args)
        return run_cmd(["git", "-C", worktree, *args], capture_output=True, text=True)
    if not isinstance(data, dict):
        raise PreconditionError("vcs.json must be an object")
    for key in ("worktree", "repo"):
        if not isinstance(data.get(key), str) or not data[key] or not Path(data[key]).is_absolute():
            raise PreconditionError(f"vcs.{key} must be an absolute path")
        canonical_path(data[key])
    for key in ("branch", "base_sha"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise PreconditionError(f"vcs.{key} must be a string")
    result = data.get("result")
    if not isinstance(result, dict):
        raise PreconditionError("vcs.result must be an object")
    form = result.get("form")
    if form == "commit":
        sha = result.get("sha")
        if not isinstance(sha, str) or not sha or "path" in result or "sha256" in result:
            raise PreconditionError("commit result must contain only a non-empty sha")
        if expected_sha is not None and sha != expected_sha:
            raise PreconditionError("vcs.result.sha does not match promoted sha")
        worktree = canonical_path(data["worktree"])
        commit = git(worktree, "rev-parse", "-q", "--verify", f"{sha}^{{commit}}")
        if commit.returncode != 0 or commit.stdout.strip() != sha:
            raise PreconditionError("vcs.result.sha is not a commit object")
        head = git(worktree, "rev-parse", "HEAD")
        if head.returncode != 0 or head.stdout.strip() != sha:
            raise PreconditionError("vcs.result.sha does not match worktree HEAD")
        ancestor = git(worktree, "merge-base", "--is-ancestor", data["base_sha"], sha)
        if ancestor.returncode != 0:
            raise PreconditionError("vcs commit does not descend from recorded base")
        return sha

    if form == "diff":
        if require_commit:
            raise PreconditionError("promotion requires a commit vcs result")
        raw_path = result.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise PreconditionError("diff result must name a path")
        expected_path = Path(canonical_path(attempt_dir if attempt_dir is not None else path.parent)) / "change.bin"
        if Path(canonical_path(raw_path)) != expected_path:
            raise PreconditionError("diff result path must be attempt change.bin")
        expected = result.get("sha256")
        if not isinstance(expected, str) or not expected:
            raise PreconditionError("diff result must contain sha256")
        expected = expected if expected.startswith("sha256:") else "sha256:" + expected
        if _file_hash(expected_path) != expected:
            raise PreconditionError("diff sha256 mismatch")
        return None
    if form == "none":
        if require_commit:
            raise PreconditionError("promotion requires a commit vcs result")
        if set(result) != {"form"}:
            raise PreconditionError("none result must contain only form")
        worktree = canonical_path(data["worktree"])
        ancestor = git(worktree, "merge-base", "--is-ancestor", data["base_sha"], "HEAD")
        if ancestor.returncode != 0:
            raise PreconditionError("vcs none result requires HEAD to descend from recorded base")
        head = git(worktree, "rev-parse", "HEAD")
        on_line = head.returncode == 0 and head.stdout.strip() == data["base_sha"]
        if not on_line and head.returncode == 0 and promotion_ref:
            on_line = git(worktree, "merge-base", "--is-ancestor", "HEAD", promotion_ref).returncode == 0
        if not on_line:
            raise PreconditionError("vcs none result requires worktree HEAD == base_sha or an already-promoted sha")
        status = git(worktree, "status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0 or status.stdout.strip():
            raise PreconditionError("vcs none result requires a clean worktree")
        return None
    raise PreconditionError("vcs.result.form must be commit, diff, or none")


def _consumed_receipt_tuple(item: dict[str, Any]) -> tuple[str, int, str, str, str | None]:
    if not isinstance(item, dict):
        raise PreconditionError("integration.consumed entries must be objects")
    producer = item.get("producer")
    output = item.get("output")
    digest = item.get("hash")
    raw_attempt = item.get("producer_attempt", item.get("attempt"))
    attempt = _attempt_int(raw_attempt, "consumed attempt")
    if attempt < 1:
        raise PreconditionError("consumed attempt must be >= 1")
    if not all(isinstance(value, str) and value for value in (producer, output, digest)):
        raise PreconditionError(f"integration.json missing consumed hash {item.get('hash')}")
    alias = item.get("alias")
    return (producer, attempt, output, digest, alias if isinstance(alias, str) and alias else None)


def vcs_typed_consumed(graph: dict[str, Any], consumed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in consumed:
        if not isinstance(item, dict):
            raise PreconditionError("consumed artifacts entries must be objects")
        specs = output_specs(graph, item.get("producer"))
        if specs.get(item.get("output")) == "vcs":
            out.append(item)
    return out


def validate_integration_payload(path: Path, consumed: list[dict[str, Any]], *, expected_sha: str | None = None, expected_base: str | None = None) -> None:
    data = _read_json(path)
    if not isinstance(data, dict):
        raise PreconditionError("integration.json must be an object")
    base_sha = data.get("base_sha")
    if not isinstance(base_sha, str) or not base_sha:
        raise PreconditionError("integration.base_sha must be a string")
    if expected_base is not None and base_sha != expected_base:
        raise PreconditionError("integration.base_sha does not match recorded base")
    commit = data.get("commit")
    head = data.get("head")
    if not isinstance(commit, str) or not commit or not isinstance(head, str) or not head:
        raise PreconditionError("integration commit/head must be non-empty")
    if commit != head:
        raise PreconditionError("integration commit/head mismatch")
    if expected_sha is not None and (commit != expected_sha or head != expected_sha):
        raise PreconditionError("integration commit/head does not match promoted sha")
    observed = data.get("consumed", [])
    if not isinstance(observed, list):
        raise PreconditionError("integration.consumed must be a list")
    expected = [_consumed_receipt_tuple(item) for item in consumed]
    actual = [_consumed_receipt_tuple(item) for item in observed]
    if len(actual) != len(expected):
        raise PreconditionError("integration.consumed sequence mismatch")
    for exp, act in zip(expected, actual):
        if act[:4] != exp[:4]:
            raise PreconditionError(f"integration.json missing consumed hash {exp[3]}")
        if act[4] is not None and act[4] != exp[4]:
            raise PreconditionError("integration consumed alias mismatch")


def _promotion_artifacts(run_dir: Path, graph: dict[str, Any], node_id: str, attempt: int, node: dict[str, Any], new_sha: str) -> list[dict[str, Any]]:
    specs = output_specs(graph, node_id)
    if "integration" not in specs.values() or "vcs" not in specs.values():
        raise PreconditionError("promotion requires declared integration and vcs outputs")
    attempt_path = attempt_dir(run_dir, node_id, attempt)
    consumed = vcs_typed_consumed(graph, _authoritative_consumed(run_dir, node_id, attempt, node))
    integration_path = attempt_path / "integration.json"
    vcs_path = attempt_path / "vcs.json"
    run = load_run(run_dir)
    validate_integration_payload(integration_path, consumed, expected_sha=new_sha, expected_base=run.get("base_sha"))
    validate_vcs_payload(vcs_path, expected_sha=new_sha, require_commit=True, attempt_dir=attempt_path)
    _validate_writer_gates(run_dir, node_id, attempt, node, vcs_path)

    records: dict[str, dict[str, Any]] = {}
    for name, typ in specs.items():
        path = attempt_path / _artifact_file_name(name, typ)
        records[name] = {"name": name, "type": typ, "path": str(path), "hash": _file_hash(path)}
    return [records[name] for name in specs]


def _matching_promoted_event(events: list[dict[str, Any]], node_id: str, attempt: int) -> dict[str, Any] | None:
    promoted = None
    for event in events_for_attempt((event for event in events if event.get("type") == "promoted"), node_id, attempt):
        promoted = event
    return promoted


def validate_promoted_receipt(run_dir: Path, events: list[dict[str, Any]], node_id: str, attempt: int) -> None:
    promoted = _matching_promoted_event(events, node_id, attempt)
    if not promoted:
        raise PreconditionError("integration success requires promoted event")
    receipt = _read_json(attempt_dir(run_dir, node_id, attempt) / "promoted.json")
    if not isinstance(receipt, dict):
        raise PreconditionError("promoted.json must be an object")
    expected = {"ref": promoted.get("ref"), "old": promoted.get("expected_old"), "new": promoted.get("new")}
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise PreconditionError("promoted.json does not match promoted event")


def _validate_success(run_dir: Path, snap: dict[str, Any], graph: dict[str, Any], event: dict[str, Any], node_id: str, node: dict[str, Any]) -> None:
    attempt = _required_attempt(event, node)
    if attempt < 1:
        raise PreconditionError("succeeded requires attempt >= 1")
    if node.get("state") == "succeeded" and _matching_promoted_event(snap["events"], node_id, attempt) is not None:
        raise PreconditionError("already succeeded by promotion")
    if node.get("state") != "running":
        raise PreconditionError("succeeded requires a running attempt")
    artifacts = _validate_artifact_records(run_dir, graph, node_id, attempt, event.get("artifacts"))
    event["artifacts"] = artifacts
    attempt_path = attempt_dir(run_dir, node_id, attempt)
    by_type = {artifact["type"]: artifact for artifact in artifacts}
    plan = graph.get("nodes", {}).get(node_id, {})
    run = load_run(run_dir)
    if plan.get("worktree") or "vcs" in by_type:
        vcs = by_type.get("vcs")
        if not vcs or Path(vcs["path"]).name != "vcs.json":
            raise PreconditionError("writer success requires vcs.json")
        validate_vcs_payload(Path(vcs["path"]), attempt_dir=attempt_path, promotion_ref=run.get("promotion_ref"))
        _validate_writer_gates(run_dir, node_id, attempt, node, Path(vcs["path"]))
    promoted = _matching_promoted_event(snap["events"], node_id, attempt)
    if "integration" in by_type or promoted:
        integration = by_type.get("integration")
        integration_path = Path(integration["path"]) if integration else attempt_dir(run_dir, node_id, attempt) / "integration.json"
        if integration_path.name != "integration.json":
            raise PreconditionError("integration success requires integration.json")
        consumed = vcs_typed_consumed(graph, read_consumed_snapshot(run_dir, node_id, attempt))
        expected_sha = promoted.get("new") if promoted else None
        validate_integration_payload(integration_path, consumed, expected_sha=expected_sha, expected_base=run.get("base_sha"))
        validate_promoted_receipt(run_dir, snap["events"], node_id, attempt)


def _validate_ready(state: dict[str, Any], graph: dict[str, Any], node_id: str) -> None:
    nodes = state["nodes"]
    plan = graph.get("nodes", {}).get(node_id, {})
    for dep in plan.get("depends_on", []) or []:
        if nodes.get(dep, {}).get("state") != "succeeded":
            raise PreconditionError(f"node {node_id} dependency {dep} is not succeeded")
    for inp in plan.get("inputs", []) or []:
        if inp.get("required", True):
            producer = nodes.get(inp["from"], {})
            art = current_artifact(state, inp["from"], inp["output"])
            if producer.get("state") == "skipped" or not art or not art.get("path") or not art.get("hash"):
                raise PreconditionError(f"node {node_id} input {inp['alias']} missing")
    if not _condition_true(plan.get("condition")):
        raise PreconditionError(f"node {node_id} condition is false")
    if not state.get("dispatch", False):
        raise PreconditionError("dispatch is disabled")


def _pending_promotion_intent(events: list[dict[str, Any]], node_id: str, attempt: int) -> dict[str, Any] | None:
    pending = None
    relevant = (event for event in events if event.get("type") in {"promotion_intent", "promoted", "blocked", "succeeded"})
    for event in events_for_attempt(relevant, node_id, attempt):
        if event.get("type") == "promotion_intent":
            pending = event
        elif pending and event.get("type") in {"promoted", "blocked", "succeeded"}:
            pending = None
    return pending


def _normalize_dispatch_model(snap: dict[str, Any], event: dict[str, Any], node_id: str, graph: dict[str, Any]) -> None:
    model = validate_model(event.get("model"))
    event["model"] = model
    prior = None
    authorized = None
    for item in snap.get("events") or []:
        if item.get("node") != node_id:
            continue
        if item.get("type") in {"dispatched", "attempt_started"}:
            prior = item.get("model")
            authorized = None
        elif item.get("type") == "replan" and "model" in item:
            authorized = item["model"]
    if prior is not None:
        prior_model = validate_model(prior) if isinstance(prior, dict) else prior
        authorized_model = validate_model(authorized) if isinstance(authorized, dict) else authorized
        if model != prior_model and model != authorized_model:
            raise PreconditionError("retry must reuse the prior model unless replanned")
        return

    expected = authorized if authorized is not None else (graph.get("nodes", {}).get(node_id, {}) or {}).get("model")
    if _request_fields(model) != _request_fields(expected):
        raise PreconditionError("dispatch requested routing does not match plan")


def _normalize_blocked_continuation(snap: dict[str, Any], event: dict[str, Any], node_id: str, node: dict[str, Any], graph: dict[str, Any]) -> None:
    _normalize_dispatch_model(snap, event, node_id, graph)
    recorded = node.get("handle") if isinstance(node.get("handle"), dict) else {}
    if "handle" in event and event.get("handle") != recorded:
        raise PreconditionError("attempt_started handle does not match recorded handle")
    live = event.get("live_identity")
    if not isinstance(live, dict):
        raise PreconditionError("attempt_started continuation requires live_identity")
    executor = node.get("executor")
    recorded_identity = recorded_herdr_identity(snap["state"], node_id, node)
    if not herdr_identity_matches(recorded_identity, live, executor):
        raise PreconditionError("attempt_started live_identity does not match recorded handle")
    routing = live.get("routing")
    if not isinstance(routing, dict):
        raise PreconditionError("attempt_started continuation requires live_identity.routing")
    live["routing"] = validate_continuation_routing(event.get("model"), routing)


def _validate_run_scoped_event(run_dir: Path, snap: dict[str, Any], graph: dict[str, Any], event: dict[str, Any]) -> bool:
    """Validate the event types that carry no node; True when the event is fully handled."""
    typ = event.get("type")
    if typ == "session_run_bound":
        if not all(isinstance(event.get(key), str) and event.get(key) for key in HOLDER_KEYS):
            raise PreconditionError("session_run_bound requires workspace, pane, and session")
        controller = snap["controller"]
        if any(controller.get(key) is not None for key in HOLDER_KEYS):
            raise PreconditionError("run is already bound")
        return True
    if typ == "graph_accepted":
        revision = event.get("revision")
        if not isinstance(revision, str):
            raise PreconditionError("graph_accepted requires staged revision")
        staged = _load_graph_revision(run_dir, revision)
        _run_from_revision(run_dir, staged)
        validate_graph(staged["graph"])
        _validate_revision_against_runtime(run_dir, snap["state"], staged["graph"], graph)
        return True
    if typ in {"lease_drain_started", "lease_transfer"}:
        return True
    if typ == "notification_sent":
        if not isinstance(event.get("key"), str) or not event["key"]:
            raise PreconditionError("notification_sent requires key")
        if event["key"] in {e.get("key") for e in snap["events"] if e.get("type") == "notification_sent"}:
            raise PreconditionError("notification already sent")
        return True
    return False


def _validate_event_append(run_dir: Path, snap: dict[str, Any], graph: dict[str, Any], event: dict[str, Any]) -> None:
    typ = event.get("type")
    if event.get("to") is not None and _EVENT_TO.get(typ) != event["to"]:
        raise PreconditionError(f"{typ} cannot target {event.get('to')}")
    if _validate_run_scoped_event(run_dir, snap, graph, event):
        return

    node_id = event.get("node")
    node = snap["state"].get("nodes", {}).get(node_id) if isinstance(node_id, str) else None
    if node is None:
        raise PreconditionError(f"unknown node {node_id!r}")
    if typ in {"dispatch_intent", "dispatched", "attempt_started"} and node_id not in graph.get("nodes", {}):
        raise PreconditionError(f"node {node_id!r} is absent from the current graph")
    state = node.get("state")
    if event.get("from") is not None and event.get("from") != state:
        raise PreconditionError(f"{typ} from {event.get('from')} does not match {state}")

    if typ == "dispatch_intent":
        if state != "ready":
            raise PreconditionError("dispatch_intent requires ready node")
        attempt = _attempt_int(event.get("attempt", max(1, int(node.get("attempt", 0)))), "attempt")
        expected = max(1, int(node.get("attempt", 0)))
        if attempt != expected:
            raise PreconditionError(f"dispatch_intent attempt must be {expected}")
        intent = node.get("dispatch_intent") if isinstance(node.get("dispatch_intent"), dict) else {}
        if int(intent.get("attempt", -1)) == attempt:
            raise PreconditionError(f"attempt {attempt} already has dispatch_intent")
        attempt_path = attempt_dir(run_dir, node_id, attempt)
        if attempt_path.exists() and any(attempt_path.iterdir()):
            raise PreconditionError(f"attempt directory is immutable: {attempt_path}")
        event["attempt"] = attempt
        event["consumed"] = _resolve_consumed(snap["state"], graph, node_id)
        event["owns"] = list(graph.get("nodes", {}).get(node_id, {}).get("owns", []) or [])
        return

    if typ in {"dispatched", "attempt_started"}:
        attempt = _required_attempt(event, node)
        intent = node.get("dispatch_intent") if isinstance(node.get("dispatch_intent"), dict) else {}
        if state == "ready" and int(intent.get("attempt", -1)) == attempt:
            _normalize_dispatch_model(snap, event, node_id, graph)
            return
        if typ == "attempt_started" and _eligible_blocked_continuation(node) and attempt == int(node.get("attempt", 0)):
            _normalize_blocked_continuation(snap, event, node_id, node, graph)
            return
        raise PreconditionError(f"{typ} requires dispatch_intent for current ready attempt")


    if typ == "artifact_forced":
        # audit-only: records a --force overwrite/deletion of otherwise-immutable attempt artifacts
        # any attempt of the node may be force-rewritten, so this records the attempt rather than gating on it
        attempt = _attempt_int(event.get("attempt", node.get("attempt", 0)), "attempt")
        if attempt < 1:
            raise PreconditionError("artifact_forced requires attempt >= 1")
        forced = _as_list(event.get("forced"), "forced")
        if not forced or not all(isinstance(name, str) and name for name in forced):
            raise PreconditionError("artifact_forced requires forced artifact names")
        event["attempt"] = attempt
        return

    if typ == "ready":
        if state not in {"planned", "blocked"}:
            raise PreconditionError("ready requires planned or blocked node")
        event.setdefault("attempt", int(node.get("attempt", 0)))
        _validate_ready(snap["state"], graph, node_id)
        return

    if typ == "planned":
        if state not in {"blocked", "ready"}:
            raise PreconditionError("planned requires blocked or ready node")
        event.setdefault("attempt", int(node.get("attempt", 0)))
        return

    if typ == "succeeded":
        _validate_success(run_dir, snap, graph, event, node_id, node)
        return

    if typ == "invalidated":
        attempt = _required_attempt(event, node)
        if state != "succeeded":
            raise PreconditionError("invalidated requires succeeded node")
        event["attempt"] = attempt
        return

    if typ == "retried":
        attempt = _required_attempt(event, node)
        if state not in RETRY_ON:
            raise PreconditionError("retried requires retryable terminal state")
        next_attempt = _attempt_int(event.get("next_attempt", attempt + 1), "next_attempt")
        if next_attempt != attempt + 1:
            raise PreconditionError("retried next_attempt must be attempt + 1")
        if not _retry_allows(graph.get("nodes", {}).get(node_id, {}), state, attempt):
            raise PreconditionError(f"retry not allowed after {state}")
        event["next_attempt"] = next_attempt
        return

    if typ == "blocked":
        attempt = _attempt_int(event.get("attempt", node.get("attempt", 0)), "attempt")
        if attempt != int(node.get("attempt", 0)):
            raise PreconditionError("blocked attempt does not match current attempt")
        if state not in {"planned", "ready", "running", "blocked", "invalidated"}:
            raise PreconditionError("blocked requires live or invalidated node")
        event["attempt"] = attempt
        return

    if typ in {"failed", "interrupted", "skipped"}:
        attempt = _attempt_int(event.get("attempt", node.get("attempt", 0)), "attempt")
        if attempt != int(node.get("attempt", 0)):
            raise PreconditionError(f"{typ} attempt does not match current attempt")
        if typ == "skipped" and state == "planned":
            event["attempt"] = attempt
            return
        if typ == "interrupted" and state == "ready" and has_current_dispatch_intent(node) and attempt >= 1:
            event["attempt"] = attempt
            return
        if attempt < 1 or state not in graph_model.ACTIVE_STATES:
            raise PreconditionError(f"{typ} requires running or blocked node")
        event["attempt"] = attempt
        return

    if typ == "promotion_intent":
        attempt = _required_attempt(event, node)
        if state != "running":
            raise PreconditionError("promotion_intent requires running attempt")
        new_sha = event.get("new")
        if not isinstance(new_sha, str) or not new_sha:
            raise PreconditionError("promotion_intent requires new sha")
        run = load_run(run_dir)
        if event.get("ref") != run.get("promotion_ref"):
            raise PreconditionError("promotion_intent ref does not match run promotion_ref")
        expected_old = _previous_promotion_sha(snap["events"], run.get("base_sha"))
        if event.get("expected_old") != expected_old:
            raise PreconditionError("promotion_intent expected_old does not match prior promoted sha")
        event["artifacts"] = _promotion_artifacts(run_dir, graph, node_id, attempt, node, new_sha)
        return

    if typ == "promoted":
        attempt = _required_attempt(event, node)
        intent = _pending_promotion_intent(snap["events"], node_id, attempt)
        if state != "running" or not intent:
            raise PreconditionError("promoted requires pending promotion_intent")
        for key in ("ref", "expected_old", "new"):
            if event.get(key) != intent.get(key):
                raise PreconditionError(f"promoted {key} does not match promotion_intent")
        return

    if typ == "replan":
        if state not in {"planned", "blocked", "ready"}:
            raise PreconditionError("replan cannot rewrite active or terminal attempt")
        if "model" in event:
            event["model"] = validate_model(event["model"])
        return

    raise PreconditionError(f"unknown event type {typ!r}")


def _apply_event(state: dict[str, Any], controller: dict[str, Any], graph: dict[str, Any], event: dict[str, Any]) -> None:
    typ = event.get("type")
    if typ == "notification_sent":
        key = event.get("key")
        if isinstance(key, str):
            state["notified"].add(key)
        return
    if typ == "lease_drain_started":
        if not any(controller.get(key) is not None for key in HOLDER_KEYS):
            for key in HOLDER_KEYS:
                if event.get(key) is not None:
                    controller[key] = event[key]
        controller["lease"] = LEASE_DRAINING
        controller["dispatch"] = False
        return
    if typ == "lease_transfer":
        for key in HOLDER_KEYS:
            if key in event:
                controller[key] = event[key]
        controller["epoch"] += 1
        state["epoch"] = controller["epoch"]
        controller["lease"] = LEASE_ACTIVE
        controller["dispatch"] = True
        return
    if typ == "session_run_bound":
        for key in HOLDER_KEYS:
            if event.get(key) is not None:
                controller[key] = event[key]
        if controller.get("lease") != LEASE_DRAINING:
            controller["lease"] = LEASE_ACTIVE
            controller["dispatch"] = True
        return
    if typ == "graph_accepted":
        return

    node_id = event.get("node")
    node = state["nodes"].get(node_id) if isinstance(node_id, str) else None
    if node is None:
        return
    if typ == "dispatch_intent":
        attempt = int(event.get("attempt", max(1, int(node.get("attempt", 0)))))
        consumed = event.get("consumed") or _resolve_consumed(state, graph, node_id)
        node["attempt"] = attempt
        node["dispatch_intent"] = {k: v for k, v in event.items() if k != "type"}
        node["consumed_artifacts"] = consumed
    elif typ in {"dispatched", "attempt_started"}:
        node["state"] = "running"
        node["attempt"] = int(event.get("attempt", node.get("attempt", 0)))
        if "handle" in event:
            node["handle"] = event["handle"]
        if "model" in event:
            node["model"] = event["model"]
        node.pop("reason", None)
        node.pop("question", None)
    elif typ in {"planned", "ready"}:
        node["state"] = typ
        node["attempt"] = int(event.get("attempt", node.get("attempt", 0)))
        if "reason" in event:
            node["reason"] = event["reason"]
        elif typ == "ready":
            node.pop("reason", None)
            node.pop("question", None)
    elif typ in STATES - {"planned", "ready", "running"}:
        node["state"] = _EVENT_TO.get(typ, typ)
        node["attempt"] = int(event.get("attempt", node.get("attempt", 0)))
        for key in ("artifacts", "reason", "question", "handle", "promotion", "old_hash", "new_hash", "producer", "output"):
            if key in event:
                node[key] = event[key]
    elif typ == "retried":
        node["state"] = "planned"
        node["attempt"] = int(event.get("next_attempt", int(event.get("attempt", node.get("attempt", 0))) + 1))
        for key in ("artifacts", "consumed_artifacts", "dispatch_intent", "handle", "old_hash", "new_hash", "output", "producer", "promoted", "promotion", "promotion_intent", "question"):
            node.pop(key, None)
        if "reason" in event:
            node["reason"] = event["reason"]
    elif typ == "replan":
        controller["dispatch"] = True
        if "model" in event:
            node["model"] = event["model"]
    elif typ in {"promotion_intent", "promoted"}:
        node[typ] = {k: v for k, v in event.items() if k != "type"}


def _runtime_overlay(existing: dict[str, Any]) -> dict[str, Any]:
    overlay = {key: existing[key] for key in RUNTIME_KEYS if key in existing}
    handle = existing.get("handle")
    if isinstance(handle, dict) and any(handle.get(key) for key in ("job", "job_id", "runId", "spawning_session", "workspace", "pane", "agent")):
        overlay["handle"] = handle
    model = existing.get("model")
    if isinstance(model, dict) and all(key in model for key in MODEL_KEYS):
        try:
            overlay["model"] = validate_model(model)
        except PreconditionError:
            overlay["model"] = model
    return overlay


def _apply_graph_revision(state: dict[str, Any], current: dict[str, Any]) -> None:
    for node_id, plan in current.get("nodes", {}).items():
        existing = state["nodes"].get(node_id)
        material = dict(plan)
        material["id"] = node_id
        material["executor"] = plan.get("executor")
        if existing is None:
            material.update({"state": "planned", "attempt": 0, "artifacts": []})
        else:
            material.update(_runtime_overlay(existing))
        state["nodes"][node_id] = material


def replay(run_dir) -> dict[str, Any]:
    """Return {state, controller, events, skipped_stale, truncated_tail}."""
    run_dir = Path(canonical_path(run_dir))
    return _replay(run_dir)[0]


def _replay(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    events, truncated_tail = _events(run_dir)
    committed = _committed_graph_revision(run_dir, events)
    run = _run_from_revision(run_dir, committed)
    revision_replay = any(event.get("type") == "graph_accepted" and event.get("revision") for event in events)
    graph = {"schema_version": SCHEMA_VERSION, "nodes": {}} if revision_replay else _graph_from_revision(run_dir, committed)
    initial_epoch = 1
    for event in events:
        if "epoch" in event:
            initial_epoch = int(event["epoch"])
            break
    state = _initial_state(run, graph, initial_epoch)
    state["notified"] = set()
    controller = {"epoch": initial_epoch, "workspace": None, "pane": None, "session": None, "run": run["path"], "dispatch": False, "lease": LEASE_ACTIVE}
    applied: list[dict[str, Any]] = []
    skipped = 0
    for event in events:
        if event.get("type") == "lease_transfer" and event.get("epoch") == controller["epoch"]:
            _apply_event(state, controller, graph, event)
            applied.append(event)
            continue
        if event.get("epoch", controller["epoch"]) != controller["epoch"]:
            skipped += 1
            continue
        if event.get("type") == "graph_accepted" and event.get("revision"):
            revised = dict(_load_graph_revision(run_dir, event["revision"])["graph"])
            revised.setdefault("nodes", {})
            _apply_graph_revision(state, revised)
            graph = revised
        _apply_event(state, controller, graph, event)
        applied.append(event)
    state["notified"] = sorted(state["notified"])
    state["epoch"] = controller["epoch"]
    state["dispatch"] = controller["dispatch"]
    if truncated_tail:
        controller["dispatch"] = False
        state["dispatch"] = False
        state["truncated_tail"] = True
        for node in state.get("nodes", {}).values():
            if node.get("state") in graph_model.ACTIVE_STATES:
                node["state"] = "interrupted"
                node["reason"] = "events.jsonl truncated; replan required"
    return {"state": state, "controller": controller, "events": applied, "skipped_stale": skipped, "truncated_tail": truncated_tail}, graph


def load_state(run_dir) -> dict[str, Any]:
    """Return replay(run_dir)['state'] (schema_version, run, epoch, nodes, notified, dispatch)."""
    return replay(run_dir)["state"]


def recorded_herdr_identity(
    state: dict[str, Any], node_id: str, node: dict[str, Any], *, strict: bool = False,
) -> dict[str, Any]:
    """Merge recorded identity; strict mutation readers also validate sidecar evidence."""
    handle = node.get("handle") if isinstance(node.get("handle"), dict) else {}
    identity = {}
    run_dir = state.get("run")
    attempt = int(node.get("attempt") or 0)
    identity_file = False
    if run_dir and attempt >= 1:
        try:
            directory = attempt_dir(Path(run_dir), node_id, attempt)
            identity_file = (directory / "dispatch-identity.json").is_file()
            loaded = load_dispatch_identity(directory)
        except PreconditionError:
            if strict:
                raise
            loaded = None
        if isinstance(loaded, dict):
            identity = loaded
        if strict and identity_file:
            if not isinstance(loaded, dict):
                raise PreconditionError("invalid dispatch-identity.json")
            for key in ("identity_kind", "workspace", "pane", "worker_id", "agent"):
                left, right = handle.get(key), identity.get(key)
                if left and right and left != right:
                    raise UsageError(f"recorded {key} conflicts with dispatch identity")
            handle_tokens = handle.get("tokens") if isinstance(handle.get("tokens"), dict) else {}
            identity_tokens = identity.get("tokens") if isinstance(identity.get("tokens"), dict) else {}
            for key in ("provenance", "run", "node", "attempt"):
                if key in handle_tokens or key in identity_tokens:
                    left, right = handle_tokens.get(key), identity_tokens.get(key)
                else:
                    left, right = handle.get(key), identity.get(key)
                if left is not None and right is not None and str(left) != str(right):
                    raise UsageError(f"recorded {key} token conflicts with dispatch identity")

    tokens = {}
    if isinstance(identity.get("tokens"), dict):
        tokens.update(identity["tokens"])
    if isinstance(handle.get("tokens"), dict):
        tokens.update(handle["tokens"])
    for key in ("provenance", "run", "node", "attempt"):
        if key not in tokens:
            value = handle.get(key, identity.get(key))
            if value is not None:
                tokens[key] = value
    recorded = {
        "workspace": handle.get("workspace") or identity.get("workspace"),
        "pane": handle.get("pane") or identity.get("pane"),
        "tokens": tokens,
    }
    identity_kind = handle.get("identity_kind", identity.get("identity_kind"))
    if identity_kind is not None:
        recorded["identity_kind"] = identity_kind
    worker_id = handle.get("worker_id") or identity.get("worker_id")
    if worker_id is not None:
        recorded["worker_id"] = worker_id
    agent = handle.get("agent") or identity.get("agent")
    if agent is not None:
        recorded["agent"] = agent
    if strict:
        recorded["identity_file"] = identity_file
    return recorded


def _previous_promotion_sha(events: list[dict[str, Any]], base_sha: str | None) -> str | None:
    expected = base_sha
    for event in events:
        if event.get("type") == "promoted" and event.get("new"):
            expected = event["new"]
    return expected


def derive_status_md(state: dict[str, Any], graph: dict[str, Any]) -> str:
    lines = ["| node | executor | state | attempt | notes |", "|---|---|---|---:|---|"]
    for node_id in sorted(state.get("nodes", {})):
        node = state["nodes"][node_id]
        note = str(node.get("reason", "")).replace("|", "\\|")
        lines.append(f"| {node_id} | {node.get('executor', '')} | {node.get('state', '')} | {node.get('attempt', 0)} | {note} |")
    return "\n".join(lines) + "\n"


def write_caches(run_dir: str | Path, state: dict[str, Any], controller: dict[str, Any]) -> None:
    run_dir = Path(canonical_path(run_dir))
    _write_cache(run_dir / "state.json", json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
    _write_cache(run_dir / "controller.json", json.dumps(controller, sort_keys=True, separators=(",", ":")) + "\n")

def _write_cache(path: Path, text: str) -> None:
    if not path.is_symlink() and path.is_file():
        try:
            if path.read_bytes() == text.encode("utf-8"):
                return
        except OSError:
            pass
    atomic_text(path, text)



def _materialize_locked(run_dir: Path, snap: dict[str, Any], graph: dict[str, Any] | None = None) -> None:
    if graph is None:
        graph = load_graph(run_dir)
    write_caches(run_dir, snap["state"], snap["controller"])
    _write_cache(run_dir / "status.md", derive_status_md(snap["state"], graph))


