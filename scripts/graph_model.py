"""Graph schema and shared pure validation primitives."""

from __future__ import annotations

from typing import Any
import re
from ioutil import PreconditionError, UsageError
from ownership import owns_overlap

SCHEMA_VERSION = 1
EXECUTORS = frozenset({"task", "workflow", "herdr-agent", "pane"})
STATES = frozenset({"planned", "ready", "running", "blocked", "succeeded", "failed", "interrupted", "skipped", "invalidated"})
OUTPUT_TYPES = frozenset({"summary", "vcs", "integration"})
RETRY_ON = frozenset({"failed", "interrupted", "invalidated"})
ATTEMPT_TERMINAL = frozenset({"succeeded", "failed", "interrupted", "skipped", "invalidated"})
SETTLED = frozenset({"idle", "done", "blocked"})
ACTIVE_STATES = frozenset({"running", "blocked"})
NATIVE_EXECUTORS = frozenset({"task", "workflow"})
HOLDER_KEYS = ("workspace", "pane", "session")
PROVENANCE_ORCHESTRATOR = "orchestrator"
LEASE_ACTIVE = "active"
LEASE_DRAINING = "draining"
MODEL_KEYS = ("requested_role", "requested_effort", "resolved", "thinking", "effort")
HERDR_EXECUTORS = frozenset({"herdr-agent", "pane"})
RUNTIME_KEYS = ("state", "attempt", "artifacts", "dispatch_intent", "consumed_artifacts", "reason", "question", "promotion", "promotion_intent", "promoted", "old_hash", "new_hash", "producer", "output")
REQUEST_KEYS = ("requested_role", "requested_effort")
NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
IDENTITY_MANAGED_NAME = "managed-name"
IDENTITY_RUNTIME_ID = "runtime-id"
WORKER_ID_RE = re.compile(r"^fo-[0-9a-f]{24}$")
_DRAIN_ALLOWED_EVENTS = frozenset({"blocked", "failed", "interrupted", "invalidated", "notification_sent", "promoted", "promotion_intent", "skipped", "succeeded"})


def declares_output(outputs: list[dict[str, Any]], output_type: str, *, match_name: bool = False, require_name: bool = False) -> bool:
    return any(
        isinstance(out, dict)
        and (out.get("type") == output_type or (match_name and out.get("name") == output_type))
        and (not require_name or (isinstance(out.get("name"), str) and bool(out["name"])))
        for out in outputs
    )




def _holder_payload(holder) -> dict[str, Any]:
    if not isinstance(holder, dict):
        raise UsageError("holder must be an object")
    out = {}
    for key in HOLDER_KEYS:
        value = holder.get(key)
        if not isinstance(value, str) or not value:
            raise UsageError(f"holder.{key} must be a non-empty string")
        out[key] = value
    return out


