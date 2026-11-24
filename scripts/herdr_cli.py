#!/usr/bin/env python3
"""Single boundary for invoking the `herdr` or `herdr-core` (`hc`) CLI."""

from __future__ import annotations


import os
import shutil
import time
import json
import subprocess
from pathlib import Path
from typing import Any
import harnesses
import ioutil
from cli_common import token_flag
from graph_model import PROVENANCE_ORCHESTRATOR


def runtime_agent_id(agent: dict[str, Any], target: str) -> str:
    """Require the server-issued identity used by user-owned pane dispatch."""
    agent_id = ioutil.first_str(agent, ("agent_id", "id"))
    if not agent_id:
        raise ioutil.PreconditionError(
            f"live agent missing stable id for {target}: user-owned pane dispatch "
            "requires server-issued agent_id/id; terminal_id, agent_session, names "
            "and pane addresses are not supported replacement-safe identities. "
            "Preflight with `ho.py init --pane TARGET` before creating metadata. "
            "Restoring dispatch to this existing pane requires upstream Herdr "
            "agent-incarnation identity and identity-conditional prompt delivery; "
            "see references/identity-reattach.md#required-upstream-agent-incarnation-contract. "
            "Preflight refusal or creating a managed worker does not restore this pane."
        )
    return agent_id



TIMEOUT = ioutil.SUBPROCESS_TIMEOUT
READ_TIMEOUT = 10
# Longer than the 5000ms state-change window `herdr agent prompt --wait` enforces
# itself, so a stalled composer reports agent_prompt_stalled rather than timeout.
PROMPT_WAIT_MS = 15000
SESSIONS_DIR = Path.home() / ".config" / "herdr" / "sessions"
_UNCONFIRMED_CODES = ("agent_prompt_stalled", "timeout")
_HC_TOKEN_PREFIX = "orch."
_HC_KNOWN_TOKENS = ("provenance", "run", "node", "attempt", "handoff-ack", "session")
_HC_STATUS_MAP = {"idle": "idle", "working": "working", "done": "done", "blocked": "blocked", "error": "blocked"}
_HERDR_APP_MOD_NAME = "herdr-orchestrator"
_HERDR_APP_RUN_ENV = "HERDR_ORCHESTRATOR_ACTIVE_RUN"
_HERDR_APP_SPACE_ENV = "HERDR_ORCHESTRATOR_SPACE_ID"


def is_core_mode() -> bool:
    """True when running inside herdr-core (HC_TERMINAL_ID set and legacy HERDR_ENV!=1)."""
    return os.environ.get("HERDR_ENV") != "1" and bool(os.environ.get("HC_TERMINAL_ID"))


def current_holder_env() -> tuple[str | None, str | None]:
    """Return (workspace, pane) for the active controller pane in legacy Herdr or herdr-core."""
    ws = os.environ.get("HERDR_WORKSPACE_ID")
    pane = os.environ.get("HERDR_PANE_ID")
    if ws or pane:
        return ws, pane
    tid = os.environ.get("HC_TERMINAL_ID")
    if tid:
        return tid, tid
    return None, None


def _hc_bin() -> str:
    found = shutil.which("hc") or str(Path.home() / ".local" / "bin" / "hc")
    if os.access(found, os.X_OK):
        return found
    raise ioutil.RunError("hc not on PATH")


def _herdr_app_bin() -> str | None:
    if os.environ.get("HERDR_ORCHESTRATOR_NO_UI") == "1":
        return None
    found = shutil.which("herdr-app") or str(Path.home() / ".local" / "bin" / "herdr-app")
    return found if os.access(found, os.X_OK) else None


