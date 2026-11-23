"""Tests for v2.0.0 architecture: XDG state_paths, per-run SQLite WAL run_store,
content-addressed blobs, structured ledger notes, dynamic brief synthesis,
zero-.gitignore ho.py workflow, and zero-.md rotate/ack handoff.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ho
import ioutil
import ledger
import run_state
import run_store
import state_paths
import testkit


def _load_script(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_ho(argv: list[str], *, env: dict[str, str | None], cwd: Path) -> tuple[int, str, str]:
    out = io.StringIO()
    err = io.StringIO()
    old_cwd = Path.cwd()
    old_env = os.environ.copy()
    try:
        os.chdir(cwd)
        for k in ("HC_TERMINAL_ID", "HERDR_WORKSPACE_ID", "HERDR_PANE_ID", "HERDR_ENV", "HERDR_SESSION", "HERDR_SOCKET_PATH"):
            os.environ.pop(k, None)
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with mock.patch.object(sys, "argv", ["ho.py", *argv]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ho.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except ioutil.UsageError as exc:
                print(f"error: {exc}", file=sys.stderr)
                code = 2
            except ioutil.PreconditionError as exc:
                print(f"error: {exc}", file=sys.stderr)
                code = 3
            except ioutil.RunError as exc:
                print(f"error: {exc}", file=sys.stderr)
                code = 1
        return code, out.getvalue(), err.getvalue()
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


class V2StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_dir = self.root / "state"
        self.repo = testkit.seed_repo(self.root / "repo")
        self.env_patch = mock.patch.dict(
            os.environ,
            {state_paths.STATE_DIR_ENV: str(self.state_dir)},
            clear=False,
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_xdg_sqlite_run_creates_zero_repo_files_and_zero_md_files(self) -> None:
        # Target repo has NO .gitignore rules for docs/runs or docs/sessions
        self.assertFalse((self.repo / ".gitignore").exists())
        env = {
            state_paths.STATE_DIR_ENV: str(self.state_dir),
            "HERDR_WORKSPACE_ID": "ws-v2",
            "HERDR_PANE_ID": "ws-v2:p1",
        }
        code, out, err = _run_ho(["init", "--job", "v2-clean"], env=env, cwd=self.repo)
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["storage"], "sqlite")
        self.assertEqual(payload["session"], "epoch-1")
        self.assertFalse(payload["reused"])

        run_dir = Path(payload["run"])
        self.assertTrue((run_dir / "run.db").is_file())
        self.assertTrue((run_dir / "blobs").is_dir())
        # Zero legacy jsonl/json/md caches written inside SQLite run dir
        for legacy in ("events.jsonl", "state.json", "controller.json", "status.md", "run.json", "graph.json"):
            self.assertFalse((run_dir / legacy).exists(), f"unexpected legacy file {legacy}")
        # Zero docs/ pollution in target repo
        self.assertFalse((self.repo / "docs").exists())

        # Re-init in same pane for same job reuses the run via index.db
        code2, out2, err2 = _run_ho(["init", "--job", "v2-clean"], env=env, cwd=self.repo)
        self.assertEqual(code2, 0, err2)
        payload2 = json.loads(out2)
        self.assertTrue(payload2["reused"])
        self.assertEqual(payload2["run"], payload["run"])

        # index.db can be deleted and rebuilt from runs/*/run.db
        idx_path = state_paths.index_db_path()
        self.assertTrue(idx_path.is_file())
        idx_path.unlink()
        self.assertIsNone(state_paths.lookup_pane("ws-v2", "ws-v2:p1"))
        rebuilt = state_paths.rebuild_index()
        self.assertEqual(rebuilt, 1)
        found = state_paths.lookup_pane("ws-v2", "ws-v2:p1")
        self.assertIsNotNone(found)
        self.assertEqual(found["run_dir"], str(run_dir))

    def test_notes_brief_and_md_on_demand(self) -> None:
        env = {
            state_paths.STATE_DIR_ENV: str(self.state_dir),
            "HERDR_WORKSPACE_ID": "ws-notes",
            "HERDR_PANE_ID": "ws-notes:p1",
        }
        code, out, err = _run_ho(["init", "--job", "notes-job"], env=env, cwd=self.repo)
        self.assertEqual(code, 0, err)
        run_dir = Path(json.loads(out)["run"])

        # Accept a simple graph without passing --run or --session (auto-discovered via index.db)
        graph_file = self.root / "graph.json"
        graph_file.write_text(
            json.dumps({
                "nodes": [
                    {"id": "slice-a", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}
                ]
            }),
            encoding="utf-8",
        )
        code, out, err = _run_ho(["accept", "--graph", str(graph_file)], env=env, cwd=self.repo)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["nodes"], ["slice-a"])

        # Record structured notes into run.db
        code, out, err = _run_ho(["note", "add", "--kind", "decision", "Use WAL SQLite per run"], env=env, cwd=self.repo)
        self.assertEqual(code, 0, err)
        code, out, err = _run_ho(["note", "add", "--kind", "focus", "Finish slice-a then integrate"], env=env, cwd=self.repo)
        self.assertEqual(code, 0, err)

        # List notes
        code, out, err = _run_ho(["note", "list"], env=env, cwd=self.repo)
        self.assertEqual(code, 0, err)
        notes = json.loads(out)["notes"]
        self.assertEqual(len(notes), 2)
        self.assertEqual(notes[0]["kind"], "decision")
        self.assertEqual(notes[1]["kind"], "focus")

        # Brief JSON + on-demand markdown
        code, out, err = _run_ho(["brief"], env=env, cwd=self.repo)
        self.assertEqual(code, 0, err)
        brief = json.loads(out)
        self.assertEqual(brief["job"], "notes-job")
        self.assertIn("Use WAL SQLite per run", brief["markdown"])
        self.assertIn("slice-a", brief["nodes"])

        code, out, err = _run_ho(["status", "--md"], env=env, cwd=self.repo)
        self.assertEqual(code, 0, err)
        self.assertIn("# Run Brief — notes-job", out)
        self.assertIn("| slice-a |", out)

        # Verify zero .md files exist anywhere under state_dir or repo
        md_files = list(self.state_dir.rglob("*.md"))
        self.assertEqual(md_files, [])

    def test_content_addressed_blobs_integrity(self) -> None:
        run_doc = ledger.claim_run(None, "blob-job", storage="sqlite", repo=self.repo)
        run_dir = Path(run_doc["path"])
        info = run_store.put_blob(run_dir, "hello blob payload", kind="prompt")
        self.assertTrue(info["sha256"].startswith("sha256:"))
        self.assertEqual(run_store.get_blob(run_dir, info["sha256"]), b"hello blob payload")

        # Tampering with blob bytes fails closed
        Path(info["path"]).write_bytes(b"corrupted")
        with self.assertRaises(ioutil.PreconditionError):
            run_store.get_blob(run_dir, info["sha256"])

    def test_zero_md_run_centric_ack_handoff_and_synthesized_prompt(self) -> None:
        run_doc = ledger.claim_run(None, "rot-job", storage="sqlite", repo=self.repo)
        run_dir = Path(run_doc["path"])
        holder1 = {"workspace": "ws-1", "pane": "ws-1:p1", "session": "epoch-1"}
        ledger.bind_session_run(run_dir, holder=holder1)
        ledger.record_note(run_dir, kind="decision", body="Keep promotion ref clean", holder=holder1)

        rotate_mod = _load_script("rotate.py")
        ack_mod = _load_script("ack-handoff.py")

        # Drain lease with handoff summary
        rotate_mod.start_lease_drain(run_dir, holder1, handoff="Waiting for slice-b review")
        prompt = rotate_mod.synthesize_successor_prompt(run_dir, SCRIPTS / "ack-handoff.py", "nonce-123")
        self.assertIn("--nonce nonce-123 --run", prompt)
        self.assertIn("Keep promotion ref clean", prompt)
        self.assertIn("Waiting for slice-b review", prompt)
        self.assertNotIn("HANDOFF.md", prompt)
        self.assertNotIn("NEXT-SESSION-PROMPT.md", prompt)

        # Successor acknowledges via _ack_run_mode
        stamps: list[tuple[str, tuple[str, ...]]] = []

        def fake_stamp(ws: str, *, source: str, tokens: tuple[str, ...], invoke=None):
            stamps.append((ws, tokens))

        with mock.patch.object(ack_mod, "stamp_workspace_tokens", side_effect=fake_stamp):
            res = ack_mod._ack_run_mode(run_dir, "nonce-123", "ws-2", "ws-2:p1")
        self.assertTrue(res["acked"])
        self.assertEqual(res["session"], "epoch-2")
        self.assertEqual(res["parent"], "epoch-1")

        # Replay shows epoch 2 active and owned by ws-2
        snap = ledger.replay(run_dir)
        self.assertEqual(snap["controller"]["epoch"], 2)
        self.assertEqual(snap["controller"]["workspace"], "ws-2")
        self.assertEqual(snap["controller"]["session"], "epoch-2")
        self.assertEqual(snap["controller"]["lease"], "active")

        # index.db updated for successor pane
        found = state_paths.lookup_pane("ws-2", "ws-2:p1")
        self.assertIsNotNone(found)
        self.assertEqual(found["session_id"], "epoch-2")

        # Same-nonce retry is idempotent (no epoch 3)
        with mock.patch.object(ack_mod, "stamp_workspace_tokens", side_effect=fake_stamp):
            res2 = ack_mod._ack_run_mode(run_dir, "nonce-123", "ws-2", "ws-2:p1")
        self.assertEqual(res2["session"], "epoch-2")
        self.assertEqual(ledger.replay(run_dir)["controller"]["epoch"], 2)

        # Wrong nonce after transfer refuses
        with self.assertRaises(ioutil.PreconditionError):
            ack_mod._ack_run_mode(run_dir, "wrong-nonce", "ws-2", "ws-2:p1")


if __name__ == "__main__":
    unittest.main()
