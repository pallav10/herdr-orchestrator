#!/usr/bin/env python3
"""Promote an integrator commit onto the run promotion ref, or deliver the final branch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cli_common
import promotion


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="Canonical run directory")
    parser.add_argument("--repo", required=True, help="Canonical target Git repository")
    parser.add_argument("--session", required=True, help="Controller session path")
    parser.add_argument("--holder-workspace", required=True, help="Controller workspace ID")
    parser.add_argument("--holder-pane", required=True, help="Controller pane ID")
    parser.add_argument("--deliver-only", action="store_true", help="Create the final delivery branch without promoting")
    parser.add_argument("--branch", help="Optional delivery branch name (with --deliver-only)")
    parser.add_argument("--node", help="Integrator node ID (required unless --deliver-only)")
    parser.add_argument("--attempt", type=int, help="Integrator attempt number (required unless --deliver-only)")
    parser.add_argument("--new-sha", help="Commit SHA to promote (required unless --deliver-only)")
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    holder = cli_common.holder_from_args(args, parser, context="--run", nonempty=True)
    if args.deliver_only:
        if args.node is not None or args.attempt is not None or args.new_sha is not None:
            parser.error("--node, --attempt, and --new-sha cannot be used with --deliver-only")
        result = promotion.deliver_branch(
            args.run,
            repo=args.repo,
            holder=holder,
            branch=args.branch,
            lock_timeout=args.lock_timeout,
        )
    else:
        if args.branch is not None:
            parser.error("--branch requires --deliver-only")
        if not args.node or args.attempt is None or not args.new_sha:
            parser.error("--node, --attempt, and --new-sha are required unless --deliver-only is set")
        if args.attempt < 1:
            parser.error("--attempt must be >= 1")
        result = promotion.promote(
            args.run,
            node=args.node,
            attempt=args.attempt,
            new_sha=args.new_sha,
            repo=args.repo,
            holder=holder,
            lock_timeout=args.lock_timeout,
        )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    cli_common.run(main)
