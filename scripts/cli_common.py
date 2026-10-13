"""Shared adapter arguments, payloads, child execution, and CLI exit mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, NoReturn
import argparse
import json
import math
import sys
import ioutil
from ioutil import RunError
from graph_model import NODE_ID_RE
from ioutil import first_str

TOKEN_VALUE_MAX = 80
SCRIPTS = Path(__file__).resolve().parent
CHILD_TIMEOUT = 10 * ioutil.SUBPROCESS_TIMEOUT

def add_lock_timeout_arg(parser: argparse.ArgumentParser) -> None:
    def positive_seconds(raw: str) -> float:
        try:
            value = float(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be a finite positive number") from exc
        if not math.isfinite(value) or value <= 0:
            raise argparse.ArgumentTypeError("must be a finite positive number")
        return value

    parser.add_argument("--lock-timeout", type=positive_seconds, default=120,
                        metavar="SECONDS", help="maximum run.lock wait in seconds (default: 120)")



class ChildFailed(Exception):
    def __init__(self, returncode: int, stderr: str):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(stderr)


def run_child(argv: list[str], child_name: str, *, timeout: float = CHILD_TIMEOUT) -> dict:
    proc = ioutil.run_cmd(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        raise ChildFailed(proc.returncode, proc.stderr)
    return parse_child_json(proc.stdout, child_name)


def run(main: Callable[[], int], *, extra_codes: dict[type[BaseException], int] | None = None) -> NoReturn:
    codes = {ioutil.UsageError: 2, ioutil.PreconditionError: 3, ioutil.RunError: 1}
    codes.update(extra_codes or {})
    try:
        sys.exit(main())
    except ChildFailed as exc:
        if exc.stderr:
            print(exc.stderr, end="" if exc.stderr.endswith("\n") else "\n", file=sys.stderr)
        sys.exit(exc.returncode)
    except tuple(codes) as exc:
        code = next(codes[cls] for cls in type(exc).__mro__ if cls in codes)
        print(str(exc), file=sys.stderr)
        sys.exit(code)


def parse_child_json(stdout: str, child_name: str) -> dict[str, Any]:
    """Parse the last line of a child CLI's stdout as JSON."""
    text = (stdout or "").strip()
    line = text.splitlines()[-1] if text else ""
    try:
        return json.loads(line)
    except json.JSONDecodeError as exc:
        raise RunError(f"{child_name} returned non-JSON stdout: {stdout!r}") from exc


def token_value(value: str) -> str:
    return value[:TOKEN_VALUE_MAX]


def token_flag(name: str, value: str) -> str:
    return f"{name}={token_value(value)}"


def run_context(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[Path, str, str] | None:
    """Resolve the all-or-none --run/--node/--attempt trio."""
    present = [args.run is not None, args.node is not None, args.attempt is not None]
    if any(present) and not all(present):
        parser.error("--run, --node, and --attempt must be supplied together")
    if not any(present):
        return None
    run = Path(args.run).resolve()
    if not run.is_absolute() or not run.is_dir():
        parser.error("--run must resolve to an existing absolute directory")
    if not NODE_ID_RE.match(args.node):
        parser.error(f"--node {args.node!r} must match {NODE_ID_RE.pattern}")
    if args.attempt < 1:
        parser.error("--attempt must be >= 1")
    return run, args.node, str(args.attempt)


def holder_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser | None = None, *, context: str | None = None, epoch: bool = False, required_message: str | None = None, nonempty: bool = False) -> dict[str, str] | None:
    if parser is not None:
        flags = "--session, --holder-workspace, and --holder-pane"
        values = [args.session, args.holder_workspace, args.holder_pane]
        if epoch:
            flags = "--session, --holder-workspace, --holder-pane, and --epoch"
            values.append(args.epoch)
        present = [bool(value) if nonempty else value is not None for value in values]
        if context is None:
            if any(present):
                parser.error(f"{flags} require --run" if nonempty else f"{flags} require --run/--node/--attempt")
            return None
        if not all(present):
            parser.error(required_message or f"{flags} must be supplied with {context}")
    return {"workspace": args.holder_workspace, "pane": args.holder_pane, "session": args.session}


def validate_auth_args(args: argparse.Namespace, parser: argparse.ArgumentParser, context: tuple[Path, str, str] | None) -> tuple[dict[str, str] | None, int | None]:
    """Validate the holder/epoch flags against the presence of a run context."""
    holder = holder_from_args(args, parser, context="--run/--node/--attempt" if context is not None else None, epoch=True)
    if context is None:
        return None, None
    if args.epoch < 1:
        parser.error("--epoch must be >= 1")
    return holder, args.epoch


def agent_workspace_id(agent: dict[str, Any]) -> str | None:
    return first_str(agent.get("workspace_id")) or first_str(
        agent.get("workspace"), ("workspace_id", "id")
    )


def agent_pane_id(agent: dict[str, Any]) -> str | None:
    pane = first_str(agent, ("pane_id", "root_pane_id"))
    if pane:
        return pane
    return first_str(agent.get("pane"), ("pane_id", "id")) or first_str(
        agent.get("root_pane"), ("pane_id", "id")
    )


def workspace_pane_id(workspace: dict[str, Any]) -> str | None:
    pane = first_str(workspace, ("pane_id", "root_pane_id"))
    if pane:
        return pane
    return first_str(workspace.get("root_pane"), ("pane_id", "id")) or first_str(
        workspace.get("pane"), ("pane_id", "id")
    )

