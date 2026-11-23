#!/usr/bin/env python3
"""Read-only environment and protocol preflight checks for herdr-orchestrator.

Usage: doctor.py [--repo REPO] [--docs-dir DOCS] [--skip-herdr]

Prints one JSON object: {"ok": bool, "checks": [{"name", "status", "detail"}]}.
Exit codes: 0 when no check has status "fail", 3 when any check fails, 1 runtime, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any

import cli_common
import context_usage
import herdr_cli
import ioutil

CLI_EXTRA_CODES = {RuntimeError: 1}
MIN_PYTHON = (3, 10)
REQUIRED_HERDR_ENV = ("HERDR_ENV", "HERDR_WORKSPACE_ID", "HERDR_PANE_ID")


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def _check_python_and_fcntl() -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    ver = sys.version_info
    ver_str = f"{ver.major}.{ver.minor}.{ver.micro}"
    if (ver.major, ver.minor) >= MIN_PYTHON:
        checks.append(_check("python", "ok", f"Python {ver_str}"))
    else:
        checks.append(_check("python", "fail", f"Python {ver_str} < {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"))

    try:
        import fcntl  # noqa: F401
        checks.append(_check("fcntl", "ok", "POSIX fcntl available"))
    except ImportError as exc:
        checks.append(_check("fcntl", "fail", f"POSIX fcntl unavailable: {exc}"))
    return checks


def _check_git_and_repo(repo_arg: str | None, docs_dir_arg: str | None) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    git_bin = shutil.which("git")
    if not git_bin:
        checks.append(_check("git", "fail", "git not found on PATH"))
        return checks
    ver_proc = ioutil.run_cmd([git_bin, "--version"], capture_output=True, text=True)
    if ver_proc.returncode != 0:
        checks.append(_check("git", "fail", f"git --version failed: {ver_proc.stderr.strip() or ver_proc.returncode}"))
        return checks
    checks.append(_check("git", "ok", ver_proc.stdout.strip() or git_bin))

    if not repo_arg:
        return checks

    repo = Path(repo_arg).expanduser().resolve(strict=False)
    if not repo.is_dir():
        checks.append(_check("git_repo", "fail", f"repo directory does not exist: {repo}"))
        return checks

    top_proc = ioutil.run_cmd(
        [git_bin, "-C", str(repo), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if top_proc.returncode != 0:
        detail = (top_proc.stderr or top_proc.stdout).strip() or f"rc={top_proc.returncode}"
        checks.append(_check("git_repo", "fail", f"not a git repository: {detail}"))
        return checks
    repo_root = Path(top_proc.stdout.strip()).resolve(strict=False)
    checks.append(_check("git_repo", "ok", str(repo_root)))

    status_proc = ioutil.run_cmd(
        [git_bin, "-C", str(repo_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if status_proc.returncode != 0:
        checks.append(_check("git_clean", "fail", f"git status failed: {status_proc.stderr.strip()}"))
    elif status_proc.stdout.strip():
        dirty_count = len(status_proc.stdout.strip().splitlines())
        checks.append(_check("git_clean", "warn", f"working tree has {dirty_count} uncommitted change(s)"))
    else:
        checks.append(_check("git_clean", "ok", "working tree clean"))

    wt_proc = ioutil.run_cmd(
        [git_bin, "-C", str(repo_root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if wt_proc.returncode == 0:
        wt_count = sum(1 for line in wt_proc.stdout.splitlines() if line.startswith("worktree "))
        checks.append(_check("git_worktree", "ok", f"git worktree supported ({wt_count} worktree(s))"))
    else:
        detail = (wt_proc.stderr or wt_proc.stdout).strip() or f"rc={wt_proc.returncode}"
        checks.append(_check("git_worktree", "fail", f"git worktree list failed: {detail}"))

    docs_path = Path(docs_dir_arg).expanduser() if docs_dir_arg else (repo_root / "docs")
    if not docs_path.is_absolute():
        docs_path = (repo_root / docs_path).resolve(strict=False)
    else:
        docs_path = docs_path.resolve(strict=False)

    try:
        docs_rel = docs_path.relative_to(repo_root)
    except ValueError:
        checks.append(_check("gitignore", "fail", f"docs dir {docs_path} is outside repo {repo_root}"))
        return checks

    symlinked = [sub for sub in ("runs", "sessions") if (docs_path / sub).is_symlink()]
    if symlinked:
        checks.append(
            _check(
                "docs_symlinks",
                "fail",
                "docs runtime dir(s) must not be symlinks: " + ", ".join(f"/{(docs_rel / sub).as_posix()}" for sub in symlinked),
            )
        )
    else:
        checks.append(_check("docs_symlinks", "ok", f"/{(docs_rel / 'runs').as_posix()} and /{(docs_rel / 'sessions').as_posix()} are not symlinks"))

    missing: list[str] = []
    for sub in ("runs", "sessions"):
        rel = (docs_rel / sub).as_posix().rstrip("/") + "/"
        ign_proc = ioutil.run_cmd(
            [git_bin, "-C", str(repo_root), "check-ignore", "-q", rel],
            capture_output=True,
            text=True,
        )
        if ign_proc.returncode == 0:
            continue
        if ign_proc.returncode == 1:
            missing.append("/" + rel)
        else:
            checks.append(_check("gitignore", "fail", f"git check-ignore failed for /{rel}: rc={ign_proc.returncode}"))
            return checks

    if missing:
        checks.append(_check("gitignore", "fail", "missing gitignore rule(s): " + ", ".join(missing)))
    else:
        checks.append(_check("gitignore", "ok", f"/{(docs_rel / 'runs').as_posix()}/ and /{(docs_rel / 'sessions').as_posix()}/ ignored"))
    return checks


def _extract_protocol_version(payload: dict[str, Any]) -> Any:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    for key in ("protocol_version", "protocol", "version", "schema_version"):
        if key in result and result[key] is not None:
            return result[key]
    return None


def _check_herdr(*, skip_herdr: bool) -> list[dict[str, str]]:
    if skip_herdr:
        return [_check("herdr", "ok", "skipped via --skip-herdr")]

    checks: list[dict[str, str]] = []
    if herdr_cli.is_core_mode():
        tid = os.environ["HC_TERMINAL_ID"]
        checks.append(_check("herdr_env", "ok", f"herdr-core terminal={tid}"))
        try:
            hc_bin = herdr_cli._hc_bin()
        except ioutil.RunError:
            checks.append(_check("herdr_bin", "fail", "hc not found on PATH"))
            return checks
        checks.append(_check("herdr_bin", "ok", hc_bin))
        try:
            payload = herdr_cli.herdr("api", "schema", "--json")
        except ioutil.RunError as exc:
            checks.append(_check("herdr_protocol", "fail", str(exc)))
            return checks
        proto = _extract_protocol_version(payload)
        checks.append(_check("herdr_protocol", "ok", f"herdr-core reachable (version={proto})" if proto else "herdr-core reachable"))
        return checks

    missing_env = [var for var in REQUIRED_HERDR_ENV if not os.environ.get(var)]
    if missing_env:
        checks.append(_check("herdr_env", "warn", "missing env var(s): " + ", ".join(missing_env)))
    elif os.environ.get("HERDR_ENV") != "1":
        checks.append(_check("herdr_env", "warn", f"HERDR_ENV={os.environ.get('HERDR_ENV')!r} (expected '1')"))
    else:
        checks.append(
            _check(
                "herdr_env",
                "ok",
                f"HERDR_ENV=1 workspace={os.environ['HERDR_WORKSPACE_ID']} pane={os.environ['HERDR_PANE_ID']}",
            )
        )

    herdr_bin = shutil.which("herdr")
    if not herdr_bin:
        checks.append(_check("herdr_bin", "fail", "herdr not found on PATH"))
        return checks
    checks.append(_check("herdr_bin", "ok", herdr_bin))

    try:
        payload = herdr_cli.herdr("api", "schema", "--json")
    except ioutil.RunError as exc:
        checks.append(_check("herdr_protocol", "fail", str(exc)))
        return checks

    proto = _extract_protocol_version(payload)
    if proto is not None:
        checks.append(_check("herdr_protocol", "ok", f"herdr api schema reachable (protocol={proto})"))
    else:
        checks.append(_check("herdr_protocol", "ok", "herdr api schema reachable"))
    return checks


def _check_context_extension() -> dict[str, str]:
    ctx_dir = Path.home() / context_usage.CONTEXT_DIRECTORY
    workspace, pane = herdr_cli.current_holder_env()

    if workspace and pane:
        snap_path = context_usage.context_snapshot_path(workspace, pane)
        if not snap_path.is_file():
            return _check(
                "omp_context_extension",
                "warn",
                f"no snapshot for current pane at {snap_path} (non-fatal unless rotating)",
            )
        try:
            data = json.loads(snap_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _check("omp_context_extension", "warn", f"unreadable context snapshot: {exc}")
        if not isinstance(data, dict):
            return _check("omp_context_extension", "warn", "snapshot JSON is not an object")
        available = data.get("available")
        if available is False:
            reason = data.get("reason")
            detail = f"context snapshot unavailable ({reason})" if isinstance(reason, str) and reason else "context snapshot unavailable"
            status = "warn" if reason == "compaction" else "fail"
            return _check("omp_context_extension", status, detail)
        if available is not None and available is not True:
            return _check("omp_context_extension", "fail", f"context snapshot has invalid available={available!r}")
        observed = data.get("observed_at_ms")
        if isinstance(observed, (int, float)) and not isinstance(observed, bool):
            age_ms = int(time.time() * 1000) - int(observed)
            if 0 <= age_ms <= context_usage.CONTEXT_MAX_AGE_MS:
                return _check("omp_context_extension", "ok", f"fresh snapshot ({age_ms}ms old)")
            return _check(
                "omp_context_extension",
                "warn",
                f"snapshot stale ({age_ms}ms old; max {context_usage.CONTEXT_MAX_AGE_MS}ms; non-fatal unless rotating)",
            )
        return _check("omp_context_extension", "warn", "snapshot missing observed_at_ms timestamp")

    if not ctx_dir.is_dir():
        return _check(
            "omp_context_extension",
            "warn",
            f"{ctx_dir} not found (non-fatal unless rotating)",
        )
    snapshots = sorted(ctx_dir.glob("*.json"))
    if not snapshots:
        return _check(
            "omp_context_extension",
            "warn",
            f"no context snapshots under {ctx_dir} (non-fatal unless rotating)",
        )
    return _check(
        "omp_context_extension",
        "ok",
        f"{len(snapshots)} snapshot(s) present under {ctx_dir}",
    )


def run_doctor(
    *,
    repo: str | None = None,
    docs_dir: str | None = None,
    skip_herdr: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    checks.extend(_check_python_and_fcntl())
    checks.extend(_check_git_and_repo(repo, docs_dir))
    checks.extend(_check_herdr(skip_herdr=skip_herdr))
    checks.append(_check_context_extension())
    ok = all(item["status"] != "fail" for item in checks)
    return {"ok": ok, "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=None, help="target git repository to validate")
    parser.add_argument("--docs-dir", default=None, help="docs directory relative to repo or absolute (default: <repo>/docs)")
    parser.add_argument("--skip-herdr", action="store_true", help="skip live Herdr environment and protocol checks")
    args = parser.parse_args()

    result = run_doctor(repo=args.repo, docs_dir=args.docs_dir, skip_herdr=args.skip_herdr)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 3


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