def _herdr_app_home() -> Path:
    raw = os.environ.get("HERDR_APP_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "herdr-app"


def _herdr_app_run(argv: list[str], *, timeout: int = READ_TIMEOUT) -> dict[str, Any] | None:
    """Best-effort herdr-app CLI invocation; returns parsed JSON dict or None if unavailable."""
    binary = _herdr_app_bin()
    if not binary:
        return None
    try:
        proc = subprocess.run([binary, *argv], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def ensure_app_mod_installed() -> Path | None:
    """Ensure the herdr-orchestrator mod is installed into herdr-app's mods/ folder."""
    src = Path(__file__).resolve().parent.parent / "mods" / _HERDR_APP_MOD_NAME
    if not (src / "main.js").is_file():
        return None
    app_home = _herdr_app_home()
    mods_dir = app_home / "mods"
    dst = mods_dir / _HERDR_APP_MOD_NAME
    ho_path = str(Path(__file__).resolve().parent / "ho.py")
    try:
        mods_dir.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink():
            if dst.resolve() != src.resolve():
                dst.unlink()
        if not dst.exists():
            dst.symlink_to(src, target_is_directory=True)
        elif dst.is_dir() and not dst.is_symlink():
            for item in src.iterdir():
                if item.is_file():
                    shutil.copy2(item, dst / item.name)
        # Record active ho.py path under appHome()/data/ so any plugin install location works dynamically.
        cfg_dir = app_home / "data"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / f"{_HERDR_APP_MOD_NAME}.json").write_text(
            json.dumps({"ho": ho_path}) + "\n", encoding="utf-8"
        )
    except OSError:
        return None
    return dst


def _app_spaces() -> list[dict[str, Any]]:
    ls = _herdr_app_run(["ls"])
    if not isinstance(ls, dict):
        return []
    spaces = ls.get("spaces")
    return [s for s in spaces if isinstance(s, dict)] if isinstance(spaces, list) else []


def ensure_run_space(
    run_dir: str | Path,
    *,
    job: str | None = None,
    repo: str | None = None,
    focus: bool = False,
) -> dict[str, str] | None:
    """Ensure a dedicated herdr-app space with the graph workflow mod tab exists for this run."""
    if not is_core_mode() or not _herdr_app_bin():
        return None
    run_path = Path(run_dir).resolve()
    run_id = run_path.name
    space_name = f"orch: {job or run_id}"
    folder = str(Path(repo).resolve()) if repo else str(run_path)
    ensure_app_mod_installed()

    existing_spaces = _app_spaces()
    space_id: str | None = None
    space_obj: dict[str, Any] | None = None
    for sp in existing_spaces:
        if sp.get("name") == space_name:
            sid = sp.get("id")
            if isinstance(sid, str) and sid:
                space_id = sid
                space_obj = sp
                break

    if not space_id:
        created = _herdr_app_run([
            "do", "space.new",
            json.dumps({
                "name": space_name,
                "folder": folder,
                "tab": "graph",
                "focus": focus,
            }),
        ])
        if not isinstance(created, dict):
            return None
        space_id = created.get("space") if isinstance(created.get("space"), str) else None
        if not space_id:
            for sp in _app_spaces():
                if sp.get("name") == space_name and isinstance(sp.get("id"), str):
                    space_id = sp["id"]
                    space_obj = sp
                    break
        if not space_id:
            return None
        # Replace the default shell tab with the interactive graph mod tab.
        default_tab = created.get("tab") if isinstance(created.get("tab"), str) else None
        ho_path = str(Path(__file__).resolve().parent / "ho.py")
        graph_tab = _herdr_app_run([
            "do", "tab.new",
            json.dumps({
                "name": "Graph",
                "space": space_id,
                "mod": _HERDR_APP_MOD_NAME,
                "props": {"run": str(run_path), "ho": ho_path},
                "focus": focus,
            }),
        ])
        if default_tab and isinstance(graph_tab, dict) and graph_tab.get("tab"):
            _herdr_app_run(["do", "tab.close", json.dumps({"id": default_tab, "confirmed": True})])
        return {"space": space_id, "tab": str(graph_tab.get("tab") or "") if isinstance(graph_tab, dict) else ""}

    # Space already exists: ensure Graph tab is present.
    tabs = space_obj.get("tabs") if isinstance(space_obj, dict) and isinstance(space_obj.get("tabs"), list) else []
    graph_tab_id = None
    for tb in tabs:
        if isinstance(tb, dict) and tb.get("name") == "Graph" and isinstance(tb.get("id"), str):
            graph_tab_id = tb["id"]
            break
    if not graph_tab_id:
        ho_path = str(Path(__file__).resolve().parent / "ho.py")
        created_tab = _herdr_app_run([
            "do", "tab.new",
            json.dumps({
                "name": "Graph",
                "space": space_id,
                "mod": _HERDR_APP_MOD_NAME,
                "props": {"run": str(run_path), "ho": ho_path},
                "focus": focus,
            }),
        ])
        if isinstance(created_tab, dict) and isinstance(created_tab.get("tab"), str):
            graph_tab_id = created_tab["tab"]
    if focus:
        _herdr_app_run(["do", "space.select", json.dumps({"id": space_id})])
        if graph_tab_id:
            _herdr_app_run(["do", "tab.select", json.dumps({"id": graph_tab_id})])
    return {"space": space_id, "tab": graph_tab_id or ""}


def _resolve_active_run_space(cwd: str) -> str | None:
    """Resolve a herdr-app space id for a spawned worker, creating/reusing one if needed."""
    env_space = os.environ.get(_HERDR_APP_SPACE_ENV)
    if env_space:
        return env_space
    run_dir = os.environ.get(_HERDR_APP_RUN_ENV)
    if run_dir:
        res = ensure_run_space(run_dir, repo=cwd, focus=False)
        if res and res.get("space"):
            return res["space"]
    # Fall back to the controller's current space so standalone workers remain visible too.
    controller_tid = os.environ.get("HC_TERMINAL_ID")
    spaces = _app_spaces()
    if controller_tid:
        for sp in spaces:
            for tb in sp.get("tabs") or []:
                for term in tb.get("terminals") or []:
                    if isinstance(term, dict) and term.get("terminal") == controller_tid:
                        sid = sp.get("id")
                        if isinstance(sid, str) and sid:
                            return sid
    for sp in spaces:
        if sp.get("active") and isinstance(sp.get("id"), str):
            return sp["id"]
    return None


def _app_adopt_terminal(addr: str, *, label: str, cwd: str, space_id: str | None = None) -> str | None:
    """Attach a live hc terminal into a visible non-focused tab in herdr-app."""
    if not _herdr_app_bin():
        return None
    sid = space_id or _resolve_active_run_space(cwd)
    if not sid:
        return None
    # Create a lightweight placeholder tab in the target space (non-focused) to obtain a valid tab id,
    # or stamp herdr.space + a fresh tab id directly so herdr-app adopts the terminal as a tab.
    tab_id = f"tb_{ioutil.new_worker_id().replace('-', '')[:24]}"
    try:
        _run_hc(
            ["label", "--json", addr, f"herdr.space={sid}", f"herdr.tab={tab_id}"],
            orig_args=("workspace", "create"),
        )
    except ioutil.RunError:
        return None
    _herdr_app_run(["do", "tab.rename", json.dumps({"id": tab_id, "name": label})])
    return tab_id


def _app_rename_tab_for_terminal(addr: str, label: str) -> None:
    if not _herdr_app_bin():
        return
    try:
        term = _hc_json(["get", "--json", addr], orig_args=("pane", "rename"))
    except ioutil.RunError:
        return
    labels = term.get("labels") if isinstance(term.get("labels"), dict) else {}
    tab_id = labels.get("herdr.tab")
    if isinstance(tab_id, str) and tab_id:
        _herdr_app_run(["do", "tab.rename", json.dumps({"id": tab_id, "name": label})])


def _extract_error_code(stderr: str) -> str | None:
    raw = stderr.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    if isinstance(error, dict) and isinstance(error.get("kind"), str):
        return error["kind"]
    result = payload.get("result")
    if isinstance(result, dict):
        error = result.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            return error["code"]
    return None


def running_sessions() -> list[str]:
    """Named sessions that currently have a server socket, sorted by name."""
    try:
        return sorted(sock.parent.name for sock in SESSIONS_DIR.glob("*/herdr.sock"))
    except OSError:
        return []


def _session_hint() -> str:
    # HERDR_SOCKET_PATH/HERDR_SESSION are not inherited by every caller (an Eval
    # kernel has neither), so "no server" usually means "wrong socket", not "no server".
    names = running_sessions()
    if not names:
        return ""
    return (
        f"; running named sessions: {', '.join(names)} "
        "(re-run with HERDR_SESSION=<name>, or herdr --session <name>)"
    )


def _is_read_only(args: tuple[str, ...]) -> bool:
    if len(args) < 2:
        return False
    head, verb = args[0], args[1]
    return (
        (head in ("agent", "workspace", "pane") and verb in ("get", "list", "show"))
        or (head == "pane" and verb == "process-info")
        or (head == "agent" and verb == "read")
        or (head == "api" and verb == "schema")
    )


def _run(args: tuple[str, ...]) -> subprocess.CompletedProcess:
    timeout = READ_TIMEOUT if _is_read_only(args) else TIMEOUT
    try:
        proc = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ioutil.RunError("herdr not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        err = ioutil.RunError(f"herdr {' '.join(args)} timed out after {timeout}s")
        err.code = "timeout"
        err.args_tuple = args
        err.returncode = None
        err.stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        err.stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        raise err from exc
    if proc.returncode != 0:
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        detail = err or out or f"rc={proc.returncode}"
        code = _extract_error_code(err) or _extract_error_code(out)
        if code == "server_not_running":
            detail += _session_hint()
        exc = ioutil.RunError(f"herdr {' '.join(args)} failed: {detail}")
        exc.code = code
        exc.args_tuple = args
        exc.returncode = proc.returncode
        exc.stdout = proc.stdout
        exc.stderr = proc.stderr
        raise exc
    return proc


def _run_hc(argv: list[str], *, orig_args: tuple[str, ...], timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run([_hc_bin(), *argv], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ioutil.RunError("hc not on PATH") from exc
    if proc.returncode != 0:
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        detail = err or out or f"rc={proc.returncode}"
        raw_code = _extract_error_code(err) or _extract_error_code(out)
        code = "agent_not_found" if raw_code == "not_found" else raw_code
        exc = ioutil.RunError(f"hc {' '.join(argv)} failed: {detail}")
        exc.code = code
        exc.args_tuple = orig_args
        exc.returncode = proc.returncode
        exc.stdout = proc.stdout
        exc.stderr = proc.stderr
        raise exc
    return proc


def _hc_json(argv: list[str], *, orig_args: tuple[str, ...], timeout: int = READ_TIMEOUT) -> dict[str, Any]:
    out = _run_hc(argv, orig_args=orig_args, timeout=timeout).stdout.strip()
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ioutil.RunError(f"hc {' '.join(argv)} returned non-JSON stdout: {out}") from exc
    if not isinstance(payload, dict):
        raise ioutil.RunError(f"hc {' '.join(argv)} returned unexpected output")
    return payload


def _hc_labels_to_tokens(labels: Any) -> dict[str, str]:
    if not isinstance(labels, dict):
        return {}
    tokens: dict[str, str] = {}
    for k, v in labels.items():
        if isinstance(k, str) and isinstance(v, str) and k.startswith(_HC_TOKEN_PREFIX):
            tokens[k[len(_HC_TOKEN_PREFIX):]] = v
    return tokens


def _hc_status(term: dict[str, Any]) -> str:
    st = term.get("status")
    if isinstance(st, dict):
        raw = st.get("state")
    else:
        raw = st
    if isinstance(raw, str) and raw in _HC_STATUS_MAP:
        return _HC_STATUS_MAP[raw]
    if term.get("state") == "finished":
        return "unknown"
    return "unknown"


def _hc_term_to_agent(term: dict[str, Any]) -> dict[str, Any]:
    addr = term.get("address")
    name = term.get("name")
    st = term.get("status")
    app = st.get("app") if isinstance(st, dict) else term.get("agent")
    agent: dict[str, Any] = {
        "workspace_id": addr,
        "pane_id": addr,
        "agent_status": _hc_status(term),
        "kind": harnesses.normalize_kind(app) or harnesses.default_kind(),
    }
    if isinstance(name, str) and name:
        agent["name"] = name
    else:
        # User-owned pane targets (unnamed terminals) use their immutable hc address as runtime agent_id.
        agent["agent_id"] = addr
    return agent


def _hc_term_to_workspace(term: dict[str, Any]) -> dict[str, Any]:
    addr = term.get("address")
    labels = term.get("labels") if isinstance(term.get("labels"), dict) else {}
    return {
        "workspace_id": addr,
        "label": labels.get("orch.label") or term.get("name") or addr,
        "root_pane": {"pane_id": addr},
        "tokens": _hc_labels_to_tokens(labels),
    }


def _parse_flag_map(rest: tuple[str, ...]) -> tuple[dict[str, str], list[str], list[str]]:
    flags: dict[str, str] = {}
    multi: list[str] = []
    pos: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--":
            pos.extend(rest[i + 1:])
            break
        if tok in ("--no-focus", "--wait", "--json"):
            flags[tok] = "1"
            i += 1
        elif tok == "--token" and i + 1 < len(rest):
            multi.append(rest[i + 1])
            i += 2
        elif tok.startswith("--") and i + 1 < len(rest):
            flags[tok] = rest[i + 1]
            i += 2
        else:
            pos.append(tok)
            i += 1
    return flags, multi, pos


def _hc_git_worktree_add(cwd: str, branch: str | None, base: str | None, path: str | None) -> tuple[str, str, str]:
    repo = str(Path(cwd).resolve())
    if not base:
        proc = ioutil.run_cmd(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True)
        if proc.returncode != 0:
            raise ioutil.RunError(f"git rev-parse HEAD failed: {proc.stderr.strip()}")
        base = proc.stdout.strip()
    if not branch:
        branch = f"herdr/{ioutil.new_worker_id()}"
    if not path:
        wt_root = Path.home() / ".herdr" / "worktrees" / Path(repo).name
        wt_root.mkdir(parents=True, exist_ok=True)
        path = str(wt_root / branch.replace("/", "-"))
    cmd = ["git", "-C", repo, "worktree", "add", "-b", branch, path, base]
    proc = ioutil.run_cmd(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ioutil.RunError(f"git worktree add failed: {proc.stderr.strip() or proc.stdout.strip()}")
    head_proc = ioutil.run_cmd(["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True)
    resolved_base = head_proc.stdout.strip() if head_proc.returncode == 0 else base
    return str(Path(path).resolve()), branch, resolved_base


def _hc_dispatch(args: tuple[str, ...]) -> dict[str, Any]:
    if len(args) < 2:
        raise ioutil.RunError(f"unsupported herdr-core command: {' '.join(args)}")
    res, verb, rest = args[0], args[1], args[2:]

    if res == "api" and verb == "schema":
        status = _hc_json(["server", "status", "--json"], orig_args=args)
        return {"result": {"protocol_version": status.get("version") or "herdr-core", "backend": "herdr-core"}}

    if res == "agent" and verb == "list":
        data = _hc_json(["ls", "--json"], orig_args=args)
        agents = [_hc_term_to_agent(t) for t in data.get("terminals", []) if isinstance(t, dict) and t.get("state") == "live"]
        return {"result": {"agents": agents}}

    if res == "agent" and verb == "get" and rest:
        term = _hc_json(["get", "--json", rest[0]], orig_args=args)
        if term.get("state") != "live":
            exc = ioutil.RunError(f"hc get {rest[0]}: terminal is {term.get('state')}")
            exc.code = "agent_not_found"
            exc.args_tuple = args
            raise exc
        return {"result": {"agent": _hc_term_to_agent(term)}}

    if res == "workspace" and verb == "list":
        data = _hc_json(["ls", "--json"], orig_args=args)
        workspaces = [_hc_term_to_workspace(t) for t in data.get("terminals", []) if isinstance(t, dict) and t.get("state") == "live"]
        return {"result": {"workspaces": workspaces}}

    if res == "workspace" and verb == "get" and rest:
        term = _hc_json(["get", "--json", rest[0]], orig_args=args)
        return {"result": {"workspace": _hc_term_to_workspace(term)}}

    if res == "workspace" and verb == "report-metadata" and rest:
        target = rest[0]
        flags, multi, _ = _parse_flag_map(rest[1:])
        labels: list[str] = []
        if flags.get("--source"):
            labels.append(f"{_HC_TOKEN_PREFIX}source={flags['--source']}")
        for tok in multi:
            if "=" in tok:
                k, v = tok.split("=", 1)
                labels.append(f"{_HC_TOKEN_PREFIX}{k}={v}")
        if labels:
            _run_hc(["label", "--json", target, *labels], orig_args=args)
        term = _hc_json(["get", "--json", target], orig_args=args)
        return {"result": {"workspace": _hc_term_to_workspace(term)}}

    if res == "workspace" and verb == "close" and rest:
        target = rest[0]
        try:
            _run_hc(["kill", "--json", target], orig_args=args)
        except ioutil.RunError as exc:
            if exc.code not in ("agent_not_found", "finished"):
                raise
        try:
            _run_hc(["rm", "--json", target], orig_args=args)
        except ioutil.RunError:
            pass
        return {"result": {"closed": target}}

    if res == "worktree" and verb == "remove":
        flags, _, pos = _parse_flag_map(rest)
        target = flags.get("--workspace") or (pos[0] if pos else "")
        if target:
            term = None
            try:
                term = _hc_json(["get", "--json", target], orig_args=args)
            except ioutil.RunError:
                pass
            try:
                _run_hc(["kill", "--json", target], orig_args=args)
            except ioutil.RunError:
                pass
            try:
                _run_hc(["rm", "--json", target], orig_args=args)
            except ioutil.RunError:
                pass
            labels = term.get("labels", {}) if isinstance(term, dict) else {}
            wt_path = labels.get("orch.worktree_path")
            wt_repo = labels.get("orch.worktree_repo")
            if wt_path and wt_repo:
                ioutil.run_cmd(["git", "-C", wt_repo, "worktree", "remove", "--force", wt_path])
        return {"result": {"removed": target}}

    if res in ("workspace", "worktree") and verb == "create":
        flags, _, _ = _parse_flag_map(rest)
        cwd = flags.get("--cwd") or os.getcwd()
        label = flags.get("--label") or "worker"
        extra_labels = [f"{_HC_TOKEN_PREFIX}label={label}"]
        wt_meta: dict[str, Any] = {}
        if res == "worktree":
            wt_path, branch, base_sha = _hc_git_worktree_add(cwd, flags.get("--branch"), flags.get("--base"), flags.get("--path"))
            cwd = wt_path
            extra_labels.extend([f"{_HC_TOKEN_PREFIX}worktree_path={wt_path}", f"{_HC_TOKEN_PREFIX}worktree_repo={Path(flags.get('--cwd') or os.getcwd()).resolve()}"])
            wt_meta = {"worktree_path": wt_path, "branch": branch, "base_sha": base_sha}
        run_argv = ["run", "--json", "--bg", "--no-parent", "--no-notify", "--outlive-parent", "--cwd", cwd]
        for lbl in extra_labels:
            run_argv.extend(["--label", lbl])
        created = _hc_json(run_argv, orig_args=args, timeout=TIMEOUT)
        addr = created.get("address") or created.get("id")
        if not addr:
            raise ioutil.RunError(f"hc run did not return terminal address: {created}")
        result: dict[str, Any] = {
            "workspace": {"workspace_id": addr, "label": label},
            "root_pane": {"pane_id": addr},
            **wt_meta,
        }
        return {"result": result}

    if res in ("workspace", "pane") and verb == "rename" and len(rest) >= 2:
        _run_hc(["label", "--json", rest[0], f"{_HC_TOKEN_PREFIX}label={rest[1]}"], orig_args=args)
        _app_rename_tab_for_terminal(rest[0], rest[1])
        return {"result": {}}

    if res == "agent" and verb == "start" and rest:
        target_name = rest[0]
        flags, _, extra = _parse_flag_map(rest[1:])
        pane = flags.get("--pane") or target_name
        kind = harnesses.default_kind(explicit=flags.get("--kind"))
        binary = harnesses.get_spec(kind).binary
        # Launch the agent directly in a fresh hc terminal bound to the same cwd/labels, replacing the placeholder shell.
        term = _hc_json(["get", "--json", pane], orig_args=args)
        cwd = term.get("cwd") or os.getcwd()
        old_labels = term.get("labels") if isinstance(term.get("labels"), dict) else {}
        tab_label = old_labels.get(f"{_HC_TOKEN_PREFIX}label") or target_name
        run_argv = ["run", "--json", "--bg", "--no-parent", "--no-notify", "--outlive-parent", "--name", target_name, "--cwd", cwd]
        for k, v in old_labels.items():
            if isinstance(k, str) and isinstance(v, str) and not k.startswith("herdr."):
                run_argv.extend(["--label", f"{k}={v}"])
        run_argv.extend(["--", binary, *extra])
        created = _hc_json(run_argv, orig_args=args, timeout=TIMEOUT)
        new_addr = created.get("address") or created.get("id")
        if isinstance(new_addr, str) and new_addr:
            _app_adopt_terminal(
                new_addr,
                label=tab_label,
                cwd=cwd,
                space_id=old_labels.get("herdr.space") if isinstance(old_labels.get("herdr.space"), str) else None,
            )
        try:
            _run_hc(["kill", "--json", pane], orig_args=args)
            _run_hc(["rm", "--json", pane], orig_args=args)
        except ioutil.RunError:
            pass
        deadline = time.monotonic() + 30
        live_term = created
        while time.monotonic() < deadline:
            try:
                live_term = _hc_json(["get", "--json", target_name], orig_args=args)
                if _hc_status(live_term) in ("idle", "done", "working"):
                    break
            except ioutil.RunError:
                pass
            time.sleep(0.5)
        return {"result": {"agent": _hc_term_to_agent(live_term), "workspace_id": new_addr, "pane_id": new_addr}}

    if res == "agent" and verb == "prompt" and len(rest) >= 2:
        target, text = rest[0], rest[1]
        flags, _, _ = _parse_flag_map(rest[2:])
        send_argv = ["send", "--json", target, text]
        _run_hc(send_argv, orig_args=args, timeout=TIMEOUT)
        if flags.get("--wait") and flags.get("--until") == "working":
            timeout_ms = int(flags.get("--timeout", str(PROMPT_WAIT_MS)))
            deadline = time.monotonic() + max(timeout_ms / 1000.0, 1.0)
            while time.monotonic() < deadline:
                term = _hc_json(["get", "--json", target], orig_args=args)
                if _hc_status(term) in ("working", "done"):
                    return {"result": {"agent": _hc_term_to_agent(term)}}
                time.sleep(0.25)
            exc = ioutil.RunError(f"hc send {target} timed out waiting for working")
            exc.code = "agent_prompt_stalled"
            exc.args_tuple = args
            raise exc
        return {"result": {}}

    if res == "agent" and verb == "send-keys" and len(rest) >= 2:
        target = rest[0]
        keys = [k.lower() for k in rest[1:]]
        _run_hc(["keys", "--json", target, *keys], orig_args=args)
        return {"result": {}}

    if res == "agent" and verb == "wait" and rest:
        target = rest[0]
        flags, _, _ = _parse_flag_map(rest[1:])
        timeout_ms = int(flags.get("--timeout", str(PROMPT_WAIT_MS)))
        want = flags.get("--until")
        if want == "working":
            deadline = time.monotonic() + max(timeout_ms / 1000.0, 1.0)
            while time.monotonic() < deadline:
                term = _hc_json(["get", "--json", target], orig_args=args)
                if _hc_status(term) in ("working", "done"):
                    return {"result": {"agent": _hc_term_to_agent(term)}}
                time.sleep(0.25)
            exc = ioutil.RunError(f"hc wait {target} timed out waiting for working")
            exc.code = "timeout"
            exc.args_tuple = args
            raise exc
        secs = max(1, int(timeout_ms / 1000))
        _run_hc(["wait", "--json", "--timeout", str(secs), target], orig_args=args, timeout=max(TIMEOUT, secs + 10))
        return {"result": {}}

    if res == "pane" and verb == "run" and len(rest) >= 2:
        _run_hc(["send", "--json", rest[0], rest[1]], orig_args=args)
        return {"result": {}}

    if res == "pane" and verb == "get" and rest:
        term = _hc_json(["get", "--json", rest[0]], orig_args=args)
        addr = term.get("address")
        st = term.get("status")
        app = st.get("app") if isinstance(st, dict) else term.get("agent")
        return {"result": {"type": "pane_info", "pane": {"pane_id": addr, "workspace_id": addr, "agent": app}}}

    if res == "pane" and verb == "process-info" and rest:
        flags, _, pos = _parse_flag_map(rest)
        target = flags.get("--pane") or (pos[0] if pos else "")
        term = _hc_json(["get", "--json", target], orig_args=args)
        fg = term.get("foreground") if isinstance(term.get("foreground"), dict) else {}
        pid = fg.get("pid") or term.get("pid")
        return {
            "result": {
                "type": "pane_process_info",
                "process_info": {
                    "pane_id": term.get("address"),
                    "foreground_process_group_id": pid,
                    "processes": [fg] if fg else [],
                },
            }
        }

    raise ioutil.RunError(f"unsupported herdr-core command: {' '.join(args)}")


def herdr(*args: str) -> dict[str, Any]:
    if is_core_mode():
        return _hc_dispatch(args)
    out = _run(args).stdout.strip()
    if not out:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ioutil.RunError(f"herdr {' '.join(args)} returned non-JSON stdout: {out}") from exc
    if not isinstance(payload, dict):
        raise ioutil.RunError(f"herdr {' '.join(args)} returned unexpected output")
    return payload


def herdr_text(*args: str) -> str:
    """Raw stdout for subcommands that do not emit JSON (e.g. `agent read`)."""
    if is_core_mode():
        if len(args) >= 3 and args[0] == "agent" and args[1] == "read":
            target = args[2]
            flags, _, _ = _parse_flag_map(args[3:])
            lines = flags.get("--lines", "120")
            return _run_hc(["read", "--lines", lines, target], orig_args=args, timeout=READ_TIMEOUT).stdout
        raise ioutil.RunError(f"unsupported herdr-core text command: {' '.join(args)}")
    return _run(args).stdout

def _unconfirmed(exc: BaseException) -> bool:
    """True when herdr accepted the text but never observed the turn start."""
    return getattr(exc, "code", None) in _UNCONFIRMED_CODES


def deliver_prompt(call, target: str, text: str) -> None:
    """Submit a prompt through `call` and prove the agent picked it up.

    A bare `herdr agent prompt` exiting 0 only proves the CLI accepted the text:
    it can land in the composer as an unsubmitted attachment while the pane sits
    idle forever. `--wait --until working` makes the server observe the turn
    actually starting; `agent_prompt_stalled`/`timeout` means the composer still
    holds it, so submit the pending composer with Enter and observe again.
    Raises RunError with code `agent_not_ready` when the agent never leaves idle
    — an unconfirmed delivery is blocked, never dispatched.

    `call` is the caller's herdr invoker (`herdr_cli.herdr` in every script), so
    the whole sequence goes through one boundary.
    """
    wait = ("--until", "working", "--timeout", str(PROMPT_WAIT_MS))
    try:
        call("agent", "prompt", target, text, "--wait", *wait)
        return
    except RuntimeError as exc:
        if not _unconfirmed(exc):
            raise
    call("agent", "send-keys", target, "Enter")
    try:
        call("agent", "wait", target, *wait)
    except RuntimeError as exc:
        if not _unconfirmed(exc):
            raise
        unconfirmed = ioutil.RunError(
            f"agent {target} never left idle after its prompt was submitted: {exc}"
        )
        unconfirmed.code = "agent_not_ready"
        raise unconfirmed from exc


def stamp_workspace_tokens(
    ws_id: str,
    run_id: str | None = None,
    node: str | None = None,
    attempt_id: str | None = None,
    *,
    source: str = PROVENANCE_ORCHESTRATOR,
    tokens: tuple[str, ...] | list[str] = (),
    invoke=None,
) -> dict[str, Any]:
    """Stamp context and caller-specific tokens in one metadata operation."""
    argv = ["workspace", "report-metadata", ws_id, "--source", source]
    for token in tokens:
        argv.extend(("--token", token))
    for key, value in (("run", run_id), ("node", node), ("attempt", attempt_id)):
        if value is not None:
            argv.extend(("--token", token_flag(key, value)))
    return (invoke or herdr)(*argv)
