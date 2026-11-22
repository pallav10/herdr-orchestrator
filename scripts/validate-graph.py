#!/usr/bin/env python3
"""Offline dry-run validator and static linter for herdr-orchestrator task graphs."""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cli_common
import graph_model
import ioutil
import ownership


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


def _adjacency(nodes: dict[str, Any]) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {}
    for node_id in sorted(nodes):
        node = nodes[node_id]
        raw_deps = node.get("depends_on") if isinstance(node, dict) else None
        if isinstance(raw_deps, list):
            seen: set[str] = set()
            deps: list[str] = []
            for dep in raw_deps:
                if isinstance(dep, str) and dep in nodes and dep not in seen:
                    seen.add(dep)
                    deps.append(dep)
            adj[node_id] = deps
        else:
            adj[node_id] = []
    return adj


def tarjan_sccs(adj: dict[str, list[str]]) -> list[list[str]]:
    """Iterative (non-recursive) Tarjan strongly connected components."""
    index_counter = 0
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    tarjan_stack: list[str] = []
    sccs: list[list[str]] = []

    for start in sorted(adj):
        if start in index:
            continue
        call_stack: list[tuple[str, int]] = [(start, 0)]
        while call_stack:
            v, pos = call_stack[-1]
            if pos == 0:
                index[v] = lowlink[v] = index_counter
                index_counter += 1
                tarjan_stack.append(v)
                on_stack.add(v)
            neighbors = adj[v]
            if pos < len(neighbors):
                call_stack[-1] = (v, pos + 1)
                w = neighbors[pos]
                if w not in index:
                    call_stack.append((w, 0))
                elif w in on_stack:
                    if index[w] < lowlink[v]:
                        lowlink[v] = index[w]
            else:
                call_stack.pop()
                if call_stack:
                    parent = call_stack[-1][0]
                    if lowlink[v] < lowlink[parent]:
                        lowlink[parent] = lowlink[v]
                if lowlink[v] == index[v]:
                    comp: list[str] = []
                    while True:
                        w = tarjan_stack.pop()
                        on_stack.remove(w)
                        comp.append(w)
                        if w == v:
                            break
                    sccs.append(comp)
    return sccs


def _canonical_cycle(cycle: list[str]) -> tuple[str, ...]:
    if not cycle:
        return ()
    min_idx = min(range(len(cycle)), key=cycle.__getitem__)
    return tuple(cycle[min_idx:] + cycle[:min_idx])


def _shortest_cycle_from(start: str, comp_set: set[str], adj: dict[str, list[str]]) -> list[str] | None:
    queue: deque[str] = deque()
    parent: dict[str, str] = {}
    for nxt in sorted(adj[start]):
        if nxt not in comp_set:
            continue
        if nxt == start:
            return [start]
        if nxt not in parent:
            parent[nxt] = start
            queue.append(nxt)
    while queue:
        curr = queue.popleft()
        for nxt in sorted(adj[curr]):
            if nxt not in comp_set:
                continue
            if nxt == start:
                path = [curr]
                while path[-1] != start:
                    path.append(parent[path[-1]])
                path.reverse()
                return path
            if nxt not in parent:
                parent[nxt] = curr
                queue.append(nxt)
    return None


def find_minimal_cycles(adj: dict[str, list[str]]) -> list[list[str]]:
    """Return all exact minimal dependency cycles derived from iterative Tarjan SCCs."""
    seen: set[tuple[str, ...]] = set()
    cycles: list[list[str]] = []
    for comp in tarjan_sccs(adj):
        if len(comp) == 1:
            node = comp[0]
            if node in adj[node]:
                key = (node,)
                if key not in seen:
                    seen.add(key)
                    cycles.append([node])
            continue
        comp_set = set(comp)
        covered: set[str] = set()
        for node in sorted(comp):
            if node in covered:
                continue
            cycle = _shortest_cycle_from(node, comp_set, adj)
            if cycle:
                key = _canonical_cycle(cycle)
                if key not in seen:
                    seen.add(key)
                    cycles.append(list(key))
                covered.update(cycle)
    cycles.sort()
    return cycles


def _reachable_deps(adj: dict[str, list[str]]) -> dict[str, set[str]]:
    reachable: dict[str, set[str]] = {}
    for start in adj:
        visited: set[str] = set()
        queue: deque[str] = deque(adj[start])
        while queue:
            dep = queue.popleft()
            if dep in visited:
                continue
            visited.add(dep)
            queue.extend(adj.get(dep, ()))
        reachable[start] = visited
    return reachable


