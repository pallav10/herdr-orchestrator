#!/usr/bin/env python3
"""Check changed file ownership against a deterministic slice manifest.

Usage: check-ownership.py --manifest FILE [--base SHA | --since SHA]
       check-ownership.py --run RUN --node N [--attempt A] [--base SHA | --since SHA]

Without --base/--since only working-tree changes (git status) are classified; a writer that
committed and left the worktree clean shows nothing. With --base/--since, files changed in
`git diff --name-only BASE..HEAD` are classified as well (the run's recorded base_sha).
`--since` is an alias of `--base`.

--run/--node reads slices from <run>/artifacts/<node>/<attempt>/ownership.json and
repos from that attempt's dispatch-identity.json (worktree_path). --attempt defaults
to the highest all-digit directory; omitted --base/--since defaults to the writer's branch point on the promoted line, else the identity base.

Manifest JSON:
  {
    "repos": ["/abs/repo/root", ...],
    "slices": {
      "slice-name": {"paths": ["glob/relative/**", ...]}
    }
  }

For compatibility, each `slices.<name>` may also be a direct paths array.

Output (one JSON line):
  {
    "clean": bool,
    "unowned": ["/abs/path", ...],
    "overlaps": [{"path": "/abs/path", "slices": ["a","b"]}, ...],
    "by_slice": {"slice": ["/abs/path", ...]},
    "files": ["/abs/path", ...]
  }

Exit codes: 0 clean, 1 runtime failure, 2 usage/manifest malformed, 3 findings.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import ioutil


def _git(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, timeout=ioutil.SUBPROCESS_TIMEOUT, **kwargs)
    except subprocess.TimeoutExpired as exc:
        raise ioutil.RunError(f"git timed out after {ioutil.SUBPROCESS_TIMEOUT}s: {' '.join(argv)}") from exc


def load_manifest(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read manifest: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest is not valid JSON: {exc}") from exc
    return normalize_manifest(payload)


def normalize_manifest(payload) -> tuple[list[str], dict[str, list[str]]]:
    if not isinstance(payload, dict):
        raise ValueError("manifest root must be a JSON object")

    repos = payload.get("repos")
    if not isinstance(repos, list) or not repos:
        raise ValueError("manifest.repos must be a non-empty array of absolute paths")

    norm_repos: list[str] = []
    for repo in repos:
        if not isinstance(repo, str) or not repo:
            raise ValueError("manifest.repos entries must be non-empty strings")
        if not Path(repo).is_absolute():
            raise ValueError(f"repo path must be absolute: {repo!r}")
        norm_repos.append(repo)

    raw_slices = payload.get("slices")
    if not isinstance(raw_slices, dict) or not raw_slices:
        raise ValueError("manifest.slices must be a non-empty object")

    slices: dict[str, list[str]] = {}
    for name, spec in raw_slices.items():
        if not isinstance(name, str) or not name:
            raise ValueError("slice names must be non-empty strings")
        paths = spec
        if isinstance(spec, dict):
            paths = spec.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError(f"slice {name!r} must define non-empty paths")

        norm_paths: list[str] = []
        for pattern in paths:
            if not isinstance(pattern, str) or not pattern:
                raise ValueError(f"slice {name!r} path globs must be non-empty strings")
            norm_paths.append(pattern)
        slices[name] = norm_paths

    return norm_repos, slices




def git_changed_paths(repo_root: str) -> list[str]:
    proc = _git(
        ["git", "-C", repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"git status failed in {repo_root}: {msg}")

    paths: list[str] = []
    recs = proc.stdout.split(b"\0")
    i = 0
    while i < len(recs):
        rec = recs[i]
        i += 1
        if not rec:
            continue
        if len(rec) < 3 or rec[2:3] != b" ":
            continue
        xy = rec[:2].decode("ascii", "replace")
        dest = rec[3:].decode("utf-8", "surrogateescape")
        orig = None
        if "R" in xy or "C" in xy:
            orig = recs[i].decode("utf-8", "surrogateescape") if i < len(recs) else ""
            i += 1
        if dest:
            paths.append(dest)
        if orig and "R" in xy:
            paths.append(orig)
    return paths


def git_committed_paths(repo_root: str, base: str) -> list[str]:
    proc = _git(
        ["git", "-C", repo_root, "diff", "--name-only", "-z", f"{base}..HEAD"],
        capture_output=True,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"git diff {base}..HEAD failed in {repo_root}: {msg}")
    return [p.decode("utf-8", "surrogateescape") for p in proc.stdout.split(b"\0") if p]


def resolve_diff_base(worktree: str | Path | None, promotion_ref: object, base_sha: str | None) -> str | None:
    if worktree and isinstance(promotion_ref, str) and promotion_ref:
        proc = _git(
            ["git", "-C", str(worktree), "merge-base", promotion_ref, "HEAD"],
            capture_output=True,
            text=True,
        )
        sha = proc.stdout.strip()
        if proc.returncode == 0 and sha:
            return sha
    return base_sha


def partition_paths_by_slice(paths: list[str], repo_root: str, slices: dict[str, list[str]]) -> tuple[list[str], list[dict[str, Any]], dict[str, list[str]]]:
    unowned: list[str] = []
    overlaps: list[dict[str, Any]] = []
    by_slice: dict[str, list[str]] = {name: [] for name in slices}

    for rel in paths:
        abs_path = str(Path(repo_root, rel))
        matches = [name for name, globs in slices.items() if any(fnmatch.fnmatch(rel, pat) for pat in globs)]
        if not matches:
            unowned.append(abs_path)
            continue
        if len(matches) > 1:
            overlaps.append({"path": abs_path, "slices": sorted(matches)})
            continue
        by_slice[matches[0]].append(abs_path)

    for files in by_slice.values():
        files.sort()
    unowned.sort()
    overlaps.sort(key=lambda item: item["path"])
    return unowned, overlaps, by_slice






def _parse_charclass(pattern: str, i: int, n: int) -> tuple[frozenset[str], bool, int]:
    """Parse the class starting at pattern[i] == '['; returns (chars, negated, index after ']').

    An unterminated '[' yields next index -1, which the caller renders as a literal '[' token.
    """
    j = i + 1
    neg = j < n and pattern[j] == '!'
    if neg:
        j += 1
    if j < n and pattern[j] == ']':
        j += 1
    while j < n and pattern[j] != ']':
        j += 1
    if j >= n:
        return frozenset(), False, -1
    body = pattern[i + 1 + int(neg):j]
    chars: set[str] = set()
    k = 0
    while k < len(body):
        if k + 2 < len(body) and body[k + 1] == '-':
            lo, hi = ord(body[k]), ord(body[k + 2])
            if lo <= hi:
                chars.update(chr(x) for x in range(lo, hi + 1))
                k += 3
                continue
        chars.add(body[k])
        k += 1
    return frozenset(chars), bool(neg), j + 1


def _glob_tokens(pattern: str) -> list[tuple]:
    tokens: list[tuple] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == '*':
            if not tokens or tokens[-1] != ('*',):
                tokens.append(('*',))
            i += 1
        elif c == '?':
            tokens.append(('?',))
            i += 1
        elif c == '[':
            chars, neg, nxt = _parse_charclass(pattern, i, n)
            if nxt < 0:
                tokens.append(('c', '['))
                i += 1
                continue
            tokens.append(('[]', neg, chars))
            i = nxt
        else:
            tokens.append(('c', c))
            i += 1
    return tokens


def _charset_intersect(left, right):
    if left is None:
        return right
    if right is None:
        return left
    left_neg, left_chars = left
    right_neg, right_chars = right
    if not left_neg and not right_neg:
        both = left_chars & right_chars
        return (False, both) if both else False
    if left_neg and not right_neg:
        both = right_chars - left_chars
        return (False, both) if both else False
    if not left_neg and right_neg:
        both = left_chars - right_chars
        return (False, both) if both else False
    return (True, left_chars | right_chars)


def _glob_consume(index: int, tokens: list[tuple]) -> list[tuple]:
    if index >= len(tokens):
        return []
    token = tokens[index]
    kind = token[0]
    if kind == '*':
        return [(None, index)]
    if kind == '?':
        return [(None, index + 1)]
    if kind == 'c':
        return [((False, frozenset((token[1],))), index + 1)]
    return [((token[1], token[2]), index + 1)]


def _glob_epsilon(index: int, tokens: list[tuple]) -> set[int]:
    out = {index}
    stack = [index]
    while stack:
        cur = stack.pop()
        if cur < len(tokens) and tokens[cur][0] == '*':
            nxt = cur + 1
            if nxt not in out:
                out.add(nxt)
                stack.append(nxt)
    return out


def _fnmatch_intersects(left: str, right: str) -> bool:
    left_tokens, right_tokens = _glob_tokens(left), _glob_tokens(right)
    start = [(i, j) for i in _glob_epsilon(0, left_tokens) for j in _glob_epsilon(0, right_tokens)]
    seen = set(start)
    pending = list(start)
    left_n, right_n = len(left_tokens), len(right_tokens)
    while pending:
        i, j = pending.pop()
        if i == left_n and j == right_n:
            return True
        for charset_i, next_i in _glob_consume(i, left_tokens):
            for charset_j, next_j in _glob_consume(j, right_tokens):
                if _charset_intersect(charset_i, charset_j) is False:
                    continue
                for ii in _glob_epsilon(next_i, left_tokens):
                    for jj in _glob_epsilon(next_j, right_tokens):
                        state = (ii, jj)
                        if state not in seen:
                            seen.add(state)
                            pending.append(state)
    return False


def owns_overlap(a: str, b: str) -> bool:
    a = os.path.normcase(a.strip("/"))
    b = os.path.normcase(b.strip("/"))
    return a == b or _fnmatch_intersects(a, b)


if __name__ == "__main__":
    import cli_common
    cli_common.run(main, extra_codes={RuntimeError: 1})
