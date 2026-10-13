"""Immutable attempt prompt assembly and the shared consumed-artifact reader API."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re
from ioutil import PreconditionError, UsageError, canonical_path, consumed_items
from graph_model import _as_list, _attempt_int
from ledger import attempt_dir, load_graph, read_consumed_snapshot
import ioutil
import ledger
from graph_model import require_active_holder


def overwrite_forced_artifact(run: Path, node: str, attempt: int, path: Path, payload: str, *, holder=None, lock_timeout=120) -> None:
    """Authorize before replacement and audit it while holding the same run lock."""
    with ledger.with_lock(run, lock_timeout=lock_timeout, node=node):
        snap = ledger.replay(run)
        if snap.get("truncated_tail"):
            raise PreconditionError("events.jsonl has truncated tail; recover before appending")
        require_active_holder(snap["controller"], holder, "forced artifact write")
        if node not in snap["state"].get("nodes", {}):
            raise PreconditionError(f"unknown node {node!r}")
        if _attempt_int(attempt, "attempt") < 1:
            raise PreconditionError("artifact_forced requires attempt >= 1")
        # Preserve append's immutable-snapshot refusal before replacing any bytes.
        ledger._repair_dispatch_snapshots_locked(run, snap)
        existed = path.is_file()
        ioutil.atomic_text(path, payload)
        if existed:
            ledger.append_event_locked(
                run, {"type": "artifact_forced", "node": node, "attempt": int(attempt), "forced": [path.name]},
                holder=holder,
            )


def prepare_dispatch_prompt(run_dir: str | Path, node_id: str, attempt: int, prompt: str = '') -> str:
    run_dir = Path(canonical_path(run_dir))
    attempt = _attempt_int(attempt, "attempt")
    if attempt < 1:
        raise PreconditionError("attempt must be >= 1")
    if not isinstance(node_id, str) or not node_id:
        raise UsageError("node_id must be a non-empty string")
    if not isinstance(prompt, str):
        raise UsageError("prompt must be a string")
    graph = load_graph(run_dir)
    node = graph.get("nodes", {}).get(node_id)
    if not isinstance(node, dict):
        raise PreconditionError(f"unknown node {node_id!r}")
    inputs = _as_list(node.get("inputs"), f"{node_id}.inputs")
    known = {item.get("alias") for item in inputs if isinstance(item, dict)}
    by_alias: dict[str, dict[str, Any]] = {}
    for item in read_consumed_snapshot(run_dir, node_id, attempt):
        alias = item["alias"]
        if alias in by_alias:
            raise PreconditionError(f"consumed artifacts duplicate alias {alias}")
        if alias not in known:
            raise PreconditionError(f"consumed artifacts extra alias {alias}")
        path = item["path"]
        digest = item["hash"]
        producer_attempt = item["producer_attempt"]
        if not Path(path).is_absolute():
            raise PreconditionError("consumed artifacts path must be absolute")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise PreconditionError("consumed artifacts hash must be sha256")
        if isinstance(producer_attempt, bool) or not isinstance(producer_attempt, int) or producer_attempt < 1:
            raise PreconditionError("consumed artifacts producer_attempt must be >= 1")
        by_alias[alias] = item
    ordered: list[dict[str, Any]] = []
    for inp in inputs:
        alias = inp["alias"]
        item = by_alias.get(alias)
        if item is None:
            if inp.get("required", True):
                raise PreconditionError(f"consumed artifacts missing input {alias}")
            continue
        if item["producer"] != inp["from"] or item["output"] != inp["output"]:
            raise PreconditionError(f"consumed artifacts inconsistent for {alias}")
        ordered.append({key: item[key] for key in ("alias", "producer", "producer_attempt", "output", "path", "hash")})
    payload = {"attempt_dir": str(attempt_dir(run_dir, node_id, attempt)), "consumed": ordered}
    return "# herdr-dispatch\n" + json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n" + prompt
