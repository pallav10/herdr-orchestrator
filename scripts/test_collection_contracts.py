#!/usr/bin/env python3
"""Collection public-entrypoint regressions for receipt/VCS gates."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from testkit import FakeHerdr, HOLDER, commit_file, git, load_script, run_module, seed_repo
HOLDER_ARGS = ["--session", HOLDER["session"], "--holder-workspace", HOLDER["workspace"], "--holder-pane", HOLDER["pane"]]


def bind_lease(run: Path) -> None:
    (run / "events.jsonl").write_text(json.dumps({"type": "session_run_bound", **HOLDER}) + "\n", encoding="utf-8")




def diamond_nodes() -> dict:
    return {
        "B": {"id": "B", "executor": "herdr-agent", "outputs": [{"name": "patch", "type": "vcs"}]},
        "C": {"id": "C", "executor": "herdr-agent", "outputs": [{"name": "patch", "type": "vcs"}]},
        "D": {
            "id": "D",
            "executor": "herdr-agent",
            "depends_on": ["B", "C"],
            "inputs": [
                {"alias": "b", "from": "B", "output": "patch", "required": True},
                {"alias": "c", "from": "C", "output": "patch", "required": True},
            ],
            "outputs": [{"name": "integration", "type": "integration"}, {"name": "vcs", "type": "vcs"}],
        },
    }


def snapshot_items() -> list[dict]:
    return [
        {"alias": "b", "producer": "B", "producer_attempt": 1, "output": "patch", "path": "/tmp/b", "hash": "sha256:b"},
        {"alias": "c", "producer": "C", "producer_attempt": 1, "output": "patch", "path": "/tmp/c", "hash": "sha256:c"},
    ]


def receipt_items(kind: str = "bc") -> list[dict]:
    b = {"producer": "B", "attempt": 1, "output": "patch", "hash": "sha256:b"}
    c = {"producer": "C", "attempt": 1, "output": "patch", "hash": "sha256:c"}
    if kind == "bc":
        return [b, c]
    if kind == "cb":
        return [c, b]
    if kind == "extra":
        return [b, c, {"producer": "X", "attempt": 1, "output": "patch", "hash": "sha256:x"}]
    if kind == "dup":
        return [b, c, b]
    raise AssertionError(kind)


def events(run: Path) -> list[dict]:
    lines = (run / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestCollectionContracts(unittest.TestCase):
    def plant(
        self,
        tmp: Path,
        *,
        extra_commit: bool = True,
        receipt_kind: str = "bc",
        base_sha: str | None = None,
        commit: str | None = None,
        head: str | None = None,
        vcs_sha: str | None = None,
        identity_kind: str = "runtime-id",
        worker_id: str | None = None,
    ) -> tuple[Path, Path, dict, str, str]:
        repo = seed_repo(tmp / "repo")
        recorded = commit_file(repo, "a.txt", "one\n", "init")
        result = recorded
        if extra_commit:
            result = commit_file(repo, "a.txt", "two\n", "two")
        run = tmp / "run-abc"
        attempt = run / "artifacts" / "D" / "1"
        attempt.mkdir(parents=True)
        worktree = str(repo.resolve())
        (run / "run.json").write_text(
            json.dumps({
                "id": run.name,
                "job": "job",
                "schema_version": 1,
                "path": str(run.resolve()),
                "base_sha": recorded,
                "repo": worktree,
            })
            + "\n",
            encoding="utf-8",
        )
        (run / "graph.json").write_text(
            json.dumps({
                "schema_version": 1,
                "run": str(run.resolve()),
                "base_sha": recorded,
                "nodes": diamond_nodes(),
            })
            + "\n",
            encoding="utf-8",
        )
        bind_lease(run)
        (attempt / "consumed_artifacts.json").write_text(json.dumps(snapshot_items()) + "\n", encoding="utf-8")
        identity = {
            "workspace": "ws",
            "pane": "pane",
            "run": str(run.resolve()),
            "run_id": run.name,
            "node": "D",
            "attempt": "1",
            "worktree_path": worktree,
            "repo": worktree,
            "branch": git(repo, "branch", "--show-current") or "HEAD",
            "base": recorded,
            "tokens": {"provenance": "orchestrator", "run": run.name, "node": "D", "attempt": "1"},
        }
        if identity_kind == "managed-name":
            identity["identity_kind"] = "managed-name"
            identity["worker_id"] = worker_id or ("fo-" + "ab" * 12)
        else:
            identity["agent"] = "agent-id"
        (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
        sha = vcs_sha if vcs_sha is not None else result
        (attempt / "vcs.json").write_text(
            json.dumps({
                "repo": worktree,
                "worktree": worktree,
                "branch": identity["branch"],
                "base_sha": recorded,
                "result": {"form": "commit", "sha": sha},
            })
            + "\n",
            encoding="utf-8",
        )
        (attempt / "integration.json").write_text(
            json.dumps({
                "base_sha": recorded if base_sha is None else base_sha,
                "consumed": receipt_items(receipt_kind),
                "commit": result if commit is None else commit,
                "head": result if head is None else head,
            })
            + "\n",
            encoding="utf-8",
        )
        return run, attempt, identity, recorded, result

    def rewrite_receipts(self, attempt: Path, *, sha: str, base_sha: str, commit: str, head: str, receipt_kind: str = "bc") -> None:
        vcs = json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))
        vcs["result"]["sha"] = sha
        vcs["base_sha"] = base_sha
        (attempt / "vcs.json").write_text(json.dumps(vcs) + "\n", encoding="utf-8")
        (attempt / "integration.json").write_text(
            json.dumps({
                "base_sha": base_sha,
                "consumed": receipt_items(receipt_kind),
                "commit": commit,
                "head": head,
            })
            + "\n",
            encoding="utf-8",
        )


    def collect(
        self,
        tmp: Path,
        run: Path,
        identity: dict,
        extra: list[str] | None = None,
        agent: dict | None = None,
        workspace: dict | None = None,
        name: str = "worker",
        node: str = "D",
        summary_text: str = "done",
    ) -> tuple[int, str, str, bool]:
        module = load_script("collect-agent.py")
        session = tmp / "session.jsonl"
        session.write_text(
            json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": summary_text}]}})
            + "\n",
            encoding="utf-8",
        )
        original = module.herdr
        def get_agent(*args):
            payload = {
                "id": "agent-id", "name": args[2], "agent_status": "done",
                "agent_session": {"value": str(session)},
            }
            payload.update({"workspace_id": "ws", "pane_id": "pane"} if agent is None else agent)
            return {"result": {"agent": payload}}

        workspace_payload = {"tokens": identity["tokens"], "root_pane": {"pane_id": "pane"}}
        if workspace is not None:
            workspace_payload.update(workspace)
        module.herdr = FakeHerdr({
            ("agent", "get"): get_agent,
            ("workspace", "get"): {"result": {"workspace": workspace_payload}},
        })
        argv = ["collect-agent.py", name, "--run", str(run), "--node", node, "--attempt", "1", *HOLDER_ARGS]
        if extra:
            argv.extend(extra)
        try:
            code, out, err = run_module(module, argv)
        finally:
            module.herdr = original
        summary = run / "artifacts" / node / "1" / "summary.json"
        return code, out, err, summary.is_file()

    def test_collect_rejects_foreign_holder_before_summary(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, _, identity, _, _ = self.plant(tmp)
            code, out, err, wrote = self.collect(
                tmp, run, identity,
                extra=["--holder-pane", "other-pane"],
            )
            self.assertEqual(code, 3)
            self.assertFalse(out.strip())
            self.assertIn("lease", err)
            self.assertFalse(wrote)

    def test_collect_rejects_invalid_contracts_before_summary(self):
        cases = [
            ("wrong_base_sha", {"base_sha": "not-the-recorded-base"}, "integration.base_sha does not match recorded base"),
            ("commit_head_mismatch", {}, "integration commit/head mismatch"),
            ("reordered_vcs_consumed", {"receipt_kind": "cb"}, "integration.json missing consumed hash sha256:b"),
            ("extra_vcs_consumed", {"receipt_kind": "extra"}, "integration.consumed sequence mismatch"),
            ("duplicate_vcs_consumed", {"receipt_kind": "dup"}, "integration.consumed sequence mismatch"),
            ("orphan_commit", {"extra_commit": False}, "vcs.result.sha does not match worktree HEAD"),
            ("non_head_commit", {}, "vcs.result.sha does not match worktree HEAD"),
            ("integration_sha_not_vcs_commit", {}, "integration commit/head does not match promoted sha"),
            ("integration_diff_vcs", {}, "promotion requires a commit vcs result"),
            ("wrong_vcs_repo", {}, "vcs repo does not match dispatch identity"),
            ("missing_writer_repo", {}, "dispatch identity missing writer repo"),
            ("label_as_agent_id", {}, "agent id does not match dispatch identity"),
            ("name_only_workspace", {}, "agent workspace does not match dispatch identity"),
            ("name_only_pane", {}, "agent pane does not match dispatch identity"),
        ]
        for case, plant_args, expected_error in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as td:
                tmp = Path(td)
                run, attempt, identity, recorded, result = self.plant(tmp, **plant_args)
                collect_args = {}
                if case == "commit_head_mismatch":
                    self.rewrite_receipts(attempt, sha=result, base_sha=recorded, commit=result, head=recorded)
                elif case == "orphan_commit":
                    tree = git(tmp / "repo", "rev-parse", "HEAD^{tree}")
                    orphan = git(tmp / "repo", "commit-tree", tree, "-m", "orphan")
                    self.rewrite_receipts(attempt, sha=orphan, base_sha=recorded, commit=orphan, head=orphan)
                elif case == "non_head_commit":
                    self.rewrite_receipts(attempt, sha=recorded, base_sha=recorded, commit=recorded, head=recorded)
                elif case == "integration_sha_not_vcs_commit":
                    self.rewrite_receipts(attempt, sha=result, base_sha=recorded, commit=recorded, head=recorded)
                elif case == "integration_diff_vcs":
                    diff = attempt / "change.bin"
                    diff.write_bytes(b"change")
                    vcs = json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))
                    vcs["result"] = {"form": "diff", "path": str(diff), "sha256": "sha256:dead"}
                    (attempt / "vcs.json").write_text(json.dumps(vcs) + "\n", encoding="utf-8")
                elif case == "wrong_vcs_repo":
                    vcs = json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))
                    vcs["repo"] = "/tmp/other-repo"
                    (attempt / "vcs.json").write_text(json.dumps(vcs) + "\n", encoding="utf-8")
                elif case == "missing_writer_repo":
                    identity.pop("repo", None)
                    (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
                elif case == "label_as_agent_id":
                    identity["agent"] = "worker"
                    (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
                elif case == "name_only_workspace":
                    collect_args["agent"] = {"workspace": {"name": identity["workspace"]}, "pane_id": identity["pane"]}
                elif case == "name_only_pane":
                    collect_args["agent"] = {"workspace_id": identity["workspace"], "pane": {"name": identity["pane"]}}
                    collect_args["workspace"] = {"root_pane": {"name": identity["pane"]}}
                before = {p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}
                code, out, err, wrote = self.collect(tmp, run, identity, **collect_args)
                self.assertEqual(code, 3)
                self.assertFalse(out.strip())
                self.assertIn(expected_error, err)
                self.assertFalse(wrote)
                self.assertEqual({p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}, before)


    def test_collect_accepts_ordered_named_vcs_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, attempt, identity, _, result = self.plant(tmp)
            code, out, err, wrote = self.collect(tmp, run, identity)
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            agent = payload["agents"][0]
            self.assertTrue(wrote)
            self.assertEqual({row["name"] for row in agent["artifacts"]}, {"summary", "vcs", "integration"})
            self.assertTrue((attempt / "summary.json").is_file())
            self.assertEqual(json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))["result"]["sha"], result)


    def test_collect_retry_reuses_identical_summary(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, attempt, identity, _, _ = self.plant(tmp)
            code1, out1, err1, wrote1 = self.collect(tmp, run, identity)
            self.assertEqual(code1, 0, err1)
            self.assertTrue(wrote1)
            summary = attempt / "summary.json"
            payload1 = json.loads(out1)
            refs1 = payload1["agents"][0]["artifacts"]
            bytes1 = summary.read_bytes()
            mtime1 = summary.stat().st_mtime_ns
            code2, out2, err2, wrote2 = self.collect(tmp, run, identity)
            self.assertEqual(code2, 0, err2)
            self.assertTrue(wrote2)
            payload2 = json.loads(out2)
            self.assertEqual(payload2["agents"][0]["artifacts"], refs1)
            self.assertEqual(payload2["agents"][0]["summary_hash"], payload1["agents"][0]["summary_hash"])
            self.assertEqual(summary.read_bytes(), bytes1)
            self.assertEqual(summary.stat().st_mtime_ns, mtime1)

    def test_collect_refuses_conflicting_summary_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, attempt, identity, _, _ = self.plant(tmp)
            summary = attempt / "summary.json"
            summary.write_text(
                json.dumps({"name": "worker", "question": None, "status": "idle", "summary": "other"}) + "\n",
                encoding="utf-8",
            )
            before = summary.read_bytes()
            mtime = summary.stat().st_mtime_ns
            code, out, err, wrote = self.collect(tmp, run, identity)
            self.assertEqual(code, 3)
            self.assertFalse(out.strip())
            self.assertTrue(err.strip())
            self.assertTrue(wrote)
            self.assertEqual(summary.read_bytes(), before)
            self.assertEqual(summary.stat().st_mtime_ns, mtime)

    def test_collect_refuses_malformed_summary_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, attempt, identity, _, _ = self.plant(tmp)
            summary = attempt / "summary.json"
            summary.write_bytes(b'{"name":"worker","status":')
            before = summary.read_bytes()
            mtime = summary.stat().st_mtime_ns
            code, out, err, wrote = self.collect(tmp, run, identity)
            self.assertEqual(code, 3)
            self.assertFalse(out.strip())
            self.assertTrue(err.strip())
            self.assertTrue(wrote)
            self.assertEqual(summary.read_bytes(), before)
            self.assertEqual(summary.stat().st_mtime_ns, mtime)

    def test_collect_status_only_does_not_write_summary(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, _, identity, _, _ = self.plant(tmp)
            code, out, err, wrote = self.collect(tmp, run, identity, extra=["--status-only"])
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(payload["agents"][0]["source"], "status")
            self.assertNotIn("artifacts", payload["agents"][0])
            self.assertFalse(wrote)

    def test_collect_accepts_managed_name_identity(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            worker_id = "fo-" + "cd" * 12
            run, attempt, identity, _, result = self.plant(tmp, identity_kind="managed-name", worker_id=worker_id)
            code, out, err, wrote = self.collect(tmp, run, identity, name=worker_id)
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            agent = payload["agents"][0]
            self.assertTrue(wrote)
            self.assertEqual(agent["name"], worker_id)
            self.assertEqual({row["name"] for row in agent["artifacts"]}, {"summary", "vcs", "integration"})
            self.assertTrue((attempt / "summary.json").is_file())
            self.assertEqual(json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))["result"]["sha"], result)

    def test_collect_rejects_managed_name_mismatches_before_summary(self):
        worker_id = "fo-" + "11" * 12
        other_id = "fo-" + "22" * 12
        cases = {
            "wrong worker id": {"name": other_id, "workspace_id": "ws", "pane_id": "pane"},
            "wrong workspace": {"name": worker_id, "workspace_id": "other-ws", "pane_id": "pane"},
            "wrong pane": {"name": worker_id, "workspace_id": "ws", "pane_id": "other-pane"},
        }
        for label, agent_override in cases.items():
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as td:
                    tmp = Path(td)
                    run, attempt, identity, _, _ = self.plant(tmp, identity_kind="managed-name", worker_id=worker_id)
                    before = {p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}
                    code, out, err, wrote = self.collect(tmp, run, identity, name=worker_id, agent=agent_override)
                    self.assertEqual(code, 3)
                    self.assertFalse(out.strip())
                    self.assertTrue(err.strip())
                    self.assertFalse(wrote)
                    self.assertEqual({p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}, before)

    def test_collect_rejects_managed_name_token_mismatch_before_summary(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            worker_id = "fo-" + "33" * 12
            run, attempt, identity, _, _ = self.plant(tmp, identity_kind="managed-name", worker_id=worker_id)
            before = {p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}
            identity["tokens"]["attempt"] = "2"
            (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            code, out, err, wrote = self.collect(tmp, run, identity, name=worker_id)
            self.assertEqual(code, 3)
            self.assertFalse(out.strip())
            self.assertTrue(err.strip())
            self.assertFalse(wrote)
            self.assertEqual({p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file() and p.name != "dispatch-identity.json"}, {k: v for k, v in before.items() if k != "dispatch-identity.json"})

    def test_collect_rejects_mixed_identity_discriminator_before_summary(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            worker_id = "fo-" + "44" * 12
            run, attempt, identity, _, _ = self.plant(tmp, identity_kind="managed-name", worker_id=worker_id)
            identity["agent"] = "agent-id"
            (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            code, out, err, wrote = self.collect(tmp, run, identity, name=worker_id)
            self.assertEqual(code, 3)
            self.assertFalse(out.strip())
            self.assertTrue(err.strip())
            self.assertFalse(wrote)

    def test_collect_legacy_identity_not_reinterpreted_as_managed_name(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            worker_id_like = "fo-" + "55" * 12
            run, attempt, identity, _, _ = self.plant(tmp)
            identity["agent"] = worker_id_like
            (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            code, out, err, wrote = self.collect(
                tmp,
                run,
                identity,
                name=worker_id_like,
                agent={"name": worker_id_like, "workspace_id": identity["workspace"], "pane_id": identity["pane"]},
            )
            self.assertEqual(code, 3)
            self.assertFalse(out.strip())
            self.assertTrue(err.strip())
            self.assertFalse(wrote)

    def test_publish_vcs_writes_none_when_head_equals_base(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = seed_repo(tmp / "repo")
            recorded = commit_file(repo, "a.txt", "one\n", "init")
            run = tmp / "run-abc"
            attempt = run / "artifacts" / "B" / "1"
            attempt.mkdir(parents=True)
            worktree = str(repo.resolve())
            branch = git(repo, "branch", "--show-current") or "HEAD"
            (run / "run.json").write_text(
                json.dumps({
                    "id": run.name,
                    "job": "job",
                    "schema_version": 1,
                    "path": str(run.resolve()),
                    "base_sha": recorded,
                    "repo": worktree,
                })
                + "\n",
                encoding="utf-8",
            )
            (run / "graph.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "run": str(run.resolve()),
                    "base_sha": recorded,
                    "nodes": {
                        "B": {"id": "B", "executor": "herdr-agent", "outputs": [{"name": "patch", "type": "vcs"}]},
                    },
                })
                + "\n",
                encoding="utf-8",
            )
            bind_lease(run)
            identity = {
                "workspace": "ws",
                "pane": "pane",
                "run": str(run.resolve()),
                "run_id": run.name,
                "node": "B",
                "attempt": "1",
                "worktree_path": worktree,
                "repo": worktree,
                "branch": branch,
                "base": recorded,
                "tokens": {"provenance": "orchestrator", "run": run.name, "node": "B", "attempt": "1"},
                "agent": "agent-id",
            }
            (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            code, out, err, wrote = self.collect(tmp, run, identity, extra=["--publish-vcs"], node="B")
            self.assertEqual(code, 0, err)
            self.assertTrue(wrote)
            vcs = json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))
            self.assertEqual(vcs["result"], {"form": "none"})
            payload = json.loads(out)
            vcs_rows = [row for row in payload["agents"][0]["artifacts"] if row["path"].endswith("vcs.json")]
            self.assertEqual(len(vcs_rows), 1)
            self.assertTrue(vcs_rows[0]["hash"].startswith("sha256:"))
            code2, out2, err2, wrote2 = self.collect(tmp, run, identity, extra=["--publish-vcs"], node="B")
            self.assertEqual(code2, 0, err2)
            self.assertTrue(wrote2)
            self.assertEqual(json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))["result"], {"form": "none"})

    def plant_writer(self, tmp: Path) -> tuple[Path, Path, dict, Path]:
        """Minimal single-writer run: node B with a vcs output over its own repo."""
        repo = seed_repo(tmp / "repo")
        recorded = commit_file(repo, "a.txt", "one\n", "init")
        run = tmp / "run-abc"
        attempt = run / "artifacts" / "B" / "1"
        attempt.mkdir(parents=True)
        worktree = str(repo.resolve())
        (run / "run.json").write_text(
            json.dumps({
                "id": run.name,
                "job": "job",
                "schema_version": 1,
                "path": str(run.resolve()),
                "base_sha": recorded,
                "repo": worktree,
            })
            + "\n",
            encoding="utf-8",
        )
        (run / "graph.json").write_text(
            json.dumps({
                "schema_version": 1,
                "run": str(run.resolve()),
                "base_sha": recorded,
                "nodes": {"B": {"id": "B", "executor": "herdr-agent", "outputs": [{"name": "patch", "type": "vcs"}]}},
            })
            + "\n",
            encoding="utf-8",
        )
        bind_lease(run)
        identity = {
            "workspace": "ws",
            "pane": "pane",
            "run": str(run.resolve()),
            "run_id": run.name,
            "node": "B",
            "attempt": "1",
            "worktree_path": worktree,
            "repo": worktree,
            "branch": git(repo, "branch", "--show-current") or "HEAD",
            "base": recorded,
            "tokens": {"provenance": "orchestrator", "run": run.name, "node": "B", "attempt": "1"},
            "agent": "agent-id",
        }
        (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
        return run, attempt, identity, repo

    def test_publish_vcs_force_replaces_a_stale_none_receipt(self):
        # run 20260918-220934: T and C were collected before their workers had run, so
        # the receipt froze at form none and no later --force ever reconsidered it.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, attempt, identity, repo = self.plant_writer(tmp)
            vcs_path = attempt / "vcs.json"

            code, _, err, _ = self.collect(tmp, run, identity, extra=["--publish-vcs"], node="B")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(vcs_path.read_text(encoding="utf-8"))["result"], {"form": "none"})
            stale = vcs_path.read_bytes()

            (repo / "a.txt").write_text("the work the agent never did\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-m", "real work")
            head = git(repo, "rev-parse", "HEAD")

            code, _, err, _ = self.collect(tmp, run, identity, extra=["--publish-vcs"], node="B")
            self.assertEqual(code, 3)
            self.assertIn("already exists with different content", err)
            self.assertEqual(vcs_path.read_bytes(), stale)
            self.assertEqual([row for row in events(run) if row.get("type") == "artifact_forced"], [])

            code, _, err, _ = self.collect(tmp, run, identity, extra=["--publish-vcs", "--force"], node="B")
            self.assertEqual(code, 0, err)
            self.assertEqual(
                json.loads(vcs_path.read_text(encoding="utf-8"))["result"],
                {"form": "commit", "sha": head},
            )
            forced = [row for row in events(run) if row.get("type") == "artifact_forced"]
            self.assertEqual([row["forced"] for row in forced], [["vcs.json"]])
            self.assertEqual((forced[0]["node"], forced[0]["attempt"]), ("B", 1))


    def test_publish_vcs_writes_none_at_promoted_sha(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = seed_repo(tmp / "repo")
            recorded = commit_file(repo, "a.txt", "one\n", "init")
            (repo / "a.txt").write_text("two\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-m", "two")
            promoted = git(repo, "rev-parse", "HEAD")
            run = tmp / "run-abc"
            attempt = run / "artifacts" / "B" / "1"
            attempt.mkdir(parents=True)
            worktree = str(repo.resolve())
            branch = git(repo, "branch", "--show-current") or "HEAD"
            promotion_ref = f"refs/heads/herdr/{run.name}"
            git(repo, "update-ref", promotion_ref, promoted)
            (run / "run.json").write_text(
                json.dumps({
                    "id": run.name,
                    "job": "job",
                    "schema_version": 1,
                    "path": str(run.resolve()),
                    "base_sha": recorded,
                    "repo": worktree,
                    "promotion_ref": promotion_ref,
                })
                + "\n",
                encoding="utf-8",
            )
            (run / "graph.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "run": str(run.resolve()),
                    "base_sha": recorded,
                    "nodes": {
                        "B": {"id": "B", "executor": "herdr-agent", "outputs": [{"name": "patch", "type": "vcs"}]},
                    },
                })
                + "\n",
                encoding="utf-8",
            )
            bind_lease(run)
            identity = {
                "workspace": "ws",
                "pane": "pane",
                "run": str(run.resolve()),
                "run_id": run.name,
                "node": "B",
                "attempt": "1",
                "worktree_path": worktree,
                "repo": worktree,
                "branch": branch,
                "base": recorded,
                "tokens": {"provenance": "orchestrator", "run": run.name, "node": "B", "attempt": "1"},
                "agent": "agent-id",
            }
            (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            code, out, err, wrote = self.collect(tmp, run, identity, extra=["--publish-vcs"], node="B")
            self.assertEqual(code, 0, err)
            self.assertTrue(wrote)
            vcs = json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))
            self.assertEqual(vcs["result"], {"form": "none"})
            (repo / "a.txt").write_text("three\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-m", "three")
            beyond = git(repo, "rev-parse", "HEAD")
            (attempt / "vcs.json").unlink()
            code2, _, err2, wrote2 = self.collect(tmp, run, identity, extra=["--publish-vcs"], node="B")
            self.assertEqual(code2, 0, err2)
            self.assertTrue(wrote2)
            vcs2 = json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))
            self.assertEqual(vcs2["result"], {"form": "commit", "sha": beyond})

    def test_publish_vcs_reuses_commit_receipt_after_promotion(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = seed_repo(tmp / "repo")
            recorded = commit_file(repo, "a.txt", "one\n", "init")
            (repo / "a.txt").write_text("two\n", encoding="utf-8")
            git(repo, "add", "a.txt")
            git(repo, "commit", "-m", "two")
            sha = git(repo, "rev-parse", "HEAD")
            run = tmp / "run-abc"
            attempt = run / "artifacts" / "B" / "1"
            attempt.mkdir(parents=True)
            worktree = str(repo.resolve())
            branch = git(repo, "branch", "--show-current") or "HEAD"
            (run / "run.json").write_text(
                json.dumps({
                    "id": run.name,
                    "job": "job",
                    "schema_version": 1,
                    "path": str(run.resolve()),
                    "base_sha": recorded,
                    "repo": worktree,
                })
                + "\n",
                encoding="utf-8",
            )
            (run / "graph.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "run": str(run.resolve()),
                    "base_sha": recorded,
                    "nodes": {
                        "B": {"id": "B", "executor": "herdr-agent", "outputs": [{"name": "patch", "type": "vcs"}]},
                    },
                })
                + "\n",
                encoding="utf-8",
            )
            bind_lease(run)
            identity = {
                "workspace": "ws",
                "pane": "pane",
                "run": str(run.resolve()),
                "run_id": run.name,
                "node": "B",
                "attempt": "1",
                "worktree_path": worktree,
                "repo": worktree,
                "branch": branch,
                "base": recorded,
                "tokens": {"provenance": "orchestrator", "run": run.name, "node": "B", "attempt": "1"},
                "agent": "agent-id",
            }
            (attempt / "dispatch-identity.json").write_text(json.dumps(identity) + "\n", encoding="utf-8")
            code, _, err, wrote = self.collect(tmp, run, identity, extra=["--publish-vcs"], node="B")
            self.assertEqual(code, 0, err)
            self.assertTrue(wrote)
            vcs = json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))
            self.assertEqual(vcs["result"], {"form": "commit", "sha": sha})
            promotion_ref = f"refs/heads/herdr/{run.name}"
            git(repo, "update-ref", promotion_ref, sha)
            run_doc = json.loads((run / "run.json").read_text(encoding="utf-8"))
            run_doc["promotion_ref"] = promotion_ref
            (run / "run.json").write_text(json.dumps(run_doc) + "\n", encoding="utf-8")
            code2, _, err2, wrote2 = self.collect(tmp, run, identity, extra=["--publish-vcs"], node="B")
            self.assertEqual(code2, 0, err2)
            self.assertTrue(wrote2)
            self.assertEqual(
                json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))["result"],
                {"form": "commit", "sha": sha},
            )
            code3, _, err3, wrote3 = self.collect(tmp, run, identity, extra=["--publish-vcs", "--force"], node="B")
            self.assertEqual(code3, 0, err3)
            self.assertTrue(wrote3)
            self.assertEqual(
                json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))["result"],
                {"form": "commit", "sha": sha},
            )

    def test_collect_refuses_none_vcs_for_integrator(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, attempt, identity, _, _ = self.plant(tmp, extra_commit=False)
            vcs = json.loads((attempt / "vcs.json").read_text(encoding="utf-8"))
            vcs["result"] = {"form": "none"}
            (attempt / "vcs.json").write_text(json.dumps(vcs) + "\n", encoding="utf-8")
            code, out, err, wrote = self.collect(tmp, run, identity)
            self.assertEqual(code, 3)
            self.assertIn("promotion requires a commit vcs result", err)
            self.assertFalse(wrote)

    def test_collect_force_overwrites_summary(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, attempt, identity, _, _ = self.plant(tmp)
            code1, out1, err1, wrote1 = self.collect(tmp, run, identity)
            self.assertEqual(code1, 0, err1)
            self.assertTrue(wrote1)
            hash1 = json.loads(out1)["agents"][0]["summary_hash"]
            summary = attempt / "summary.json"
            first = json.loads(summary.read_text(encoding="utf-8"))
            self.assertEqual(first["summary"], "done")
            code2, _, err2, _ = self.collect(tmp, run, identity, summary_text="revised")
            self.assertEqual(code2, 3)
            self.assertIn("summary.json already exists", err2)
            self.assertEqual(json.loads(summary.read_text(encoding="utf-8")), first)
            code3, out3, err3, _ = self.collect(tmp, run, identity, extra=["--force"], summary_text="revised")
            self.assertEqual(code3, 0, err3)
            payload3 = json.loads(out3)
            self.assertNotEqual(payload3["agents"][0]["summary_hash"], hash1)
            self.assertEqual(json.loads(summary.read_text(encoding="utf-8"))["summary"], "revised")
            code4, _, _, _ = self.collect(tmp, run, identity, extra=["--force", "--status-only"])
            self.assertEqual(code4, 2)

    def test_collect_caps_stdout_but_keeps_full_summary_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            run, attempt, identity, _, _ = self.plant(tmp)
            summary = "s" * 5000
            code, out, err, wrote = self.collect(tmp, run, identity, summary_text=summary)
            self.assertEqual(code, 0, err)
            self.assertTrue(wrote)
            agent = json.loads(out)["agents"][0]
            self.assertEqual(agent["summary"], summary[:1500])
            self.assertIs(agent["summary_truncated"], True)
            self.assertEqual(json.loads((attempt / "summary.json").read_text())["summary"], summary)
            code, out, err, _ = self.collect(tmp, run, identity, summary_text=summary, extra=["--full-summary"])
            self.assertEqual(code, 0, err)
            agent = json.loads(out)["agents"][0]
            self.assertEqual(agent["summary"], summary)
            self.assertNotIn("summary_truncated", agent)



class TestSummaryTailContracts(unittest.TestCase):
    def test_fifty_megabyte_tail_reads_less_than_one_megabyte(self):
        from unittest.mock import patch
        module = load_script("collect-agent.py")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "session.jsonl"
            summary = "é" * 40000
            with path.open("wb") as fh:
                chunk = (json.dumps({"type": "message", "message": {"role": "user", "content": "x" * 950}}) + "\n").encode() * 1024
                for _ in range(52):
                    fh.write(chunk)
                tail = (json.dumps({"type": "assistant", "message": {"content": summary}}, ensure_ascii=False) + "\n").encode()
                suffix = b'{"type":"message","message":{"role":"assistant","content":[{"type":"toolCall"}]}}\n'
                suffix += b'{"type":"message","message":{"role":"assistant","content":'
                # Force the first 64KB boundary into the middle of a two-byte character.
                start = tail.index("é".encode())
                if (len(tail) + len(suffix) - 64 * 1024 - start) % 2 == 0:
                    suffix += b"x"
                fh.write(tail + suffix)
            self.assertGreater(path.stat().st_size, 50 * 1024 * 1024)
            bytes_read = 0
            real_open = Path.open

            class CountingFile:
                def __enter__(self):
                    self.fh = real_open(path, "rb")
                    return self

                def __exit__(self, *args):
                    self.fh.close()

                def seek(self, *args):
                    return self.fh.seek(*args)

                def tell(self):
                    return self.fh.tell()

                def read(self, size):
                    nonlocal bytes_read
                    value = self.fh.read(size)
                    bytes_read += len(value)
                    return value

            with patch.object(Path, "open", return_value=CountingFile()):
                self.assertEqual(module.parse_session_summary(path), summary)
            self.assertLess(bytes_read, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
