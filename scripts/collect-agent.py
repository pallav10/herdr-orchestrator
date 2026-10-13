#!/usr/bin/env python3
"""Collect deterministic status/summary snapshots for named Herdr agents.

Usage: collect-agent.py NAME [NAME ...] [--status-only] [--full-summary] [--force] [--run PATH --node ID --attempt N --session S --holder-workspace W --holder-pane P [--publish-vcs]] [--lock-timeout SECONDS]

For each NAME, resolves `herdr agent get NAME` and emits one JSON line:
  {"agents":[{"name","state","summary","source","session_path"}]}

Default mode reads the agent's OMP session transcript (agent_session.value) and
extracts the last assistant text reply. If no session path is available, falls
back to `herdr agent read NAME --source recent-unwrapped --lines 120` and strips
terminal chrome/ANSI escapes.
Stdout summaries are capped at 1,500 characters unless --full-summary is given;
summary_truncated is present only when cut. Contextual summary.json stays full.

--publish-vcs writes vcs.json from the dispatched worktree (form commit, or none
when HEAD == base and clean). --force replaces any receipt whose content
genuinely differs — the re-read summary, and a vcs receipt re-derived from the
current HEAD instead of a stale one — and appends one artifact_forced audit
event per replaced receipt under the run lock. Identical bytes are left alone.

Exit codes: 0 ok, 1 runtime failure, 2 usage, 3 one or more names/artifacts refused.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


import graph_model
import ioutil
import cli_common

CLI_EXTRA_CODES = {RuntimeError: 1}
import ledger
import receipts
from receipts import GitFacts
from ioutil import first_str, load_json
from artifacts import consumed_items, overwrite_forced_artifact
from graph_model import declares_output
from herdr_cli import herdr, herdr_text
from cli_common import agent_pane_id, agent_workspace_id, holder_from_args, run_context, token_value, workspace_pane_id


ANSI_CSI_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
ANSI_OSC_RE = re.compile(r"\x1B\][^\x07]*(?:\x07|\x1B\\\\)")
BOX_LINE_RE = re.compile(r"^[\s\u2500-\u257f]+$")






def context_tokens(context: tuple[Path, str, str]) -> dict[str, str]:
    run, node, attempt = context
    return {"run": token_value(run.name), "node": token_value(node), "attempt": token_value(attempt)}


def attempt_dir_for(context: tuple[Path, str, str]) -> Path:
    run, node, attempt = context
    return ledger.attempt_dir(run, node, int(attempt))


def require_lease(run: Path, holder: dict[str, str]) -> None:
    """Refuse receipt writes unless the caller holds the run's active lease."""
    controller = ledger.replay(run)["controller"]
    graph_model.require_active_holder(controller, holder, "collect-agent receipt write")


def recorded_base(run: Path, identity: dict[str, Any]) -> str | None:
    data = ledger.load_run(run)
    sha = data.get("base_sha")
    if isinstance(sha, str) and sha:
        return sha
    base = identity.get("base")
    if isinstance(base, str) and base:
        return base
    sha = ledger.load_accepted_graph(run).get("base_sha")
    return sha if isinstance(sha, str) and sha else None

def recorded_promotion_ref(run: Path) -> str | None:
    ref = ledger.load_run(run).get("promotion_ref")
    return ref if isinstance(ref, str) and ref else None


def ownership_has_paths(value: object) -> bool:
    if isinstance(value, dict):
        paths = value.get("paths")
        if isinstance(paths, list) and any(isinstance(p, str) and p for p in paths):
            return True
        return any(ownership_has_paths(v) for k, v in value.items() if k != "paths")
    if isinstance(value, list):
        return any(ownership_has_paths(v) for v in value)
    return False


def identity_is_writer(identity: object) -> bool:
    return isinstance(identity, dict) and bool(
        identity.get("worktree") or identity.get("worktree_path") or identity.get("branch")
    )


def node_executor(run: Path, node: str) -> str:
    plan = ledger.load_accepted_node(run, node)
    executor = plan.get("executor")
    if not isinstance(executor, str) or not executor:
        raise ioutil.PreconditionError(f"node {node} declares no executor in graph.json")
    return executor


