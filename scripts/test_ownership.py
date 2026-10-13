#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ownership
from testkit import load_script

check_ownership = load_script("check-ownership.py")


class NormalizeManifestTest(unittest.TestCase):
    def test_nested_paths_shape(self):
        repos, slices = ownership.normalize_manifest(
            {"repos": ["/repo"], "slices": {"a": {"paths": ["src/a/**"]}}}
        )
        self.assertEqual(repos, ["/repo"])
        self.assertEqual(slices, {"a": ["src/a/**"]})

    def test_direct_array_shape_is_equivalent(self):
        _, slices = ownership.normalize_manifest({"repos": ["/repo"], "slices": {"a": ["src/a/**"]}})
        self.assertEqual(slices, {"a": ["src/a/**"]})

    def test_relative_repo_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "absolute"):
            ownership.normalize_manifest({"repos": ["repo"], "slices": {"a": ["x"]}})

    def test_empty_slice_paths_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "non-empty paths"):
            ownership.normalize_manifest({"repos": ["/repo"], "slices": {"a": {"paths": []}}})

    def test_missing_sections_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "repos"):
            ownership.normalize_manifest({"slices": {"a": ["x"]}})
        with self.assertRaisesRegex(ValueError, "slices"):
            ownership.normalize_manifest({"repos": ["/repo"]})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            ownership.normalize_manifest([{"repos": ["/repo"]}])


class PartitionTest(unittest.TestCase):
    slices = {
        "a": ["src/a/*", "shared/*"],
        "b": ["src/b/*", "shared/*"],
    }

    def test_partitions_owned_overlapping_and_unowned(self):
        unowned, overlaps, by_slice = ownership.partition_paths_by_slice(
            ["src/b/z.py", "src/a/y.py", "shared/s.py", "docs/readme.md"],
            "/repo",
            self.slices,
        )
        self.assertEqual(unowned, ["/repo/docs/readme.md"])
        self.assertEqual(overlaps, [{"path": "/repo/shared/s.py", "slices": ["a", "b"]}])
        self.assertEqual(by_slice, {"a": ["/repo/src/a/y.py"], "b": ["/repo/src/b/z.py"]})

    def test_overlapping_path_is_not_also_attributed_to_a_slice(self):
        _, _, by_slice = ownership.partition_paths_by_slice(["shared/s.py"], "/repo", self.slices)
        self.assertEqual(by_slice, {"a": [], "b": []})

    def test_output_is_sorted_and_absolute(self):
        unowned, _, by_slice = ownership.partition_paths_by_slice(
            ["src/a/b.py", "src/a/a.py", "d/2", "d/1"], "/repo", self.slices
        )
        self.assertEqual(unowned, ["/repo/d/1", "/repo/d/2"])
        self.assertEqual(by_slice["a"], ["/repo/src/a/a.py", "/repo/src/a/b.py"])

    def test_glob_matches_relative_path_not_absolute(self):
        unowned, _, by_slice = ownership.partition_paths_by_slice(
            ["src/a/x.py"], "/some/other/root", {"a": ["src/a/*"]}
        )
        self.assertEqual(unowned, [])
        self.assertEqual(by_slice["a"], ["/some/other/root/src/a/x.py"])


class ResolveDiffBaseTest(unittest.TestCase):
    def stub_git(self, rc: int, stdout: str):
        patcher = mock.patch.object(
            ownership.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=""),
        )
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_merge_base_wins_over_identity_base(self):
        run = self.stub_git(0, "deadbeef\n")
        self.assertEqual(ownership.resolve_diff_base("/wt", "origin/promoted", "identitysha"), "deadbeef")
        self.assertEqual(
            run.call_args[0][0],
            ["git", "-C", "/wt", "merge-base", "origin/promoted", "HEAD"],
        )

    def test_falls_back_to_base_sha_when_merge_base_fails(self):
        self.stub_git(128, "")
        self.assertEqual(ownership.resolve_diff_base("/wt", "origin/promoted", "identitysha"), "identitysha")

    def test_falls_back_when_merge_base_prints_nothing_on_success(self):
        self.stub_git(0, "\n")
        self.assertEqual(ownership.resolve_diff_base("/wt", "origin/promoted", "identitysha"), "identitysha")

    def test_no_worktree_or_no_promotion_ref_skips_git_entirely(self):
        run = self.stub_git(0, "deadbeef\n")
        self.assertEqual(ownership.resolve_diff_base(None, "origin/promoted", "base"), "base")
        self.assertEqual(ownership.resolve_diff_base("/wt", None, "base"), "base")
        self.assertEqual(ownership.resolve_diff_base("/wt", "", "base"), "base")
        run.assert_not_called()


class LoadManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_missing_file_raises_valueerror(self):
        with self.assertRaisesRegex(ValueError, "cannot read manifest"):
            ownership.load_manifest(self.root / "nope.json")

    def test_malformed_json_raises_valueerror(self):
        path = self.root / "m.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            ownership.load_manifest(path)

    def test_valid_file_is_normalized(self):
        path = self.root / "m.json"
        path.write_text(json.dumps({"repos": ["/repo"], "slices": {"a": {"paths": ["x/*"]}}}), encoding="utf-8")
        self.assertEqual(ownership.load_manifest(path), (["/repo"], {"a": ["x/*"]}))


class LoadRunManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def plant(self, node: str, attempt: str, *, ownership_text: str, identity_text: str) -> Path:
        d = self.run / "artifacts" / node / attempt
        d.mkdir(parents=True)
        (d / "ownership.json").write_text(ownership_text, encoding="utf-8")
        (d / "dispatch-identity.json").write_text(identity_text, encoding="utf-8")
        return d

    def test_defaults_to_highest_numeric_attempt(self):
        for attempt, wt in (("2", "/wt2"), ("10", "/wt10"), ("notanattempt", "/wtx")):
            self.plant(
                "P",
                attempt,
                ownership_text=json.dumps({"slices": {"P": ["src/*"]}}),
                identity_text=json.dumps({"worktree_path": wt, "base": "sha-" + attempt}),
            )
        repos, slices, base = check_ownership.load_run_manifest(self.run, "P", None)
        self.assertEqual(repos, ["/wt10"])
        self.assertEqual(slices, {"P": ["src/*"]})
        self.assertEqual(base, "sha-10")

    def test_missing_node_dir_raises_filenotfound(self):
        with self.assertRaises(FileNotFoundError):
            check_ownership.load_run_manifest(self.run, "P", None)

    def test_missing_identity_file_names_the_missing_path(self):
        d = self.run / "artifacts" / "P" / "1"
        d.mkdir(parents=True)
        (d / "ownership.json").write_text(json.dumps({"slices": {"P": ["src/*"]}}), encoding="utf-8")
        with self.assertRaises(FileNotFoundError) as ctx:
            check_ownership.load_run_manifest(self.run, "P", "1")
        self.assertIn("dispatch-identity.json", str(ctx.exception))

    def test_malformed_attempt_json_raises_valueerror(self):
        self.plant(
            "P",
            "1",
            ownership_text="{oops",
            identity_text=json.dumps({"worktree_path": "/wt", "base": "sha"}),
        )
        with self.assertRaisesRegex(ValueError, "not valid"):
            check_ownership.load_run_manifest(self.run, "P", "1")

    def test_missing_worktree_path_is_rejected_by_normalization(self):
        self.plant(
            "P",
            "1",
            ownership_text=json.dumps({"slices": {"P": ["src/*"]}}),
            identity_text=json.dumps({"base": "sha"}),
        )
        with self.assertRaises(ValueError):
            check_ownership.load_run_manifest(self.run, "P", "1")

    def test_absent_base_is_none(self):
        self.plant(
            "P",
            "1",
            ownership_text=json.dumps({"slices": {"P": ["src/*"]}}),
            identity_text=json.dumps({"worktree_path": "/wt", "base": ""}),
        )
        _, _, base = check_ownership.load_run_manifest(self.run, "P", "1")
        self.assertIsNone(base)


if __name__ == "__main__":
    unittest.main()
