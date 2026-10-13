#!/usr/bin/env python3
"""Focused regression check for orchestration prompt wiring."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from testkit import FakeHerdr, HOLDER, load_script, run_module




def dispatched(filename: str, *args: str) -> str:
    module = load_script(filename)
    routes = {("agent", "prompt"): {}}
    if filename == "spawn-agent.py":
        routes.update({
            ("workspace", "create"): {
                "result": {"workspace": {"workspace_id": "ws"}, "root_pane": {"pane_id": "pane"}},
            },
            ("pane", "rename"): {},
            ("workspace", "rename"): {},
            ("workspace", "report-metadata"): {},
            ("agent", "start"): {"result": {"agent": {"id": "agent-id"}}},
            ("agent", "get"): lambda *argv: {
                "result": {"agent": {"id": "agent-id", "name": argv[2]}},
            },
            ("workspace", "get"): {"result": {"workspace": {"tokens": {}}}},
        })
    fake = FakeHerdr(routes)
    original_herdr = module.herdr
    module.herdr = fake
    try:
        code, out, err = run_module(module, [filename, *args])
        assert code == 0, err
    finally:
        module.herdr = original_herdr
    prompts = [call for call in fake.calls if call[:2] == ("agent", "prompt")]
    assert len(prompts) == 1
    assert prompts[0][4:] == ("--wait", "--until", "working", "--timeout", "15000")
    return prompts[0][3]


ORDERED_CONSUMED = [
    {"alias": "b", "producer": "Src", "producer_attempt": 2, "output": "summary", "path": "/tmp/b", "hash": "sha256:" + hashlib.sha256(b"b").hexdigest()},
    {"alias": "c", "producer": "Src", "producer_attempt": 1, "output": "vcs", "path": "/tmp/c", "hash": "sha256:" + hashlib.sha256(b"c").hexdigest()},
]
SHUFFLED_CONSUMED = [ORDERED_CONSUMED[1], ORDERED_CONSUMED[0]]
CONSUMER_INPUTS = [
    {"alias": "b", "from": "Src", "output": "summary"},
    {"alias": "c", "from": "Src", "output": "vcs"},
    {"alias": "d", "from": "Src", "output": "summary", "required": False},
]


def plant_dispatch_run(root: Path) -> Path:
    run = (root / "run").resolve()
    run.mkdir()
    nodes = {
        "Src": {
            "id": "Src",
            "executor": "task",
            "outputs": [{"name": "summary", "type": "summary"}, {"name": "vcs", "type": "vcs"}],
        },
    }
    for node_id, executor in (("T", "task"), ("W", "workflow"), ("H", "herdr-agent")):
        nodes[node_id] = {
            "id": node_id,
            "executor": executor,
            "depends_on": ["Src"],
            "inputs": CONSUMER_INPUTS,
        }
    (run / "run.json").write_text(
        json.dumps({
            "id": "run-id",
            "job": "job",
            "schema_version": 1,
            "path": str(run),
            "base_sha": "base",
            "repo": str((root / "repo").resolve()),
        })
        + "\n",
        encoding="utf-8",
    )
    (run / "graph.json").write_text(
        json.dumps({"schema_version": 1, "run": str(run), "base_sha": "base", "nodes": nodes}) + "\n",
        encoding="utf-8",
    )
    holder = HOLDER
    events = [{"type": "session_run_bound", **holder}]
    for node_id in ("T", "W", "H"):
        attempt_dir = run / "artifacts" / node_id / "1"
        attempt_dir.mkdir(parents=True)
        (attempt_dir / "consumed_artifacts.json").write_text(json.dumps(SHUFFLED_CONSUMED) + "\n", encoding="utf-8")
        events.extend([
            {"type": "ready", "node": node_id, "from": "planned", "to": "ready", "attempt": 1},
            {"type": "dispatch_intent", "node": node_id, "attempt": 1},
        ])
    (run / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return run


def check_spawn_wrong_executor_skips_prompt() -> None:
    with TemporaryDirectory() as tmp:
        run = plant_dispatch_run(Path(tmp))
        module = load_script("spawn-agent.py")
        fake = FakeHerdr({})
        original_herdr = module.herdr
        module.herdr = fake
        try:
            code, out, err = run_module(module, [
                "spawn-agent.py", "worker", "--run", str(run), "--node", "T", "--attempt", "1",
                "--session", HOLDER["session"], "--holder-workspace", HOLDER["workspace"],
                "--holder-pane", HOLDER["pane"], "--epoch", "1", "--prompt", "work",
            ])
        finally:
            module.herdr = original_herdr
        assert code == 2
        assert fake.calls == []


class PromptContractTests(unittest.TestCase):
    def test_spawn_wrong_executor_skips_prompt(self):
        check_spawn_wrong_executor_skips_prompt()

    def test_orchestrate_prefix_routing(self):
        assert dispatched("spawn-agent.py", "worker", "--prompt", "work") == "orchestrate\nwork"
        assert dispatched("spawn-agent.py", "worker", "--kind", "other", "--prompt", "work") == "work"
        assert dispatched("spawn-agent.py", "worker", "--no-orchestrate", "--prompt", "work") == "work"
        assert dispatched("prompt-agent.py", "worker", "--prompt", "work") == "orchestrate\nwork"
        assert dispatched("prompt-agent.py", "worker", "--no-orchestrate", "--prompt", "work") == "work"


if __name__ == "__main__":
    unittest.main()
