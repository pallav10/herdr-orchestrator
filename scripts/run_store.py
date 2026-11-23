"""Per-run SQLite WAL authoritative store + content-addressed blobs/.

Each run directory owns:
  run.db    — SQLite WAL database (schema user_version = 1):
              * kv(key, value)            — run metadata, current controller, state, graph
              * events(seq, epoch, type, node, t, payload) — append-only event log
              * graph_revisions(revision, payload)         — immutable accepted graph revisions
              * notes(id, epoch, kind, body, created_at)   — structured controller handoff notes
              * blobs_meta(sha256, kind, size_bytes, created_at) — blob index
  blobs/    — write-once content-addressed files keyed by sha256 hex digest.
  run.lock  — fcntl flock (owned by ledger.with_lock for external side-effect sections).

SQLite transactions are ALWAYS short-lived and NEVER held across external
subprocesses (herdr/git). External side-effect atomicity remains owned by
`ledger.with_lock` / `authz.dispatch_guard`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any

import ioutil

DB_NAME = "run.db"
BLOBS_DIR = "blobs"
SCHEMA_VERSION = 1
NOTE_KINDS = frozenset({"decision", "contract", "blocker", "focus", "gotcha", "handoff"})
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def db_path(run_dir: str | Path) -> Path:
    return Path(ioutil.canonical_path(run_dir)) / DB_NAME


def has_db(run_dir: str | Path) -> bool:
    return db_path(run_dir).is_file()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _connect(run_dir: str | Path, *, create: bool = False) -> sqlite3.Connection:
    path = db_path(run_dir)
    if not create and not path.is_file():
        raise ioutil.PreconditionError(f"run database missing: {path}")
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(
    run_dir: str | Path,
    *,
    run: dict[str, Any],
    controller: dict[str, Any],
    state: dict[str, Any],
    graph: dict[str, Any],
) -> Path:
    """Initialize a fresh per-run SQLite WAL database atomically."""
    run_path = Path(ioutil.canonical_path(run_dir))
    (run_path / BLOBS_DIR).mkdir(exist_ok=True)
    conn = _connect(run_path, create=True)
    try:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    node TEXT,
                    t TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
                CREATE INDEX IF NOT EXISTS idx_events_node ON events(node);
                CREATE TABLE IF NOT EXISTS graph_revisions (
                    revision TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_notes_kind ON notes(kind);
                CREATE TABLE IF NOT EXISTS blobs_meta (
                    sha256 TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                [
                    ("run", _json_dumps(run)),
                    ("controller", _json_dumps(controller)),
                    ("state", _json_dumps(state)),
                    ("graph", _json_dumps(graph)),
                ],
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()
    return db_path(run_path)


def read_kv(run_dir: str | Path, key: str) -> Any | None:
    conn = _connect(run_dir)
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    finally:
        conn.close()
    return json.loads(row["value"]) if row is not None else None


def read_events(run_dir: str | Path) -> list[dict[str, Any]]:
    conn = _connect(run_dir)
    try:
        rows = conn.execute("SELECT payload FROM events ORDER BY seq ASC").fetchall()
    finally:
        conn.close()
    return [json.loads(r["payload"]) for r in rows]


def store_graph_revision(run_dir: str | Path, revision: str, payload: dict[str, Any]) -> None:
    conn = _connect(run_dir)
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO graph_revisions (revision, payload, created_at) VALUES (?, ?, ?)",
                (revision, _json_dumps(payload), _now_iso()),
            )
    finally:
        conn.close()


def load_graph_revision(run_dir: str | Path, revision: str) -> dict[str, Any] | None:
    conn = _connect(run_dir)
    try:
        row = conn.execute(
            "SELECT payload FROM graph_revisions WHERE revision = ?", (revision,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row["payload"]) if row is not None else None


def commit_events_and_projection(
    run_dir: str | Path,
    events: list[dict[str, Any]],
    *,
    run: dict[str, Any] | None = None,
    state: dict[str, Any],
    controller: dict[str, Any],
    graph: dict[str, Any],
) -> None:
    """Atomically append events, extract structured notes, and persist derived projections."""
    conn = _connect(run_dir)
    try:
        with conn:
            for ev in events:
                epoch = int(ev.get("epoch", controller.get("epoch", 1)))
                typ = str(ev.get("type") or "")
                node = ev.get("node") if isinstance(ev.get("node"), str) else None
                ts = str(ev.get("t") or _now_iso())
                conn.execute(
                    "INSERT INTO events (epoch, type, node, t, payload) VALUES (?, ?, ?, ?, ?)",
                    (epoch, typ, node, ts, _json_dumps(ev)),
                )
                if typ == "note_recorded":
                    kind = str(ev.get("kind") or "decision")
                    body = str(ev.get("body") or "")
                    conn.execute(
                        "INSERT INTO notes (epoch, kind, body, created_at) VALUES (?, ?, ?, ?)",
                        (epoch, kind, body, ts),
                    )
                elif typ == "lease_drain_started" and isinstance(ev.get("handoff"), str) and ev["handoff"].strip():
                    conn.execute(
                        "INSERT INTO notes (epoch, kind, body, created_at) VALUES (?, ?, ?, ?)",
                        (epoch, "handoff", ev["handoff"].strip(), ts),
                    )
            kv_rows = [
                ("controller", _json_dumps(controller)),
                ("state", _json_dumps(state)),
                ("graph", _json_dumps(graph)),
            ]
            if run is not None:
                kv_rows.append(("run", _json_dumps(run)))
            conn.executemany("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", kv_rows)
    finally:
        conn.close()


def read_notes(
    run_dir: str | Path,
    *,
    kind: str | None = None,
    epoch: int | None = None,
) -> list[dict[str, Any]]:
    """Return structured controller handoff notes from run.db (or replay fallback)."""
    if not has_db(run_dir):
        return []
    conn = _connect(run_dir)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if epoch is not None:
            clauses.append("epoch = ?")
            params.append(int(epoch))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT id, epoch, kind, body, created_at FROM notes{where} ORDER BY id ASC",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def put_blob(run_dir: str | Path, data: bytes | str, *, kind: str = "generic") -> dict[str, Any]:
    """Write an immutable content-addressed blob under <run_dir>/blobs/<sha256>."""
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    digest = hashlib.sha256(raw).hexdigest()
    run_path = Path(ioutil.canonical_path(run_dir))
    blobs = run_path / BLOBS_DIR
    blobs.mkdir(exist_ok=True)
    target = blobs / digest
    if not target.exists():
        tmp = blobs / f".{digest}.{os.getpid()}.tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
    if has_db(run_path):
        conn = _connect(run_path)
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO blobs_meta (sha256, kind, size_bytes, created_at) VALUES (?, ?, ?, ?)",
                    (digest, kind, len(raw), _now_iso()),
                )
        finally:
            conn.close()
    return {
        "sha256": f"sha256:{digest}",
        "digest": digest,
        "path": ioutil.canonical_path(target),
        "size_bytes": len(raw),
        "kind": kind,
    }


def get_blob(run_dir: str | Path, ref: str) -> bytes:
    """Read a content-addressed blob and verify its sha256 integrity."""
    digest = ref.removeprefix("sha256:")
    if not _SHA256_HEX_RE.fullmatch(digest):
        raise ioutil.PreconditionError(f"invalid blob ref: {ref!r}")
    run_path = Path(ioutil.canonical_path(run_dir))
    target = run_path / BLOBS_DIR / digest
    if not target.is_file() or target.is_symlink():
        raise ioutil.PreconditionError(f"missing blob: {ref}")
    raw = target.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != digest:
        raise ioutil.PreconditionError(f"corrupt blob {ref}: actual sha256:{actual}")
    return raw


def synthesize_brief(
    run_dir: str | Path,
    *,
    run: dict[str, Any],
    state: dict[str, Any],
    controller: dict[str, Any],
    notes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a structured + markdown brief directly from run.db state and notes (zero .md files on disk)."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for note in notes:
        by_kind.setdefault(note["kind"], []).append(note)

    nodes_summary: dict[str, dict[str, Any]] = {}
    for node_id, node in sorted((state.get("nodes") or {}).items()):
        entry: dict[str, Any] = {
            "state": node.get("state"),
            "executor": node.get("executor"),
            "attempt": node.get("attempt", 0),
        }
        if node.get("reason"):
            entry["reason"] = node["reason"]
        nodes_summary[node_id] = entry

    lines = [
        f"# Run Brief — {run.get('job', '')}",
        f"- Run: `{run.get('id', '')}` ({run.get('path', '')})",
        f"- Epoch: `{controller.get('epoch', 1)}` · Lease: `{controller.get('lease', 'active')}` · Dispatch: `{controller.get('dispatch', False)}`",
    ]
    if run.get("repo"):
        lines.append(f"- Repo: `{run['repo']}` (base `{run.get('base_sha') or 'unpinned'}`)")

    for heading, key in (
        ("Current focus", "focus"),
        ("Handoff summary", "handoff"),
        ("Decisions", "decision"),
        ("Cross-slice contracts", "contract"),
        ("Blockers", "blocker"),
        ("Gotchas", "gotcha"),
    ):
        items = by_kind.get(key) or []
        if not items:
            continue
        lines.append("")
        lines.append(f"## {heading}")
        for item in items:
            lines.append(f"- [e{item['epoch']}] {item['body']}")

    if nodes_summary:
        lines.append("")
        lines.append("## Nodes")
        lines.append("| node | executor | state | attempt | notes |")
        lines.append("|---|---|---|---:|---|")
        for node_id, info in nodes_summary.items():
            reason = str(info.get("reason", "")).replace("|", "\\|")
            lines.append(
                f"| {node_id} | {info.get('executor', '')} | {info.get('state', '')} | {info.get('attempt', 0)} | {reason} |"
            )

    return {
        "run": run.get("path"),
        "run_id": run.get("id"),
        "job": run.get("job"),
        "epoch": controller.get("epoch", 1),
        "lease": controller.get("lease"),
        "notes": notes,
        "nodes": nodes_summary,
        "markdown": "\n".join(lines) + "\n",
    }
