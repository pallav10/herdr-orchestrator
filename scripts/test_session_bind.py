#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import ledger
import run_state
import session_docs

INIT = SCRIPTS / "init-docs.py"


class SessionBindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, ignore: str = "") -> Path:
        repo = self.root / f"repo-{len(list(self.root.iterdir()))}"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, text=True, check=True)
        if ignore:
            (repo / ".gitignore").write_text(ignore, encoding="utf-8")
        return repo

    def cli(self, repo: Path, *args: str, env: dict[str, str] | None = None,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        child_env = os.environ.copy()
        child_env.pop("HERDR_WORKSPACE_ID", None)
        child_env.pop("HERDR_PANE_ID", None)
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, str(INIT), *args],
            cwd=cwd or repo,
            env=child_env,
            capture_output=True,
            text=True,
        )

    def session_path(self, repo: Path, payload: dict) -> Path:
        path = Path(payload["session"])
        return path if path.is_absolute() else repo / path

    def test_init_docs_fails_closed_when_ignore_rules_are_missing(self):
        repo = self.repo()
        proc = self.cli(repo, "--dir", "docs", "--job", "Job")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("missing gitignore rule", proc.stderr)
        self.assertIn("/docs/sessions/", proc.stderr)
        self.assertIn("/docs/runs/", proc.stderr)
        self.assertFalse((repo / "docs").exists())

    def test_init_docs_names_each_missing_ignore_rule(self):
        cases = [
            ("/docs/runs/\n", "/docs/sessions/", False),
            ("/docs/sessions/\n", "/docs/runs/", False),
            ("/docs/runs/\n", "/docs/sessions/", True),
        ]
        for ignore, missing, with_run in cases:
            with self.subTest(missing=missing, with_run=with_run):
                repo = self.repo(ignore)
                args = ["--dir", "docs", "--job", "Job"]
                if with_run:
                    run = run_state.claim_run(repo / "docs", "Job")["path"]
                    args += ["--run", run]
                proc = self.cli(repo, *args)
                self.assertEqual(proc.returncode, 3)
                self.assertIn(missing, proc.stderr)
                self.assertFalse((repo / "docs" / "sessions").exists())

    def test_explicit_run_binds_absolute_and_progress_is_pointer(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        run = run_state.claim_run(repo / "docs", "Job")["path"]

        proc = self.cli(repo, "--dir", "docs", "--job", "Job", "--run", run)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        session = self.session_path(repo, payload)
        meta = json.loads((session / "session.json").read_text(encoding="utf-8"))
        progress = (session / "progress.md").read_text(encoding="utf-8")
        controller = run_state.replay(run)["controller"]

        self.assertEqual(payload["run"], run)
        self.assertEqual(meta["run"], run)
        self.assertTrue(Path(payload["run"]).is_absolute())
        self.assertIn(f"Run: {run}", progress)
        self.assertNotIn("| Slice | Executor |", progress)
        self.assertEqual(meta["workspace"], session_docs.UNKNOWN_OWNER)
        self.assertEqual(meta["pane"], session_docs.UNKNOWN_OWNER)
        self.assertEqual(
            {key: controller[key] for key in run_state.HOLDER_KEYS},
            {"workspace": session_docs.UNKNOWN_OWNER, "pane": session_docs.UNKNOWN_OWNER, "session": str(session.resolve())},
        )

    def test_existing_caller_creates_run_and_reuses_owner_session(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        env = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane"}

        first = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        second = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        one = json.loads(first.stdout)
        two = json.loads(second.stdout)

        self.assertTrue(Path(one["run"]).is_absolute())
        self.assertTrue((Path(one["run"]) / "run.json").is_file())
        self.assertEqual(two["run"], one["run"])
        self.assertEqual(two["session"], one["session"])
        self.assertTrue(two["reused"])
        controller = run_state.replay(one["run"])["controller"]
        self.assertEqual(
            {key: controller[key] for key in run_state.HOLDER_KEYS},
            {"workspace": "workspace", "pane": "pane", "session": str(self.session_path(repo, one).resolve())},
        )

    def test_same_workspace_different_panes_do_not_reuse_session(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        env_a = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane-a"}
        env_b = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane-b"}

        first = self.cli(repo, "--dir", "docs", "--job", "Job", env=env_a)
        self.assertEqual(first.returncode, 0, first.stderr)
        one = json.loads(first.stdout)
        first_holder = {"workspace": "workspace", "pane": "pane-a", "session": str(self.session_path(repo, one).resolve())}

        refused = self.cli(repo, "--dir", "docs", "--job", "Job", "--run", one["run"], env=env_b)
        self.assertEqual(refused.returncode, 3, refused.stderr)
        self.assertIn("different holder", refused.stderr)
        self.assertEqual(
            {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            first_holder,
        )

        second = self.cli(repo, "--dir", "docs", "--job", "Job", env=env_b)
        self.assertEqual(second.returncode, 0, second.stderr)
        two = json.loads(second.stdout)
        self.assertNotEqual(two["session"], one["session"])
        self.assertNotEqual(two["run"], one["run"])
        self.assertFalse(two["reused"])
        self.assertEqual(
            {key: run_state.replay(two["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            {"workspace": "workspace", "pane": "pane-b", "session": str(self.session_path(repo, two).resolve())},
        )

    def test_load_session_rejects_latest_pointer(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        proc = self.cli(repo, "--dir", "docs", "--job", "Job")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        with self.assertRaisesRegex(RuntimeError, "concrete directory"):
            session_docs.load_session(repo / "docs" / "sessions" / "latest")

    def test_load_session_rejects_fake_docs_root(self):
        fake = self.root / "fake-docs" / "not-sessions" / "session-id"
        fake.mkdir(parents=True)
        (fake / "session.json").write_text("{}\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "under sessions"):
            session_docs.load_session(fake)

    def test_symlinked_meta_run_is_not_reused(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        env = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane"}

        first = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        one = json.loads(first.stdout)
        session = self.session_path(repo, one)
        original_holder = {"workspace": "workspace", "pane": "pane", "session": str(session.resolve())}
        alias = repo / "docs" / "runs" / "alias"
        alias.symlink_to(Path(one["run"]), target_is_directory=True)
        meta_path = session / "session.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["run"] = str(alias)
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        notes = "## Run\nInformational prose, not a locator.\n## Decisions\nRetain this constraint.\n"
        (session / "HANDOFF.md").write_text(notes, encoding="utf-8")

        with patch.dict(os.environ, env):
            with self.assertRaisesRegex(run_state.PreconditionError, "canonical"):
                session_docs.open_session(
                    repo / "docs", job="Job", template_dir=SCRIPTS.parent / "references" / "templates", parent=session,
                )

        second = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        two = json.loads(second.stdout)

        self.assertEqual((session / "HANDOFF.md").read_text(encoding="utf-8"), notes)
        self.assertEqual((self.session_path(repo, two) / "HANDOFF.md").read_text(encoding="utf-8"), notes)
        self.assertNotEqual(two["session"], one["session"])
        self.assertFalse(two["reused"])
        self.assertNotEqual(two["run"], str(alias))
        self.assertNotEqual(two["run"], one["run"])
        self.assertTrue(Path(two["run"]).is_absolute())
        self.assertEqual(
            {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            original_holder,
        )
        self.assertEqual(
            {key: run_state.replay(two["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            {"workspace": "workspace", "pane": "pane", "session": str(self.session_path(repo, two).resolve())},
        )

    def test_open_session_rejects_relative_run(self):
        docs = self.root / "docs"
        docs.mkdir()
        template_dir = SCRIPTS.parent / "references" / "templates"
        with self.assertRaises(run_state.PreconditionError) as ctx:
            session_docs.open_session(
                docs, job="Job", template_dir=template_dir, run="docs/runs/x",
            )
        self.assertIn("absolute", str(ctx.exception))
        self.assertIn("docs/runs/x", str(ctx.exception))
        self.assertFalse((docs / "sessions").exists())
        self.assertFalse((docs / "runs").exists())

    def test_init_docs_rejects_relative_run(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        proc = self.cli(repo, "--dir", "docs", "--job", "Job", "--run", "docs/runs/x")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("--run", proc.stderr)
        self.assertIn("absolute", proc.stderr)
        self.assertIn("docs/runs/x", proc.stderr)
        self.assertFalse((repo / "docs" / "sessions").exists())
        self.assertFalse((repo / "docs" / "runs").exists())

    def test_initial_session_binds_exact_holder(self):
        docs = self.root / "docs"
        docs.mkdir()
        template_dir = SCRIPTS.parent / "references" / "templates"
        captured = []

        def fake_bind(run_dir, holder=None, *, lock_timeout=120):
            captured.append({"run": run_dir, "holder": holder})

        with patch.object(session_docs.ledger, "bind_session_run", fake_bind):
            with patch.dict(os.environ, {"HERDR_WORKSPACE_ID": "ws-1", "HERDR_PANE_ID": "pane-1"}):
                result = session_docs.open_session(docs, job="Job", template_dir=template_dir)

        self.assertFalse(result["reused"])
        self.assertEqual(len(captured), 1)
        session = (docs / "sessions" / result["id"]).resolve()
        self.assertEqual(
            captured[0]["holder"],
            {"workspace": "ws-1", "pane": "pane-1", "session": str(session)},
        )
        self.assertEqual(captured[0]["run"], result["run"])
        self.assertTrue(Path(result["run"]).is_absolute())
        self.assertNotEqual(session.name, session_docs.LATEST_LINK)

    def test_reused_session_binds_exact_holder(self):
        docs = self.root / "docs"
        docs.mkdir()
        template_dir = SCRIPTS.parent / "references" / "templates"
        captured = []

        def fake_bind(run_dir, holder=None, *, lock_timeout=120):
            captured.append({"run": run_dir, "holder": holder})

        env = {"HERDR_WORKSPACE_ID": "ws-1", "HERDR_PANE_ID": "pane-1"}
        with patch.object(session_docs.ledger, "bind_session_run", fake_bind):
            with patch.dict(os.environ, env):
                first = session_docs.open_session(docs, job="Job", template_dir=template_dir)
                second = session_docs.open_session(docs, job="Job", template_dir=template_dir)

        self.assertTrue(second["reused"])
        self.assertEqual(second["session"], first["session"])
        self.assertEqual(len(captured), 2)
        session = (docs / "sessions" / first["id"]).resolve()
        holder = {"workspace": "ws-1", "pane": "pane-1", "session": str(session)}
        self.assertEqual(captured[0]["holder"], holder)
        self.assertEqual(captured[1]["holder"], holder)
        self.assertEqual(captured[1]["run"], first["run"])
        self.assertTrue(Path(second["run"]).is_absolute())

    def test_deferred_successor_preserves_predecessor_holder(self):
        docs = self.root / "docs"
        docs.mkdir()
        template_dir = SCRIPTS.parent / "references" / "templates"
        env = {"HERDR_WORKSPACE_ID": "ws-1", "HERDR_PANE_ID": "pane-1"}
        binds = []
        real_bind = ledger.bind_session_run

        def fake_bind(run_dir, holder=None, *, lock_timeout=120):
            binds.append({"run": run_dir, "holder": holder})
            return real_bind(run_dir, holder=holder, lock_timeout=lock_timeout)

        with patch.object(session_docs.ledger, "bind_session_run", fake_bind):
            with patch.dict(os.environ, env):
                first = session_docs.open_session(docs, job="Job", template_dir=template_dir)
                parent = (docs / "sessions" / first["id"]).resolve()
                successor = session_docs.open_session(
                    docs,
                    job="Job",
                    template_dir=template_dir,
                    parent=parent,
                    run=first["run"],
                    defer_run_binding=True,
                )

        self.assertFalse(successor["reused"])
        self.assertNotEqual(successor["id"], first["id"])
        self.assertEqual(successor["run"], first["run"])
        self.assertEqual(successor["parent"], first["id"])
        self.assertTrue(Path(successor["run"]).is_absolute())
        session = docs / "sessions" / successor["id"]
        meta = json.loads((session / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["run"], first["run"])
        self.assertEqual(len(binds), 1)
        holder = {"workspace": "ws-1", "pane": "pane-1", "session": str(parent)}
        self.assertEqual(binds[0]["holder"], holder)
        self.assertEqual(
            {key: run_state.replay(first["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            holder,
        )
        self.assertNotEqual(session.name, session_docs.LATEST_LINK)

    def test_deferred_binding_rejects_missing_or_mismatched_lineage(self):
        docs = self.root / "docs"
        docs.mkdir()
        template_dir = SCRIPTS.parent / "references" / "templates"
        with self.assertRaisesRegex(run_state.PreconditionError, "explicit parent"):
            session_docs.open_session(
                docs, job="Job", template_dir=template_dir, defer_run_binding=True,
            )
        self.assertFalse((docs / "sessions").exists())
        self.assertFalse((docs / "runs").exists())

        env = {"HERDR_WORKSPACE_ID": "ws-1", "HERDR_PANE_ID": "pane-1"}
        with patch.dict(os.environ, env):
            first = session_docs.open_session(docs, job="Job", template_dir=template_dir)
        parent = (docs / "sessions" / first["id"]).resolve()
        holder = {key: run_state.replay(first["run"])["controller"][key] for key in run_state.HOLDER_KEYS}
        other = run_state.claim_run(docs, "Other")["path"]
        before = {path.name for path in (docs / "sessions").iterdir() if not path.is_symlink()}

        cases = [
            {"run": first["run"]},
            {"parent": parent},
            {"parent": parent, "run": other},
        ]
        for kwargs in cases:
            with self.assertRaises(run_state.PreconditionError):
                session_docs.open_session(
                    docs, job="Job", template_dir=template_dir, defer_run_binding=True, **kwargs,
                )
            after = {path.name for path in (docs / "sessions").iterdir() if not path.is_symlink()}
            self.assertEqual(after, before)
            self.assertEqual(
                {key: run_state.replay(first["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
                holder,
            )


    def test_conflicting_explicit_run_refuses_without_writes(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        env = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane"}
        first = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        one = json.loads(first.stdout)
        session = self.session_path(repo, one)
        other = run_state.claim_run(repo / "docs", "Job")["path"]
        sessions_before = {p.name for p in (repo / "docs" / "sessions").iterdir() if not p.is_symlink()}
        runs_before = {p.name for p in (repo / "docs" / "runs").iterdir() if p.is_dir()}
        meta_before = (session / "session.json").read_text(encoding="utf-8")
        holder = {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS}

        refused = self.cli(repo, "--dir", "docs", "--job", "Job", "--run", other, env=env)
        self.assertEqual(refused.returncode, 3, refused.stderr)
        self.assertIn("conflicting run", refused.stderr)
        self.assertIn(one["run"], refused.stderr)
        self.assertEqual(
            {p.name for p in (repo / "docs" / "sessions").iterdir() if not p.is_symlink()},
            sessions_before,
        )
        self.assertEqual(
            {p.name for p in (repo / "docs" / "runs").iterdir() if p.is_dir()},
            runs_before,
        )
        self.assertEqual((session / "session.json").read_text(encoding="utf-8"), meta_before)
        self.assertEqual(
            {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            holder,
        )

        template_dir = SCRIPTS.parent / "references" / "templates"
        with patch.dict(os.environ, env):
            with self.assertRaisesRegex(run_state.PreconditionError, "conflicting run"):
                session_docs.open_session(
                    repo / "docs", job="Job", template_dir=template_dir, run=other,
                )
        self.assertEqual((session / "session.json").read_text(encoding="utf-8"), meta_before)
        self.assertEqual(
            {p.name for p in (repo / "docs" / "sessions").iterdir() if not p.is_symlink()},
            sessions_before,
        )

    def test_matching_explicit_run_reuses_existing_session(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        env = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane"}
        first = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        one = json.loads(first.stdout)
        second = self.cli(repo, "--dir", "docs", "--job", "Job", "--run", one["run"], env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        two = json.loads(second.stdout)
        self.assertTrue(two["reused"])
        self.assertEqual(two["session"], one["session"])
        self.assertEqual(two["run"], one["run"])

    def test_force_new_claims_fresh_session_and_run(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        env = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane"}
        first = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        one = json.loads(first.stdout)
        session = self.session_path(repo, one)
        meta_before = (session / "session.json").read_text(encoding="utf-8")
        holder = {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS}

        second = self.cli(repo, "--dir", "docs", "--job", "Job", "--new", env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        two = json.loads(second.stdout)
        self.assertFalse(two["reused"])
        self.assertNotEqual(two["session"], one["session"])
        self.assertNotEqual(two["run"], one["run"])
        self.assertTrue(Path(two["run"]).is_absolute())
        self.assertTrue((Path(two["run"]) / "run.json").is_file())
        self.assertEqual((session / "session.json").read_text(encoding="utf-8"), meta_before)
        self.assertEqual(
            {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            holder,
        )
        self.assertEqual(
            {key: run_state.replay(two["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            {"workspace": "workspace", "pane": "pane", "session": str(self.session_path(repo, two).resolve())},
        )

    def test_force_new_binds_explicit_run(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        env = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane"}
        first = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        one = json.loads(first.stdout)
        session = self.session_path(repo, one)
        other = run_state.claim_run(repo / "docs", "Job")["path"]
        holder = {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS}

        second = self.cli(repo, "--dir", "docs", "--job", "Job", "--new", "--run", other, env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        two = json.loads(second.stdout)
        self.assertFalse(two["reused"])
        self.assertNotEqual(two["session"], one["session"])
        self.assertEqual(two["run"], other)
        self.assertEqual(json.loads((session / "session.json").read_text(encoding="utf-8"))["run"], one["run"])
        self.assertEqual(
            {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            holder,
        )
        self.assertEqual(
            {key: run_state.replay(other)["controller"][key] for key in run_state.HOLDER_KEYS},
            {"workspace": "workspace", "pane": "pane", "session": str(self.session_path(repo, two).resolve())},
        )

    def test_new_job_is_not_blocked_by_other_job_binding(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        env = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane"}
        first = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        self.assertEqual(first.returncode, 0, first.stderr)
        one = json.loads(first.stdout)
        holder = {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS}

        second = self.cli(repo, "--dir", "docs", "--job", "Other", env=env)
        self.assertEqual(second.returncode, 0, second.stderr)
        two = json.loads(second.stdout)
        self.assertFalse(two["reused"])
        self.assertNotEqual(two["session"], one["session"])
        self.assertNotEqual(two["run"], one["run"])
        self.assertEqual(
            {key: run_state.replay(one["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            holder,
        )


    def test_runtime_dir_symlink_is_refused_before_writes(self):
        template_dir = SCRIPTS.parent / "references" / "templates"
        for name in ("runs", "sessions"):
            with self.subTest(name=name):
                repo = self.repo("/docs/sessions/\n/docs/runs/\n")
                docs = repo / "docs"
                docs.mkdir()
                evil = self.root / f"evil-{name}"
                evil.mkdir()
                (docs / name).symlink_to(evil)
                proc = self.cli(repo, "--dir", "docs", "--job", "Job")
                self.assertEqual(proc.returncode, 3, proc.stderr)
                self.assertIn("symlink", proc.stderr)
                self.assertIn(f"{name}/", proc.stderr)
                self.assertEqual(list(evil.iterdir()), [])
                other = "sessions" if name == "runs" else "runs"
                self.assertFalse((docs / other).exists())

                with self.assertRaisesRegex(run_state.PreconditionError, "symlink"):
                    session_docs.open_session(docs, job="Job", template_dir=template_dir)
                self.assertEqual(list(evil.iterdir()), [])
                self.assertFalse((docs / other).exists())

    def test_printed_session_and_bootstrap_are_cwd_portable(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        proc = self.cli(repo, "--dir", "docs", "--job", "Job")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        other = self.root / "other-cwd"
        other.mkdir()

        def from_other_cwd(printed: str) -> Path:
            path = Path(printed)
            return path if path.is_absolute() else other / path

        session = from_other_cwd(payload["session"])
        self.assertTrue((session / "session.json").is_file(), payload["session"])
        self.assertEqual(payload["session"], run_state.canonical_path(session))
        self.assertEqual(
            payload["files"],
            [str(session / name) for name in session_docs.DOC_NAMES],
        )
        for printed in payload["files"]:
            self.assertTrue(from_other_cwd(printed).is_file(), printed)
        next_text = (session / "NEXT-SESSION-PROMPT.md").read_text(encoding="utf-8")
        self.assertIn(f"{session}/HANDOFF.md", next_text)
        self.assertIn(f"{session}/session.json", next_text)
        self.assertEqual(run_state.replay(payload["run"])["controller"]["session"], payload["session"])

    def test_legacy_same_pane_without_run_migrates_in_place(self):
        repo = self.repo("/docs/sessions/\n/docs/runs/\n")
        env = {"HERDR_WORKSPACE_ID": "workspace", "HERDR_PANE_ID": "pane"}
        session = repo / "docs" / "sessions" / "20200101-000000-job"
        session.mkdir(parents=True)
        ledger = "# Progress\n\n| Slice | Executor |\n| --- | --- |\n| A | task |\n"
        handoff = "# Handoff\n\nlegacy handoff\n"
        nxt = "# Next\n\nlegacy next\n"
        meta = {
            "id": session.name,
            "job": "Job",
            "owner": "workspace",
            "created": "2020-01-01T00:00:00.000000+00:00",
            "workspace": "workspace",
            "pane": "pane",
            "parent": None,
        }
        (session / "session.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        (session / "progress.md").write_text(ledger, encoding="utf-8")
        (session / "HANDOFF.md").write_text(handoff, encoding="utf-8")
        (session / "NEXT-SESSION-PROMPT.md").write_text(nxt, encoding="utf-8")

        proc = self.cli(repo, "--dir", "docs", "--job", "Job", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["reused"])
        self.assertEqual(Path(payload["session"]), session.resolve())
        self.assertTrue(Path(payload["run"]).is_absolute())
        self.assertTrue((Path(payload["run"]) / "run.json").is_file())
        self.assertEqual(json.loads((session / "session.json").read_text(encoding="utf-8"))["run"], payload["run"])
        self.assertEqual((session / "progress.md").read_text(encoding="utf-8"), ledger)
        self.assertEqual((session / "HANDOFF.md").read_text(encoding="utf-8"), handoff)
        self.assertEqual((session / "NEXT-SESSION-PROMPT.md").read_text(encoding="utf-8"), nxt)
        self.assertEqual(
            {key: run_state.replay(payload["run"])["controller"][key] for key in run_state.HOLDER_KEYS},
            {"workspace": "workspace", "pane": "pane", "session": run_state.canonical_path(session)},
        )

if __name__ == "__main__":
    unittest.main()
