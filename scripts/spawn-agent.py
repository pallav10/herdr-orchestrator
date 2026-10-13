#!/usr/bin/env python3
"""Deterministically spawn a Herdr worker: workspace/worktree -> pane -> agent -> prompt.

Usage: spawn-agent.py NAME [--cwd DIR] [--kind omp]
                      [--branch NAME] [--base REF] [--path PATH]
                      [--model-role ROLE | --model provider/model[:thinking]]
                      [--prompt TEXT | --prompt-file PATH] [--plan PATH]
                      [--orchestrate | --no-orchestrate]
                      [--retries 10] [--retry-delay 2.0]
                      [--run PATH --node ID --attempt N --session PATH
                       --holder-workspace ID --holder-pane ID --epoch N]
                      [--resume [--delivered]] [--requested-effort E]
                      [--lock-timeout SECONDS]

Does, in one deterministic step, what the manual flow does:
  1. writer isolation (`--branch`, `--base`, and/or `--path`):
       `herdr worktree create --label LABEL --no-focus [--cwd DIR]
        [--branch B] [--base REF] [--path PATH]`
       `--cwd`, when set, is `worktree_creation_source`: a primary checkout is
       passed unchanged; a linked worktree is replaced by its verified primary.
       Read-only `workspace create` keeps the requested cwd. Identity `repo`
       stays the accepted run repo. Omitting `--branch` lets Herdr auto-generate
       one; `--base` is applied when present. Otherwise:
       `herdr workspace create --cwd DIR --label LABEL --no-focus`
       Contextual LABEL is the human title plus ` · <worker_id>` until
       dispatch-identity.json exists (that suffix is how an orphan is found);
       then the suffix is dropped. The human title is `<NAME> · a<attempt>` when
       NAME is the node id, else `<NAME> · <node>/a<attempt>`. Uncontextual LABEL
       is NAME. The pane gets the human title right after creation. Same-attempt
       resume repairs pane and workspace titles only when it already calls Herdr.
       Display renames are warn-only and append no ledger events.
  2. reads `.result.root_pane.pane_id` from the JSON response (never the UI)
  3. resolves the model: --model and --model-role are alternatives. Roles are
     looked up in `omp config get modelRoles --json` (e.g. default/slow/smol);
   atomically cached in ~/.omp/run/herdr-cache/model-roles.json while readable
   ~/.omp/agent/config.yml retains its st_mtime_ns; corrupt caches are misses.
     the `provider/model[:thinking]` spec is split into `--model` + `--thinking`
     passed after `--`; neither flag -> pane default
  4. Contextual `--run/--node/--attempt` generates `worker_id`
     (`fo-[0-9a-f]{24}`) and persists immutable `launch-intent.json` before
     any Herdr side effect, then `herdr agent start WORKER_ID --kind KIND
     --pane PANE [-- --model M --thinking T]`, retried on `agent_pane_busy`.
   Busy retries sleep 0.25, 0.5, 1, 2... seconds capped at --retry-delay.
     Uncontextual invocation starts under NAME unchanged. `agent_not_ready`
     means the name is live but blocked at first-run UI: stamp tokens, skip
     the prompt, return blocked. Do not wait in the foreground and do not
     close the workspace.
  5. resolve live agent identity/routing. Contextual mode verifies the
     returned name equals worker_id (never waits for a Herdr-issued
     agent_id) and persists dispatch-identity.json with identity_kind
     managed-name/worker_id, never the CLI alias. Herdr 0.8.2 issues no
     agent id; uncontextual `agent` equals NAME (the command target) when
     the server returns none. Then stamp provenance/role[/plan] and
     contextual run/node/attempt before any work prompt. Stamp/identity
     failure retains a started agent and rolls back only an unstarted
     workspace; work is not delivered.
  6. optional prompt delivery through `herdr_cli.deliver_prompt`: `herdr agent
     prompt NAME PROMPT --wait --until working --timeout 15000`, then
     `agent send-keys NAME Enter` + `agent wait --until working` when the CLI
     reports `agent_prompt_stalled`/`timeout` (the text reached the composer but
     was never submitted). An ok exit from `agent prompt` is not proof of
     delivery; only an observed departure from idle is.
     Text is passed through prepare_prompt: --kind omp defaults to a leading
     bare `orchestrate` line; other kinds default off. Override with
     --orchestrate / --no-orchestrate.
     Skipped when start returned `agent_not_ready`. `agent_blocked` and an
     unconfirmed delivery are both reported as blocked, not retried, and do not
     roll back. Non-blocked prompt failure keeps the identity so reconcile can
     reattach; no second launch. Post-start failures report workspace/pane and
     the exact `prompt-agent.py` follow-up. Token values capped at 80 chars,
     matching Herdr 0.8.2.

Contextual launches leave four sidecars in the attempt dir, in order:
  launch-intent.json      before any Herdr side effect (worker_id, request)
  dispatch-identity.json  after `agent start` (workspace/pane/routing)
  prompt-attempt.json     immediately before `agent prompt` (never removed)
  launch-complete.json    after confirmed delivery (prompted/blocked/tokens);
                          prompted=true means the agent was observed leaving idle
Resume is at-most-once by construction: `--resume` (contextual only)
  - launch-complete.json exists: reprint the payload (resumed=true), no
    Herdr calls;
  - else dispatch-identity.json exists: if prompt-attempt.json exists the
    send was attempted but never acknowledged -> exit 3 without touching
    Herdr, printing the inspect/deliver commands and the `--resume
    --delivered` acknowledgement; otherwise the prompt was never sent:
    verify the live worker (get/stamp/prompt, no create/start) and record;
  - else launch-intent.json exists: proceed only when `agent get` reports
    `agent_not_found` AND `workspace list` has no workspace labeled with the
    worker_id (any other outcome -> exit 3); then create/start/deliver
    reusing the persisted worker_id;
  - else a normal first launch.
  `--resume --delivered` (requires dispatch-identity.json) records
  launch-complete.json with prompted=true after the operator delivered the
  prompt by hand; no Herdr calls.

Prints one JSON line:
  {"name","workspace","pane","agent","model","thinking","routing","prompted","blocked","worktree","tokens","resumed"}
  Contextual dispatch also includes "identity_kind","worker_id","alias","repo"
  (and "delivered": true after `--resume --delivered`);
  "name" is the worker_id (canonical target), never the CLI alias. Record the
  whole line as the `dispatched` handle: writer success gates compare it to
  dispatch-identity.json field by field.
Exit codes: 0 ok, 1 herdr/omp failure, 2 bad arguments, 3 precondition/refusal.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from prompt_contract import prepare_prompt


import artifacts
import authz
import graph_model
import ioutil
import cli_common

CLI_EXTRA_CODES = {RuntimeError: 1}
import ledger
from herdr_cli import deliver_prompt, herdr, stamp_workspace_tokens
from graph_model import AGENT_NAME_RE
from cli_common import SCRIPTS, run_context, token_flag, token_value, validate_auth_args
from ioutil import atomic_text, first_str, payload_field
from routing import routing_for, split_spec


def resolve_role(role: str) -> str:
    config = Path.home() / ".omp/agent/config.yml"
    cache = Path.home() / ".omp/run/herdr-cache/model-roles.json"
    try:
        with config.open("rb") as fh:
            mtime = config.stat().st_mtime_ns
    except OSError:
        mtime = None
    roles = None
    if mtime is not None:
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if (isinstance(saved, dict) and saved.get("config_mtime_ns") == mtime
                    and isinstance(saved.get("roles"), dict)
                    and all(isinstance(value, str) for value in saved["roles"].values())):
                roles = saved["roles"]
        except (OSError, ValueError, AttributeError):
            pass
    if roles is None:
        proc = ioutil.run_cmd(["omp", "config", "get", "modelRoles", "--json"], capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"omp config get modelRoles failed: {proc.stderr.strip()}")
        roles = json.loads(proc.stdout)["value"]
        if mtime is not None:
            try:
                if config.stat().st_mtime_ns == mtime:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    ioutil.atomic_json(cache, {"config_mtime_ns": mtime, "roles": roles})
            except OSError:
                pass
    if role not in roles:
        raise RuntimeError(f"model role {role!r} not in modelRoles {sorted(roles)}")
    return roles[role]


def created_ids(result: dict[str, Any]) -> tuple[str, str]:
    workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else {}
    root = result.get("root_pane") if isinstance(result.get("root_pane"), dict) else {}
    ws_id = workspace.get("workspace_id")
    pane = root.get("pane_id")
    if not isinstance(ws_id, str) or not ws_id or not isinstance(pane, str) or not pane:
        raise RuntimeError("create response missing workspace_id or root_pane.pane_id")
    return ws_id, pane


def context_tokens(context: tuple[Path, str, str] | None, run_id: str | None = None) -> dict[str, str] | None:
    if context is None:
        return None
    _, node, attempt = context
    return {"provenance": graph_model.PROVENANCE_ORCHESTRATOR, "run": token_value(run_id), "node": token_value(node), "attempt": token_value(attempt)}


def sidecar_path(kind: str, context, parser, *, require_absent: bool = False) -> Path | None:
    if context is None:
        return None
    run, node, attempt = context
    try:
        directory = ledger.attempt_dir(run, node, int(attempt))
    except (ioutil.UsageError, ioutil.PreconditionError, ValueError) as exc:
        parser.error(str(exc))
    if not directory.is_dir():
        parser.error(f"attempt artifact dir missing: {directory}")
    path = directory / f"{kind}.json"
    if require_absent and path.exists():
        parser.error(f"{kind.replace('-', ' ')} already exists: {path}")
    return path

def _objects(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []




def load_contextual_spawn(context: tuple[Path, str, str], parser: argparse.ArgumentParser) -> dict[str, Any]:
    run, node_id, attempt = context
    try:
        loaded = ledger.load_run(run)
    except (ioutil.UsageError, ioutil.PreconditionError) as exc:
        parser.error(str(exc))
    repo = loaded.get("repo")
    if not isinstance(repo, str) or not repo:
        parser.error("run.json missing repo")
    try:
        canonical_repo = ioutil.canonical_path(repo)
    except ioutil.UsageError as exc:
        parser.error(str(exc))
    if repo != canonical_repo:
        parser.error("run.json.repo must be canonical")
    try:
        graph = ledger.load_accepted_graph(run)
    except (ioutil.UsageError, ioutil.PreconditionError, ioutil.RunError) as exc:
        if not (run / "graph.json").is_file():
            parser.error(f"accepted graph missing: {run / 'graph.json'}")
        parser.error(f"cannot read accepted graph: {exc}")
    try:
        graph_model.validate_graph(graph)
    except ioutil.PreconditionError as exc:
        parser.error(f"graph is not accepted: {exc}")
    if not isinstance(graph, dict) or not isinstance(graph.get("base_sha"), str) or not graph["base_sha"]:
        parser.error("graph is not accepted")
    if graph.get("run") != loaded.get("path"):
        parser.error("graph.run mismatch")
    if graph.get("base_sha") != loaded.get("base_sha"):
        parser.error("graph.base_sha mismatch")
    run_id = loaded.get("id")
    if not isinstance(run_id, str) or not run_id:
        parser.error("run.json missing id")
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict) or not isinstance(nodes.get(node_id), dict):
        parser.error(f"graph missing node {node_id}")
    node = nodes[node_id]
    if node.get("executor") != "herdr-agent":
        parser.error(f"node {node_id} executor must be herdr-agent")
    integration = graph_model.declares_output(node.get("outputs") or [], "integration")
    writer = bool(node.get("worktree") or node.get("owns")) or graph_model.declares_output(node.get("outputs") or [], "vcs") or integration
    worktree = node.get("worktree")
    graph_branch = worktree.get("branch") if isinstance(worktree, dict) else None
    if graph_branch is not None and (not isinstance(graph_branch, str) or not graph_branch):
        parser.error("graph worktree.branch must be a non-empty string")
    integration_branch = f"{run_id}/{node_id}/{attempt}" if integration else None
    if integration and graph_branch not in (None, integration_branch):
        parser.error(f"integration graph worktree.branch must be {integration_branch}")
    prior_worktrees: set[str] = set()
    if integration:
        for prior in (run / "artifacts" / node_id).iterdir():
            if not prior.is_dir() or not prior.name.isdigit() or int(prior.name) >= int(attempt):
                continue
            identity_path = ledger.attempt_dir(run, node_id, int(prior.name)) / "dispatch-identity.json"
            if not identity_path.is_file():
                continue
            try:
                identity = json.loads(identity_path.read_text(encoding="utf-8"))
                prior_path = identity.get("worktree_path") if isinstance(identity, dict) else None
                if not isinstance(prior_path, str) or not prior_path:
                    raise ValueError("missing worktree_path")
                prior_worktrees.add(ioutil.canonical_path(prior_path))
            except (OSError, json.JSONDecodeError, ioutil.UsageError, ValueError) as exc:
                parser.error(f"invalid prior integration dispatch identity {identity_path}: {exc}")
    return {
        "run_id": run_id,
        "node": node,
        "base_sha": graph["base_sha"],
        "repo": repo,
        "writer": writer,
        "integration": integration,
        "required_branch": integration_branch or graph_branch,
        "forbidden_worktree_paths": {repo, *prior_worktrees},
    }


def stable_agent(payload: dict[str, Any] | None, *, name: bool = False) -> str | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    keys = ("name",) if name else ("agent_id", "id")
    agent = result.get("agent")
    return first_str(result, keys) or (first_str(agent, keys) if not name or isinstance(agent, dict) else None)


def _dicts(value: dict[str, Any]) -> list[dict[str, Any]]:
    out = [obj for key in ("worktree", "workspace") if isinstance((obj := value.get(key)), dict)]
    out.append(value)
    out.extend(v for v in value.values() if isinstance(v, dict) and v not in out)
    return out


def _created_string(created: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for obj in _dicts(created):
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def worktree_details(
    created: dict[str, Any],
    path_arg: str | None,
    branch_arg: str | None,
    base_arg: str | None,
    enabled: bool,
    *,
    require_complete: bool = False,
) -> tuple[str | None, str | None, str | None]:
    if not enabled:
        return None, None, None
    path = _created_string(created, ("worktree_path", "path", "checkout_path"))
    if path is None and path_arg:
        path = path_arg
    path = str(Path(path).resolve()) if path else None
    branch = _created_string(created, ("branch", "branch_name")) or branch_arg
    resolved_base = _created_string(created, ("base_sha", "resolved_base", "resolved_base_sha", "base", "base_ref"))
    if require_complete and path and branch and not resolved_base:
        proc = ioutil.run_cmd(
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        resolved_base = proc.stdout.strip() if proc.returncode == 0 else None
        if not resolved_base:
            detail = proc.stderr.strip() or f"exit {proc.returncode}"
            raise RuntimeError(f"git -C {path} rev-parse HEAD failed: {detail}")
    base = resolved_base or (None if require_complete else base_arg)
    missing = [
        name for name, value in (("worktree_path", path), ("branch", branch), ("base", base))
        if not value
    ]
    if missing and require_complete:
        raise RuntimeError("worktree create response missing " + ", ".join(missing))
    return path, branch, base


def write_once(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    label = path.stem.replace("-", " ")
    try:
        with path.open("x", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    except FileExistsError as exc:
        raise RuntimeError(f"{label} already exists: {path}") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot write {label}: {exc}") from exc


def write_sidecar(path: Path, payload: dict[str, Any]) -> None:
    try:
        atomic_text(path, json.dumps(payload, sort_keys=True) + "\n")
    except OSError as exc:
        raise RuntimeError(f"cannot write {path.name}: {exc}") from exc




def rollback_commands(ws_id: str, *, worktree: bool) -> list[list[str]]:
    if worktree:
        return [["worktree", "remove", "--workspace", ws_id]]
    return [["workspace", "close", ws_id]]


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid {path.name}")
    return data


def verify_live_worker(worker_id: str, identity: dict[str, Any]) -> dict[str, Any]:
    live = herdr("agent", "get", worker_id)
    returned_name = stable_agent(live, name=True)
    if returned_name != worker_id:
        raise RuntimeError(
            f"agent get response name {returned_name!r} does not match worker_id {worker_id!r}"
        )
    live_ws = payload_field(live, {"workspace_id"})
    if live_ws is not None and live_ws != identity.get("workspace"):
        raise RuntimeError(
            f"agent get workspace_id {live_ws!r} does not match worker_id {worker_id}"
        )
    live_pane = payload_field(live, {"pane_id"})
    if live_pane is not None and live_pane != identity.get("pane"):
        raise RuntimeError(
            f"agent get pane_id {live_pane!r} does not match worker_id {worker_id}"
        )
    return live


def deliver_after_start(
    *,
    ws_id: str,
    pane: str,
    agent_target: str,
    role: str,
    plan_path: str | None,
    context: tuple[Path, str, str] | None,
    plan: dict[str, Any] | None,
    prompt: str | None,
    not_ready: bool,
    marker_path: Path | None,
) -> tuple[bool, bool, dict]:
    extra_tokens = [
        token_flag("provenance", graph_model.PROVENANCE_ORCHESTRATOR),
        token_flag("role", role),
    ]
    if plan_path:
        extra_tokens.append(token_flag("plan", plan_path))
    node = attempt_id = None
    run_id = None
    if context:
        _, node, attempt_id = context
        run_id = plan["run_id"]
    stamp_workspace_tokens(
        ws_id, run_id, node, attempt_id, tokens=extra_tokens, invoke=herdr,
    )
    if context:
        display_rename("workspace", ws_id, human_label(role, node, attempt_id))
    tokens = herdr("workspace", "get", ws_id)["result"]["workspace"].get("tokens", {})
    prompted = False
    blocked = not_ready
    if prompt is not None and not not_ready:
        if marker_path is not None:
            write_sidecar(marker_path, {"worker_id": agent_target, "workspace": ws_id, "pane": pane, "t": time.time()})
        try:
            deliver_prompt(herdr, agent_target, prompt)
            prompted = True
        except RuntimeError as exc:
            # unconfirmed delivery and a blocked agent are both `blocked`, never `dispatched`
            if getattr(exc, "code", None) not in {"agent_blocked", "agent_not_ready"}:
                raise
            blocked = True
    return prompted, blocked, tokens


def prompt_command(agent_target: str, args: argparse.Namespace) -> str | None:
    script = SCRIPTS / "prompt-agent.py"
    if args.prompt_file is not None:
        return f"python3 {script} {agent_target} --lock-timeout {args.lock_timeout} --prompt-file {Path(args.prompt_file).resolve()}"
    if args.prompt is not None:
        return f"python3 {script} {agent_target} --lock-timeout {args.lock_timeout} --prompt {shlex.quote(args.prompt)}"
    return None


def ambiguous_delivery_error(worker_id: str, ws_id: str, pane: str, args: argparse.Namespace) -> ioutil.PreconditionError:
    deliver = prompt_command(worker_id, args) or f"python3 {SCRIPTS / 'prompt-agent.py'} {worker_id} --lock-timeout {args.lock_timeout} --prompt-file <F> (or --prompt ...)"
    acknowledge = shlex.join([
        "python3", str(SCRIPTS / "spawn-agent.py"), args.name,
        "--run", args.run, "--node", args.node, "--attempt", str(args.attempt),
        "--session", args.session, "--holder-workspace", args.holder_workspace,
        "--holder-pane", args.holder_pane, "--epoch", str(args.epoch),
        "--lock-timeout", str(args.lock_timeout),
        "--resume", "--delivered",
    ])
    return ioutil.PreconditionError(
        f"prompt delivery for {worker_id} is ambiguous (send attempted, completion unrecorded); "
        f"workspace {ws_id} pane {pane}; "
        f"inspect: herdr agent read {worker_id} --source recent --lines 40; "
        f"deliver if missing: {deliver}; "
        f"then acknowledge: {acknowledge}"
    )


def follow_up_error(
    exc: BaseException,
    agent_target: str,
    ws_id: str,
    pane: str,
    args: argparse.Namespace,
) -> RuntimeError:
    live = f"{exc}; agent {agent_target} is live in workspace {ws_id} pane {pane} without its prompt"
    follow_up = prompt_command(agent_target, args)
    if follow_up is not None:
        live = f"{live}; deliver it with: {follow_up}"
    return RuntimeError(live)


def record_and_payload(
    *,
    complete_path: Path | None,
    worker_id: str | None,
    ws_id: str,
    pane: str,
    prompted: bool,
    blocked: bool,
    tokens: dict[str, str],
    agent_target: str,
    routing: dict[str, Any],
    use_worktree: bool,
    worktree_path,
    branch,
    base,
    context,
    alias: str,
    repo,
    agent,
    resumed: bool,
) -> dict[str, Any]:
    if complete_path is not None:
        write_sidecar(complete_path, {
            "worker_id": worker_id,
            "workspace": ws_id,
            "pane": pane,
            "prompted": prompted,
            "blocked": blocked,
            "tokens": tokens,
        })
    payload = {
        "name": agent_target,
        "workspace": ws_id,
        "pane": pane,
        "model": routing.get("resolved") if isinstance(routing, dict) else None,
        "thinking": routing.get("thinking") if isinstance(routing, dict) else None,
        "routing": routing,
        "prompted": prompted,
        "blocked": blocked,
        "worktree": use_worktree,
        "worktree_path": worktree_path,
        "branch": branch,
        "base": base,
        "tokens": tokens,
        "resumed": resumed,
    }
    if context:
        payload["identity_kind"] = graph_model.IDENTITY_MANAGED_NAME
        payload["worker_id"] = worker_id
        payload["alias"] = alias
        payload["repo"] = repo
    else:
        payload["agent"] = agent
    return payload




def dispatch_identity(context, plan, worker_id, ws_id, pane, branch, base, worktree, worktree_path, *, routing=None):
    identity = {
        "workspace": ws_id, "pane": pane, "branch": branch, "base": base,
        "worktree": worktree, "worktree_path": worktree_path,
        "tokens": context_tokens(context, plan["run_id"] if plan else None),
    }
    if routing is not None:
        identity["routing"] = routing
    if context:
        run, node, attempt = context
        identity.update(
            kind="herdr-agent", identity_kind=graph_model.IDENTITY_MANAGED_NAME,
            worker_id=worker_id, run=str(run), run_id=plan["run_id"],
            node=node, attempt=attempt, repo=plan["repo"],
        )
    return identity


def resume_from_launch_record(identity_path, complete_path, args, plan) -> int:
    """Resume launch-complete without rewriting immutable launch evidence."""
    identity = _read_json_object(identity_path)
    record = _read_json_object(complete_path)
    worker_id = identity.get("worker_id")
    payload = record_and_payload(
        complete_path=None, worker_id=worker_id, agent_target=worker_id,
        ws_id=record.get("workspace", identity.get("workspace")),
        pane=record.get("pane", identity.get("pane")),
        prompted=record.get("prompted"), blocked=record.get("blocked"), tokens=record.get("tokens"),
        routing=identity.get("routing") if isinstance(identity.get("routing"), dict) else {},
        use_worktree=identity.get("worktree"), worktree_path=identity.get("worktree_path"),
        branch=identity.get("branch"), base=identity.get("base"), context=True,
        alias=args.name, repo=identity.get("repo") or (plan["repo"] if plan else None),
        agent=None, resumed=True,
    )
    print(json.dumps(payload))
    return 0


def resume_from_dispatch_identity(
    identity_path, marker_path, complete_path, args, context, plan, prompt
) -> int:
    """Resume against an existing dispatch identity: deliver (or record) the prompt."""
    identity = _read_json_object(identity_path)
    worker_id = identity["worker_id"]
    ws_id = identity["workspace"]
    pane = identity["pane"]
    agent_target = worker_id
    routing = identity.get("routing") if isinstance(identity.get("routing"), dict) else {}
    if args.delivered:
        prompted, blocked = True, False
        tokens = identity.get("tokens") if isinstance(identity.get("tokens"), dict) else {}
    elif marker_path.exists():
        raise ambiguous_delivery_error(worker_id, ws_id, pane, args)
    else:
        verify_live_worker(worker_id, identity)
        _, node, attempt_id = context
        display_rename("pane", pane, human_label(args.name, node, attempt_id))
        try:
            prompted, blocked, tokens = deliver_after_start(
                ws_id=ws_id,
                pane=pane,
                agent_target=agent_target,
                role=args.name,
                plan_path=args.plan,
                context=context,
                plan=plan,
                prompt=prompt,
                not_ready=False,
                marker_path=marker_path,
            )
        except Exception as exc:
            raise follow_up_error(exc, agent_target, ws_id, pane, args) from exc
    payload = record_and_payload(
        complete_path=complete_path,
        worker_id=worker_id,
        ws_id=ws_id,
        pane=pane,
        prompted=prompted,
        blocked=blocked,
        tokens=tokens,
        agent_target=agent_target,
        routing=routing,
        use_worktree=bool(identity.get("worktree")),
        worktree_path=identity.get("worktree_path"),
        branch=identity.get("branch"),
        base=identity.get("base"),
        context=context,
        alias=args.name,
        repo=identity.get("repo") or plan["repo"],
        agent=None,
        resumed=True,
    )
    if args.delivered:
        payload["delivered"] = True
    print(json.dumps(payload))
    return 0


def resume_from_launch_intent(launch_intent_path_, run_path, node_id, attempt_id) -> str:
    """Resume from a launch intent with no identity: prove nothing launched; returns worker_id."""
    intent = _read_json_object(launch_intent_path_)
    if (intent.get("run"), intent.get("node"), str(intent.get("attempt"))) != (
        str(run_path), node_id, attempt_id
    ):
        raise ioutil.PreconditionError("launch intent does not match this run/node/attempt")
    worker_id = intent["worker_id"]
    try:
        live = herdr("agent", "get", worker_id)
    except RuntimeError as exc:
        if getattr(exc, "code", None) != "agent_not_found":
            raise ioutil.PreconditionError(
                f"launch intent worker {worker_id} lookup inconclusive: {exc}"
            ) from exc
    else:
        ws_ref = payload_field(live, {"workspace_id"})
        raise ioutil.PreconditionError(
            f"launch intent worker {worker_id} is live without dispatch identity "
            f"(workspace {ws_ref}, pane {payload_field(live, {'pane_id'})}); no prompt was sent — "
            f"close it with: herdr workspace close {ws_ref} "
            f"(worktrees: herdr worktree remove --workspace {ws_ref}) and re-run"
        )
    try:
        listing = herdr("workspace", "list").get("result")
    except RuntimeError as exc:
        raise ioutil.PreconditionError(
            f"launch intent worker {worker_id} workspace listing inconclusive: {exc}"
        ) from exc
    workspaces = listing.get("workspaces") if isinstance(listing, dict) else None
    if not isinstance(workspaces, list):
        raise ioutil.PreconditionError(
            f"launch intent worker {worker_id} workspace listing inconclusive"
        )
    for workspace in _objects(workspaces):
        if worker_id in str(workspace.get("label") or "").split(" · "):
            ws_ref = workspace.get("workspace_id")
            raise ioutil.PreconditionError(
                f"workspace {ws_ref} exists for launch intent worker {worker_id} without dispatch identity; "
                f"close it with: herdr workspace close {ws_ref} "
                f"(worktrees: herdr worktree remove --workspace {ws_ref}) and re-run"
            )
    return worker_id


def human_label(name: str, node: str | None = None, attempt: str | None = None) -> str:
    """Visible title. No context keeps NAME. Matching node id is `<name> · a<attempt>`; an alias keeps `<name> · <node>/a<attempt>`."""
    if node is None:
        return name
    if name == node:
        return f"{name} · a{attempt}"
    return f"{name} · {node}/a{attempt}"


def display_rename(kind: str, target: str, label: str) -> None:
    try:
        herdr(kind, "rename", target, label)
    except RuntimeError as exc:
        print(f"warn: {kind} rename failed for {target}: {exc}", file=sys.stderr)


def _same_checkout(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return ioutil.canonical_path(left) == ioutil.canonical_path(right)
    except ioutil.UsageError:
        return str(Path(left).resolve(strict=False)) == str(Path(right).resolve(strict=False))


def build_create_argv(args, initial_label: str, use_worktree: bool) -> list[str]:
    if use_worktree:
        create = ["worktree", "create", "--label", initial_label, "--no-focus"]
        if args.cwd:
            create += ["--cwd", ioutil.worktree_creation_source(args.cwd)]
        if args.branch:
            create += ["--branch", args.branch]
        if args.base:
            create += ["--base", args.base]
        if args.path:
            create += ["--path", args.path]
        return create
    create = ["workspace", "create", "--label", initial_label, "--no-focus"]
    if args.cwd:
        create[2:2] = ["--cwd", args.cwd]
    return create


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--kind", default="omp")
    parser.add_argument("--branch", default=None, help="worktree branch; omitted => Herdr generates one")
    parser.add_argument("--base", default=None, help="worktree base ref (implies worktree create)")
    parser.add_argument("--path", default=None, help="worktree checkout path")
    models = parser.add_mutually_exclusive_group()
    models.add_argument("--model-role", default=None)
    models.add_argument("--model", default=None, help="provider/model[:thinking]")
    prompts = parser.add_mutually_exclusive_group()
    prompts.add_argument("--prompt", default=None)
    prompts.add_argument("--prompt-file", default=None)
    parser.add_argument("--plan", default=None)
    parser.add_argument("--run", default=None)
    parser.add_argument("--node", default=None)
    parser.add_argument("--attempt", type=int, default=None)
    parser.add_argument("--session", default=None)
    parser.add_argument("--holder-workspace", default=None)
    parser.add_argument("--holder-pane", default=None)
    parser.add_argument("--epoch", type=int, default=None)
    parser.add_argument("--requested-effort", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--delivered", action="store_true", help="with --resume: record the prompt as delivered by hand")
    parser.add_argument("--retries", type=int, default=10)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    orch = parser.add_mutually_exclusive_group()
    orch.add_argument("--orchestrate", action="store_true", dest="orchestrate")
    orch.add_argument("--no-orchestrate", action="store_false", dest="orchestrate")
    parser.set_defaults(orchestrate=None)

    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()

    if not AGENT_NAME_RE.match(args.name):
        parser.error(f"name {args.name!r} must match {AGENT_NAME_RE.pattern}")
    if args.plan:
        try:
            with open(args.plan, encoding="utf-8") as fh:
                fh.read(1)
        except OSError as exc:
            parser.error(f"cannot read --plan: {exc}")
    context = run_context(args, parser)
    if args.resume and context is None:
        parser.error("--resume requires --run/--node/--attempt")
    if args.delivered and not args.resume:
        parser.error("--delivered requires --resume")
    holder, epoch = validate_auth_args(args, parser, context)
    identity_path = sidecar_path("dispatch-identity", context, parser, require_absent=not args.resume)
    launch_intent_path_ = sidecar_path("launch-intent", context, parser, require_absent=not args.resume)
    complete_path = sidecar_path("launch-complete", context, parser)
    marker_path = complete_path.with_name("prompt-attempt.json") if complete_path is not None else None
    plan = load_contextual_spawn(context, parser) if context else None

    orchestrate = args.kind == "omp" if args.orchestrate is None else args.orchestrate

    prompt = args.prompt
    if args.prompt_file is not None:
        try:
            with open(args.prompt_file, encoding="utf-8") as fh:
                prompt = fh.read()
        except OSError as exc:
            parser.error(f"cannot read --prompt-file: {exc}")
    if plan is not None:
        if args.cwd is not None:
            cwd_abs = str(Path(args.cwd).expanduser().resolve(strict=False))
            if cwd_abs != plan["repo"] and not ioutil.is_worktree_of(cwd_abs, plan["repo"]):
                parser.error(f"--cwd must resolve to accepted run repo {plan['repo']} or one of its worktrees")
            args.cwd = plan["repo"] if (plan["writer"] or plan["integration"]) else cwd_abs
        else:
            args.cwd = plan["repo"]
        run, node, attempt = context
        try:
            prompt = artifacts.prepare_dispatch_prompt(run, node, attempt, prompt or "")
        except (ioutil.UsageError, ioutil.PreconditionError) as exc:
            parser.error(str(exc))
        if plan["writer"] or plan["integration"]:
            if args.base is not None and args.base != plan["base_sha"]:
                parser.error(f"--base must equal graph.base_sha {plan['base_sha']}")
            args.base = plan["base_sha"]
        if plan["required_branch"]:
            if args.branch is not None and args.branch != plan["required_branch"]:
                parser.error(f"--branch must equal {plan['required_branch']}")
            args.branch = plan["required_branch"]
        if plan["integration"] and args.path and Path(args.path).exists():
            parser.error(f"refusing to reuse worktree: {args.path}")
    if prompt:
        prompt = prepare_prompt(prompt, orchestrate=orchestrate)

    spec = args.model or (resolve_role(args.model_role) if args.model_role else None)
    model, thinking = split_spec(spec) if spec else ("", "")
    if spec:
        provider, sep, leaf = model.partition("/")
        if not (sep and provider and leaf):
            origin = (
                f"--model {args.model!r}" if args.model
                else f"modelRoles[{args.model_role!r}] = {spec!r}"
            )
            parser.error(
                f"{origin} must be provider/model[:thinking]; "
                "bare aliases like 'slow'/'smol' are not models"
            )

    use_worktree = bool(args.branch or args.path or args.base)

    try:
        if context is not None:
            run_path, node_id, attempt_id = context
            guard = authz.dispatch_guard(
                run_path, node_id, int(attempt_id), holder=holder, epoch=epoch, continuation=False,
                lock_timeout=args.lock_timeout,
            )
        else:
            guard = contextlib.nullcontext()
        with guard:
            identity_exists = identity_path is not None and identity_path.exists()
            intent_exists = launch_intent_path_ is not None and launch_intent_path_.exists()
            complete_exists = complete_path is not None and complete_path.exists()
            resumed = bool(args.resume)
            if args.delivered and not identity_exists:
                parser.error("--delivered requires an existing dispatch-identity.json")
            if resumed and complete_exists:
                return resume_from_launch_record(identity_path, complete_path, args, plan)
            if resumed and identity_exists:
                return resume_from_dispatch_identity(identity_path, marker_path, complete_path, args, context, plan, prompt)
            if resumed and intent_exists:
                worker_id = resume_from_launch_intent(launch_intent_path_, run_path, node_id, attempt_id)
            else:
                if identity_exists:
                    parser.error(f"dispatch identity already exists: {identity_path}")
                if intent_exists:
                    parser.error(f"launch intent already exists: {launch_intent_path_}")
                worker_id = None
                if context is not None:
                    worker_id = ioutil.new_worker_id()
                    write_once(launch_intent_path_, {
                        "identity_kind": graph_model.IDENTITY_MANAGED_NAME,
                        "worker_id": worker_id,
                        "run": str(run_path),
                        "run_id": plan["run_id"],
                        "node": node_id,
                        "attempt": attempt_id,
                        "kind": args.kind,
                        "model": args.model,
                        "model_role": args.model_role,
                        "requested_effort": args.requested_effort,
                        "branch": args.branch,
                        "base": args.base,
                        "path": args.path,
                        "worktree": use_worktree,
                        "plan": args.plan,
                    })
            agent_target = worker_id or args.name
            title = human_label(args.name, node_id, attempt_id) if context is not None else args.name
            # worker_id stays in the workspace label until dispatch-identity.json exists.
            initial_label = f"{title} · {worker_id}" if worker_id else title
            create = build_create_argv(args, initial_label, use_worktree)
            created = herdr(*create)["result"]
            ws_id, pane = created_ids(created)
            display_rename("pane", pane, title)
            created_worktree_path = created_branch = created_base = None
            started = False
            start_payload: dict[str, Any] | None = None
            agent_get_payload: dict[str, Any] | None = None
            agent = None
            rollback_worktree = use_worktree and not (plan and plan["integration"])
            creation_cwd = create[create.index("--cwd") + 1] if use_worktree and "--cwd" in create else None
            protect_source = False
            start_failed = False

            try:
                created_worktree_path, created_branch, created_base = worktree_details(
                    created, args.path, args.branch, args.base, use_worktree, require_complete=context is not None
                )
                if plan and use_worktree and (
                    _same_checkout(created_worktree_path, plan["repo"])
                    or _same_checkout(created_worktree_path, creation_cwd)
                ):
                    protect_source = True
                if plan and plan["integration"]:
                    if created_worktree_path in plan["forbidden_worktree_paths"] or protect_source:
                        raise RuntimeError("integration worktree must be fresh and differ from the accepted repository")
                    rollback_worktree = True
                elif protect_source:
                    raise RuntimeError("contextual worktree must not be the accepted repository or its creation source")
                if plan and plan["writer"] and created_base != plan["base_sha"]:
                    raise RuntimeError(f"contextual worktree base must be {plan['base_sha']}")
                if plan and plan["required_branch"] and created_branch != plan["required_branch"]:
                    raise RuntimeError(f"contextual worktree branch must be {plan['required_branch']}")

                start = ["agent", "start", agent_target, "--kind", args.kind, "--pane", pane]
                if model:
                    start += ["--", "--model", model]
                    if thinking:
                        start += ["--thinking", thinking]
                not_ready = False
                for attempt in range(args.retries):
                    try:
                        start_payload = herdr(*start)
                        started = True
                        break
                    except RuntimeError as exc:
                        code = getattr(exc, "code", None)
                        if code == "agent_not_ready":
                            started = True
                            not_ready = True
                            break
                        if code != "agent_pane_busy" or attempt == args.retries - 1:
                            start_failed = True
                            raise
                        time.sleep(min(0.25 * (2 ** attempt), args.retry_delay))

                try:
                    agent_get_payload = herdr("agent", "get", agent_target)
                except RuntimeError as exc:
                    # routing metadata is best-effort: `agent get` can lose a race with a
                    # just-started agent. routing_for() falls back to start_payload.
                    print(f"warn: agent get failed for {agent_target}: {exc}", file=sys.stderr)

                routing = routing_for(
                    [start_payload, agent_get_payload],
                    requested_model=args.model,
                    requested_role=args.model_role,
                    requested_effort=args.requested_effort,
                    explicit_model=model,
                    explicit_thinking=thinking,
                )
                identity = dispatch_identity(
                    context, plan, worker_id, ws_id, pane, created_branch, created_base,
                    use_worktree, created_worktree_path, routing=routing,
                )
                if context:
                    write_once(identity_path, identity)
                    returned_name = stable_agent(agent_get_payload, name=True) or stable_agent(start_payload, name=True)
                    if returned_name != worker_id:
                        raise RuntimeError(f"agent get/start response name {returned_name!r} does not match worker_id {worker_id!r}")
                    agent = None
                else:
                    agent = stable_agent(agent_get_payload) or stable_agent(start_payload)
                    if not agent:
                        returned_name = stable_agent(agent_get_payload, name=True) or stable_agent(start_payload, name=True)
                        if returned_name == agent_target:
                            agent = agent_target
                    if agent:
                        identity["agent"] = agent
                    write_once(identity_path, identity)
                    if not agent:
                        raise RuntimeError("start/get response missing stable agent id")

                prompted, blocked, tokens = deliver_after_start(
                    ws_id=ws_id,
                    pane=pane,
                    agent_target=agent_target,
                    role=args.name,
                    plan_path=args.plan,
                    context=context,
                    plan=plan,
                    prompt=prompt,
                    not_ready=not_ready,
                    marker_path=marker_path,
                )
            except Exception as exc:
                if started:
                    raise follow_up_error(exc, agent_target, ws_id, pane, args) from exc
                if start_failed and context is not None:
                    write_once(identity_path, dispatch_identity(
                        context, plan, worker_id, ws_id, pane, created_branch, created_base,
                        use_worktree, created_worktree_path,
                    ))
                    raise
                if protect_source:
                    raise RuntimeError(f"{exc} (rollback skipped: refusing to remove protected creation source)") from exc
                notes: list[str] = []
                for cmd in rollback_commands(ws_id, worktree=rollback_worktree):
                    try:
                        herdr(*cmd)
                        notes.append("rolled back: " + " ".join(cmd))
                        break
                    except Exception as close_exc:
                        notes.append(f"{' '.join(cmd)} failed: {close_exc}")
                if rollback_worktree and not any(n.startswith("rolled back:") for n in notes):
                    try:
                        herdr("workspace", "close", ws_id)
                        notes.append(f"rolled back: closed {ws_id}")
                    except Exception as close_exc:
                        notes.append(f"ROLLBACK FAILED for {ws_id}: {close_exc}")
                raise RuntimeError(f"{exc} ({'; '.join(notes) or 'ROLLBACK FAILED'})") from exc

            payload = record_and_payload(
                complete_path=complete_path,
                worker_id=worker_id,
                ws_id=ws_id,
                pane=pane,
                prompted=prompted,
                blocked=blocked,
                tokens=tokens,
                agent_target=agent_target,
                routing=routing,
                use_worktree=use_worktree,
                worktree_path=created_worktree_path,
                branch=created_branch,
                base=created_base,
                context=context,
                alias=args.name,
                repo=plan["repo"] if plan else None,
                agent=agent,
                resumed=resumed,
            )
            print(json.dumps(payload))
            return 0
    except ioutil.UsageError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
