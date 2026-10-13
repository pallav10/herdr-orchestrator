#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-ownership.py"


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def init_repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
    git(repo, "config", "user.email", "t@example.invalid")
    git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-m", "init")
    return repo, git(repo, "rev-parse", "HEAD")


class TestCheckOwnership(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, dict, str]:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
        )
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return proc.returncode, payload, proc.stderr

    def plant_attempt(self, run: Path, node: str, attempt: str, *, worktree: Path, base: str, globs: list[str]) -> Path:
        attempt_dir = run / "artifacts" / node / attempt
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "ownership.json").write_text(
            json.dumps({"slices": {node: globs}}) + "\n",
            encoding="utf-8",
        )
        (attempt_dir / "dispatch-identity.json").write_text(
            json.dumps({"worktree_path": str(worktree), "base": base}) + "\n",
            encoding="utf-8",
        )
        return attempt_dir

    def test_run_mode_derives_manifest_from_attempt_and_defaults_since_to_base(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, base = init_repo(root)
            dest = repo / "src" / "a" / "x.txt"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("x\n", encoding="utf-8")
            git(repo, "add", "src/a/x.txt")
            git(repo, "commit", "-m", "owned")
            run = root / "run"
            self.plant_attempt(run, "N", "1", worktree=repo, base=base, globs=["src/a/**"])

            code, out, err = self.run_cli("--run", str(run), "--node", "N")
            self.assertEqual(code, 0, err)
            abs_x = str(dest)
            self.assertEqual(out["files"], [abs_x])
            self.assertEqual(out["unowned"], [])

            (repo / "docs").mkdir()
            (repo / "docs" / "z.md").write_text("z\n", encoding="utf-8")
            code, out, err = self.run_cli("--run", str(run), "--node", "N")
            self.assertEqual(code, 3, err)
            abs_z = str(repo / "docs" / "z.md")
            self.assertIn(abs_z, out["unowned"])
            self.assertEqual(out["files"], sorted([abs_x, abs_z]))

    def test_since_alias_and_explicit_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, base = init_repo(root)
            dest = repo / "src" / "a" / "x.txt"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("x\n", encoding="utf-8")
            git(repo, "add", "src/a/x.txt")
            git(repo, "commit", "-m", "owned")
            run = root / "run"
            self.plant_attempt(run, "N", "1", worktree=repo, base=base, globs=["src/a/**"])
            self.plant_attempt(run, "N", "2", worktree=repo, base=base, globs=["docs/**"])

            code1, out1, err1 = self.run_cli("--run", str(run), "--node", "N", "--attempt", "1")
            self.assertEqual(code1, 0, err1)
            self.assertEqual(out1["files"], [str(dest)])
            self.assertEqual(out1["unowned"], [])
            self.assertEqual(out1["by_slice"]["N"], [str(dest)])

            code2, out2, err2 = self.run_cli("--run", str(run), "--node", "N")
            self.assertEqual(code2, 3, err2)
            self.assertIn(str(dest), out2["unowned"])
            self.assertEqual(out2["files"], [str(dest)])

            code_since, out_since, err_since = self.run_cli(
                "--run", str(run), "--node", "N", "--attempt", "1", "--since", base,
            )
            code_base, out_base, err_base = self.run_cli(
                "--run", str(run), "--node", "N", "--attempt", "1", "--base", base,
            )
            self.assertEqual(code_since, 0, err_since)
            self.assertEqual(code_base, 0, err_base)
            self.assertEqual(out_since, out_base)

            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"repos": [str(repo)], "slices": {"N": ["src/a/**"]}}) + "\n",
                encoding="utf-8",
            )
            code_since, out_since, err_since = self.run_cli("--manifest", str(manifest), "--since", base)
            code_base, out_base, err_base = self.run_cli("--manifest", str(manifest), "--base", base)
            self.assertEqual(code_since, code_base, err_since or err_base)
            self.assertEqual(out_since, out_base)

    def test_manifest_and_run_are_exclusive_and_one_required(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, base = init_repo(root)
            run = root / "run"
            self.plant_attempt(run, "N", "1", worktree=repo, base=base, globs=["src/a/**"])
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"repos": [str(repo)], "slices": {"N": ["src/a/**"]}}) + "\n",
                encoding="utf-8",
            )

            code, _, _ = self.run_cli("--manifest", str(manifest), "--run", str(run), "--node", "N")
            self.assertEqual(code, 2)

            code, _, _ = self.run_cli()
            self.assertEqual(code, 2)

            code, _, _ = self.run_cli("--node", "N")
            self.assertEqual(code, 2)

    def test_run_mode_defaults_since_to_promoted_branch_point(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            repo, base = init_repo(root)
            docs = repo / "docs"
            docs.mkdir()
            (docs / "z.md").write_text("z\n", encoding="utf-8")
            git(repo, "add", "docs/z.md")
            git(repo, "commit", "-m", "P")
            promoted = git(repo, "rev-parse", "HEAD")
            dest = repo / "src" / "a" / "x.txt"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("x\n", encoding="utf-8")
            git(repo, "add", "src/a/x.txt")
            git(repo, "commit", "-m", "owned")

            run = root / "run"
            ref = "refs/heads/herdr/test"
            self.plant_attempt(run, "N", "1", worktree=repo, base=base, globs=["src/a/**"])
            (run / "run.json").write_text(json.dumps({"path": str(run.resolve()), "promotion_ref": ref}) + "\n", encoding="utf-8")
            git(repo, "update-ref", ref, promoted)

            code, out, err = self.run_cli("--run", str(run), "--node", "N")
            self.assertEqual(code, 0, err)
            self.assertTrue(out["clean"])
            self.assertEqual(out["files"], [str(dest)])
            self.assertEqual(out["unowned"], [])

            code, out, err = self.run_cli("--run", str(run), "--node", "N", "--since", base)
            self.assertEqual(code, 3, err)
            self.assertIn(str(repo / "docs" / "z.md"), out["unowned"])


if __name__ == "__main__":
    unittest.main()
