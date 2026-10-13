"""Shared errors, atomic I/O, identifiers, and deadline-bound git primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import datetime as _dt
import hashlib
import json
import os
import re
import secrets
import subprocess
from dataclasses import dataclass, field


@dataclass
class GitFacts:
    """Live Git observations for one read-only invocation phase, never serialized.

    Discard after a subprocess or operation that may change the worktree.
    """
    worktree: str
    _commands: dict[tuple[str, ...], subprocess.CompletedProcess] = field(default_factory=dict, init=False, repr=False)

    def git(self, worktree: str, *args: str) -> subprocess.CompletedProcess:
        if canonical_path(worktree) != canonical_path(self.worktree):
            raise PreconditionError("GitFacts worktree mismatch")
        key = tuple(args)
        head = self._commands.get(("rev-parse", "HEAD"))
        if key[:2] == ("merge-base", "--is-ancestor") and head is not None and head.returncode == 0:
            key = tuple(head.stdout.strip() if arg == "HEAD" else arg for arg in key)
        if key not in self._commands:
            if "-z" in key:
                self._commands[key] = run_cmd(["git", "-C", worktree, *key], capture_output=True)
            else:
                self._commands[key] = run_cmd(["git", "-C", worktree, *key], capture_output=True, text=True, errors="surrogateescape")
        return self._commands[key]

_DISPATCH_GUARD_DEPTH = 0
SUBPROCESS_TIMEOUT = 60  # seconds; mirrors herdr_cli.TIMEOUT


class RunError(RuntimeError):
    """Runtime failure; CLI exit 1."""


class UsageError(RunError):
    """Usage failure; CLI exit 2."""


class PreconditionError(RunError):
    """Precondition failure; CLI exit 3."""

def load_json(path: Path) -> Any:
    """Read JSON values; malformed or unreadable files are precondition refusals."""
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise PreconditionError(f"cannot read {path}: {exc}") from exc


def consumed_items(path: Path) -> list[dict[str, Any]]:
    """Read list snapshots and the legacy object-with-consumed representation."""
    raw = load_json(path)
    items = raw.get("consumed", []) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise PreconditionError("consumed_artifacts.json must be a list or object with consumed")
    if any(not isinstance(item, dict) for item in items):
        raise PreconditionError("consumed_artifacts.json entries must be objects")
    return items




def slug(text: str, *, limit: int | None = 24, fallback: str = "job", allowed: str | None = None, word_boundary: bool = False, trim_after_limit: bool = True) -> str:
    if allowed is not None:
        value = "".join(ch if ch in allowed else "-" for ch in text.lower())
    else:
        value = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    if limit is not None and len(value) > limit:
        value = value[:limit]
        if word_boundary:
            value = value.rsplit("-", 1)[0] if "-" in value else value
    if trim_after_limit:
        value = value.strip("-")
    return value or fallback



def new_worker_id() -> str:
    return f"fo-{secrets.token_hex(12)}"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical_path(path: str | Path) -> str:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise UsageError(f"path must be absolute: {path!r}")
    return str(raw.resolve(strict=False))




def run_cmd(argv, *, timeout: float = SUBPROCESS_TIMEOUT, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run with a mandatory deadline.

    A wedged git/herdr child would otherwise hang an orchestrator pane forever;
    surface it as a domain RunError instead of a bare TimeoutExpired.
    """
    try:
        return subprocess.run(argv, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise RunError(f"command timed out after {timeout}s: {' '.join(str(a) for a in argv)}") from exc


def atomic_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def atomic_json(path: Path, payload) -> None:
    atomic_text(path, json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PreconditionError(f"missing {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise PreconditionError(f"invalid JSON in {path.name}: {exc}") from exc
    except OSError as exc:
        raise RunError(f"cannot read {path}: {exc}") from exc


def _run_git(repo, *args: str) -> str:
    proc = run_cmd(["git", "-C", canonical_path(repo), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RunError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.strip()


def _git_ref(repo, ref: str) -> str | None:
    proc = run_cmd(["git", "-C", canonical_path(repo), "rev-parse", "-q", "--verify", ref], capture_output=True, text=True)
    value = proc.stdout.strip()
    return value if proc.returncode == 0 and value else None


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def first_str(obj: object, keys: tuple[str, ...] = ()) -> str | None:
    if isinstance(obj, str) and obj:
        return obj
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def payload_text(value: object) -> str | None:
    return first_str(value, ("value", "id", "name"))


def payload_field(payload: object, keys: set[str]) -> str | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys:
                text = payload_text(value)
                if text:
                    return text
            found = payload_field(value, keys)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = payload_field(item, keys)
            if found:
                return found
    return None
def _absolute_git_dir(path: str) -> str:
    raw = Path(path)
    if not raw.is_absolute() or not raw.is_dir():
        raise PreconditionError(
            f"worktree creation source refused: git metadata {path!r} is not an absolute directory"
        )
    return canonical_path(raw)


def _git_dir_and_common(cwd: str) -> tuple[str, str]:
    proc = run_cmd(
        ["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        raise PreconditionError(
            f"worktree creation source refused for {cwd}: git rev-parse --git-dir --git-common-dir failed: {detail}"
        )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        raise PreconditionError(
            f"worktree creation source refused for {cwd}: git rev-parse returned {len(lines)} paths, expected git-dir and git-common-dir"
        )
    return _absolute_git_dir(lines[0].strip()), _absolute_git_dir(lines[1].strip())


def worktree_creation_source(cwd: str) -> str:
    """Primary checkout Herdr may create a worktree from. Never guesses cwd."""
    git_dir, common = _git_dir_and_common(cwd)
    if git_dir == common:
        return cwd
    proc = run_cmd(
        ["git", "-C", cwd, "worktree", "list", "--porcelain", "-z"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        raise PreconditionError(
            f"worktree creation source refused for {cwd}: git worktree list --porcelain -z failed: {detail}"
        )
    first = proc.stdout.split("\0\0", 1)[0].split("\0", 1)[0]
    prefix = "worktree "
    if not first.startswith(prefix) or not first[len(prefix):]:
        raise PreconditionError(
            f"worktree creation source refused for {cwd}: git worktree list --porcelain -z missing primary worktree record"
        )
    recorded = first[len(prefix):]
    if not Path(recorded).is_absolute():
        raise PreconditionError(
            f"worktree creation source refused for {cwd}: primary worktree path is not absolute"
        )
    try:
        source = canonical_path(recorded)
    except UsageError as exc:
        raise PreconditionError(
            f"worktree creation source refused for {cwd}: {exc}"
        ) from exc
    source_git, source_common = _git_dir_and_common(source)
    if source_git != source_common or source_common != common:
        raise PreconditionError(
            f"worktree creation source refused for {cwd}: primary worktree is not a non-linked checkout of the same common dir"
        )
    return source


def is_worktree_of(path: str, repo: str) -> bool:
    """True when `path` is a linked git worktree whose common dir is `<repo>/.git`."""
    proc = run_cmd(
        ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return False
    return str(Path(proc.stdout.strip()).resolve(strict=False)) == str(Path(repo, ".git").resolve(strict=False))
