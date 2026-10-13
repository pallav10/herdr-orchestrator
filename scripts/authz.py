"""Holder-authorized dispatch scope across authorization and external launch."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from typing import Iterator
import contextlib
import ioutil
from ioutil import PreconditionError, canonical_path
from graph_model import HOLDER_KEYS, NODE_ID_RE, _attempt_int, _eligible_blocked_continuation, has_current_dispatch_intent, require_active_holder
from ledger import load_graph, replay, with_lock


@contextlib.contextmanager
def dispatch_guard(run_dir: str | Path, node_id: str, attempt: int, *, holder: dict[str, str] | None, epoch: int, continuation: bool = False, lock_timeout=120) -> Iterator[dict[str, Any]]:
    run_dir = Path(canonical_path(run_dir))
    if not isinstance(node_id, str) or not NODE_ID_RE.fullmatch(node_id):
        raise PreconditionError(f"invalid node id {node_id!r}")
    attempt = _attempt_int(attempt, "attempt")
    if attempt < 1:
        raise PreconditionError("attempt must be >= 1")
    if isinstance(epoch, bool):
        raise PreconditionError("epoch must be an integer")
    try:
        epoch_n = int(epoch)
    except (TypeError, ValueError) as exc:
        raise PreconditionError("epoch must be an integer") from exc

    with with_lock(run_dir, lock_timeout=lock_timeout, node=node_id):
        snap = replay(run_dir)
        if snap.get("truncated_tail"):
            raise PreconditionError("events.jsonl has a truncated tail; repair before appending")
        controller = snap["controller"]
        state = snap["state"]
        require_active_holder(controller, holder, "dispatch_guard")
        if int(controller.get("epoch", 0)) != epoch_n:
            raise PreconditionError("dispatch_guard epoch does not match controller")
        if not controller.get("dispatch") or not state.get("dispatch"):
            raise PreconditionError("dispatch is disabled")
        graph = load_graph(run_dir)
        if node_id not in graph.get("nodes", {}):
            raise PreconditionError(f"node {node_id!r} is absent from the current graph")
        node = (state.get("nodes") or {}).get(node_id)
        if not isinstance(node, dict):
            raise PreconditionError(f"unknown node {node_id!r}")
        if int(node.get("attempt", 0) or 0) != attempt:
            raise PreconditionError(f"dispatch_guard attempt {attempt} does not match current attempt {node.get('attempt', 0)}")
        if continuation:
            if not _eligible_blocked_continuation(node):
                raise PreconditionError("dispatch_guard continuation requires eligible blocked Herdr attempt")
        elif node.get("state") != "ready" or not has_current_dispatch_intent(node):
            raise PreconditionError("dispatch_guard requires ready node with current dispatch_intent")
        ioutil._DISPATCH_GUARD_DEPTH += 1
        try:
            yield snap
        finally:
            ioutil._DISPATCH_GUARD_DEPTH -= 1
