"""XDG state layout and rebuildable pane/repo index for herdr-orchestrator.

Default state root resolution:
  1. $HERDR_ORCHESTRATOR_STATE_DIR (explicit override, used by tests and custom setups)
  2. $XDG_STATE_HOME/herdr-orchestrator
  3. ~/.local/state/herdr-orchestrator

Directory layout:
  <state_root>/
    context/                 # measured context snapshots
    index.db                 # SQLite WAL lookup: (workspace, pane) -> active run_dir, repo -> runs
    runs/<run-id>/
      run.db                 # per-run SQLite WAL authoritative store
      run.lock               # fcntl flock for external side-effect sections
      blobs/<sha256>         # content-addressed write-once payloads
      artifacts/<node>/<attempt>/
"""

from __future__ import annotations
import contextlib

import datetime as _dt
import os
from pathlib import Path
import sqlite3
from typing import Any

import ioutil

STATE_DIR_ENV = "HERDR_ORCHESTRATOR_STATE_DIR"
INDEX_SCHEMA_VERSION = 1


def state_root(override: str | Path | None = None) -> Path:
    """Return canonical state root directory, creating it if needed."""
    if override is not None:
        raw = Path(override).expanduser()
    elif os.environ.get(STATE_DIR_ENV):
        raw = Path(os.environ[STATE_DIR_ENV]).expanduser()
    elif os.environ.get("XDG_STATE_HOME"):
        raw = Path(os.environ["XDG_STATE_HOME"]).expanduser() / "herdr-orchestrator"
    else:
        raw = Path.home() / ".local" / "state" / "herdr-orchestrator"
    raw.mkdir(parents=True, exist_ok=True)
    if raw.is_symlink() or not raw.is_dir():
        raise ioutil.PreconditionError(f"state root must be a real directory: {raw}")
    return Path(ioutil.canonical_path(raw))


def runs_root(override: str | Path | None = None) -> Path:
    root = state_root(override) / "runs"
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ioutil.PreconditionError(f"runs dir must be a real directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return Path(ioutil.canonical_path(root))


def index_db_path(override: str | Path | None = None) -> Path:
    return state_root(override) / "index.db"


def git_common_dir(path: str | Path | None = None) -> str | None:
    """Resolve canonical absolute git common dir (shared across linked worktrees)."""
    cwd = Path(path).expanduser().resolve() if path is not None else Path.cwd().resolve()
    try:
        proc = ioutil.run_cmd(
            ["git", "-C", str(cwd), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    return ioutil.canonical_path(raw)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
@contextlib.contextmanager
def _connect_index(override: str | Path | None = None):
    db_path = index_db_path(override)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS panes (
                workspace TEXT NOT NULL,
                pane TEXT NOT NULL,
                run_id TEXT NOT NULL,
                run_dir TEXT NOT NULL,
                job TEXT NOT NULL,
                repo_common_dir TEXT,
                session_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workspace, pane)
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                run_dir TEXT NOT NULL UNIQUE,
                job TEXT NOT NULL,
                repo_common_dir TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo_common_dir);
            """
        )
        conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
        with conn:
            yield conn
    finally:
        conn.close()


def register_run(
    run_id: str,
    run_dir: str | Path,
    *,
    job: str,
    repo_common_dir: str | None = None,
    created_at: str | None = None,
    override: str | Path | None = None,
) -> None:
    now = _now_iso()
    run_abs = ioutil.canonical_path(run_dir)
    with _connect_index(override) as conn:
        conn.execute(
            """
            INSERT INTO runs (run_id, run_dir, job, repo_common_dir, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                run_dir = excluded.run_dir,
                job = excluded.job,
                repo_common_dir = COALESCE(excluded.repo_common_dir, runs.repo_common_dir),
                updated_at = excluded.updated_at
            """,
            (run_id, run_abs, job, repo_common_dir, created_at or now, now),
        )


def bind_pane(
    workspace: str,
    pane: str,
    *,
    run_id: str,
    run_dir: str | Path,
    job: str,
    session_id: str,
    repo_common_dir: str | None = None,
    override: str | Path | None = None,
) -> None:
    now = _now_iso()
    run_abs = ioutil.canonical_path(run_dir)
    with _connect_index(override) as conn:
        conn.execute(
            """
            INSERT INTO panes (workspace, pane, run_id, run_dir, job, repo_common_dir, session_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace, pane) DO UPDATE SET
                run_id = excluded.run_id,
                run_dir = excluded.run_dir,
                job = excluded.job,
                repo_common_dir = COALESCE(excluded.repo_common_dir, panes.repo_common_dir),
                session_id = excluded.session_id,
                updated_at = excluded.updated_at
            """,
            (workspace, pane, run_id, run_abs, job, repo_common_dir, session_id, now),
        )


def lookup_pane(
    workspace: str,
    pane: str,
    *,
    override: str | Path | None = None,
) -> dict[str, Any] | None:
    if not workspace or not pane:
        return None
    db_path = index_db_path(override)
    if not db_path.is_file():
        return None
    with _connect_index(override) as conn:
        row = conn.execute(
            "SELECT workspace, pane, run_id, run_dir, job, repo_common_dir, session_id, updated_at "
            "FROM panes WHERE workspace = ? AND pane = ?",
            (workspace, pane),
        ).fetchone()
    if row is None:
        return None
    record = dict(row)
    run_path = Path(record["run_dir"])
    if not (run_path / "run.db").is_file() and not (run_path / "run.json").is_file():
        return None
    return record


def list_runs(
    *,
    repo_common_dir: str | None = None,
    override: str | Path | None = None,
) -> list[dict[str, Any]]:
    db_path = index_db_path(override)
    if not db_path.is_file():
        return []
    with _connect_index(override) as conn:
        if repo_common_dir:
            rows = conn.execute(
                "SELECT run_id, run_dir, job, repo_common_dir, created_at, updated_at "
                "FROM runs WHERE repo_common_dir = ? ORDER BY created_at DESC",
                (repo_common_dir,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT run_id, run_dir, job, repo_common_dir, created_at, updated_at "
                "FROM runs ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def rebuild_index(override: str | Path | None = None) -> int:
    """Rebuild index.db by scanning <state_root>/runs/*/run.db."""
    root = runs_root(override)
    count = 0
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        db_file = child / "run.db"
        if not db_file.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                meta = conn.execute("SELECT key, value FROM kv WHERE key IN ('run', 'controller')").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        import json
        kv = {r["key"]: json.loads(r["value"]) for r in meta}
        run = kv.get("run")
        ctrl = kv.get("controller")
        if not isinstance(run, dict) or not run.get("id"):
            continue
        register_run(
            run["id"],
            child,
            job=str(run.get("job") or ""),
            repo_common_dir=run.get("repo_common_dir"),
            created_at=run.get("created"),
            override=override,
        )
        if isinstance(ctrl, dict) and ctrl.get("workspace") and ctrl.get("pane") and ctrl.get("session"):
            bind_pane(
                str(ctrl["workspace"]),
                str(ctrl["pane"]),
                run_id=str(run["id"]),
                run_dir=child,
                job=str(run.get("job") or ""),
                session_id=str(ctrl["session"]),
                repo_common_dir=run.get("repo_common_dir"),
                override=override,
            )
        count += 1
    return count
