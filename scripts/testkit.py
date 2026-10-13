"""Temporary, independent fixtures shared by the script contract suites."""

from __future__ import annotations

import atexit
import contextlib
import functools
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import weakref
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import ioutil
import cli_common
import ledger
import scheduler



DEFAULT_HOLDER = {"workspace": "controller-ws", "pane": "controller-pane", "session": "controller-session"}
HOLDER = DEFAULT_HOLDER
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}
os.environ.update(GIT_ENV)


def dispatch_intent_event(node: str, attempt: int = 1, *, consumed=None, owns=None) -> dict:
    return {
        "type": "dispatch_intent",
        "node": node,
        "attempt": int(attempt),
        "consumed": [] if consumed is None else list(consumed),
        "owns": [] if owns is None else list(owns),
    }


def plant_run_files(
    run: Path,
    *,
    node: str = "Node.1",
    attempt: str = "1",
    base_sha: str = "base",
    run_id: str | None = None,
    nodes: dict | None = None,
    consumed: list | None = None,
    repo: str | None = None,
) -> Path:
    run = Path(run)
    run.mkdir(parents=True, exist_ok=True)
    try:
        attempt_dir = ledger.attempt_dir(run.resolve(), node, int(attempt))
    except (ioutil.UsageError, ioutil.PreconditionError, ValueError):
        attempt_dir = run / "artifacts" / node / str(attempt)
    attempt_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id or run.name
    source_repo = repo if repo is not None else str((run.parent / "repo").resolve())
    (run / "run.json").write_text(
        json.dumps({
            "id": rid,
            "job": "job",
            "schema_version": 1,
            "path": str(run.resolve()),
            "base_sha": base_sha,
            "repo": source_repo,
        }) + "\n",
        encoding="utf-8",
    )
    if nodes is None:
        nodes = {node: {"id": node, "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}
    consumed_list = [] if consumed is None else list(consumed)
    owns_list = list((nodes.get(node) or {}).get("owns") or [])
    (run / "graph.json").write_text(
        json.dumps({
            "schema_version": 1,
            "run": str(run.resolve()),
            "base_sha": base_sha,
            "nodes": nodes,
        }) + "\n",
        encoding="utf-8",
    )
    (attempt_dir / "consumed_artifacts.json").write_text(
        json.dumps(consumed_list) + "\n",
        encoding="utf-8",
    )
    (attempt_dir / "ownership.json").write_text(
        json.dumps({"slices": {node: owns_list}}) + "\n",
        encoding="utf-8",
    )
    write_events(run, [
        {"type": "session_run_bound", **DEFAULT_HOLDER},
        {"type": "ready", "node": node, "from": "planned", "to": "ready", "attempt": int(attempt)},
        dispatch_intent_event(node, int(attempt), consumed=consumed_list, owns=owns_list),
    ])
    return attempt_dir


def write_events(run: Path, events: list[dict]) -> None:
    (run / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
        check=True, env={**os.environ, **GIT_ENV},
    ).stdout.strip()


@functools.cache
def _seed() -> tuple[Path, str]:
    root = Path(tempfile.mkdtemp(prefix="herdr-test-seed-"))
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    repo = root / "repo"
    subprocess.run(["git", "init", "--initial-branch=main", "--template=", str(repo)], capture_output=True, text=True, check=True)
    sha = commit_file(repo, "file.txt", "one\n", "init")
    return repo, sha


def seed_repo(dst: Path) -> Path:
    return Path(shutil.copytree(_seed()[0], dst, symlinks=True))


def seed_base_sha() -> str:
    return _seed()[1]


def make_worktree(repo: Path, path: Path, branch: str, *, base: str = "HEAD") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path


def commit_file(repo: Path, rel: str, text: str, msg: str) -> str:
    dest = Path(repo) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    git(repo, "add", "--", rel)
    git(repo, "commit", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def new_run(tmp: Path, graph: dict | None, *, repo: Path | None = None,
            holder: dict | None = HOLDER, job: str = "Job",
            created: str = "2026-09-04T01:02:03+00:00") -> Path:
    tmp = Path(tmp)
    run = Path(ledger.claim_run(tmp / "docs", job, created=created)["dir"])
    if holder is not None:
        ledger.bind_session_run(run, holder=holder)
    if graph is not None:
        if repo is None:
            repo = seed_repo(tmp / f"repo-{run.name}")
        ledger.accept_graph(run, graph, repo=repo, holder=holder)
        scheduler.reconcile(run, holder=holder)
    return run


def snapshot_run(run_dir: Path) -> Callable[[], None]:
    """Restore only this run tree; never traverse symlinks into repo fixtures."""
    run_dir = Path(run_dir)
    backup = tempfile.TemporaryDirectory(prefix="herdr-test-snapshot-")
    saved = Path(backup.name) / "run"
    try:
        shutil.copytree(run_dir, saved, symlinks=True)
    except BaseException:
        backup.cleanup()
        raise

    def restore() -> None:
        if run_dir.is_symlink():
            run_dir.unlink()
        elif run_dir.exists():
            shutil.rmtree(run_dir)
        shutil.copytree(saved, run_dir, symlinks=True)

    weakref.finalize(restore, backup.cleanup)
    return restore


def load_script(name: str):
    path = Path(__file__).resolve().parent / name
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_module(module, argv: list[str]) -> tuple[int, str, str]:
    original_argv = sys.argv
    out, err = io.StringIO(), io.StringIO()
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli_common.run(module.main, extra_codes=getattr(module, "CLI_EXTRA_CODES", None))
            except SystemExit as exc:
                code = int(exc.code or 0)
    finally:
        sys.argv = original_argv
    return code, out.getvalue(), err.getvalue()


READ_ONLY_VERBS = frozenset({
    (subject, verb)
    for subject in ("agent", "workspace", "pane")
    for verb in ("get", "list", "show")
} | {
    ("agent", "wait"),
})


def mutating_calls(calls: Sequence[Sequence[str]]) -> list[Sequence[str]]:
    return [call for call in calls if tuple(call[:2]) not in READ_ONLY_VERBS]


class FakeHerdr:
    def __init__(self, routes: Mapping[tuple[str, ...], object]):
        self.routes = dict(routes)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        # Exact routes take precedence over two-word verb routes.
        key = args if args in self.routes else args[:2]
        if key not in self.routes:
            raise AssertionError(f"unrouted Herdr call: {args!r}")
        response = self.routes[key]
        if isinstance(response, BaseException):
            raise response
        return response(*args) if callable(response) else response