def _as_list(value: object, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PreconditionError(f"{name} must be a list")
    return value


def _node_outputs(nodes: dict[str, Any], node_id: str) -> dict[str, dict[str, Any]]:
    return {out["name"]: out for out in _as_list(nodes[node_id].get("outputs"), f"{node_id}.outputs") if isinstance(out, dict) and "name" in out}




def _effective_owns(nodes: dict[str, Any], node_id: str, memo: dict[str, list[str]]) -> list[str]:
    """Integrator `**` owns the union of its transitive dependencies' owns plus declared extras."""
    if node_id in memo:
        return memo[node_id]
    node = nodes[node_id]
    owns = list(_as_list(node.get("owns"), f"{node_id}.owns"))
    integrates = declares_output(_as_list(node.get("outputs"), f"{node_id}.outputs"), "integration")
    if integrates and "**" in owns:
        closure: list[str] = []
        pending = list(_as_list(node.get("depends_on"), f"{node_id}.depends_on"))
        while pending:
            dep = pending.pop(0)
            if dep in closure or dep not in nodes:
                continue
            closure.append(dep)
            pending.extend(_as_list(nodes[dep].get("depends_on"), f"{dep}.depends_on"))
        merged = [item for dep in closure for item in _effective_owns(nodes, dep, memo)] + [item for item in owns if item != "**"]
        owns = list(dict.fromkeys(merged))
    memo[node_id] = owns
    return owns


def validate_graph(graph: dict[str, Any]) -> None:
    if not isinstance(graph, dict):
        raise PreconditionError("graph must be an object")
    if graph.get("schema_version") != SCHEMA_VERSION:
        raise PreconditionError("graph.schema_version must be 1")
    nodes = graph.get("nodes")
    if not isinstance(nodes, dict):
        raise PreconditionError("graph.nodes must be an object")

    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
            raise PreconditionError(f"invalid node id {node_id!r}")
        if not isinstance(node, dict):
            raise PreconditionError(f"node {node_id} must be an object")
        if node.get("id") != node_id:
            raise PreconditionError(f"node key {node_id!r} must match node.id")

        if node.get("executor") not in EXECUTORS:
            raise PreconditionError(f"node {node_id} executor must be one of {sorted(EXECUTORS)}")
        deps = _as_list(node.get("depends_on"), f"{node_id}.depends_on")
        for dep in deps:
            if dep not in nodes:
                raise PreconditionError(f"node {node_id} depends on unknown node {dep}")
        if node.get("ready_when", "all_succeeded") != "all_succeeded":
            raise PreconditionError(f"node {node_id} ready_when must be all_succeeded")
        retry = node.get("retry")
        if retry is not None:
            if not isinstance(retry, dict):
                raise PreconditionError(f"node {node_id}.retry must be an object")
            if int(retry.get("max_attempts", 0)) < 1:
                raise PreconditionError(f"node {node_id}.retry.max_attempts must be >= 1")
            if not set(_as_list(retry.get("on"), f"{node_id}.retry.on")).issubset(RETRY_ON):
                raise PreconditionError(f"node {node_id}.retry.on contains unsupported states")
            if "side_effect_safe" in retry and not isinstance(retry["side_effect_safe"], bool):
                raise PreconditionError(f"node {node_id}.retry.side_effect_safe must be bool")
        seen_outputs: set[str] = set()
        has_vcs = False
        for out in _as_list(node.get("outputs"), f"{node_id}.outputs"):
            if not isinstance(out, dict) or not isinstance(out.get("name"), str) or not isinstance(out.get("type"), str):
                raise PreconditionError(f"node {node_id}.outputs entries need name and type")
            if out["type"] not in OUTPUT_TYPES:
                raise PreconditionError(f"node {node_id} output {out['name']} has unsupported type")
            if out["name"] in seen_outputs:
                raise PreconditionError(f"node {node_id} output names must be unique")
            seen_outputs.add(out["name"])
            has_vcs = has_vcs or out["type"] == "vcs"
        if node.get("worktree") is not None and not has_vcs:
            raise PreconditionError(f"node {node_id} worktree nodes must declare a vcs output")
        integrates = declares_output(_as_list(node.get("outputs"), f"{node_id}.outputs"), "integration")
        if integrates and isinstance(node.get("worktree"), dict) and node["worktree"].get("branch") is not None:
            raise PreconditionError(f"node {node_id} integration worktree.branch is derived per attempt (<run>/<node>/<attempt>); omit it")
        seen_aliases: set[str] = set()
        for inp in _as_list(node.get("inputs"), f"{node_id}.inputs"):
            if not isinstance(inp, dict):
                raise PreconditionError(f"node {node_id}.inputs entries must be objects")
            if not all(isinstance(inp.get(k), str) and inp[k] for k in ("alias", "from", "output")):
                raise PreconditionError(f"node {node_id}.inputs need alias, from, output")
            if inp["alias"] in seen_aliases:
                raise PreconditionError(f"node {node_id} input aliases must be unique")
            seen_aliases.add(inp["alias"])
            if inp["from"] not in deps:
                raise PreconditionError(f"node {node_id} input {inp['alias']} must come from depends_on")
            if inp["output"] not in _node_outputs(nodes, inp["from"]):
                raise PreconditionError(f"node {node_id} input {inp['alias']} references missing output")
            if "required" in inp and not isinstance(inp["required"], bool):
                raise PreconditionError(f"node {node_id} input {inp['alias']}.required must be bool")

        if node.get("executor") == "pane":
            handle = node.get("handle")
            if not isinstance(handle, dict) or not isinstance(handle.get("name"), str) or not handle["name"]:
                raise PreconditionError(f"pane node {node_id} missing a stored handle name")
        owns = _as_list(node.get("owns"), f"{node_id}.owns")
        if any(not isinstance(item, str) or not item for item in owns):
            raise PreconditionError(f"node {node_id}.owns entries must be non-empty strings")

    temporary: set[str] = set()
    permanent: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in permanent:
            return
        if node_id in temporary:
            raise PreconditionError(f"cycle detected at {node_id}")
        temporary.add(node_id)
        for dep in nodes[node_id].get("depends_on", []) or []:
            visit(dep)
        temporary.remove(node_id)
        permanent.add(node_id)

    for node_id in nodes:
        visit(node_id)

    dependencies: dict[str, set[str]] = {}

    def transitive_dependencies(node_id: str) -> set[str]:
        if node_id not in dependencies:
            closure: set[str] = set()
            for dep in nodes[node_id].get("depends_on", []) or []:
                closure.add(dep)
                closure.update(transitive_dependencies(dep))
            dependencies[node_id] = closure
        return dependencies[node_id]

    for node_id in nodes:
        transitive_dependencies(node_id)

    ids = list(nodes)
    memo: dict[str, list[str]] = {}
    owns = {node_id: _effective_owns(nodes, node_id, memo) for node_id in ids}
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if left in dependencies[right] or right in dependencies[left]:
                continue
            for a in owns[left]:
                for b in owns[right]:
                    if owns_overlap(a, b):
                        raise PreconditionError(f"owns overlap without serialization: {left} {a!r} and {right} {b!r}")


def _eligible_blocked_continuation(node: dict[str, Any]) -> bool:
    if node.get("state") != "blocked" or node.get("executor") not in HERDR_EXECUTORS:
        return False
    if str(node.get("reason", "")).startswith(("dep ", "input ")):
        return False
    handle = node.get("handle") if isinstance(node.get("handle"), dict) else {}
    if parse_herdr_identity(handle, node.get("executor"), require_context=False) is None:
        return False
    return has_current_dispatch_intent(node)


def holder_exact(controller: dict[str, Any], holder) -> bool:
    return isinstance(holder, dict) and all(controller.get(key) is not None and holder.get(key) == controller.get(key) for key in HOLDER_KEYS)


def require_active_holder(controller: dict[str, Any], holder, operation: str) -> None:
    """Raise PreconditionError unless holder is the exact holder of the run's active lease."""
    if controller.get("lease") != LEASE_ACTIVE or not holder_exact(controller, holder):
        raise PreconditionError(f"{operation} requires the exact holder of the run's active lease")


def _event_matches_controller(event: dict[str, Any], controller: dict[str, Any]) -> bool:
    return all(event.get(key) in (None, controller.get(key)) for key in HOLDER_KEYS)


def _check_holder(controller: dict[str, Any], event: dict[str, Any], holder) -> None:
    typ = event.get("type")
    lease = controller.get("lease")
    bound = any(controller.get(key) is not None for key in HOLDER_KEYS)
    if typ == "session_run_bound":
        if bound:
            raise PreconditionError("run is already bound")
        if not isinstance(holder, dict) or any(holder.get(key) != event.get(key) for key in HOLDER_KEYS):
            raise PreconditionError("session_run_bound requires matching holder")
        return
    if typ == "lease_drain_started":
        if lease != LEASE_ACTIVE or not bound or not holder_exact(controller, holder) or not _event_matches_controller(event, controller):
            raise PreconditionError("lease_drain_started requires active current holder")
    elif typ == "lease_transfer":
        if lease != LEASE_DRAINING or not all(event.get(key) is not None for key in HOLDER_KEYS) or not holder_exact(controller, holder):
            raise PreconditionError("lease_transfer requires draining current holder")
    elif lease == LEASE_DRAINING:
        if typ not in _DRAIN_ALLOWED_EVENTS or not holder_exact(controller, holder):
            raise PreconditionError("draining holder may only finish current attempts")
    elif bound:
        if not holder_exact(controller, holder):
            raise PreconditionError("mutating event requires active current holder")
    else:
        raise PreconditionError("mutating event requires bound current holder")


def artifact_name(artifact: dict[str, Any]) -> str | None:
    name = artifact.get("name")
    if isinstance(name, str):
        return name
    output = artifact.get("output")
    if isinstance(output, str):
        return output[:-5] if output.endswith(".json") else output
    return None


def _attempt_int(value, name: str) -> int:
    if isinstance(value, bool):
        raise PreconditionError(f"{name} must be an integer")
    try:
        attempt = int(value)
    except (TypeError, ValueError) as exc:
        raise PreconditionError(f"{name} must be an integer") from exc
    if attempt < 0:
        raise PreconditionError(f"{name} must be >= 0")
    return attempt


def _required_attempt(event: dict[str, Any], node: dict[str, Any]) -> int:
    if "attempt" not in event:
        raise PreconditionError(f"{event.get('type')} requires attempt")
    attempt = _attempt_int(event["attempt"], "attempt")
    if attempt != int(node.get("attempt", 0)):
        raise PreconditionError(f"{event.get('type')} attempt {attempt} does not match current attempt {node.get('attempt', 0)}")
    return attempt


def output_specs(graph: dict[str, Any], node_id: str) -> dict[str, str]:
    return {out["name"]: out["type"] for out in _as_list(graph.get("nodes", {}).get(node_id, {}).get("outputs"), f"{node_id}.outputs") if isinstance(out, dict) and "name" in out and "type" in out}


def validate_model(model: object) -> dict[str, str | None]:
    if not isinstance(model, dict):
        raise PreconditionError("dispatch model must be an object")
    missing = [key for key in MODEL_KEYS if key not in model]
    if missing:
        raise PreconditionError(f"dispatch model missing {missing[0]}")
    out = {}
    for key in MODEL_KEYS:
        value = model[key]
        if value is not None and (not isinstance(value, str) or not value):
            raise PreconditionError(f"dispatch model {key} must be null or a non-empty string")
        out[key] = value
    requested = model.get("requested_model", None)
    if requested is not None:
        if not isinstance(requested, str) or not requested:
            raise PreconditionError("dispatch model requested_model must be null or a non-empty string")
        out["requested_model"] = requested
    return out


def validate_continuation_routing(recorded: object, observed: object) -> dict[str, str | None]:
    recorded_model = validate_model(recorded)
    observed_model = validate_model(observed)
    for key in ("resolved", "thinking", "effort"):
        value = observed_model.get(key)
        if value is not None and value != recorded_model.get(key):
            raise PreconditionError("continuation live routing does not match recorded model")
    return observed_model


def _request_fields(model: object) -> dict[str, str | None]:
    if not isinstance(model, dict):
        return {key: None for key in REQUEST_KEYS}
    return {key: model.get(key) for key in REQUEST_KEYS}


def _condition_true(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise PreconditionError(f"unsupported condition {value!r}")


def current_artifact(state: dict[str, Any], node_id: str, output_name: str) -> dict[str, Any] | None:
    node = state.get("nodes", {}).get(node_id, {})
    if node.get("state") != "succeeded":
        return None
    for art in reversed(node.get("artifacts", []) or []):
        if artifact_name(art) == output_name and art.get("path") and art.get("hash"):
            return art
    return None


def has_current_dispatch_intent(node: dict[str, Any]) -> bool:
    intent = node.get("dispatch_intent") if isinstance(node.get("dispatch_intent"), dict) else {}
    attempt = int(node.get("attempt", 0) or 0)
    return attempt >= 1 and int(intent.get("attempt", -1)) == attempt


def _herdr_token(record: dict[str, Any], name: str) -> str | None:
    tokens = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
    value = tokens.get(name, record.get(name))
    return None if value is None else str(value)


def parse_herdr_identity(record: dict[str, Any], executor: str, *, require_context: bool = True) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    workspace, pane = record.get("workspace"), record.get("pane")
    if not (isinstance(workspace, str) and workspace and isinstance(pane, str) and pane):
        return None
    discriminator = record.get("identity_kind")
    if discriminator is None or discriminator == IDENTITY_RUNTIME_ID:
        agent = record.get("agent")
        if not (isinstance(agent, str) and agent):
            return None
        identity_kind, target = IDENTITY_RUNTIME_ID, agent
    elif discriminator == IDENTITY_MANAGED_NAME:
        if executor == "pane":
            return None
        if record.get("agent"):
            return None
        worker_id = record.get("worker_id")
        if not (isinstance(worker_id, str) and WORKER_ID_RE.fullmatch(worker_id)):
            return None
        identity_kind, target = IDENTITY_MANAGED_NAME, worker_id
    else:
        return None
    tokens = {}
    for key in ("provenance", "run", "node", "attempt"):
        value = _herdr_token(record, key)
        if value is not None:
            tokens[key] = value
    if require_context:
        if not all(tokens.get(key) for key in ("run", "node", "attempt")):
            return None
        if executor == "herdr-agent" and tokens.get("provenance") != PROVENANCE_ORCHESTRATOR:
            return None
    return {"identity_kind": identity_kind, "target": target, "workspace": workspace, "pane": pane, "tokens": tokens}


def herdr_identity_matches(recorded: dict[str, Any], live: dict[str, Any], executor: str) -> bool:
    left = parse_herdr_identity(recorded, executor, require_context=True)
    right = parse_herdr_identity(live, executor, require_context=True)
    if left is None or right is None:
        return False
    if left["identity_kind"] != right["identity_kind"] or left["target"] != right["target"]:
        return False
    if left["workspace"] != right["workspace"] or left["pane"] != right["pane"]:
        return False
    if any(left["tokens"].get(key) != right["tokens"].get(key) for key in ("run", "node", "attempt")):
        return False
    if executor == "herdr-agent" and left["tokens"].get("provenance") != right["tokens"].get("provenance"):
        return False
    return True


def herdr_command_target(record: dict[str, Any], executor: str) -> str | None:
    parsed = parse_herdr_identity(record, executor, require_context=False)
    return parsed["target"] if parsed else None


def _retry_allows(plan: dict[str, Any], reason: str, attempt: int) -> bool:
    retry = plan.get("retry") or {}
    if plan.get("executor") in NATIVE_EXECUTORS and reason in {"failed", "interrupted"} and retry.get("side_effect_safe") is not True:
        return False
    return reason in set(retry.get("on", [])) and attempt < int(retry.get("max_attempts", 1))










































