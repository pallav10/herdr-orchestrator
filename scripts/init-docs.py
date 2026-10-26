#!/usr/bin/env python3
"""Initialize per-session orchestrator docs without mutating prior sessions.

Usage: init-docs.py [--dir docs] [--job NAME] [--new] [--run PATH] [--lock-timeout SECONDS]
       [--pane TARGET ...]

Opens a session under --dir/sessions/<id>/. progress.md is a fresh run-pointer
stub; HANDOFF.md and NEXT-SESSION-PROMPT.md may carry prose forward from the
newest session for this job, else legacy top-level docs, else
references/templates. An existing session is reused only
when it belongs to this pane's workspace/pane identity and job; outside Herdr
there is no identity, so every run opens a fresh session. --new always does.
Prints one JSON line: session, id, job, owner, parent, reused, source, run, files.
Handoff notes are informational; session.json is the canonical run binding.
Rotation/ack require HANDOFF.md and NEXT-SESSION-PROMPT.md, not progress.md
or recent note timestamps. Invalid acknowledgment metadata exits 3.
The printed `session` and `run` paths are canonical absolute values for this pane;
docs/sessions/latest is an informational pointer that nothing should resolve.
Exit codes: 0 ok, 1 runtime failure, 2 usage, 3 precondition/refusal.
Use --pane once per planned user-owned pane to check live stable identity support
before creating or binding run/session metadata. This does not enroll the pane.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import ioutil
import cli_common
from herdr_cli import herdr, runtime_agent_id

CLI_EXTRA_CODES = {RuntimeError: 1}
import session_docs

from cli_common import SCRIPTS


def _git_root(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    raise ioutil.PreconditionError(f"--dir must be inside a git repository: {path}")


def _repo_display(repo: Path, path: Path) -> str:
    try:
        rel = path.relative_to(repo)
    except ValueError as exc:
        raise ioutil.PreconditionError(f"--dir must be under git repository root: {path}") from exc
    return "/" + rel.as_posix().rstrip("/") + "/"


def _check_ignored(repo: Path, path: Path) -> str | None:
    label = _repo_display(repo, path)
    rel = label.lstrip("/")
    proc = ioutil.run_cmd(["git", "-C", str(repo), "check-ignore", "-q", rel])
    if proc.returncode == 0:
        return None
    if proc.returncode == 1:
        return label
    raise ioutil.RunError(f"git check-ignore failed for {label}: exit {proc.returncode}")


def _check_docs_root(docs_root: Path, run: str | None) -> Path:
    docs_root = session_docs.resolve_docs_root(docs_root)
    repo = _git_root(docs_root)
    missing = [
        label for label in (
            _check_ignored(repo, docs_root / "sessions"),
            _check_ignored(repo, docs_root / "runs"),
        ) if label
    ]
    if missing:
        raise ioutil.PreconditionError("missing gitignore rule: " + ", ".join(missing))
    if run is not None:
        run_dir = Path(run).expanduser()
        if not run_dir.is_absolute():
            raise ioutil.PreconditionError(f"--run must be an absolute path: {run!r}")
        run_dir = run_dir.resolve(strict=False)
        try:
            run_dir.relative_to(docs_root / "runs")
        except ValueError as exc:
            raise ioutil.PreconditionError(f"--run must be under {_repo_display(repo, docs_root / 'runs')}") from exc
    return docs_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="docs", help="destination directory")
    parser.add_argument("--job", default=None, help="job label for substitutions")
    parser.add_argument("--new", action="store_true", help="force a fresh session")
    parser.add_argument("--run", default=None, help="existing run directory to bind")
    parser.add_argument("--pane", action="append", default=[], metavar="TARGET",
                        help="preflight each planned user-owned pane before metadata writes")
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    target_dir = Path(args.dir).expanduser()
    if target_dir.exists() and not target_dir.is_dir():
        parser.error(f"--dir must be a directory path: {target_dir}")
    target_dir = _check_docs_root(target_dir, args.run)

    template_dir = SCRIPTS.parent / "references" / "templates"
    job = (args.job or Path.cwd().name).strip()
    if not job:
        parser.error("--job resolved to an empty value")

    for target in args.pane:
        payload = herdr("agent", "get", target)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        agent = result.get("agent") if isinstance(result.get("agent"), dict) else result
        runtime_agent_id(agent, target)

    result = session_docs.open_session(
        target_dir, job=job, template_dir=template_dir, force_new=args.new, run=args.run,
        lock_timeout=args.lock_timeout,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
