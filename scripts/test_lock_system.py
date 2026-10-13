#!/usr/bin/env python3
"""Fallback §7.5 system checks; temporary runs and a process-backed Herdr sandbox.

Run with: python3 scripts/test_lock_system.py
No live Herdr is invoked. Pipes identify acquisition attempts and protected
operations; watchdogs bound every child. Rotation's positive context gate needs
parent-owned live verification: this suite never manufactures context evidence.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()
sys.path.insert(0, str(SCRIPTS))
WATCHDOG = 15
WAIT = 0.5
CLI_NAMES = (
    "spawn-agent.py", "prompt-agent.py", "dispatch-agent.py", "retire-agents.py",
    "collect-agent.py", "finish-agent.py", "ack-handoff.py", "rotate.py",
    "reconcile.py", "init-docs.py",
)


def send(message):
    fd = os.environ.get("LOCK_TEST_REPORT")
    if fd is not None:
        os.write(int(fd), (json.dumps(message) + "\n").encode())


def barrier(label):
    send(label)
    ready, _, _ = select.select([int(os.environ["LOCK_TEST_RELEASE"])], [], [], WATCHDOG)
    if not ready or os.read(int(os.environ["LOCK_TEST_RELEASE"]), 1) != b"x":
        raise RuntimeError(f"watchdog at {label}")


def clean_child_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("LOCK_TEST_")}


def sandbox_call(*argv):
    """An actual external process, never the installed Herdr executable."""
    proc = subprocess.run(
        [sys.executable, str(SELF), "--backend", *map(str, argv)],
        env=clean_child_env(), capture_output=True, text=True, timeout=WATCHDOG,
    )
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip())
    result = json.loads(proc.stdout)
    if "error" in result:
        exc = RuntimeError(result["error"])
        exc.code = result["code"]
        raise exc
    if os.environ.get("LOCK_TEST_EFFECT") == " ".join(argv[:2]):
        barrier("effect:" + " ".join(argv[:2]))
    return result


def sandbox_child(argv, child_name=None, *, timeout=None):
    import cli_common
    command = [sys.executable, str(SELF), "--child", "cli", Path(argv[1]).name, *argv[2:]]
    env = clean_child_env()
    config = json.loads(env["LOCK_CONFIG"])
    config.pop("boundary", None)
    env["LOCK_CONFIG"] = json.dumps(config)
    with Path(os.environ["LOCK_SANDBOX"]).joinpath("children.jsonl").open("a") as fh:
        fh.write(json.dumps(argv) + "\n")
    proc = subprocess.run(command, env=env, capture_output=True, text=True,
                          timeout=min(timeout or WATCHDOG, WATCHDOG))
    if proc.returncode:
        raise cli_common.ChildFailed(proc.returncode, proc.stderr)
    return cli_common.parse_child_json(proc.stdout, child_name or Path(argv[1]).name)


def backend(argv):
    root = Path(os.environ["LOCK_SANDBOX"])
    state_path = root / "world.json"
    state = json.loads(state_path.read_text())
    with (root / "calls.jsonl").open("a") as fh:
        fh.write(json.dumps(argv) + "\n")
    verb = tuple(argv[:2])
    name = argv[2] if len(argv) > 2 else "h"
    result = {}
    if verb == ("workspace", "create"):
        state["surfaces"].append({"label": argv[argv.index("--label") + 1], "workspace_id": "ws-h", "pane_id": "pane-h"})
        result = {"workspace": {"workspace_id": "ws-h"}, "root_pane": {"pane_id": "pane-h"}}
    elif verb == ("agent", "start"):
        state["workers"].append(name)
        result = {"agent": {"id": "agent-h", "name": name}}
    elif verb == ("agent", "get"):
        if name != state["legacy_agent"] and name not in state["workers"]:
            print(json.dumps({"error": "agent not found", "code": "agent_not_found"}))
            return 0
        result = {"agent": {"id": state["legacy_agent"], "agent_id": state["legacy_agent"], "name": name,
                  "workspace_id": state["workspace"], "pane_id": state["pane"], "agent_status": "done",
                  "model": "provider/test", "agent_session": {"value": str(root / "trace.jsonl")}}}
    elif verb == ("agent", "list"):
        result = {"agents": [
            {"id": state["legacy_agent"] if name == state["legacy_agent"] else "agent-h",
             "name": name, "workspace_id": state["workspace"], "pane_id": state["pane"],
             "agent_status": "done", "model": "provider/test"}
            for name in dict.fromkeys([state["legacy_agent"], *state["workers"]])
        ]}
    elif verb == ("workspace", "get"):
        result = {"workspace": {"workspace_id": name, "tokens": state["tokens"],
                  "root_pane": {"pane_id": state["pane"]}}}
    elif verb == ("workspace", "list"):
        result = {"workspaces": [{"workspace_id": x["workspace_id"], "label": x["label"],
                                  "tokens": state["tokens"]} for x in state["surfaces"]]}
    elif verb == ("workspace", "report-metadata"):
        for item in argv[3:]:
            if "=" in item:
                key, value = item.split("=", 1)
                state["tokens"][key] = value
    elif verb == ("workspace", "rename"):
        for surface in state["surfaces"]:
            if surface["workspace_id"] == name:
                surface["label"] = argv[3]
    elif verb == ("agent", "prompt"):
        state["prompts"].append(argv)
    elif verb == ("workspace", "close"):
        state["closed"].append(name)
    elif verb not in {("pane", "rename"), ("workspace", "rename"),
                      ("agent", "wait"), ("agent", "send-keys")}:
        raise RuntimeError(f"unsupported sandbox operation: {argv}")
    state_path.write_text(json.dumps(state))
    print(json.dumps({"result": result}))
    return 0


def instrument_wait():
    original = fcntl.flock
    reported = False

    def flock(fd, operation):
        nonlocal reported
        try:
            return original(fd, operation)
        except OSError as exc:
            if operation & fcntl.LOCK_NB and exc.errno in (errno.EAGAIN, errno.EACCES) and not reported:
                reported = True
                send("waiting")
            raise
    fcntl.flock = flock


def instrument_boundary(module, dotted, occurrence=1, *, after=False, path_name=None):
    target = module
    parts = dotted.split(".")
    for part in parts[:-1]:
        target = getattr(target, part)
    name = parts[-1]
    original = getattr(target, name)
    calls = 0

    def wrapped(*args, **kwargs):
        nonlocal calls
        if path_name is not None and (not args or Path(args[0]).name != path_name):
            return original(*args, **kwargs)
        calls += 1
        if calls == occurrence and not after:
            barrier("boundary:" + dotted)
        result = original(*args, **kwargs)
        if calls == occurrence and after:
            barrier("boundary:" + dotted)
        return result
    setattr(target, name, wrapped)


def child(action, args):
    import authz
    import cli_common
    import graph_model
    import ioutil
    import ledger
    import promotion
    import scheduler
    import session_docs
    from testkit import load_script

    instrument_wait()
    original_popen = subprocess.Popen
    def recorded_popen(argv, *a, **kw):
        if isinstance(argv, (list, tuple)) and argv and Path(argv[0]).name == "git" and "update-ref" in argv:
            with (Path(os.environ["LOCK_SANDBOX"]) / "git-effects.jsonl").open("a") as fh:
                fh.write(json.dumps(list(map(str, argv))) + "\n")
        return original_popen(argv, *a, **kw)
    subprocess.Popen = recorded_popen
    config = json.loads(os.environ["LOCK_CONFIG"])
    run = Path(config["run"])
    holder = config["holder"]
    node = config.get("node", "H")
    timeout = float(config.get("timeout", WAIT))
    modules = {"ledger": ledger, "promotion": promotion, "scheduler": scheduler,
               "session_docs": session_docs, "authz": authz}
    if action == "cli":
        module = load_script(args[0])
        if hasattr(module, "herdr"):
            module.herdr = sandbox_call
        if hasattr(module, "herdr_text"):
            module.herdr_text = lambda *argv: "sandbox summary"
        if hasattr(module, "run_child"):
            module.run_child = sandbox_child
        if args[0] == "rotate.py":
            import reconcile
            reconcile.herdr = sandbox_call
        sys.argv = [args[0], *args[1:]]
        if config.get("default"):
            original = argparse.ArgumentParser.parse_args
            def parse(parser, *a, **kw):
                parsed = original(parser, *a, **kw)
                print(json.dumps(parsed.lock_timeout))
                raise SystemExit(0)
            argparse.ArgumentParser.parse_args = parse
        if config.get("boundary"):
            instrument_boundary(module, config["boundary"], config.get("occurrence", 1), after=config.get("after", False), path_name=config.get("path_name"))
        cli_common.run(module.main, extra_codes=getattr(module, "CLI_EXTRA_CODES", None))
    if action == "raw-holder":
        with (run / "run.lock").open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            barrier("held")
        return 0
    if action in {"holder", "guard-holder", "transfer-holder"}:
        guard = authz.dispatch_guard(run, node, 1, holder=holder, epoch=1, lock_timeout=timeout) if action == "guard-holder" else ledger.with_lock(run, lock_timeout=timeout, node=config.get("owner_node"))
        with guard:
            barrier("held")
            if action == "transfer-holder":
                change = config["change"]
                if change == "transfer":
                    ledger.append_event_locked(run, {"type": "lease_drain_started", **holder}, holder=holder)
                    ledger.append_event_locked(run, {"type": "lease_transfer", **config["successor"]}, holder=holder)
                elif change == "draining":
                    ledger.append_event_locked(run, {"type": "lease_drain_started", **holder}, holder=holder)
                elif change == "disabled":
                    # An actual interrupted append disables dispatch on replay.
                    with (run / "events.jsonl").open("ab") as fh:
                        fh.write(b'{"type":')
                        fh.flush()
                        os.fsync(fh.fileno())
                    # Establish the truthful disabled projection before the
                    # comparison; a reconcile may legitimately repair caches.
                    ledger._materialize_locked(run, ledger.replay(run), ledger.load_graph(run))
                elif change == "epoch":
                    ledger.append_event_locked(run, {"type": "lease_drain_started", **holder}, holder=holder)
                    ledger.append_event_locked(run, {"type": "lease_transfer", **holder}, holder=holder)
                barrier("changed")
        return 0
    if action == "acquire":
        def acquire():
            with ledger.with_lock(run, lock_timeout=timeout):
                send("acquired")
                if config.get("check_holder"):
                    graph_model.require_active_holder(ledger.replay(run)["controller"], config["check_holder"], "system contender")
                if config.get("effect"):
                    (run / "unexpected-effect").write_text("effect")
        cli_common.run(acquire)
    if action == "nested":
        def nested():
            with authz.dispatch_guard(run, node, 1, holder=holder, epoch=1, lock_timeout=timeout):
                ledger.append_event(run, {"type": "notification_sent", "key": "system"}, holder=holder, lock_timeout=timeout)
        cli_common.run(nested)
    if action == "owner-failure":
        def fail(*_a, **_kw):
            raise OSError("system owner write denied")
        ledger.write_lock_owner = fail
        def mutation():
            ledger.append_event(run, {"type": "notification_sent", "key": "system"}, holder=holder, lock_timeout=timeout)
        cli_common.run(mutation)
    if action == "api":
        name = args[0]
        module_name = name.split(".", 1)[0]
        module = modules.get(module_name) or load_script(module_name + ".py")
        if config.get("boundary"):
            instrument_boundary(module, config["boundary"], config.get("occurrence", 1))
        graph = config["graph"]
        repo = config["repo"]
        def invoke():
            if name == "ledger.bind_session_run":
                ledger.bind_session_run(run, holder=holder, lock_timeout=timeout)
            elif name == "ledger.accept_graph":
                ledger.accept_graph(run, graph, repo=repo, holder=holder, lock_timeout=timeout)
            elif name == "ledger.append_event":
                ledger.append_event(run, {"type": "notification_sent", "key": "system"}, holder=holder, lock_timeout=timeout)
            elif name == "scheduler.reconcile":
                scheduler.reconcile(run, holder=holder, lock_timeout=timeout)
            elif name == "scheduler.reconcile_readonly":
                scheduler.reconcile(run, lock_timeout=timeout)
            elif name == "scheduler.reconcile_event":
                scheduler.reconcile(run, event={"type": "notification_sent", "key": "system"}, holder=holder, lock_timeout=timeout)
            elif name == "promotion.promote":
                promotion.promote(run, node=node, attempt=1, new_sha=config["sha"], repo=repo, holder=holder, lock_timeout=timeout)
            elif name == "promotion.deliver_branch":
                promotion.deliver_branch(run, repo=repo, holder=holder, branch=config.get("branch"), lock_timeout=timeout)
            elif name == "promotion.recover_promotion":
                promotion.recover_promotion(run, repo, holder=holder, lock_timeout=timeout)
            elif name == "rotate.start_lease_drain":
                module.start_lease_drain(run, holder, lock_timeout=timeout)
            elif name == "rotate.successor_transfer_matches":
                module.successor_transfer_matches(run, holder, "test-nonce", {}, lock_timeout=timeout)
            elif name == "reconcile._recorded_herdr_identities":
                module._recorded_herdr_identities(run, lock_timeout=timeout)
            elif name == "session_docs.open_session":
                session_docs.open_session(Path(config["docs"]), job="system", template_dir=SCRIPTS.parent / "references" / "templates", run=str(run), lock_timeout=timeout)
            else:
                raise RuntimeError(name)
        cli_common.run(invoke)
    raise RuntimeError(f"unknown action {action}")


class Process:
    """Test barrier fds carry synchronization only, never authorization."""
    def __init__(self, fixture, action, args=(), **config):
        report_read, report_write = os.pipe()
        release_read, self.release_write = os.pipe()
        self.report_read = report_read
        self.buffer = b""
        self.messages = []
        env = {**fixture.env, "LOCK_CONFIG": json.dumps({**fixture.config, **config}),
               "LOCK_TEST_REPORT": str(report_write), "LOCK_TEST_RELEASE": str(release_read)}
        if "effect" in config and isinstance(config["effect"], str):
            env["LOCK_TEST_EFFECT"] = config["effect"]
        self.started = time.monotonic()
        self.proc = subprocess.Popen(
            [sys.executable, str(SELF), "--child", action, *args], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            pass_fds=(report_write, release_read),
        )
        os.close(report_write)
        os.close(release_read)
        fixture.processes.append(self)

    def expect(self, expected):
        deadline = time.monotonic() + WATCHDOG
        while expected not in self.messages:
            ready, _, _ = select.select([self.report_read], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                raise AssertionError(f"watchdog waiting for {expected}; received {self.messages}")
            data = os.read(self.report_read, 4096)
            if not data:
                out, err = self.finish()
                raise AssertionError(f"child exited {self.proc.returncode} before {expected}: {out} {err}")
            self.buffer += data
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                self.messages.append(json.loads(line))
        return self

    def release(self):
        os.write(self.release_write, b"x")

    def finish(self):
        try:
            return self.proc.communicate(timeout=WATCHDOG)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
            raise AssertionError(f"child watchdog: {out} {err}")

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.communicate(timeout=WATCHDOG)
        os.close(self.report_read)
        os.close(self.release_write)


class Fixture:
    def __init__(self, mode="ready"):
        import ledger
        from testkit import new_run, seed_repo
        from test_retire_gate import RetireGateTest
        from test_run_state_contracts import RunStateContractTest
        self.tmp = tempfile.TemporaryDirectory(prefix="herdr-lock-system-")
        self.root = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "--template=", str(self.root)], capture_output=True, check=True, timeout=WATCHDOG)
        (self.root / ".git" / "info").mkdir(exist_ok=True)
        (self.root / ".git" / "info" / "exclude").write_text("docs/sessions/\ndocs/runs/\n")
        self.processes = []
        self.docs = self.root / "docs"
        session = self.docs / "sessions" / "controller"
        session.mkdir(parents=True)
        self.holder = {"workspace": "controller-ws", "pane": "controller-pane", "session": str(session)}
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "LOCK_"))}
        self.env.update(HERDR_WORKSPACE_ID=self.holder["workspace"], HERDR_PANE_ID=self.holder["pane"], LOCK_SANDBOX=str(self.root))
        self.graph = {"schema_version": 1, "nodes": {"H": {"id": "H", "executor": "herdr-agent", "outputs": [{"name": "summary", "type": "summary"}]}}}
        if mode == "prompt-ready":
            self.graph["nodes"]["H"].update(executor="pane", handle={"name": "h"})
        self.retire = RetireGateTest("runTest")
        self.retire.root, self.retire.holder, self.retire.repos, self.retire._wt = self.root, self.holder, {}, 0
        self.contract = RunStateContractTest("runTest")
        self.contract.root, self.contract.initial_holder = self.root, self.holder
        self.contract.repos, self.contract._wt = {}, 0
        self.sha = None
        self.node = "H"
        if mode in {"integration", "promoted", "pending"}:
            if mode == "pending":
                raw, self.repo, self.sha, _ = self.contract.pending_promotion_run(ref_at="old")
            else:
                raw, self.repo, self.sha = self.contract.promoted_run()
            self.run = Path(raw)
            self.node = "D"
            self.graph = self.contract.promotion_graph()
            if mode == "promoted":
                ledger_result = __import__("promotion").promote(self.run, node="D", attempt=1, new_sha=self.sha, repo=self.repo, holder=self.holder)
                assert ledger_result["state"]["nodes"]["D"]["state"] == "succeeded"
            else:
                receipt = self.run / "artifacts" / "D" / "1" / "integration.json"
                payload = json.loads(receipt.read_text())
                payload["previous_collection"] = True
                receipt.write_text(json.dumps(payload) + "\n")
        else:
            self.repo = seed_repo(self.root / "repo")
            self.run = new_run(self.root, None if mode == "unaccepted" else self.graph, repo=self.repo, holder=self.holder)
            self.retire.repos[str(self.run)] = self.repo
            if mode in {"ready", "prompt-ready"}:
                ledger.append_event(self.run, {"type": "dispatch_intent", "node": "H", "attempt": 1}, holder=self.holder)
            elif mode in {"running", "terminal"}:
                self.retire.start_attempt(self.run, "H", 1)
                if mode == "terminal":
                    receipt = self.run / "artifacts" / "H" / "1" / "summary.json"
                    receipt.write_text(json.dumps({"name": "h", "status": "done", "summary": "previous"}, sort_keys=True) + "\n")
                    ledger.append_event(self.run, {"type": "succeeded", "node": "H", "attempt": 1, "artifacts": [{"name": "summary", "type": "summary", "path": str(receipt), "hash": ledger.sha256_file(receipt)}]}, holder=self.holder)
            if mode == "running":
                (self.run / "artifacts" / "H" / "1" / "summary.json").write_text(json.dumps({"name": "h", "status": "done", "summary": "previous"}, sort_keys=True) + "\n")
                vcs = self.retire.vcs_payload(self.run, "H", 1)
                vcs["result"] = {"form": "none"}
                (self.run / "artifacts" / "H" / "1" / "vcs.json").write_text(json.dumps(vcs) + "\n")
            if mode == "drained":
                ledger.append_event(self.run, {"type": "lease_drain_started", **self.holder}, holder=self.holder)
        (session / "session.json").write_text(json.dumps({"id": session.name, "job": "system", "owner": self.holder["workspace"], "run": str(self.run), **self.holder}))
        for name in ("HANDOFF.md", "NEXT-SESSION-PROMPT.md"):
            (session / name).write_text("isolated system fixture\n")
        tokens = {"provenance": "orchestrator", "run": self.run.name, "node": self.node, "attempt": "1"}
        if mode == "prompt-ready":
            tokens["provenance"] = "user"
        identity_path = self.run / "artifacts" / self.node / "1" / "dispatch-identity.json"
        identity = json.loads(identity_path.read_text()) if identity_path.exists() else {}
        (self.root / "world.json").write_text(json.dumps({"tokens": tokens, "workers": [], "surfaces": [], "prompts": [], "closed": [],
            "workspace": identity.get("workspace", "ws-h"), "pane": identity.get("pane", "pane-h"), "legacy_agent": identity.get("agent", "h")}))
        (self.root / "trace.jsonl").write_text(json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "new summary"}]}}) + "\n")
        self.config = {"run": str(self.run), "holder": self.holder, "node": self.node, "repo": str(self.repo), "graph": self.graph, "docs": str(self.docs), "sha": self.sha}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        for proc in self.processes:
            proc.close()
        self.tmp.cleanup()

    def auth(self, epoch=False):
        args = ["--session", self.holder["session"], "--holder-workspace", self.holder["workspace"], "--holder-pane", self.holder["pane"]]
        return args + (["--epoch", "1"] if epoch else [])

    def argv(self, name):
        context = ["--run", str(self.run), "--node", self.node, "--attempt", "1"]
        if name == "spawn-agent.py":
            return ["h", *context, *self.auth(True), "--prompt", "system full text", "--no-orchestrate"]
        if name == "prompt-agent.py":
            return ["h", *context, *self.auth(True), "--prompt", "system full text", "--no-orchestrate"]
        if name == "dispatch-agent.py":
            return [*context, *self.auth(True), "--prompt", "system full text", "--no-orchestrate"]
        if name == "collect-agent.py":
            identity_path = self.run / "artifacts" / self.node / "1" / "dispatch-identity.json"
            identity = json.loads(identity_path.read_text()) if identity_path.exists() else {}
            target = identity.get("worker_id") or identity.get("agent") or "h"
            return [target, *context, *self.auth(), "--force"]
        if name == "finish-agent.py":
            return [*context, *self.auth(), "--force"]
        if name == "retire-agents.py":
            return ["h", "--run", str(self.run), *self.auth()]
        if name == "ack-handoff.py":
            return ["--nonce", "test-nonce", "--session", self.holder["session"]]
        if name == "rotate.py":
            return ["--session", self.holder["session"]]
        if name == "reconcile.py":
            return ["--run", str(self.run), *self.auth(), "--event", json.dumps({"type": "notification_sent", "key": "system"})]
        if name == "init-docs.py":
            return ["--dir", str(self.docs), "--job", "system", "--run", str(self.run)]
        raise AssertionError(name)

    def launch(self, name, *, timeout=WAIT, **config):
        return Process(self, "cli", [name, *self.argv(name), "--lock-timeout", str(timeout)], timeout=timeout, **config)

    def snapshot(self):
        files = {str(p.relative_to(self.run)): p.read_bytes() for p in self.run.rglob("*") if p.is_file() and p.name not in {"run.lock", "run.lock.owner"}}
        refs = subprocess.run(["git", "-C", str(self.repo), "show-ref"], capture_output=True, check=False, timeout=WATCHDOG).stdout
        return files, refs

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def mutations(self):
        from testkit import READ_ONLY_VERBS
        path = self.root / "git-effects.jsonl"
        git_calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in self.calls() if tuple(row[:2]) not in READ_ONLY_VERBS] + git_calls

    def world(self):
        return json.loads((self.root / "world.json").read_text())


class LockSystem(unittest.TestCase):
    def refusal(self, process, *, diagnostic=True):
        out, err = process.finish()
        self.assertEqual(process.proc.returncode, 3, err or out)
        self.assertEqual(out, "")
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertLess(time.monotonic() - process.started, WATCHDOG)
        if diagnostic:
            self.assertIn("run.lock", err)
            self.assertIn("diagnostic, unverified", err)
        return err

    def probe(self, fixture):
        with (fixture.run / "run.lock").open("a") as fh:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_fallback_run_lock_timeout_exit_three_bounded(self):
        for name in CLI_NAMES:
            mode = "prompt-ready" if name == "prompt-agent.py" else "integration" if name == "finish-agent.py" else "running" if name == "collect-agent.py" else "ready"
            with self.subTest(cli=name), Fixture(mode) as f:
                default = Process(f, "cli", [name, *f.argv(name)], default=True)
                out, err = default.finish()
                self.assertEqual(default.proc.returncode, 0, err)
                self.assertEqual(json.loads(out), 120)
                for value in ("nan", "inf", "-inf", "0", "-1", "not-a-number"):
                    invalid = Process(f, "cli", [name, *f.argv(name), "--lock-timeout=" + value])
                    out, err = invalid.finish()
                    self.assertEqual(invalid.proc.returncode, 2, err)
                    self.assertEqual(out, "")
                holder = Process(f, "holder").expect("held")
                before = f.snapshot()
                if name == "rotate.py":
                    # Main must retain its genuine context refusal. The protected
                    # helper is exercised separately, never by bypassing the gate.
                    main = f.launch(name)
                    self.refusal(main, diagnostic=False)
                    self.assertEqual(f.mutations(), [])
                    contender = Process(f, "api", ["rotate.start_lease_drain"])
                else:
                    contender = f.launch(name)
                contender.expect("waiting")
                self.refusal(contender)
                self.assertEqual(before, f.snapshot())
                self.assertEqual(f.mutations(), [])
                holder.release()
                holder.finish()

    def test_fallback_lock_diagnostic_pid_script_node_since(self):
        for action, owner_node in (("guard-holder", "H"), ("holder", None)):
            with self.subTest(holder=action), Fixture() as f:
                holder = Process(f, action, owner_node=owner_node).expect("held")
                path = f.run / "run.lock.owner"
                owner = json.loads(path.read_text())
                self.assertEqual(owner["pid"], holder.proc.pid)
                self.assertEqual(owner["node"], owner_node)
                self.assertEqual(owner["run"], str(f.run.resolve()))
                self.assertEqual(owner["lock"], "run.lock")
                self.assertEqual(owner["mode"], "exclusive")
                self.assertIsNone(owner["epoch"])
                for key in ("script", "since", "token"):
                    self.assertTrue(owner[key])
                err = self.refusal(Process(f, "acquire").expect("waiting"))
                for value in (str(f.run / "run.lock"), str(owner["pid"]), owner["script"], owner["since"]):
                    self.assertIn(value, err)
                if owner_node:
                    self.assertIn("H", err)
                else:
                    self.assertNotIn("node=", err)
                    self.assertNotIn("node:", err)
                for broken in (None, "{bad json", "[]"):
                    if broken is None:
                        path.unlink()
                    else:
                        path.write_text(broken)
                    err = self.refusal(Process(f, "acquire").expect("waiting"))
                    self.assertIn("holder unknown/unverified", err)
                    if broken is None:
                        self.assertFalse(path.exists())
                    else:
                        self.assertEqual(path.read_text(), broken)
                # Restore our token so normal cleanup remains part of the check.
                path.write_text(json.dumps(owner))
                holder.release()
                holder.finish()
                self.assertFalse(path.exists())

    def test_fallback_stale_owner_cannot_authorize_or_takeover(self):
        with Fixture() as f:
            dead = subprocess.Popen([sys.executable, "-c", "pass"])
            dead.wait(timeout=WATCHDOG)
            holder = Process(f, "raw-holder").expect("held")
            path = f.run / "run.lock.owner"
            stale = {"pid": dead.pid, "script": "dead-script", "node": "H", "since": "old", "token": "dead-token", "run": str(f.run), "lock": "run.lock", "mode": "exclusive", "epoch": None}
            for pid in (dead.pid, os.getpid()):
                stale["pid"] = pid
                path.write_text(json.dumps(stale))
                self.refusal(Process(f, "acquire", effect=True).expect("waiting"))
                self.assertEqual(json.loads(path.read_text()), stale)
                self.assertFalse((f.run / "unexpected-effect").exists())
            holder.release()
            holder.finish()
            managed = Process(f, "holder").expect("held")
            killed_owner = path.read_bytes()
            managed.proc.send_signal(signal.SIGKILL)
            managed.finish()
            self.assertEqual(path.read_bytes(), killed_owner)
            wrong = {**f.holder, "workspace": "foreign"}
            contender = Process(f, "acquire", check_holder=wrong, effect=True).expect("acquired")
            self.refusal(contender, diagnostic=False)
            self.assertFalse((f.run / "unexpected-effect").exists())
            self.assertFalse(path.exists())
            successor = Process(f, "holder").expect("held")
            successor_owner = json.loads(path.read_text())
            self.assertNotEqual(successor_owner["token"], json.loads(killed_owner)["token"])
            replacement = {**successor_owner, "token": "successor-token"}
            path.write_text(json.dumps(replacement))
            successor.release()
            out, err = successor.finish()
            self.assertEqual(successor.proc.returncode, 0, err or out)
            self.assertEqual(json.loads(path.read_text()), replacement)

    def test_fallback_stale_holder_epoch_no_side_effects(self):
        paths = (
            ("spawn-agent.py", "ready"), ("prompt-agent.py", "prompt-ready"),
            ("dispatch-agent.py", "ready"), ("finish-agent.py", "integration"),
            ("collect-agent.py", "running"), ("retire-agents.py", "terminal"),
            ("ledger.accept_graph", "ready"), ("promotion.promote", "integration"),
            ("promotion.deliver_branch", "promoted"), ("promotion.recover_promotion", "pending"),
        )
        for change in ("transfer", "epoch", "draining", "disabled"):
            for name, mode in paths:
                # Epoch and dispatch flags gate launches; same-holder draining
                # native completion and non-dispatch mutations retain their rules.
                if change in {"epoch", "disabled"} and name not in {"spawn-agent.py", "prompt-agent.py", "dispatch-agent.py"}:
                    continue
                if change == "draining" and name in {"promotion.promote", "promotion.recover_promotion"}:
                    continue
                with self.subTest(change=change, path=name), Fixture(mode) as f:
                    successor = {**f.holder, "workspace": "successor-ws", "pane": "successor-pane"}
                    holder = Process(f, "transfer-holder", change=change, successor=successor, timeout=8).expect("held")
                    contender = f.launch(name, timeout=8) if name.endswith(".py") else Process(f, "api", [name], timeout=8)
                    contender.expect("waiting")
                    holder.release()
                    holder.expect("changed")
                    before = f.snapshot()
                    holder.release()
                    holder.finish()
                    self.refusal(contender, diagnostic=False)
                    self.assertEqual(before, f.snapshot())
                    self.assertEqual(f.mutations(), [])

    def test_fallback_sigkill_releases_kernel_lock_no_duplicate_launch(self):
        for effect in ("intent", "workspace create", "identity", "agent prompt"):
            with self.subTest(crash=effect), Fixture() as f:
                if effect == "intent":
                    spawn = f.launch("spawn-agent.py", boundary="write_once", after=True).expect("boundary:write_once")
                elif effect == "identity":
                    spawn = f.launch("spawn-agent.py", boundary="deliver_after_start").expect("boundary:deliver_after_start")
                else:
                    spawn = f.launch("spawn-agent.py", effect=effect).expect("effect:" + effect)
                self.probe(f)
                attempt = f.run / "artifacts" / "H" / "1"
                intent = (attempt / "launch-intent.json").read_bytes()
                spawn.proc.send_signal(signal.SIGKILL)
                spawn.finish()
                contender = Process(f, "acquire").expect("acquired")
                contender.finish()
                resume = Process(f, "cli", ["spawn-agent.py", *f.argv("spawn-agent.py"), "--resume", "--lock-timeout", str(WAIT)])
                if effect == "workspace create":
                    # Existing protocol refuses an orphan surface, rather than
                    # inventing recovery or creating a second worker/surface.
                    self.refusal(resume, diagnostic=False)
                    self.assertEqual(len(f.world()["surfaces"]), 1)
                    self.assertEqual(f.world()["workers"], [])
                    self.assertEqual(f.world()["prompts"], [])
                    self.assertFalse((attempt / "dispatch-identity.json").exists())
                    self.assertFalse((attempt / "launch-complete.json").exists())
                    self.assertEqual((attempt / "launch-intent.json").read_bytes(), intent)
                    continue
                if effect == "agent prompt":
                    self.refusal(resume, diagnostic=False)
                    self.assertFalse((attempt / "launch-complete.json").exists())
                    self.assertTrue((attempt / "prompt-attempt.json").is_file())
                    acknowledged = Process(f, "cli", ["spawn-agent.py", *f.argv("spawn-agent.py"), "--resume", "--delivered", "--lock-timeout", str(WAIT)])
                    out, err = acknowledged.finish()
                    self.assertEqual(acknowledged.proc.returncode, 0, err or out)
                else:
                    out, err = resume.finish()
                    self.assertEqual(resume.proc.returncode, 0, err or out)
                world = f.world()
                self.assertEqual(len(world["surfaces"]), 1)
                self.assertEqual(len(world["workers"]), 1)
                self.assertEqual(len(world["prompts"]), 1)
                self.assertIn("system full text", " ".join(world["prompts"][0]))
                self.assertEqual((attempt / "launch-intent.json").read_bytes(), intent)
                sidecars = {p.name: p.read_bytes() for p in attempt.glob("*launch*.json")}
                again = Process(f, "cli", ["spawn-agent.py", *f.argv("spawn-agent.py"), "--resume", "--lock-timeout", str(WAIT)])
                out, err = again.finish()
                self.assertEqual(again.proc.returncode, 0, err or out)
                self.assertEqual(f.world(), world)
                self.assertEqual({p.name: p.read_bytes() for p in attempt.glob("*launch*.json")}, sidecars)

    def test_fallback_retirement_and_graph_wait_bounded(self):
        with Fixture("terminal") as f:
            retirement = f.launch("retire-agents.py", effect="workspace close").expect("effect:workspace close")
            self.probe(f)
            before = f.snapshot()
            close_count = len(f.world()["closed"])
            self.refusal(f.launch("retire-agents.py").expect("waiting"))
            self.refusal(Process(f, "api", ["ledger.accept_graph"]).expect("waiting"))
            self.assertEqual(before, f.snapshot())
            self.assertEqual(len(f.world()["closed"]), close_count)
            retirement.release()
            out, err = retirement.finish()
            self.assertEqual(retirement.proc.returncode, 0, err or out)
        with Fixture("unaccepted") as f:
            acceptance = Process(f, "api", ["ledger.accept_graph"], boundary="_materialize_graph_revision_locked").expect("boundary:_materialize_graph_revision_locked")
            self.probe(f)
            before = f.snapshot()
            self.refusal(Process(f, "api", ["ledger.accept_graph"]).expect("waiting"))
            self.assertEqual(before, f.snapshot())
            acceptance.release()
            out, err = acceptance.finish()
            self.assertEqual(acceptance.proc.returncode, 0, err or out)
        with Fixture("terminal") as f:
            # Actual --collect command carries NO run/node/attempt: it reads a
            # summary while the parent's close loop still owns run.lock.
            before = f.snapshot()
            retire = Process(f, "cli", ["retire-agents.py", *f.argv("retire-agents.py"), "--collect", "--lock-timeout", str(WAIT)], effect="workspace close").expect("effect:workspace close")
            self.probe(f)
            self.refusal(Process(f, "acquire").expect("waiting"))
            self.assertEqual(before, f.snapshot())
            child_calls = [json.loads(line) for line in (f.root / "children.jsonl").read_text().splitlines()]
            self.assertEqual(len(child_calls), 1)
            argv = child_calls[0]
            self.assertEqual(Path(argv[1]).name, "collect-agent.py")
            self.assertNotIn("--run", argv)
            self.assertNotIn("--node", argv)
            self.assertNotIn("--attempt", argv)
            retire.release()
            out, err = retire.finish()
            self.assertEqual(retire.proc.returncode, 0, err or out)
            self.assertTrue(json.loads(out)["retired"][0]["summary"])
            self.assertEqual(before, f.snapshot())

    def test_fallback_lock_extent_unchanged(self):
        cases = (
            ("ledger.bind_session_run", "ready", "replay"),
            ("ledger.accept_graph", "unaccepted", "_materialize_graph_revision_locked"),
            ("ledger.append_event", "ready", "append_event_locked"),
            ("promotion.promote", "integration", "_finalize_promotion_locked"),
            ("promotion.deliver_branch", "promoted", "_deliver_branch_locked"),
            ("promotion.recover_promotion", "pending", "_recover_promotion_locked"),
            ("scheduler.reconcile", "ready", "_repair_dispatch_snapshots_locked"),
            ("scheduler.reconcile_readonly", "ready", "_materialize_locked"),
            ("scheduler.reconcile_event", "ready", "append_event_locked"),
            ("rotate.start_lease_drain", "ready", "ledger.append_event_locked"),
            ("rotate.successor_transfer_matches", "ready", "ledger.replay"),
            ("reconcile._recorded_herdr_identities", "ready", "ledger.replay"),
            ("session_docs.open_session", "ready", "ledger.replay"),
            ("collect-agent.py", "running", "ioutil.atomic_text"),
            ("finish-agent.py", "integration", "ioutil.atomic_text"),
            ("ack-handoff.py", "drained", "ledger.append_event_locked"),
            ("ack-handoff.py", "drained", "ledger.replay"),
            ("dispatch-agent.py", "ready", "ledger.append_event_locked"),
        )
        for name, mode, boundary in cases:
            with self.subTest(path=name), Fixture(mode) as f:
                process = f.launch(name, boundary=boundary) if name.endswith(".py") else Process(f, "api", [name], boundary=boundary)
                process.expect("boundary:" + boundary)
                self.probe(f)
                before = f.snapshot()
                self.refusal(Process(f, "acquire").expect("waiting"))
                self.assertEqual(before, f.snapshot())
                process.release()
                out, err = process.finish()
                self.assertEqual(process.proc.returncode, 0, err or out)
        with Fixture("integration") as f:
            # publish_vcs replaces a differing receipt and audits it under the
            # same lock; attach_artifacts' forced summary extent is above.
            vcs = f.run / "artifacts" / "D" / "1" / "vcs.json"
            old = json.loads(vcs.read_text())
            old["result"] = {"form": "none"}
            vcs.write_text(json.dumps(old) + "\n")
            argv = f.argv("collect-agent.py")
            argv[0] = json.loads((vcs.parent / "dispatch-identity.json").read_text())["agent"]
            process = Process(f, "cli", ["collect-agent.py", *argv, "--publish-vcs", "--lock-timeout", str(WAIT)],
                              boundary="ioutil.atomic_text", path_name="vcs.json").expect("boundary:ioutil.atomic_text")
            self.probe(f)
            before = f.snapshot()
            self.refusal(Process(f, "acquire").expect("waiting"))
            self.assertEqual(before, f.snapshot())
            process.release()
            out, err = process.finish()
            self.assertEqual(process.proc.returncode, 0, err or out)
        for name, effect in (("spawn-agent.py", "agent prompt"), ("prompt-agent.py", "agent prompt")):
            with self.subTest(path=name), Fixture("prompt-ready" if name == "prompt-agent.py" else "ready") as f:
                process = f.launch(name, effect=effect)
                process.expect("effect:" + effect)
                self.probe(f)
                self.refusal(Process(f, "acquire").expect("waiting"))
                process.release()
                out, err = process.finish()
                self.assertEqual(process.proc.returncode, 0, err or out)
        with Fixture() as f:
            before = f.snapshot()
            self.refusal(Process(f, "nested"), diagnostic=False)
            self.assertEqual(before, f.snapshot())
            acquire = Process(f, "acquire").expect("acquired")
            acquire.finish()

    def test_fallback_owner_write_failure_fails_closed(self):
        with Fixture() as f:
            before = f.snapshot()
            process = Process(f, "owner-failure")
            err = self.refusal(process, diagnostic=False)
            self.assertIn("owner", err)
            self.assertIn("denied", err)
            self.assertEqual(before, f.snapshot())
            self.assertFalse((f.run / "run.lock.owner").exists())
            acquire = Process(f, "acquire").expect("acquired")
            acquire.finish()
            self.assertEqual(acquire.proc.returncode, 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--backend":
        raise SystemExit(backend(sys.argv[2:]))
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        raise SystemExit(child(sys.argv[2], sys.argv[3:]))
    # Existing fixture Git helpers use subprocess.run: retain their real
    # processes, adding only a finite watchdog to otherwise unbounded calls.
    original_run = subprocess.run
    def finite_run(*args, **kwargs):
        kwargs.setdefault("timeout", WATCHDOG)
        return original_run(*args, **kwargs)
    subprocess.run = finite_run
    unittest.main()
