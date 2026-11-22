#!/usr/bin/env python3
"""Render and validate a parameterized graph template.

Usage: render-graph.py --template NAME_OR_PATH [--var KEY=VAL ...] [--out FILE]

Expands `{{KEY}}` placeholders in a JSON graph template, fails closed (exit 2)
on unresolved placeholders or malformed variables, validates the rendered graph
with `graph_model.validate_graph()`, and prints (and optionally writes) the
valid graph JSON.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import cli_common
import graph_model
import ioutil
from ioutil import UsageError

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "references" / "templates" / "graphs"
VAR_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
ANY_PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")


def resolve_template_path(name_or_path: str) -> Path:
    if not isinstance(name_or_path, str) or not name_or_path.strip():
        raise UsageError("--template must be a non-empty name or path")
    candidate = Path(name_or_path).expanduser()
    if candidate.is_file():
        return candidate
    for name in (name_or_path, f"{name_or_path}.json"):
        bundled = TEMPLATES_DIR / name
        if bundled.is_file():
            return bundled
    raise UsageError(f"template not found: {name_or_path}")


def parse_vars(raw_vars: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw_vars:
        if "=" not in item:
            raise UsageError(f"--var must be KEY=VAL, got {item!r}")
        key, val = item.split("=", 1)
        if not VAR_KEY_RE.fullmatch(key):
            raise UsageError(f"invalid --var key {key!r}")
        parsed[key] = val
    return parsed


def _expand_str(text: str, variables: dict[str, str], unresolved: set[str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in variables:
            return variables[key]
        unresolved.add(key)
        return match.group(0)

    expanded = PLACEHOLDER_RE.sub(_replace, text)
    for leftover in ANY_PLACEHOLDER_RE.findall(expanded):
        unresolved.add(leftover)
    return expanded


def _expand_value(value: Any, variables: dict[str, str], unresolved: set[str]) -> Any:
    if isinstance(value, str):
        return _expand_str(value, variables, unresolved)
    if isinstance(value, list):
        return [_expand_value(item, variables, unresolved) for item in value]
    if isinstance(value, dict):
        return {
            _expand_str(k, variables, unresolved) if isinstance(k, str) else k: _expand_value(v, variables, unresolved)
            for k, v in value.items()
        }
    return value


def render_template(name_or_path: str, variables: dict[str, str]) -> dict[str, Any]:
    path = resolve_template_path(name_or_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"cannot parse template {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise UsageError(f"template {path} root must be a JSON object")
    unresolved: set[str] = set()
    rendered = _expand_value(raw, variables, unresolved)
    if unresolved:
        missing = ", ".join(sorted(unresolved))
        raise UsageError(f"unresolved template placeholders: {missing}")
    graph_model.validate_graph(rendered)
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True, help="template name in references/templates/graphs/ or file path")
    parser.add_argument("--var", action="append", default=[], metavar="KEY=VAL", help="template variable substitution (repeatable)")
    parser.add_argument("--out", default=None, metavar="FILE", help="optional path to write the rendered graph JSON")
    args = parser.parse_args()

    rendered = render_template(args.template, parse_vars(args.var))
    if args.out is not None:
        out_path = Path(args.out).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        ioutil.atomic_text(out_path, json.dumps(rendered, indent=2) + "\n")
    print(json.dumps(rendered))
    return 0


if __name__ == "__main__":
    cli_common.run(main)
