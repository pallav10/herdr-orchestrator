#!/usr/bin/env python3
"""Shared layout for per-session orchestrator docs.

Layout under the docs root (default `docs`):

    docs/sessions/<id>/{progress.md,HANDOFF.md,NEXT-SESSION-PROMPT.md,session.json}
    docs/sessions/latest -> <id>            # informational pointer only

Authority is the concrete session directory path, passed explicitly. Nothing
reads `latest`: a live predecessor and its successor, or two jobs in one repo,
would otherwise race a shared pointer and write into each other's session.

A prior session directory is never mutated. Continuity is the bound absolute
run path. New sessions write a progress pointer stub; HANDOFF/NEXT may carry prose.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
from typing import Any

import ioutil
import ledger


DOC_NAMES = ("progress.md", "HANDOFF.md", "NEXT-SESSION-PROMPT.md")
BOOTSTRAP_NAMES = ("HANDOFF.md", "NEXT-SESSION-PROMPT.md")
META_NAME = "session.json"
SESSIONS_DIR = "sessions"
LATEST_LINK = "latest"
UNKNOWN_OWNER = "unknown"





def sessions_dir(root: Path) -> Path:
    return root / SESSIONS_DIR


def owner_id() -> str:
    """Workspace/pane identity, or UNKNOWN_OWNER outside Herdr."""
    return os.environ.get("HERDR_WORKSPACE_ID") or os.environ.get("HERDR_PANE_ID") or UNKNOWN_OWNER


def read_meta(session: Path) -> dict[str, Any]:
    path = session / META_NAME
    if not path.is_file():
        return {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ioutil.PreconditionError(f"unreadable {path}: {exc}") from exc
    return meta if isinstance(meta, dict) else {}


def session_run(session: Path, *, error_type=ioutil.PreconditionError) -> Path:
    try:
        raw = read_meta(session).get("run")
        if not isinstance(raw, str) or not raw.strip():
            raise ioutil.PreconditionError(f"{session / META_NAME} missing run; run init-docs")
        loaded = _resolve_run(session.parents[1], raw)
        if raw != loaded:
            raise ioutil.PreconditionError(f"session.json.run is not canonical: {raw}")
        return Path(loaded)
    except ioutil.RunError as exc:
        raise error_type(str(exc)) from exc


def holder_identity(session: Path, holder: dict[str, str]) -> tuple[str, str]:
    if not isinstance(holder, dict):
        raise ioutil.PreconditionError("holder workspace and pane are required")
    workspace, pane = holder.get("workspace"), holder.get("pane")
    if not isinstance(workspace, str) or not workspace or not isinstance(pane, str) or not pane:
        raise ioutil.PreconditionError("holder workspace and pane are required")
    if os.environ.get("HERDR_WORKSPACE_ID") != workspace or os.environ.get("HERDR_PANE_ID") != pane:
        raise ioutil.PreconditionError("holder does not match HERDR_WORKSPACE_ID/HERDR_PANE_ID")
    meta = read_meta(session)
    if meta.get("workspace") != workspace or meta.get("pane") != pane:
        raise ioutil.PreconditionError("session.json workspace/pane does not match holder")
    recorded = holder.get("session")
    if isinstance(recorded, str) and recorded and recorded != ioutil.canonical_path(session):
        raise ioutil.PreconditionError("holder session does not match the session directory")
    return workspace, pane


def _created_key(session: Path, meta: dict[str, Any]) -> tuple[int, dt.datetime | None, str]:
    """Sort key: recorded creation instant, name only as a tiebreak.

    Names are NOT lexicographically ordered — a same-second collision suffix
    makes `…-010` precede `…-002` — so ordering must never rely on them.
    """
    created = meta.get("created")
    if isinstance(created, str):
        try:
            return (1, dt.datetime.fromisoformat(created), session.name)
        except ValueError:
            # Hand-edited or legacy metadata with an unparseable `created`: fall through
            # to the (0, None, name) bucket so it sorts before all timestamped sessions
            # instead of breaking every caller that orders sessions.
            pass
    return (0, None, session.name)


def ordered_sessions(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Sessions with their metadata, oldest first by recorded creation time.

    Excludes the `latest` pointer and any directory a concurrent pane has
    claimed but not yet populated (no metadata to order or match on).
    """
    parent = sessions_dir(root)
    if not parent.is_dir():
        return []
    found = [
        (p, read_meta(p))
        for p in parent.iterdir()
        if p.is_dir() and not p.is_symlink()
    ]
    return sorted((pair for pair in found if pair[1]), key=lambda pair: _created_key(*pair))


