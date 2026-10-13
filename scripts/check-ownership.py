#!/usr/bin/env python3
"""Check changed file ownership against a deterministic slice manifest."""

import argparse
import json
from pathlib import Path
from typing import Any

import ioutil
import ledger
from ownership import normalize_manifest, load_manifest, resolve_diff_base, git_changed_paths, git_committed_paths, partition_paths_by_slice

import cli_common
def load_run_manifest(run: Path, node: str, attempt: str | None) -> tuple[list[str], dict[str, list[str]], str | None]:
    node_dir = Path(run) / "artifacts" / node
    if attempt is None:
        names = [p.name for p in node_dir.iterdir() if p.is_dir() and p.name.isdigit()] if node_dir.is_dir() else []
        if not names:
            raise FileNotFoundError(f"missing {node_dir}")
        attempt = max(names, key=lambda name: int(name))
    attempt_dir = node_dir / str(attempt)
    ownership_path = attempt_dir / "ownership.json"
    identity_path = attempt_dir / "dispatch-identity.json"
    for path in (ownership_path, identity_path):
        if not path.is_file():
            raise FileNotFoundError(f"missing {path}")
    try:
        ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"attempt JSON is not valid: {exc}") from exc
    if not isinstance(ownership, dict):
        raise ValueError("ownership.json root must be a JSON object")
    if not isinstance(identity, dict):
        raise ValueError("dispatch-identity.json root must be a JSON object")
    repos, slices = normalize_manifest(
        {"repos": [identity.get("worktree_path")], "slices": ownership.get("slices")}
    )
    base = identity.get("base")
    return repos, slices, base if isinstance(base, str) and base else None
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--manifest")
    mode.add_argument("--run")
    parser.add_argument("--node")
    parser.add_argument("--attempt")
    parser.add_argument("--base", "--since", dest="base", default=None, help="also classify files committed since this sha (BASE..HEAD)")
    args = parser.parse_args()

    if args.run is not None:
        if not args.node:
            parser.error("--node is required with --run")
        try:
            repos, slices, identity_base = load_run_manifest(Path(args.run), args.node, args.attempt)
        except FileNotFoundError as exc:
            raise ioutil.UsageError(str(exc)) from exc
        except ValueError as exc:
            parser.error(str(exc))
        if args.base is None:
            try:
                ref = ledger.load_run(Path(args.run).resolve()).get("promotion_ref")
            except ioutil.PreconditionError as exc:
                # Legacy manifest-only attempts may predate run metadata.
                missing = exc.__cause__
                if not isinstance(missing, FileNotFoundError) or Path(missing.filename) != Path(args.run).resolve() / "run.json":
                    raise
                ref = None
            args.base = resolve_diff_base(repos[0], ref, identity_base)
    else:
        try:
            repos, slices = load_manifest(Path(args.manifest))
        except ValueError as exc:
            parser.error(str(exc))

    all_unowned: list[str] = []
    all_overlaps: list[dict[str, Any]] = []
    merged_by_slice: dict[str, list[str]] = {name: [] for name in slices}

    for repo in repos:
        changed = git_changed_paths(repo)
        if args.base:
            changed = sorted(set(changed) | set(git_committed_paths(repo, args.base)))
        unowned, overlaps, by_slice = partition_paths_by_slice(changed, repo, slices)
        all_unowned.extend(unowned)
        all_overlaps.extend(overlaps)
        for name, files in by_slice.items():
            merged_by_slice[name].extend(files)

    for files in merged_by_slice.values():
        files.sort()
    all_unowned.sort()
    all_overlaps.sort(key=lambda item: item["path"])
    all_files = sorted(
        set(all_unowned)
        | {item["path"] for item in all_overlaps}
        | {path for files in merged_by_slice.values() for path in files}
    )

    clean = not all_unowned and not all_overlaps
    print(
        json.dumps(
            {
                "clean": clean,
                "unowned": all_unowned,
                "overlaps": all_overlaps,
                "by_slice": merged_by_slice,
                "files": all_files,
            }
        )
    )
    return 0 if clean else 3

if __name__ == "__main__":
    cli_common.run(main, extra_codes={RuntimeError: 1})