def _effective_owns_iterative(nodes: dict[str, Any], reachable: dict[str, set[str]]) -> dict[str, list[str]]:
    raw_owns: dict[str, list[str]] = {}
    integrates: dict[str, bool] = {}
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            raw_owns[node_id] = []
            integrates[node_id] = False
            continue
        owns = node.get("owns")
        raw_owns[node_id] = [item for item in owns if isinstance(item, str) and item] if isinstance(owns, list) else []
        outputs = node.get("outputs")
        integrates[node_id] = (
            graph_model.declares_output(outputs, "integration")
            if isinstance(outputs, list)
            else False
        )

    effective: dict[str, list[str]] = {}
    for node_id in nodes:
        owns = raw_owns[node_id]
        if integrates[node_id] and "**" in owns:
            merged: list[str] = []
            for dep in sorted(reachable.get(node_id, ())):
                for item in raw_owns.get(dep, ()):
                    if item != "**" or not integrates.get(dep, False):
                        merged.append(item)
            merged.extend(item for item in owns if item != "**")
            effective[node_id] = list(dict.fromkeys(merged))
        else:
            effective[node_id] = list(owns)
    return effective


def find_ownership_overlaps(nodes: dict[str, Any], adj: dict[str, list[str]]) -> list[dict[str, Any]]:
    reachable = _reachable_deps(adj)
    owns = _effective_owns_iterative(nodes, reachable)
    ids = sorted(nodes)
    overlaps: list[dict[str, Any]] = []
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            if left in reachable[right] or right in reachable[left]:
                continue
            for a in owns[left]:
                for b in owns[right]:
                    if ownership.owns_overlap(a, b):
                        overlaps.append({"nodes": [left, right], "patterns": [a, b]})
    return overlaps


def compute_waves(adj: dict[str, list[str]]) -> list[list[str]]:
    in_degree = {node_id: len(deps) for node_id, deps in adj.items()}
    dependents: dict[str, list[str]] = {node_id: [] for node_id in adj}
    for node_id, deps in adj.items():
        for dep in deps:
            dependents[dep].append(node_id)

    waves: list[list[str]] = []
    current = sorted(node_id for node_id, deg in in_degree.items() if deg == 0)
    while current:
        waves.append(current)
        nxt: list[str] = []
        for node_id in current:
            for dep_of in dependents[node_id]:
                in_degree[dep_of] -= 1
                if in_degree[dep_of] == 0:
                    nxt.append(dep_of)
        current = sorted(nxt)
    return waves


def analyze_graph(graph: Any) -> dict[str, Any]:
    errors: list[str] = []
    cycles: list[list[str]] = []
    overlaps: list[dict[str, Any]] = []
    waves: list[list[str]] = []

    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if isinstance(nodes, dict):
        adj = _adjacency(nodes)
        cycles = find_minimal_cycles(adj)
        overlaps = find_ownership_overlaps(nodes, adj)
        if not cycles:
            waves = compute_waves(adj)

    validate_error: str | None = None
    try:
        graph_model.validate_graph(graph)
    except ioutil.PreconditionError as exc:
        validate_error = str(exc)
    except RecursionError:
        if not cycles:
            validate_error = "graph dependency depth exceeded recursion limit"
    if validate_error is not None:
        is_cycle_err = validate_error.startswith("cycle detected at ") and bool(cycles)
        is_overlap_err = validate_error.startswith("owns overlap without serialization: ") and bool(overlaps)
        if not is_cycle_err and not is_overlap_err:
            errors.append(validate_error)

    for cycle in cycles:
        errors.append(f"dependency cycle: {' -> '.join(cycle + [cycle[0]])}")
    for overlap in overlaps:
        left, right = overlap["nodes"]
        a, b = overlap["patterns"]
        errors.append(f"owns overlap without serialization: {left} {a!r} and {right} {b!r}")

    valid = not errors and not cycles and not overlaps
    max_parallelism = max((len(wave) for wave in waves), default=0)
    return {
        "valid": valid,
        "errors": errors,
        "cycles": cycles,
        "overlaps": overlaps,
        "waves": waves,
        "max_parallelism": max_parallelism,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-file", required=True, help="Path to graph JSON file, or '-' for stdin")
    args = parser.parse_args()

    graph = _load_graph_json(args.graph_file)
    report = analyze_graph(graph)
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report["valid"] else 3


if __name__ == "__main__":
    cli_common.run(main)