def load_session(path: Path) -> Path:
    """Validate an explicitly named session directory."""
    raw = Path(path).expanduser()
    if raw.name == LATEST_LINK or raw.is_symlink():
        raise ioutil.PreconditionError(f"session must be a concrete directory, not {LATEST_LINK}: {raw}")
    session = raw.resolve()
    if session.parent.name != SESSIONS_DIR:
        raise ioutil.PreconditionError(f"session must be under {SESSIONS_DIR}/: {session}")
    if not session.is_dir():
        raise ioutil.PreconditionError(f"not a session directory: {session}")
    if not (session / META_NAME).is_file():
        raise ioutil.PreconditionError(f"missing {META_NAME} in {session}")
    return session


def find_owner_session(root: Path, owner: str | None = None, job: str | None = None) -> Path | None:
    """Newest session belonging to this pane. Always None without a real identity."""
    owner = owner or owner_id()
    if owner == UNKNOWN_OWNER:
        return None
    workspace = os.environ.get("HERDR_WORKSPACE_ID")
    pane = os.environ.get("HERDR_PANE_ID")
    for session, meta in reversed(ordered_sessions(root)):
        if meta.get("owner") != owner:
            continue
        if workspace and meta.get("workspace") != workspace:
            continue
        if pane and meta.get("pane") != pane:
            continue
        if job is not None and meta.get("job") != job:
            continue
        return session
    return None


def find_job_session(root: Path, job: str) -> Path | None:
    """Newest session for this job, any owner — the carry-forward source."""
    for session, meta in reversed(ordered_sessions(root)):
        if meta.get("job") == job:
            return session
    return None


def resolve_docs_root(root: Path) -> Path:
    """Canonical docs root. sessions/ and runs/ must be real dirs, not symlinks."""
    root = Path(root).expanduser().resolve(strict=False)
    for name in (SESSIONS_DIR, "runs"):
        path = root / name
        if path.is_symlink():
            raise ioutil.PreconditionError(
                f"{name}/ must be a real directory under the docs root, not a symlink: {path}"
            )
    return root


def _session_abs(session: Path) -> str:
    raw = Path(session).expanduser()
    concrete = raw.resolve(strict=False)
    if raw.name == LATEST_LINK or concrete.name == LATEST_LINK:
        raise ioutil.PreconditionError(
            f"session must be a concrete directory, not {LATEST_LINK}: {session}"
        )
    return ioutil.canonical_path(concrete)


def render(text: str, context: dict[str, str]) -> str:
    for key, value in context.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _claim_session(parent: Path, base: str) -> Path:
    """Create and return a session dir. `mkdir` itself claims the id, so two
    panes starting the same job in the same second cannot collide."""
    candidate = base
    suffix = 2
    while True:
        session = parent / candidate
        try:
            session.mkdir()
        except FileExistsError:
            candidate = f"{base}-{suffix:03d}"
            suffix += 1
            continue
        return session


PROGRESS_POINTER = """# Progress — {{JOB}}

_Date: {{DATE}} · Session: {{SESSION}} · Orchestrator pane: {{PANE}}_

Run: {{RUN}}

This file is pane notes, not the live ledger. Authority is `events.jsonl`; `state.json` is a disposable replay projection under the run path above.
"""


def _progress_text(template_dir: Path, context: dict[str, str]) -> tuple[str, str]:
    template = template_dir / "progress.md"
    if not template.is_file():
        raise ioutil.PreconditionError(f"missing template: {template}")
    text = render(template.read_text(encoding="utf-8"), context)
    if "Run:" not in text or "| Slice | Executor |" in text:
        text = render(PROGRESS_POINTER, context)
    return text, "template"


