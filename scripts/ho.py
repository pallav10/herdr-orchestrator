#!/usr/bin/env python3
"""Unified CLI front-door for herdr-orchestrator with auto-context and `ho next` loop.

Subcommands:
  init       Open/reuse pane session and canonical run binding
  accept     Normalize and accept a task graph (file or stdin)
  next       Inspect state and emit exact copy-paste-ready next commands
  dispatch   Dispatch a ready node with auto-context and auto-epoch
  monitor    Monitor all currently running managed agents
  finish     Finish a running node attempt (auto-resolves attempt)
  promote    Promote an integrator node (auto-resolves new_sha from receipts)
  retire     Retire all eligible terminal orchestrator agents
  status     Compact run status + next_command
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

import cli_common
import graph_model
import ioutil
import ledger
import promotion
import scheduler
import session_docs
import run_store
import state_paths
from herdr_cli import current_holder_env, herdr

CLI_EXTRA_CODES = {RuntimeError: 1}
HO_SCRIPT = Path(__file__).resolve()
SCRIPTS = cli_common.SCRIPTS

SCHEMA_EXAMPLE = (
    '{"schema_version": 1, "nodes": {"A": {"id": "A", "executor": "herdr-agent", '
    '"outputs": [{"name": "summary", "type": "summary"}]}}}'
)


def _cmd_str(parts: list[str]) -> str:
    return " ".join(shlex.quote(p) for p in parts)


def _ho_cmd(*args: str) -> str:
    return _cmd_str(["python3", str(HO_SCRIPT), *args])


def _find_docs_root(explicit: str | None) -> Path | None:
    if explicit:
        return Path(ioutil.canonical_path(Path(explicit).expanduser().resolve()))
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        docs = candidate / "docs"
        if (docs / session_docs.SESSIONS_DIR).is_dir():
            return docs
        if (candidate / ".git").exists():
            return docs if docs.is_dir() else None
    return None


def _discover_session_for_pane(docs_root: Path, workspace: str, pane: str) -> tuple[Path, Path]:
    sessions = session_docs.ordered_sessions(docs_root)
    matches: list[tuple[Path, Path]] = []
    for session_dir, meta in sessions:
        if meta.get("workspace") == workspace and meta.get("pane") == pane:
            run_raw = meta.get("run")
            if isinstance(run_raw, str) and run_raw.strip():
                matches.append((Path(ioutil.canonical_path(session_dir)), Path(ioutil.canonical_path(run_raw))))
    if not matches:
        raise ioutil.PreconditionError(
            f"no session under {docs_root} matches pane ({workspace}, {pane}); "
            f"run `{_ho_cmd('init', '--dir', str(docs_root), '--job', '<job>')}` or pass --run/--session"
        )
    return matches[-1]


def _resolve_context(
    args: argparse.Namespace,
    *,
    require_run: bool = True,
    require_holder: bool = True,
    require_epoch: bool = False,
) -> dict[str, Any]:
    env_ws, env_pane = current_holder_env()
    workspace = getattr(args, "holder_workspace", None) or env_ws
    pane = getattr(args, "holder_pane", None) or env_pane
    session_raw = getattr(args, "session", None)
    run_raw = getattr(args, "run", None)
    docs_raw = getattr(args, "docs", None)

    session_path: Path | None = None
    session_id_str: str | None = None
    if session_raw:
        if str(session_raw).startswith("epoch-"):
            session_id_str = str(session_raw)
        else:
            session_path = Path(ioutil.canonical_path(session_raw))
            session_id_str = str(session_path)
    run_path: Path | None = Path(ioutil.canonical_path(run_raw)) if run_raw else None

    if session_path is not None and run_path is None:
        run_path = session_docs.session_run(session_path)

    # 1. Try XDG index.db lookup first (unless explicit --docs was passed)
    if docs_raw is None and (run_path is None or session_id_str is None) and workspace and pane:
        indexed = state_paths.lookup_pane(workspace, pane)
        if indexed is not None:
            idx_run = Path(ioutil.canonical_path(indexed["run_dir"]))
            if run_path is None or run_path == idx_run:
                if run_path is None:
                    run_path = idx_run
                if session_id_str is None and indexed.get("session_id"):
                    session_id_str = str(indexed["session_id"])
                    if not session_id_str.startswith("epoch-"):
                        session_path = Path(session_id_str)

    # 2. Fallback to legacy docs/sessions discovery
    if session_id_str is None and (require_holder or run_path is None):
        docs_root = _find_docs_root(docs_raw)
        if docs_root is not None and workspace and pane:
            try:
                disc_session, disc_run = _discover_session_for_pane(docs_root, workspace, pane)
                if run_path is None or run_path == disc_run:
                    session_path = disc_session
                    session_id_str = str(disc_session)
                    if run_path is None:
                        run_path = disc_run
                else:
                    for sdir, meta in reversed(session_docs.ordered_sessions(docs_root)):
                        if meta.get("workspace") == workspace and meta.get("pane") == pane and meta.get("run") == str(run_path):
                            session_path = Path(ioutil.canonical_path(sdir))
                            session_id_str = str(session_path)
                            break
            except ioutil.PreconditionError:
                if run_path is None:
                    raise

    if run_path is None and require_run:
        raise ioutil.PreconditionError(
            f"cannot resolve active run: set HERDR_WORKSPACE_ID/HERDR_PANE_ID or pass --run/--session, "
            f"or initialize with `{_ho_cmd('init', '--job', '<job>')}`"
        )

    snap: dict[str, Any] | None = None
    if run_path is not None and (run_store.has_db(run_path) or (run_path / "run.json").is_file()):
        snap = ledger.replay(run_path)
        ctrl = snap.get("controller") if isinstance(snap.get("controller"), dict) else {}
        if session_id_str is None and isinstance(ctrl.get("session"), str) and ctrl["session"]:
            if (not workspace or ctrl.get("workspace") == workspace) and (not pane or ctrl.get("pane") == pane):
                if not require_holder or run_store.has_db(run_path):
                    session_id_str = ctrl["session"]
                    if not session_id_str.startswith("epoch-"):
                        session_path = Path(ioutil.canonical_path(session_id_str))
        if not require_holder:
            if workspace is None and isinstance(ctrl.get("workspace"), str) and ctrl["workspace"]:
                workspace = ctrl["workspace"]
            if pane is None and isinstance(ctrl.get("pane"), str) and ctrl["pane"]:
                pane = ctrl["pane"]

    holder: dict[str, str] | None = None
    if workspace and pane and session_id_str:
        holder = {"workspace": workspace, "pane": pane, "session": session_id_str}
    elif require_holder:
        missing = [
            flag
            for flag, val in (
                ("--holder-workspace (or HERDR_WORKSPACE_ID)", workspace),
                ("--holder-pane (or HERDR_PANE_ID)", pane),
                ("--session", session_id_str),
            )
            if not val
        ]
        raise ioutil.PreconditionError(
            f"cannot resolve controller holder (missing {', '.join(missing)}); "
            f"pass explicit flags or run `{_ho_cmd('init', '--job', '<job>')}`"
        )

    epoch: int | None = getattr(args, "epoch", None)
    if epoch is None and snap is not None:
        ctrl = snap.get("controller") if isinstance(snap.get("controller"), dict) else {}
        if isinstance(ctrl.get("epoch"), int):
            epoch = ctrl["epoch"]
    if require_epoch and epoch is None:
        raise ioutil.PreconditionError("cannot resolve controller epoch; pass --epoch or ensure run is initialized")

    return {
        "run": run_path,
        "session": session_path if session_path is not None else session_id_str,
        "session_id": session_id_str,
        "workspace": workspace,
        "pane": pane,
        "holder": holder,
        "epoch": epoch,
        "snap": snap,
    }

def _normalize_graph(raw: Any) -> dict[str, Any]:
    """Forgiving, unambiguous shorthand normalization before strict graph_model.validate_graph."""
    if not isinstance(raw, dict):
        raise ioutil.PreconditionError(f"graph must be a JSON object. Valid example: {SCHEMA_EXAMPLE}")
    graph = dict(raw)
    if "schema_version" not in graph:
        graph["schema_version"] = graph_model.SCHEMA_VERSION
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        mapped: dict[str, Any] = {}
        for idx, item in enumerate(nodes):
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ioutil.PreconditionError(
                    f"graph.nodes[{idx}] must be an object with non-empty string 'id'. Valid example: {SCHEMA_EXAMPLE}"
                )
            nid = item["id"]
            if nid in mapped:
                raise ioutil.PreconditionError(f"duplicate node id {nid!r} in graph.nodes list")
            mapped[nid] = dict(item)
        graph["nodes"] = mapped
    elif isinstance(nodes, dict):
        normalized_nodes: dict[str, Any] = {}
        for key, val in nodes.items():
            if isinstance(val, dict) and "id" not in val:
                node_copy = dict(val)
                node_copy["id"] = key
                normalized_nodes[key] = node_copy
            else:
                normalized_nodes[key] = val
        graph["nodes"] = normalized_nodes
    return graph


def _git_repo_from_path(path: Path) -> str:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return ioutil.canonical_path(candidate)
    raise ioutil.PreconditionError(f"cannot determine git repository from {path}; pass --repo PATH")


def _live_agents_by_name() -> dict[str, dict[str, Any]] | None:
    """Best-effort live Herdr agent lookup; returns None if herdr is unavailable."""
    try:
        payload = herdr("agent", "list")
        agents = payload.get("result", {}).get("agents", [])
        if not isinstance(agents, list):
            return None
        return {
            a["name"]: a
            for a in agents
            if isinstance(a, dict) and isinstance(a.get("name"), str) and a["name"]
        }
    except Exception:
        return None


def _agent_target_for_node(run_path: Path, node_id: str, node: dict[str, Any]) -> str | None:
    attempt = int(node.get("attempt") or 0)
    if attempt >= 1:
        ident_path = ledger.attempt_dir(run_path, node_id, attempt) / "dispatch-identity.json"
        if ident_path.is_file():
            try:
                ident = json.loads(ident_path.read_text(encoding="utf-8"))
                if isinstance(ident, dict):
                    wid = ident.get("worker_id") or ident.get("agent")
                    if isinstance(wid, str) and wid:
                        return wid
            except (OSError, json.JSONDecodeError):
                pass
    handle = node.get("handle")
    if isinstance(handle, dict):
        for key in ("worker_id", "agent", "name"):
            val = handle.get(key)
            if isinstance(val, str) and val:
                return val
    return None


def _needs_promotion(run_path: Path, graph: dict[str, Any], node_id: str, node: dict[str, Any]) -> bool:
    if node.get("state") != "running":
        return False
    plan = (graph.get("nodes") or {}).get(node_id) or {}
    if not graph_model.declares_output(plan.get("outputs") or [], "integration"):
        return False
    attempt = int(node.get("attempt") or 0)
    if attempt < 1:
        return False
    adir = ledger.attempt_dir(run_path, node_id, attempt)
    return (adir / "integration.json").is_file() and (adir / "vcs.json").is_file()


def _ready_nodes_from_state(state: dict[str, Any], graph: dict[str, Any]) -> list[str]:
    nodes = state.get("nodes")
    if not state.get("dispatch") or not isinstance(nodes, dict):
        return []
    plans = graph.get("nodes", {})
    transitions = {ev["node"]: ev["to"] for ev in scheduler._computed_lifecycle_events(state, graph)}
    return sorted(
        nid for nid, node in nodes.items()
        if nid in plans and transitions.get(nid, node.get("state")) == "ready"
    )


def _retirable_targets(run_path: Path, state: dict[str, Any], graph: dict[str, Any], live: dict[str, dict[str, Any]] | None) -> list[str]:
    if not live:
        return []
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    graph_nodes = graph.get("nodes") if isinstance(graph.get("nodes"), dict) else {}
    targets: list[str] = []
    for nid, nstate in nodes.items():
        if not isinstance(nstate, dict):
            continue
        if nstate.get("state") not in graph_model.ATTEMPT_TERMINAL:
            continue
        plan = graph_nodes.get(nid) or {}
        if plan.get("executor") != "herdr-agent":
            continue
        target = _agent_target_for_node(run_path, nid, nstate)
        if target and target in live and target not in targets:
            targets.append(target)
    return sorted(targets)


def _compute_next(
    run_path: Path | None,
    snap: dict[str, Any] | None,
    *,
    live: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if run_path is None or snap is None:
        docs_dir = str(Path.cwd().resolve() / "docs")
        return {
            "phase": "init",
            "summary": "No active run bound to this pane. Initialize a session and run first.",
            "nodes": {},
            "commands": [_ho_cmd("init", "--dir", docs_dir, "--job", "<job-name>")],
        }

    run_flag = ["--run", str(run_path)]
    graph = ledger.load_graph(run_path)
    state = snap.get("state") if isinstance(snap.get("state"), dict) else {}
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    compact_nodes = {nid: (n.get("state") if isinstance(n, dict) else "unknown") for nid, n in sorted(nodes.items())}

    if not isinstance(graph, dict) or not graph.get("nodes"):
        return {
            "phase": "accept_graph",
            "summary": "Run initialized without an accepted graph. Write graph.json and accept it.",
            "nodes": compact_nodes,
            "commands": [_ho_cmd("accept", "--graph", "<graph.json>", *run_flag)],
        }

    plans = graph["nodes"]
    running = [nid for nid, state in compact_nodes.items() if state == "running"]
    settled: list[str] = []
    promotable: list[str] = []
    for nid in running:
        if _needs_promotion(run_path, graph, nid, nodes[nid]):
            promotable.append(nid)
        elif live and plans.get(nid, {}).get("executor") == "herdr-agent":
            target = _agent_target_for_node(run_path, nid, nodes[nid])
            agent = live.get(target, {}) if target else {}
            if (agent.get("agent_status") or agent.get("state")) in graph_model.SETTLED:
                settled.append(nid)

    if settled:
        return {
            "phase": "finish",
            "summary": f"Settled worker(s) ready to collect and finish: {', '.join(settled)}.",
            "nodes": compact_nodes,
            "commands": [_ho_cmd("finish", nid, *run_flag) for nid in settled],
        }
    if promotable:
        return {
            "phase": "promote",
            "summary": f"Integrator ready for promotion: {', '.join(promotable)}.",
            "nodes": compact_nodes,
            "commands": [_ho_cmd("promote", nid, *run_flag) for nid in promotable],
        }

    ready = _ready_nodes_from_state(state, graph)
    if ready:
        commands: list[str] = []
        external: list[str] = []
        for nid in ready:
            executor = plans[nid]["executor"]
            if executor == "herdr-agent":
                commands.append(_ho_cmd("dispatch", nid, "--prompt-file", f"<prompt-{nid}.md>", *run_flag))
            elif executor == "script":
                commands.append(_ho_cmd("dispatch", nid, *run_flag))
            else:
                external.append(f"{nid} ({executor})")
        summary = f"{len(ready)} ready node(s): {', '.join(ready)}."
        if external:
            summary += f" Use the corresponding executor for {', '.join(external)}; ho dispatch does not start them."
        return {"phase": "dispatch", "summary": summary, "nodes": compact_nodes, "commands": commands}

    if running:
        return {
            "phase": "monitor",
            "summary": f"Waiting on {len(running)} running node(s): {', '.join(running)}.",
            "nodes": compact_nodes,
            "commands": [_ho_cmd("monitor", *run_flag)],
        }

    blocked = [
        nid for nid, state in compact_nodes.items()
        if state in {"planned", "ready", "blocked", "failed", "interrupted", "invalidated"}
    ]
    if blocked:
        return {
            "phase": "blocked",
            "summary": f"Node(s) need attention ({', '.join(blocked)}). Inspect status or reconcile.",
            "nodes": compact_nodes,
            "commands": [_ho_cmd("status", *run_flag)],
        }

    # 5. All terminal-good: check if managed workers still need retirement
    retirable = _retirable_targets(run_path, state, graph, live)
    if retirable:
        return {
            "phase": "retire",
            "summary": f"All graph nodes complete. {len(retirable)} orchestrator agent(s) ready to retire.",
            "nodes": compact_nodes,
            "commands": [_ho_cmd("retire", "--all", *run_flag)],
        }

    return {
        "phase": "done",
        "summary": "All graph nodes completed and no managed workers remain live.",
        "nodes": compact_nodes,
        "commands": [],
    }


def _init_xdg_run(args: argparse.Namespace) -> dict[str, Any]:
    """Initialize a zero-gitignore, SQLite-backed run under XDG state dir."""
    from herdr_cli import runtime_agent_id
    for target in args.pane or []:
        info = herdr("agent", "get", target)
        if not runtime_agent_id(info):
            raise ioutil.PreconditionError(f"user-owned pane {target!r} has no stable runtime agent_id/id")

    workspace, pane = current_holder_env()
    has_identity = bool(workspace and pane)
    ws_val = workspace or session_docs.UNKNOWN_OWNER
    pane_val = pane or session_docs.UNKNOWN_OWNER

    repo_root: str | None = None
    if getattr(args, "repo", None):
        repo_root = ioutil.canonical_path(Path(args.repo).expanduser().resolve())
    else:
        try:
            repo_root = _git_repo_from_path(Path.cwd().resolve())
        except ioutil.PreconditionError:
            repo_root = None
    repo_common = state_paths.git_common_dir(repo_root) if repo_root else None

    if has_identity and not args.new and not args.run:
        existing = state_paths.lookup_pane(ws_val, pane_val)
        if existing is not None and existing.get("job") == args.job:
            run_path = Path(ioutil.canonical_path(existing["run_dir"]))
            if run_store.has_db(run_path):
                return {
                    "run": str(run_path),
                    "run_id": existing["run_id"],
                    "session": existing["session_id"],
                    "job": args.job,
                    "workspace": ws_val,
                    "pane": pane_val,
                    "reused": True,
                    "storage": "sqlite",
                }

    if args.run:
        run_path = Path(ioutil.canonical_path(args.run))
        run_doc = ledger.load_run(run_path)
    else:
        run_doc = ledger.claim_run(None, args.job, storage="sqlite", repo=repo_root)
        run_path = Path(run_doc["path"])

    session_id = "epoch-1"
    holder = {"workspace": ws_val, "pane": pane_val, "session": session_id}
    ledger.bind_session_run(run_path, holder=holder, lock_timeout=args.lock_timeout)
    if has_identity:
        state_paths.bind_pane(
            ws_val,
            pane_val,
            run_id=run_doc["id"],
            run_dir=run_path,
            job=args.job,
            session_id=session_id,
            repo_common_dir=repo_common,
        )
    return {
        "run": str(run_path),
        "run_id": run_doc["id"],
        "session": session_id,
        "job": args.job,
        "workspace": ws_val,
        "pane": pane_val,
        "reused": False,
        "storage": "sqlite",
    }


def _cmd_init(args: argparse.Namespace) -> int:
    if args.dir:
        argv = [sys.executable, str(SCRIPTS / "init-docs.py"), "--dir", args.dir, "--job", args.job]
        if args.new:
            argv.append("--new")
        if args.run:
            argv += ["--run", args.run]
        for p in args.pane or []:
            argv += ["--pane", p]
        argv += ["--lock-timeout", str(args.lock_timeout)]
        result = cli_common.run_child(argv, "init-docs.py")
    else:
        result = _init_xdg_run(args)
    run_path = result.get("run")
    next_cmd = _ho_cmd("accept", "--graph", "<graph.json>", "--run", run_path) if isinstance(run_path, str) else _ho_cmd("next")
    print(json.dumps({**result, "next_command": next_cmd}))
    return 0

def _cmd_accept(args: argparse.Namespace) -> int:
    ctx = _resolve_context(args, require_run=True, require_holder=True)
    run_path: Path = ctx["run"]
    if args.graph == "-":
        raw_text = sys.stdin.read()
    else:
        try:
            raw_text = Path(args.graph).read_text(encoding="utf-8")
        except OSError as exc:
            raise ioutil.UsageError(f"cannot read --graph {args.graph}: {exc}") from exc
    try:
        raw_obj = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ioutil.UsageError(f"invalid JSON in graph: {exc}. Valid example: {SCHEMA_EXAMPLE}") from exc

    normalized = _normalize_graph(raw_obj)
    try:
        graph_model.validate_graph(normalized)
    except ioutil.PreconditionError as exc:
        raise ioutil.PreconditionError(f"{exc}. Valid schema example: {SCHEMA_EXAMPLE}") from exc

    repo = args.repo
    if not repo:
        run_doc = ledger.load_run(run_path)
        repo = run_doc.get("repo")
    if not repo:
        try:
            repo = _git_repo_from_path(run_path)
        except ioutil.PreconditionError:
            repo = _git_repo_from_path(Path.cwd().resolve())
    accepted = ledger.accept_graph(run_path, normalized, repo=repo, holder=ctx["holder"], lock_timeout=args.lock_timeout)
    snap = ledger.replay(run_path)
    nxt = _compute_next(run_path, snap, live=None)
    print(json.dumps({
        "run": str(run_path),
        "base_sha": accepted.get("base_sha"),
        "promotion_ref": accepted.get("promotion_ref"),
        "nodes": sorted((accepted.get("nodes") or {}).keys()),
        "next_command": nxt["commands"][0] if nxt["commands"] else _ho_cmd("next", "--run", str(run_path)),
    }))
    return 0


def _cmd_next(args: argparse.Namespace) -> int:
    try:
        ctx = _resolve_context(args, require_run=True, require_holder=args.reconcile)
    except ioutil.PreconditionError:
        if args.run or args.session or args.reconcile:
            raise
        print(json.dumps(_compute_next(None, None)))
        return 0

    run_path: Path = ctx["run"]
    if args.reconcile:
        scheduler.reconcile(run_path, holder=ctx["holder"], lock_timeout=args.lock_timeout)
    snap = ledger.replay(run_path)
    live = _live_agents_by_name()
    print(json.dumps(_compute_next(run_path, snap, live=live)))
    return 0


def _cmd_dispatch(args: argparse.Namespace) -> int:
    ctx = _resolve_context(args, require_run=True, require_holder=True, require_epoch=True)
    graph = ledger.load_graph(ctx["run"]) or {}
    plan = graph.get("nodes", {}).get(args.node)
    if plan is None:
        raise ioutil.PreconditionError(f"unknown node {args.node!r}")
    executor = plan["executor"]
    timeout = cli_common.CHILD_TIMEOUT
    if executor == "herdr-agent":
        if not (args.prompt_file or args.prompt):
            raise ioutil.UsageError("dispatching a herdr-agent requires --prompt-file or --prompt")
        child = "dispatch-agent.py"
    elif executor == "script":
        if any(value is not None for value in (args.prompt_file, args.prompt, args.model_role, args.model, args.name, args.kind, args.requested_effort)):
            raise ioutil.UsageError("prompt, model, kind, effort, and name options are only supported for herdr-agent nodes")
        child = "run-script.py"
        timeout += plan.get("timeout_sec") or 0
    else:
        raise ioutil.PreconditionError(f"ho dispatch does not support executor {executor!r} for node {args.node!r}")

    argv = [
        sys.executable,
        str(SCRIPTS / child),
        "--run", str(ctx["run"]),
        "--node", args.node,
        "--session", str(ctx["session"]),
        "--holder-workspace", ctx["workspace"],
        "--holder-pane", ctx["pane"],
        "--epoch", str(ctx["epoch"]),
        "--lock-timeout", str(args.lock_timeout),
    ]
    if args.prompt_file:
        argv += ["--prompt-file", args.prompt_file]
    if args.prompt:
        argv += ["--prompt", args.prompt]
    if args.cwd:
        argv += ["--cwd", args.cwd]
    if args.model_role:
        argv += ["--model-role", args.model_role]
    if args.model:
        argv += ["--model", args.model]
    if args.requested_effort:
        argv += ["--requested-effort", args.requested_effort]
    if args.kind:
        argv += ["--kind", args.kind]
    if args.name:
        argv += ["--name", args.name]
    if args.orchestrate is True:
        argv.append("--orchestrate")
    elif args.orchestrate is False:
        argv.append("--no-orchestrate")
    result = cli_common.run_child(argv, child, timeout=timeout)
    print(json.dumps({**result, "next_command": _ho_cmd("next", "--run", str(ctx["run"]))}))
    return 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    ctx = _resolve_context(args, require_run=True, require_holder=False)
    run_path: Path = ctx["run"]
    snap = ctx["snap"] or ledger.replay(run_path)
    nodes = (snap.get("state") or {}).get("nodes") or {}
    targets: list[str] = []
    for nid, nstate in sorted(nodes.items()):
        if isinstance(nstate, dict) and nstate.get("state") == "running":
            target = _agent_target_for_node(run_path, nid, nstate)
            if target and target not in targets:
                targets.append(target)
    if not targets:
        raise ioutil.PreconditionError("no running managed agents to monitor")
    argv = [sys.executable, str(SCRIPTS / "monitor-agents.py"), *targets]
    if args.timeout is not None:
        argv += ["--timeout", str(args.timeout)]
    if ctx["pane"]:
        argv += ["--notify-pane", ctx["pane"], "--label", run_path.name]
    proc = subprocess.run(argv)
    return proc.returncode


def _cmd_finish(args: argparse.Namespace) -> int:
    ctx = _resolve_context(args, require_run=True, require_holder=True)
    run_path: Path = ctx["run"]
    snap = ctx["snap"] or ledger.replay(run_path)
    attempt = args.attempt
    if attempt is None:
        node = ((snap.get("state") or {}).get("nodes") or {}).get(args.node)
        if not isinstance(node, dict) or not int(node.get("attempt") or 0):
            raise ioutil.PreconditionError(f"cannot determine active attempt for node {args.node}; pass --attempt")
        attempt = int(node["attempt"])
    argv = [
        sys.executable,
        str(SCRIPTS / "finish-agent.py"),
        "--run", str(run_path),
        "--node", args.node,
        "--attempt", str(attempt),
        "--session", str(ctx["session"]),
        "--holder-workspace", ctx["workspace"],
        "--holder-pane", ctx["pane"],
        "--lock-timeout", str(args.lock_timeout),
    ]
    if args.force:
        argv.append("--force")
    if args.since:
        argv += ["--since", args.since]
    result = cli_common.run_child(argv, "finish-agent.py")
    next_cmd = _ho_cmd("promote", args.node, "--run", str(run_path)) if result.get("promote") else _ho_cmd("next", "--run", str(run_path))
    print(json.dumps({**result, "next_command": next_cmd}))
    return 0


def _resolve_promotion_sha(attempt_dir: Path) -> str:
    for filename, keys in (("integration.json", ("head", "commit")), ("vcs.json", ("head", "commit"))):
        path = attempt_dir / filename
        if not path.is_file():
            continue
        data = ioutil.load_json(path)
        if not isinstance(data, dict):
            continue
        for key in keys:
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
        res = data.get("result")
        if isinstance(res, dict) and isinstance(res.get("sha"), str) and res["sha"]:
            return res["sha"]
    raise ioutil.PreconditionError(
        f"cannot resolve new_sha from {attempt_dir / 'integration.json'} or {attempt_dir / 'vcs.json'}; run `ho finish` first"
    )


def _cmd_promote(args: argparse.Namespace) -> int:
    ctx = _resolve_context(args, require_run=True, require_holder=True)
    run_path: Path = ctx["run"]
    snap = ctx["snap"] or ledger.replay(run_path)
    attempt = args.attempt
    if attempt is None:
        node = ((snap.get("state") or {}).get("nodes") or {}).get(args.node)
        if not isinstance(node, dict) or not int(node.get("attempt") or 0):
            raise ioutil.PreconditionError(f"cannot determine attempt for node {args.node}; pass --attempt")
        attempt = int(node["attempt"])
    adir = ledger.attempt_dir(run_path, args.node, attempt)
    new_sha = _resolve_promotion_sha(adir)
    repo = args.repo or ledger.load_run(run_path).get("repo") or _git_repo_from_path(run_path)
    result = promotion.promote(
        run_path,
        node=args.node,
        attempt=attempt,
        new_sha=new_sha,
        repo=repo,
        holder=ctx["holder"],
        lock_timeout=args.lock_timeout,
    )
    print(json.dumps({
        "node": args.node,
        "attempt": attempt,
        "new_sha": new_sha,
        "delivery_branch": result.get("delivery_branch"),
        "delivery_note": result.get("delivery_note"),
        "state": (result.get("state") or {}).get("nodes", {}).get(args.node, {}).get("state"),
        "next_command": _ho_cmd("next", "--run", str(run_path)),
    }))
    return 0


def _cmd_retire(args: argparse.Namespace) -> int:
    ctx = _resolve_context(args, require_run=True, require_holder=True)
    run_path: Path = ctx["run"]
    snap = ctx["snap"] or ledger.replay(run_path)
    graph = ledger.load_graph(run_path) or {}
    live = _live_agents_by_name()
    targets = _retirable_targets(run_path, snap.get("state") or {}, graph, live)
    if not targets:
        print(json.dumps({"retired": [], "refused": [], "next_command": _ho_cmd("next", "--run", str(run_path))}))
        return 0
    argv = [
        sys.executable,
        str(SCRIPTS / "retire-agents.py"),
        *targets,
        "--run", str(run_path),
        "--session", str(ctx["session"]),
        "--holder-workspace", ctx["workspace"],
        "--holder-pane", ctx["pane"],
        "--lock-timeout", str(args.lock_timeout),
    ]
    result = cli_common.run_child(argv, "retire-agents.py")
    print(json.dumps({**result, "next_command": _ho_cmd("next", "--run", str(run_path))}))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    ctx = _resolve_context(args, require_run=True, require_holder=False)
    run_path: Path = ctx["run"]
    snap = ctx["snap"] or ledger.replay(run_path)
    if getattr(args, "md", False):
        run_doc = ledger.load_run(run_path)
        notes = ledger.list_notes(run_path)
        brief = run_store.synthesize_brief(
            run_path,
            run=run_doc,
            state=snap["state"],
            controller=snap["controller"],
            notes=notes,
        )
        sys.stdout.write(brief["markdown"])
        return 0
    live = _live_agents_by_name()
    nxt = _compute_next(run_path, snap, live=live)
    ctrl = snap.get("controller") if isinstance(snap.get("controller"), dict) else {}
    print(json.dumps({
        "run": str(run_path),
        "epoch": ctrl.get("epoch"),
        "lease": ctrl.get("lease"),
        "phase": nxt["phase"],
        "nodes": nxt["nodes"],
        "next_command": nxt["commands"][0] if nxt["commands"] else None,
    }))
    return 0


def _cmd_note(args: argparse.Namespace) -> int:
    if args.note_action == "add":
        ctx = _resolve_context(args, require_run=True, require_holder=True)
        res = ledger.record_note(
            ctx["run"],
            kind=args.kind,
            body=args.body,
            holder=ctx["holder"],
            lock_timeout=args.lock_timeout,
        )
        print(json.dumps({
            "run": str(ctx["run"]),
            "recorded": True,
            "event": res["event"],
        }))
        return 0
    ctx = _resolve_context(args, require_run=True, require_holder=False)
    notes = ledger.list_notes(ctx["run"], kind=args.kind, epoch=args.epoch)
    print(json.dumps({"run": str(ctx["run"]), "notes": notes}))
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    ctx = _resolve_context(args, require_run=True, require_holder=False)
    run_path: Path = ctx["run"]
    snap = ctx["snap"] or ledger.replay(run_path)
    run_doc = ledger.load_run(run_path)
    notes = ledger.list_notes(run_path)
    brief = run_store.synthesize_brief(
        run_path,
        run=run_doc,
        state=snap["state"],
        controller=snap["controller"],
        notes=notes,
    )
    if args.md:
        sys.stdout.write(brief["markdown"])
    else:
        print(json.dumps(brief))
    return 0


def _cmd_rotate(args: argparse.Namespace) -> int:
    ctx = _resolve_context(args, require_run=True, require_holder=True)
    argv = [sys.executable, str(SCRIPTS / "rotate.py")]
    if run_store.has_db(ctx["run"]):
        argv += ["--run", str(ctx["run"])]
        if args.focus:
            argv += ["--focus", args.focus]
    else:
        argv += ["--session", str(ctx["session"])]
    if args.label:
        argv += ["--label", args.label]
    if args.kind:
        argv += ["--kind", args.kind]
    if args.model_role:
        argv += ["--model-role", args.model_role]
    if args.model:
        argv += ["--model", args.model]
    argv += ["--timeout", str(args.timeout), "--lock-timeout", str(args.lock_timeout)]
    result = cli_common.run_child(argv, "rotate.py")
    print(json.dumps(result))
    return 0


def _cmd_ack(args: argparse.Namespace) -> int:
    argv = [sys.executable, str(SCRIPTS / "ack-handoff.py"), "--nonce", args.nonce]
    if args.run:
        argv += ["--run", args.run]
    elif args.session:
        argv += ["--session", args.session]
    else:
        raise ioutil.UsageError("ack requires --run or --session")
    argv += ["--lock-timeout", str(args.lock_timeout)]
    result = cli_common.run_child(argv, "ack-handoff.py")
    print(json.dumps(result))
    return 0

def _add_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run", default=None, help="canonical run directory (auto-discovered if omitted)")
    parser.add_argument("--session", default=None, help="canonical session directory (auto-discovered if omitted)")
    parser.add_argument("--docs", default=None, help="docs root for session auto-discovery")
    parser.add_argument("--holder-workspace", default=None, help="defaults to HERDR_WORKSPACE_ID")
    parser.add_argument("--holder-pane", default=None, help="defaults to HERDR_PANE_ID")
    cli_common.add_lock_timeout_arg(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ho.py", description="Unified CLI for herdr-orchestrator.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize a durable run (XDG SQLite by default; --dir for legacy docs/)")
    p_init.add_argument("--dir", default=None, help="legacy docs root directory (omit for XDG SQLite storage)")
    p_init.add_argument("--repo", default=None, help="target git repo path (auto-detected from cwd if omitted)")
    p_init.add_argument("--job", required=True, help="job name")
    p_init.add_argument("--new", action="store_true")
    p_init.add_argument("--run", default=None)
    p_init.add_argument("--pane", action="append", default=None)
    cli_common.add_lock_timeout_arg(p_init)
    p_init.set_defaults(func=_cmd_init)
    p_accept = sub.add_parser("accept", help="Normalize and accept a task graph")
    p_accept.add_argument("--graph", required=True, help="graph JSON file path or '-' for stdin")
    p_accept.add_argument("--repo", default=None, help="target git repo path (auto-detected if omitted)")
    _add_context_args(p_accept)
    p_accept.set_defaults(func=_cmd_accept)

    p_next = sub.add_parser("next", help="Inspect state and emit exact copy-paste-ready next commands")
    p_next.add_argument("--reconcile", action="store_true", help="run holder-authorized reconcile before computing next")
    _add_context_args(p_next)
    p_next.set_defaults(func=_cmd_next)

    p_dispatch = sub.add_parser("dispatch", help="Dispatch a ready node with auto-context & auto-epoch")
    p_dispatch.add_argument("node", help="graph node id")
    prompts = p_dispatch.add_mutually_exclusive_group()
    prompts.add_argument("--prompt-file", default=None)
    prompts.add_argument("--prompt", default=None)
    p_dispatch.add_argument("--cwd", default=None)
    p_dispatch.add_argument("--model-role", default=None)
    p_dispatch.add_argument("--model", default=None)
    p_dispatch.add_argument("--requested-effort", default=None)
    p_dispatch.add_argument("--kind", default=None, help="worker harness kind (omp, claude, codex, copilot, pi, grok, ...)")
    p_dispatch.add_argument("--name", default=None)
    p_dispatch.add_argument("--epoch", type=int, default=None)
    orch = p_dispatch.add_mutually_exclusive_group()
    orch.add_argument("--orchestrate", action="store_true", dest="orchestrate")
    orch.add_argument("--no-orchestrate", action="store_false", dest="orchestrate")
    p_dispatch.set_defaults(orchestrate=None)
    _add_context_args(p_dispatch)
    p_dispatch.set_defaults(func=_cmd_dispatch)

    p_monitor = sub.add_parser("monitor", help="Monitor all currently running managed agents")
    p_monitor.add_argument("--timeout", type=float, default=None)
    _add_context_args(p_monitor)
    p_monitor.set_defaults(func=_cmd_monitor)

    p_finish = sub.add_parser("finish", help="Finish a running node attempt (auto-resolves attempt)")
    p_finish.add_argument("node", help="graph node id")
    p_finish.add_argument("--attempt", type=int, default=None)
    p_finish.add_argument("--force", action="store_true")
    p_finish.add_argument("--since", default=None)
    _add_context_args(p_finish)
    p_finish.set_defaults(func=_cmd_finish)

    p_promote = sub.add_parser("promote", help="Promote an integrator node (auto-resolves new_sha)")
    p_promote.add_argument("node", help="integrator node id")
    p_promote.add_argument("--attempt", type=int, default=None)
    p_promote.add_argument("--repo", default=None)
    _add_context_args(p_promote)
    p_promote.set_defaults(func=_cmd_promote)

    p_retire = sub.add_parser("retire", help="Retire all eligible terminal orchestrator agents")
    p_retire.add_argument("--all", required=True, action="store_true", help="retire all eligible terminal orchestrator agents")
    _add_context_args(p_retire)
    p_retire.set_defaults(func=_cmd_retire)

    p_status = sub.add_parser("status", help="Compact run status + next_command (--md for rendered markdown)")
    p_status.add_argument("--md", action="store_true", help="render human-readable markdown brief to stdout")
    _add_context_args(p_status)
    p_status.set_defaults(func=_cmd_status)

    p_note = sub.add_parser("note", help="Record or list structured controller handoff notes in the ledger")
    note_sub = p_note.add_subparsers(dest="note_action", required=True)
    p_note_add = note_sub.add_parser("add", help="Record a structured note in the run ledger")
    p_note_add.add_argument("--kind", required=True, choices=sorted(run_store.NOTE_KINDS))
    p_note_add.add_argument("body", help="note text")
    _add_context_args(p_note_add)
    p_note_add.set_defaults(func=_cmd_note)
    p_note_list = note_sub.add_parser("list", help="List recorded notes")
    p_note_list.add_argument("--kind", default=None, choices=sorted(run_store.NOTE_KINDS))
    p_note_list.add_argument("--epoch", type=int, default=None)
    _add_context_args(p_note_list)
    p_note_list.set_defaults(func=_cmd_note)

    p_brief = sub.add_parser("brief", help="Synthesize structured run + handoff brief directly from run.db")
    p_brief.add_argument("--md", action="store_true", help="render as markdown to stdout")
    _add_context_args(p_brief)
    p_brief.set_defaults(func=_cmd_brief)

    p_rotate = sub.add_parser("rotate", help="Drain and hand off controller lease to a successor pane")
    p_rotate.add_argument("--focus", default=None, help="handoff summary / current focus recorded on drain")
    p_rotate.add_argument("--label", default="successor")
    p_rotate.add_argument("--kind", default=None)
    p_rotate.add_argument("--model-role", default="default")
    p_rotate.add_argument("--model", default=None)
    p_rotate.add_argument("--timeout", type=int, default=300)
    _add_context_args(p_rotate)
    p_rotate.set_defaults(func=_cmd_rotate)

    p_ack = sub.add_parser("ack", help="Acknowledge controller handoff in successor pane")
    p_ack.add_argument("--nonce", required=True)
    p_ack.add_argument("--run", default=None)
    p_ack.add_argument("--session", default=None)
    cli_common.add_lock_timeout_arg(p_ack)
    p_ack.set_defaults(func=_cmd_ack)

    return parser

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
