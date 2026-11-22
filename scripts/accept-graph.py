#!/usr/bin/env python3
"""Accept a task graph for a bound run and print the accepted graph JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cli_common
import ioutil
import ledger


def _load_graph_json(graph_file: str) -> object:
    if graph_file == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(graph_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ioutil.UsageError(f"cannot read --graph-file {graph_file!r}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ioutil.UsageError(f"--graph-file is not valid JSON: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Canonical run directory")
    parser.add_argument("--graph-file", required=True, help="Path to graph JSON file, or '-' for stdin")
    parser.add_argument("--repo", required=True, help="Canonical target Git repository")
    parser.add_argument("--session", required=True, help="Controller session path")
    parser.add_argument("--holder-workspace", required=True, help="Controller workspace ID")
    parser.add_argument("--holder-pane", required=True, help="Controller pane ID")
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    holder = cli_common.holder_from_args(args, parser, context="--run", nonempty=True)
    graph = _load_graph_json(args.graph_file)
    accepted = ledger.accept_graph(
        args.run,
        graph,
        repo=args.repo,
        holder=holder,
        lock_timeout=args.lock_timeout,
    )
    print(json.dumps(accepted, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    cli_common.run(main)