def _seed_text(name: str, prev: Path | None, legacy_root: Path, template_dir: Path,
               context: dict[str, str]) -> tuple[str, str]:
    """Return (text, source) for one doc: carry-forward, legacy, or template."""
    if name == "progress.md":
        return _progress_text(template_dir, context)
    if prev is not None:
        source = prev / name
        if source.is_file():
            text = source.read_text(encoding="utf-8").replace(prev.name, context["SESSION"])
            return render(text, context), "carry-forward"
    legacy = legacy_root / name
    if legacy.is_file():
        text = legacy.read_text(encoding="utf-8")
        return render(text, context), "legacy"
    template = template_dir / name
    if not template.is_file():
        raise ioutil.PreconditionError(f"missing template: {template}")
    text = render(template.read_text(encoding="utf-8"), context)
    return text, "template"


def _point_latest(root: Path, session_id: str) -> None:
    link = sessions_dir(root) / LATEST_LINK
    staging = link.with_name(f".{LATEST_LINK}.{os.getpid()}")
    if staging.is_symlink() or staging.exists():
        staging.unlink()
    staging.symlink_to(session_id, target_is_directory=True)
    os.replace(staging, link)


def _result(session: Path, meta: dict[str, Any], *, reused: bool, source: str) -> dict[str, Any]:
    session_abs = _session_abs(session)
    return {
        "session": session_abs,
        "id": session.name,
        "job": meta.get("job"),
        "owner": meta.get("owner"),
        "parent": meta.get("parent"),
        "reused": reused,
        "source": source,
        "run": meta.get("run"),
        "files": [str(Path(session_abs) / name) for name in DOC_NAMES],
    }


def _require_absolute_run(value) -> Path:
    run_dir = Path(value).expanduser()
    if not run_dir.is_absolute():
        raise ioutil.PreconditionError(f"run path must be absolute: {value!r}")
    return run_dir


def _require_run_under_root(root: Path, run_dir: Path) -> None:
    runs_root = resolve_docs_root(root) / "runs"
    try:
        Path(run_dir).relative_to(runs_root)
    except ValueError as exc:
        raise ioutil.PreconditionError(f"session run must be under {runs_root}") from exc


def _existing_meta_run(session: Path, meta: dict[str, Any]) -> str | None:
    value = meta.get("run")
    if not value:
        return None
    raw_value = str(value)
    run_dir = Path(raw_value).expanduser()
    if not run_dir.is_absolute() or not (run_dir / "run.json").is_file():
        return None
    # Initializer treats invalid bindings as non-reusable, not as missing-key backfill.
    _require_run_under_root(session.parents[1], run_dir.resolve(strict=False))
    if raw_value != ioutil.canonical_path(run_dir):
        return None
    return str(session_run(session))

def _resolve_run(root: Path, value) -> str:
    run_dir = _require_absolute_run(value).resolve(strict=False)
    _require_run_under_root(root, run_dir)
    if not (run_dir / "run.json").is_file():
        raise ioutil.PreconditionError(f"run directory missing run.json: {run_dir}")
    try:
        return ledger.load_run(run_dir)["path"]
    except ioutil.RunError as exc:
        raise ioutil.PreconditionError(str(exc)) from exc


def _parent_run(root: Path, prev: Path | None) -> str | None:
    if prev is None or not read_meta(prev).get("run"):
        return None
    run_dir = session_run(prev)
    _require_run_under_root(root, run_dir)
    return str(run_dir)

def _bind_holder(session: Path, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace": meta.get("workspace"),
        "pane": meta.get("pane"),
        "session": _session_abs(session),
    }


def _bind_session_run(run_dir, session: Path, meta: dict[str, Any], *, lock_timeout=120) -> None:
    ledger.bind_session_run(run_dir, holder=_bind_holder(session, meta), lock_timeout=lock_timeout)