def load_dispatch_identity(attempt_dir: Path, executor: str) -> dict[str, Any]:
    path = attempt_dir / "dispatch-identity.json"
    if not path.is_file():
        raise ioutil.PreconditionError("missing dispatch-identity.json")
    data = load_json(path)
    if not isinstance(data, dict):
        raise ioutil.PreconditionError("dispatch-identity.json must be an object")
    for key in ("workspace", "pane"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ioutil.PreconditionError(f"dispatch identity missing {key}")
    if not isinstance(data.get("tokens"), dict):
        raise ioutil.PreconditionError("dispatch identity missing tokens")
    if graph_model.parse_herdr_identity(data, executor, require_context=True) is None:
        raise ioutil.PreconditionError("dispatch identity has an invalid or mixed identity discriminator")
    return data

def require_writer_identity(identity: dict[str, Any]) -> None:
    for key in ("repo", "worktree_path", "branch", "base"):
        if not isinstance(identity.get(key), str) or not identity[key]:
            raise ioutil.PreconditionError(f"dispatch identity missing writer {key}")


def verify_dispatch_identity(
    agent: dict[str, Any],
    identity: dict[str, Any],
    context: tuple[Path, str, str],
    executor: str,
) -> None:
    run, node, attempt = context
    for key, value in (("run", str(run)), ("node", node), ("attempt", attempt), ("run_id", run.name)):
        if key in identity and identity[key] != value:
            raise ioutil.PreconditionError(f"dispatch identity {key} mismatch")
    expected_tokens = context_tokens(context)
    identity_tokens = identity["tokens"]
    for key, value in expected_tokens.items():
        if identity_tokens.get(key) != value:
            raise ioutil.PreconditionError(f"dispatch identity {key} token mismatch")

    recorded = graph_model.parse_herdr_identity(identity, executor, require_context=True)
    if recorded is None:
        raise ioutil.PreconditionError("dispatch identity has an invalid or mixed identity discriminator")

    workspace = agent_workspace_id(agent)
    if workspace != identity["workspace"]:
        raise ioutil.PreconditionError("agent workspace does not match dispatch identity")
    try:
        workspace_obj = herdr("workspace", "get", workspace)["result"]["workspace"]
    except ioutil.RunError as exc:
        raise ioutil.PreconditionError(f"workspace metadata unavailable: {workspace}") from exc
    if not isinstance(workspace_obj, dict):
        raise RuntimeError(f"unexpected herdr workspace get payload for {workspace}")
    tokens = workspace_obj.get("tokens")
    if not isinstance(tokens, dict):
        raise ioutil.PreconditionError("workspace tokens missing")
    for key, value in expected_tokens.items():
        if tokens.get(key) != value:
            raise ioutil.PreconditionError(f"workspace {key} token does not match dispatch identity")

    pane = agent_pane_id(agent) or workspace_pane_id(workspace_obj)
    if pane != identity["pane"]:
        raise ioutil.PreconditionError("agent pane does not match dispatch identity")

    live: dict[str, Any] = {"workspace": workspace, "pane": pane, "tokens": tokens}
    if recorded["identity_kind"] == graph_model.IDENTITY_MANAGED_NAME:
        live["identity_kind"] = graph_model.IDENTITY_MANAGED_NAME
        name = agent.get("name")
        if isinstance(name, str) and name:
            live["worker_id"] = name
    else:
        live["identity_kind"] = graph_model.IDENTITY_RUNTIME_ID
        agent_id = first_str(agent, ("agent_id", "id"))
        if agent_id:
            live["agent"] = agent_id
    if not graph_model.herdr_identity_matches(identity, live, executor):
        raise ioutil.PreconditionError("agent id does not match dispatch identity")


def validate_vcs(vcs_path: Path, identity: object, attempt_dir: Path, *, require_commit: bool = False, promotion_ref: str | None = None, git_facts: GitFacts | None = None) -> str | None:
    data = load_json(vcs_path)
    if not isinstance(data, dict):
        raise ioutil.PreconditionError("vcs.json must be an object")
    if not isinstance(identity, dict):
        raise ioutil.PreconditionError("dispatch identity missing writer repo")
    expected_repo = identity.get("repo")
    if not isinstance(expected_repo, str) or not expected_repo:
        raise ioutil.PreconditionError("dispatch identity missing writer repo")
    if data.get("repo") != expected_repo:
        raise ioutil.PreconditionError("vcs repo does not match dispatch identity")
    expected_worktree = identity.get("worktree_path")
    if expected_worktree is not None and data.get("worktree") != expected_worktree:
        raise ioutil.PreconditionError("vcs worktree does not match dispatch identity")
    expected_branch = identity.get("branch")
    if expected_branch is not None and data.get("branch") != expected_branch:
        raise ioutil.PreconditionError("vcs branch does not match dispatch identity")
    expected_base = identity.get("base")
    if expected_base is not None and data.get("base_sha") != expected_base:
        raise ioutil.PreconditionError("vcs base_sha does not match dispatch identity")
    return receipts.validate_vcs_payload(vcs_path, attempt_dir=attempt_dir, require_commit=require_commit, promotion_ref=promotion_ref, git_facts=git_facts)


def declared_outputs(run: Path, node: str) -> list[dict[str, Any]]:
    outputs = ledger.load_accepted_node(run, node).get("outputs", [])
    return [out for out in outputs if isinstance(out, dict)]


def declared_output(outputs: list[dict[str, Any]], output_type: str) -> tuple[str, str]:
    for out in outputs:
        name = out.get("name")
        typ = out.get("type")
        if name == output_type or typ == output_type:
            return (name if isinstance(name, str) and name else output_type, typ if isinstance(typ, str) and typ else output_type)
    return output_type, output_type




def artifact_row(outputs: list[dict[str, Any]], output_type: str, path: Path) -> dict[str, str]:
    name, typ = declared_output(outputs, output_type)
    return {"name": name, "output": name, "type": typ, "path": str(path.resolve()), "hash": ioutil.sha256_file(path)}






def _git(worktree: str, *args: str, git_facts: GitFacts | None = None) -> str:
    proc = git_facts.git(worktree, *args) if git_facts is not None else ioutil.run_cmd(["git", "-C", worktree, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ioutil.PreconditionError(f"git {' '.join(args)} failed in {worktree}: {(proc.stderr or proc.stdout).strip()[:200]}")
    return proc.stdout.strip()


def _receipt_differs(path: Path, payload: str) -> bool:
    """True when the receipt must be replaced: absent, unreadable, or different bytes."""
    try:
        return path.read_bytes() != payload.encode("utf-8")
    except OSError:
        return True


def publish_vcs(
    context: tuple[Path, str, str],
    identity: dict[str, Any],
    promotion_ref: str | None = None,
    *,
    force: bool = False,
    holder: dict[str, str] | None = None,
    git_facts: GitFacts | None = None,
    lock_timeout=120,
) -> Path:
    """Write vcs.json from the dispatched worktree's HEAD.

    form is none when HEAD equals the recorded base or is already a promoted sha
    (`git merge-base --is-ancestor HEAD <promotion_ref>`), else commit. An existing
    vcs.json matching the identity with result commit at current HEAD is reused
    before form is inferred, so a commit receipt survives later promotion.
    Refuses a dirty worktree, a HEAD that does not descend from the recorded
    base, or an existing vcs.json with different content (attempt artifacts are
    immutable).

    `force` re-derives the receipt from the current HEAD instead of keeping a
    stale one — typically a form `none` recorded before the worker had committed.
    The promoted-sha downgrade is skipped while forcing: it exists only to keep an
    already-recorded commit receipt stable across promotion, and there is no
    trustworthy recorded receipt to preserve here. A replacement is written and
    audited as `artifact_forced` under the run lock.
    """
    run, node, attempt = context
    attempt_dir = attempt_dir_for(context)
    require_writer_identity(identity)
    worktree = identity["worktree_path"]
    facts = git_facts if git_facts is not None else GitFacts(worktree)
    if _git(worktree, "status", "--porcelain", "--untracked-files=all", git_facts=facts):
        raise ioutil.PreconditionError(f"worktree is dirty; commit or discard before publishing: {worktree}")
    head = _git(worktree, "rev-parse", "HEAD", git_facts=facts)
    base = identity["base"]
    if facts.git(worktree, "merge-base", "--is-ancestor", base, head).returncode != 0:
        raise ioutil.PreconditionError(f"worktree HEAD {head[:12]} does not descend from recorded base {base[:12]}")
    vcs_path = attempt_dir / "vcs.json"
    existing = load_json(vcs_path) if vcs_path.is_file() else None
    if (
        isinstance(existing, dict)
        and existing.get("worktree") == worktree
        and existing.get("repo") == identity["repo"]
        and existing.get("branch") == identity["branch"]
        and existing.get("base_sha") == base
        and existing.get("result") == {"form": "commit", "sha": head}
    ):
        return vcs_path
    on_promoted = not force and bool(promotion_ref) and facts.git(worktree, "merge-base", "--is-ancestor", head, promotion_ref).returncode == 0
    result = {"form": "none"} if head == base or on_promoted else {"form": "commit", "sha": head}
    vcs = {
        "worktree": worktree,
        "repo": identity["repo"],
        "branch": identity["branch"],
        "base_sha": base,
        "result": result,
    }
    payload = json.dumps(vcs, indent=1, sort_keys=True) + "\n"
    if existing is not None:
        if existing == vcs:
            return vcs_path
        if not force:
            raise ioutil.PreconditionError("vcs.json already exists with different content")
        overwrite_forced_artifact(run, node, int(attempt), vcs_path, payload, holder=holder, lock_timeout=lock_timeout)
        return vcs_path
    ioutil.atomic_text(vcs_path, payload)
    return vcs_path

def attach_artifacts(agent: dict[str, Any], context: tuple[Path, str, str], identity: dict[str, Any], force=False, holder: dict[str, str] | None = None, *, git_facts: GitFacts | None = None, lock_timeout=120) -> dict[str, Any]:
    run, node, attempt = context
    attempt_dir = attempt_dir_for(context)
    if not attempt_dir.is_dir():
        raise ioutil.PreconditionError(f"attempt artifact dir missing: {attempt_dir}")

    outputs = declared_outputs(run, node)
    ownership_path = attempt_dir / "ownership.json"
    consumed_path = attempt_dir / "consumed_artifacts.json"
    vcs_path = attempt_dir / "vcs.json"
    integration_path = attempt_dir / "integration.json"
    ownership = load_json(ownership_path) if ownership_path.is_file() else None
    writer = vcs_path.is_file() or identity_is_writer(identity) or ownership_has_paths(ownership)
    needs_integration = declares_output(outputs, "integration", match_name=True) or integration_path.is_file()
    if writer or needs_integration:
        require_writer_identity(identity)
        run_doc = ledger.load_run(run)
        recorded_repo = run_doc.get("repo") if isinstance(run_doc, dict) else None
        if isinstance(recorded_repo, str) and recorded_repo and identity["repo"] != recorded_repo:
            raise ioutil.PreconditionError("dispatch identity repo does not match run repo")
        if not vcs_path.is_file():
            raise ioutil.PreconditionError("missing vcs.json for integration output" if needs_integration else "missing vcs.json for writer")
    extra_artifacts: list[dict[str, str]] = []
    vcs_sha = None
    if vcs_path.is_file():
        vcs_sha = validate_vcs(vcs_path, identity, attempt_dir, require_commit=needs_integration, promotion_ref=recorded_promotion_ref(run), git_facts=git_facts)
        extra_artifacts.append(artifact_row(outputs, "vcs", vcs_path))
    if needs_integration:
        if not integration_path.is_file():
            raise ioutil.PreconditionError("missing integration.json for integration output")
        if not vcs_sha:
            raise ioutil.PreconditionError("integration requires a commit vcs result")
        graph = ledger.load_accepted_graph(run)
        receipts.validate_integration_payload(
            integration_path,
            receipts.vcs_typed_consumed(graph, consumed_items(consumed_path)),
            expected_sha=vcs_sha,
            expected_base=recorded_base(run, identity),
        )
        extra_artifacts.append(artifact_row(outputs, "integration", integration_path))


    summary = {
        "name": agent["name"],
        "status": agent["state"],
        "summary": agent["summary"],
        "question": agent.get("question"),
    }
    summary_path = attempt_dir / "summary.json"
    payload = json.dumps(summary, sort_keys=True) + "\n"
    if force and _receipt_differs(summary_path, payload):
        try:
            overwrite_forced_artifact(run, node, int(attempt), summary_path, payload, holder=holder, lock_timeout=lock_timeout)
        except OSError as exc:
            raise ioutil.PreconditionError(f"cannot write summary.json: {exc}") from exc
    else:
        try:
            with summary_path.open("x", encoding="utf-8") as fh:
                fh.write(payload)
        except FileExistsError:
            try:
                existing = summary_path.read_bytes()
                data = json.loads(existing)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ioutil.PreconditionError("summary.json already exists") from exc
            if data != summary:
                raise ioutil.PreconditionError("summary.json already exists")
        except OSError as exc:
            raise ioutil.PreconditionError(f"cannot write summary.json: {exc}") from exc
    artifacts = [artifact_row(outputs, "summary", summary_path), *extra_artifacts]

    return {**agent, "status": agent["state"], "summary_hash": artifacts[0]["hash"], "artifacts": artifacts}


# Empirically verified from infra session JSONL on 2026-08-11:
# - Each line is a JSON object with top-level `type`.
# - Conversation turns are `type=="message"` with payload in `message`.
# - Assistant turns are `message.role=="assistant"`.
# - `message.content` is a parts array (`text`/`toolCall`/`thinking`/...), and
#   assistant-visible prose is in `{"type":"text","text":...}` parts.
def assistant_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def parse_session_summary(session_path: Path) -> str:
    # Split bytes before decoding so a UTF-8 character crossing a block survives.
    with session_path.open("rb") as fh:
        fh.seek(0, 2)
        position = fh.tell()
        pending = b""
        while position or pending:
            if position:
                size = min(position, 64 * 1024)
                position -= size
                fh.seek(position)
                lines = (fh.read(size) + pending).split(b"\n")
                pending = lines.pop(0) if position else b""
            else:
                lines, pending = [pending], b""
            for line in reversed(lines):
                try:
                    row = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(row, dict):
                    continue
                msg = row.get("message", row)
                if not isinstance(msg, dict) or msg.get("role", row.get("role", row.get("type"))) != "assistant":
                    continue
                text = assistant_text(msg.get("content"))
                if text:
                    return text
    return ""


def trailing_question(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None
    match = re.search(r"([^\n\r?]*\?)\s*$", stripped, flags=re.S)
    if not match:
        return None
    return " ".join(match.group(1).split())


def strip_terminal_chrome(raw: str) -> str:
    text = ANSI_OSC_RE.sub("", ANSI_CSI_RE.sub("", raw))
    kept: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if BOX_LINE_RE.match(s):
            continue
        if s.startswith(("╭──", "╰─", "├──", "$ ", "⟨Wall:")):
            continue
        if s.startswith("│") and s.endswith("│"):
            inner = s.strip("│").strip()
            if not inner or inner.startswith(("$ ", "Output")):
                continue
            kept.append(inner)
            continue
        kept.append(s)
    return "\n".join(kept).strip()


def terminal_summary(name: str) -> str:
    raw = herdr_text("agent", "read", name, "--source", "recent-unwrapped", "--lines", "120")
    cleaned = strip_terminal_chrome(raw)
    if not cleaned:
        return ""
    lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
    return " ".join(lines[-4:]).strip()


def collect_one(name: str, status_only: bool, context: tuple[Path, str, str] | None = None, identity: dict[str, Any] | None = None, executor: str = "herdr-agent") -> dict[str, Any]:
    result = herdr("agent", "get", name)
    agent = result.get("result", {}).get("agent", {})
    if not isinstance(agent, dict):
        raise RuntimeError(f"unexpected herdr agent get payload for {name}")

    state = str(agent.get("agent_status") or "unknown")
    session_info = agent.get("agent_session")
    session_path = session_info.get("value") if isinstance(session_info, dict) else None
    if not isinstance(session_path, str) or not session_path:
        session_path = None

    if context is not None:
        if identity is None:
            raise ioutil.PreconditionError("missing dispatch-identity.json")
        verify_dispatch_identity(agent, identity, context, executor)

    if status_only:
        return {
            "name": name,
            "state": state,
            "summary": "",
            "source": "status",
            "session_path": session_path,
        }

    summary = ""
    source = "terminal"
    resolved_session_path: str | None = None
    if session_path and Path(session_path).is_file():
        summary = parse_session_summary(Path(session_path))
        source = "session"
        resolved_session_path = session_path
    else:
        summary = terminal_summary(name)

    out = {
        "name": name,
        "state": state,
        "summary": summary,
        "source": source,
        "session_path": resolved_session_path,
    }
    if state == "blocked":
        question = trailing_question(summary)
        if question:
            out["question"] = question
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="+")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--full-summary", action="store_true", help="print the complete summary instead of the 1,500-character cap")
    parser.add_argument("--force", action="store_true",
                        help="replace summary.json and vcs.json when the re-collected content differs, auditing each replacement as artifact_forced (attempt artifacts are otherwise immutable)")
    parser.add_argument("--run", default=None)
    parser.add_argument("--node", default=None)
    parser.add_argument("--attempt", type=int, default=None)
    parser.add_argument("--publish-vcs", action="store_true",
                        help="writer attempts: write vcs.json (form=commit, or none when HEAD == base and clean) from the dispatched worktree before collecting")
    parser.add_argument("--session", default=None)
    parser.add_argument("--holder-workspace", default=None)
    parser.add_argument("--holder-pane", default=None)
    cli_common.add_lock_timeout_arg(parser)
    args = parser.parse_args()
    if args.force and args.status_only:
        parser.error("--force cannot be combined with --status-only")
    context = run_context(args, parser)
    if context and len(args.names) != 1:
        parser.error("--run/--node/--attempt collection accepts exactly one NAME")
    holder = holder_from_args(args, parser, context="--run/--node/--attempt" if context is not None else None)
    identity = None
    executor = "herdr-agent"
    if context:
        executor = node_executor(context[0], context[1])
        identity = load_dispatch_identity(attempt_dir_for(context), executor)

    agents: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in args.names:
        try:
            agent = collect_one(name, args.status_only, context, identity, executor)
            if context and not args.status_only:
                assert identity is not None
                require_lease(context[0], holder)
                facts = GitFacts(identity["worktree_path"]) if identity_is_writer(identity) and identity.get("worktree_path") else None
                if args.publish_vcs:
                    publish_vcs(
                        context,
                        identity,
                        promotion_ref=recorded_promotion_ref(context[0]),
                        force=args.force,
                        holder=holder,
                        git_facts=facts,
                    lock_timeout=args.lock_timeout,
                    )
                agent = attach_artifacts(agent, context, identity, force=args.force, holder=holder, git_facts=facts, lock_timeout=args.lock_timeout)
            if not args.full_summary and len(agent.get("summary", "")) > 1500:
                agent = {**agent, "summary": agent["summary"][:1500], "summary_truncated": True}
            agents.append(agent)
        except ioutil.PreconditionError:
            raise
        except ioutil.RunError as exc:
            if getattr(exc, "code", None) == "agent_not_found":
                missing.append(name)
                continue
            raise

    if missing:
        raise ioutil.PreconditionError(f"agent(s) not found: {', '.join(missing)}")


    print(json.dumps({"agents": agents}))
    return 0


if __name__ == "__main__":
    cli_common.run(main, extra_codes=CLI_EXTRA_CODES)
