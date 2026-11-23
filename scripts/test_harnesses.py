"""Unit tests for multi-harness registry, launch flags, transcripts, and context hooks."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import context_usage  # noqa: E402
import graph_model  # noqa: E402
import harnesses  # noqa: E402
import session_docs  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").replace(".py", ""), SCRIPTS / name)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HarnessRegistryTests(unittest.TestCase):
    def test_aliases_and_precedence(self) -> None:
        self.assertEqual(harnesses.normalize_kind("claude-code"), "claude")
        self.assertEqual(harnesses.normalize_kind("copilot-cli"), "copilot")
        self.assertEqual(harnesses.normalize_kind("grok-build"), "grok")
        self.assertEqual(harnesses.normalize_kind("oh-my-pi"), "omp")
        self.assertEqual(harnesses.normalize_kind("custom-cli"), "custom-cli")

        with mock.patch.dict(os.environ, {"HERDR_ORCHESTRATOR_KIND": "codex"}, clear=False):
            self.assertEqual(harnesses.default_kind(), "codex")
            self.assertEqual(harnesses.default_kind(graph_kind="pi"), "pi")
            self.assertEqual(harnesses.default_kind(node_kind="claude-code", graph_kind="pi"), "claude")
            self.assertEqual(
                harnesses.default_kind(explicit="grok-build", node_kind="claude", graph_kind="pi"),
                "grok",
            )

    def test_model_argv_and_validation_per_harness(self) -> None:
        self.assertEqual(
            harnesses.build_model_argv("omp", model="anthropic/claude-opus-4-6", thinking="high"),
            ["--model", "anthropic/claude-opus-4-6", "--thinking", "high"],
        )
        self.assertEqual(
            harnesses.build_model_argv("claude", model="sonnet", effort="high"),
            ["--model", "sonnet", "--effort", "high"],
        )
        self.assertEqual(
            harnesses.build_model_argv("codex", model="o3", effort="high"),
            ["-m", "o3", "-c", "model_reasoning_effort=high"],
        )
        self.assertEqual(
            harnesses.build_model_argv("copilot", model="gpt-5", effort="medium"),
            ["--model", "gpt-5", "--reasoning-effort", "medium"],
        )
        self.assertEqual(
            harnesses.build_model_argv("pi", model="openai/gpt-4o", thinking="low"),
            ["--model", "openai/gpt-4o", "--thinking", "low"],
        )
        self.assertEqual(
            harnesses.build_model_argv("grok", model="grok-4"),
            ["--model", "grok-4"],
        )

        # OMP enforces provider/model; non-OMP harnesses allow bare aliases.
        with self.assertRaises(ValueError):
            harnesses.validate_model_spec("omp", "sonnet", origin="--model")
        harnesses.validate_model_spec("claude", "sonnet", origin="--model")
        harnesses.validate_model_spec("codex", "o3", origin="--model")

    def test_non_omp_role_lookup_from_roles_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            roles_file = home / ".config" / "herdr-orchestrator" / "roles.json"
            roles_file.parent.mkdir(parents=True)
            roles_file.write_text(
                json.dumps({"claude": {"default": "sonnet", "slow": "opus"}, "codex": {"default": "o3"}}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("XDG_CONFIG_HOME", None)
                self.assertEqual(harnesses.resolve_role_for_kind("claude", "slow", home=home), "opus")
                self.assertEqual(harnesses.resolve_role_for_kind("codex", "default", home=home), "o3")
                with self.assertRaises(RuntimeError):
                    harnesses.resolve_role_for_kind("copilot", "default", home=home)

    def test_graph_model_accepts_default_kind_and_node_kind(self) -> None:
        graph = {
            "schema_version": 1,
            "default_kind": "claude",
            "nodes": {
                "A": {"id": "A", "executor": "herdr-agent", "kind": "codex"},
                "B": {"id": "B", "executor": "herdr-agent"},
            },
        }
        graph_model.validate_graph(graph)

        bad_graph = {
            "schema_version": 1,
            "default_kind": "",
            "nodes": {"A": {"id": "A", "executor": "herdr-agent"}},
        }
        with self.assertRaises(Exception):
            graph_model.validate_graph(bad_graph)

    def test_process_matching(self) -> None:
        self.assertTrue(harnesses.is_harness_process({"name": "claude"}, "claude"))
        self.assertTrue(harnesses.is_harness_process({"name": "node", "argv": ["node", "/usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"]}, "claude"))
        self.assertFalse(harnesses.is_harness_process({"name": "pyright-langserver"}, "claude"))
        self.assertTrue(harnesses.is_harness_process({"name": "codex"}, "codex"))
        self.assertTrue(harnesses.is_harness_process({"name": "copilot"}, "copilot"))
        self.assertTrue(harnesses.is_harness_process({"name": "pi"}, "pi"))
        self.assertTrue(harnesses.is_harness_process({"name": "grok"}, "grok"))


class ClaudeContextHookAndUsageTests(unittest.TestCase):
    def test_claude_hook_writes_valid_v2_snapshot_accepted_by_context_usage(self) -> None:
        hook_mod = _load("claude-context-hook.py")
        with tempfile.TemporaryDirectory() as td:
            home = Path(td).resolve()
            session_dir = home / "docs" / "sessions" / "s1"
            session_dir.mkdir(parents=True)
            meta = {"workspace": "ws-1", "pane": "ws-1:p1", "session_id": "s1", "run": str(home / "docs" / "runs" / "r1")}
            (session_dir / session_docs.META_NAME).write_text(json.dumps(meta), encoding="utf-8")

            now_ms = int(time.time() * 1000)
            env = {
                "HOME": str(home),
                "HERDR_ENV": "1",
                "HERDR_SOCKET_PATH": "/tmp/herdr.sock",
                "HERDR_SESSION": "default",
                "HERDR_WORKSPACE_ID": "ws-1",
                "HERDR_PANE_ID": "ws-1:p1",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                snap_payload = hook_mod.build_snapshot(
                    {
                        "session_id": "claude-sess-42",
                        "model": {"id": "claude-sonnet-4-6"},
                        "context_window": {"context_window_size": 200000, "used_percentage": 60},
                    },
                    workspace="ws-1",
                    pane="ws-1:p1",
                    pid=4242,
                    now_ms=now_ms,
                )
                written = hook_mod._write_snapshot("ws-1", "ws-1:p1", snap_payload)
                self.assertTrue(written.is_file())
                self.assertEqual(written.name, hashlib.sha256(b"ws-1\nws-1:p1").hexdigest() + ".json")

                def fake_herdr(*args):
                    if args[:2] == ("pane", "get"):
                        return {"result": {"type": "pane_info", "pane": {"workspace_id": "ws-1", "pane_id": "ws-1:p1", "agent": "claude-code"}}}
                    if args[:2] == ("pane", "process-info"):
                        return {
                            "result": {
                                "type": "pane_process_info",
                                "process_info": {
                                    "pane_id": "ws-1:p1",
                                    "foreground_process_group_id": 4242,
                                    "foreground_processes": [{"pid": 4242, "name": "claude"}],
                                },
                            }
                        }
                    raise AssertionError(args)

                with mock.patch.object(context_usage, "herdr", side_effect=fake_herdr):
                    evidence = context_usage.require_context_usage(
                        session_dir,
                        {"workspace": "ws-1", "pane": "ws-1:p1", "session": str(session_dir)},
                    )
                self.assertEqual(evidence["harness"], "claude")
                self.assertEqual(evidence["tokens"], 120000)
                self.assertEqual(evidence["context_window"], 200000)
                self.assertAlmostEqual(evidence["fraction"], 0.6)


class TranscriptAndMetricsMultiHarnessTests(unittest.TestCase):
    def test_collect_and_metrics_parse_codex_and_openai_shapes(self) -> None:
        collect_mod = _load("collect-agent.py")
        metrics_mod = _load("run-metrics.py")
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "codex-session.jsonl"
            rows = [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 200,
                                "output_tokens": 300,
                                "total_tokens": 1500,
                            }
                        },
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Implemented the fix cleanly."}],
                    },
                },
            ]
            log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            self.assertEqual(collect_mod.parse_session_summary(log), "Implemented the fix cleanly.")
            usage = metrics_mod._parse_session_usage(log)
            assert usage is not None
            self.assertEqual(usage["input_tokens"], 1000)
            self.assertEqual(usage["cache_read_tokens"], 200)
            self.assertEqual(usage["output_tokens"], 300)
            self.assertEqual(usage["total_tokens"], 1500)


if __name__ == "__main__":
    unittest.main()