def open_session(root: Path, *, job: str, template_dir: Path, owner: str | None = None,
                 parent: Path | None = None, force_new: bool = False, run=None,
                 defer_run_binding: bool = False, lock_timeout=120) -> dict[str, Any]:
    """Reuse this pane's session for the job, else open a fresh one seeded from it.

    Reuse requires a real workspace/pane identity; an unknown owner always opens
    a new session, so repeated starts outside Herdr never overwrite each other.
    A valid same-pane same-job binding is reused first. A same-pane same-job
    session with no `run` key is reused in place: bind a canonical run (explicit
    `--run` or newly claimed) into that session.json without rewriting docs.
    A conflicting explicit `run` is refused unless `force_new` or an explicit
    new-session `parent` (ack-handoff) authorizes a fresh session. Implicit
    carry-forward may seed documents, but it never selects a run. Run continuity
    requires an explicit `run` or `parent` except for that in-place migrate.
    Only the returned session is written; a predecessor used only as prose parent
    is read, never modified.
    Deferred binding is legal only with an explicit concrete parent and explicit
    absolute run whose canonical paths match; it writes a fresh session and
    skips only bind_session_run.
    """
    if defer_run_binding and (parent is None or run is None):
        raise ioutil.PreconditionError(
            "defer_run_binding requires explicit parent and absolute run"
        )

    root = resolve_docs_root(root)
    requested_run = _resolve_run(root, run) if run is not None else None
    if defer_run_binding:
        parent = load_session(parent)
        if _parent_run(root, parent) != requested_run:
            raise ioutil.PreconditionError(
                "defer_run_binding parent run must match explicit run"
            )


    owner = owner or owner_id()
    owned = find_owner_session(root, owner, job) if parent is None else None

    if owned is not None and not force_new:
        owned_meta = read_meta(owned)
        existing = owned_meta.get("run") if "run" in owned_meta else None
        if existing is None or existing == "":
            run_abs = requested_run
            if run_abs is None:
                run_abs = ledger.claim_run(root, job)["path"]
                _require_run_under_root(root, Path(run_abs))
            migrated = {**owned_meta, "run": run_abs}
            _bind_session_run(run_abs, owned, migrated, lock_timeout=lock_timeout)
            ioutil.atomic_text(owned / META_NAME, json.dumps(migrated, indent=2) + "\n")
            return _result(owned, migrated, reused=True, source="existing")
        run_abs = _existing_meta_run(owned, owned_meta)
        if run_abs is not None:
            if requested_run is not None and requested_run != run_abs:
                raise ioutil.PreconditionError(
                    f"existing session already bound to {run_abs}; "
                    f"refusing conflicting run {requested_run}"
                )
            _bind_session_run(run_abs, owned, owned_meta, lock_timeout=lock_timeout)
            return _result(owned, {**owned_meta, "run": run_abs}, reused=True, source="existing")
    prev = parent or owned or find_job_session(root, job)
    run_abs = requested_run if requested_run is not None else _parent_run(root, parent)
    if run_abs is None:
        run_abs = ledger.claim_run(root, job)["path"]
        _require_run_under_root(root, Path(run_abs))

    parent_dir = sessions_dir(root)
    parent_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    session = _claim_session(parent_dir, f"{stamp}-{ioutil.slug(job, trim_after_limit=False)}")
    session_id = session.name

    context = {
        "JOB": job,
        "DATE": dt.date.today().isoformat(),
        "PANE": os.environ.get("HERDR_PANE_ID", UNKNOWN_OWNER),
        "SESSION": session_id,
        "SESSION_DIR": _session_abs(session),
        "RUN": run_abs,
    }

    sources: set[str] = set()
    try:
        for name in DOC_NAMES:
            text, source = _seed_text(name, prev, root, template_dir, context)
            ioutil.atomic_text(session / name, text)
            sources.add(source)

        meta = {
            "id": session_id,
            "job": job,
            "owner": owner,
            "created": dt.datetime.now().astimezone().isoformat(timespec="microseconds"),
            "workspace": os.environ.get("HERDR_WORKSPACE_ID") or UNKNOWN_OWNER,
            "pane": os.environ.get("HERDR_PANE_ID") or UNKNOWN_OWNER,
            "parent": prev.name if prev is not None else None,
            "run": run_abs,
        }
        ioutil.atomic_text(session / META_NAME, json.dumps(meta, indent=2) + "\n")
        if not defer_run_binding:
            _bind_session_run(run_abs, session, meta, lock_timeout=lock_timeout)
    except BaseException:
        shutil.rmtree(session, ignore_errors=True)
        raise
    _point_latest(root, session_id)

    source = "carry-forward" if "carry-forward" in sources else sorted(sources)[0]
    return _result(session, meta, reused=False, source=source)
