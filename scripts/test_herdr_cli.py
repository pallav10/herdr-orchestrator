#!/usr/bin/env python3

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import herdr_cli
import run_state


def completed(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["herdr"], returncode=rc, stdout=stdout, stderr=stderr)


class HerdrCliTest(unittest.TestCase):
    def stub(self, *, result=None, side_effect=None):
        patcher = mock.patch.object(herdr_cli.subprocess, "run", side_effect=side_effect, return_value=result)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def error(self, code: str) -> run_state.RunError:
        # message deliberately echoes the flags herdr_cli passes, including --timeout
        exc = run_state.RunError(f"herdr agent prompt w text --wait --until working --timeout 15000 failed: {code}")
        exc.code = code
        return exc

    def test_failure_raises_runerror_carrying_full_invocation_context(self):
        stderr = '{"error": {"code": "PANE_NOT_FOUND", "message": "nope"}}'
        self.stub(result=completed(rc=4, stdout="partial", stderr=stderr))
        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.herdr("pane", "show", "p1")
        exc = ctx.exception
        self.assertEqual(exc.code, "PANE_NOT_FOUND")
        self.assertEqual(exc.args_tuple, ("pane", "show", "p1"))
        self.assertEqual(exc.returncode, 4)
        self.assertEqual(exc.stdout, "partial")
        self.assertEqual(exc.stderr, stderr)
        self.assertIn("nope", str(exc))

    def test_failure_code_falls_back_to_stdout_payload(self):
        self.stub(result=completed(rc=1, stdout='{"result": {"error": {"code": "BUSY"}}}', stderr=""))
        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.herdr("pane", "run")
        self.assertEqual(ctx.exception.code, "BUSY")

    def test_missing_binary_becomes_runerror(self):
        self.stub(side_effect=FileNotFoundError("herdr"))
        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.herdr("pane", "list")
        self.assertEqual(str(ctx.exception), "herdr not on PATH")

    def test_timeout_expired_becomes_runerror_with_timeout_code(self):
        self.stub(side_effect=subprocess.TimeoutExpired(cmd=["herdr", "agent", "list"], timeout=10, output="out", stderr="err"))
        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.herdr("agent", "list")
        exc = ctx.exception
        self.assertEqual(exc.code, "timeout")
        self.assertEqual(exc.args_tuple, ("agent", "list"))
        self.assertIsNone(exc.returncode)
        self.assertEqual(exc.stdout, "out")
        self.assertEqual(exc.stderr, "err")
        self.assertIn("timed out", str(exc))

    def test_empty_stdout_is_empty_dict(self):
        self.stub(result=completed(stdout="   \n"))
        self.assertEqual(herdr_cli.herdr("pane", "kill"), {})

    def test_non_json_stdout_raises_with_stdout_echoed(self):
        self.stub(result=completed(stdout="herdr: usage error"))
        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.herdr("pane", "list")
        self.assertIn("herdr: usage error", str(ctx.exception))

    def test_non_dict_json_payload_is_rejected(self):
        self.stub(result=completed(stdout='[{"pane": "p1"}]'))
        with self.assertRaises(run_state.RunError):
            herdr_cli.herdr("pane", "list")

    def test_dict_payload_is_returned(self):
        self.stub(result=completed(stdout='{"panes": [{"id": "p1"}]}\n'))
        self.assertEqual(herdr_cli.herdr("pane", "list"), {"panes": [{"id": "p1"}]})

    def test_extract_error_code_returns_none_when_absent_or_unusable(self):
        self.assertIsNone(herdr_cli._extract_error_code(""))
        self.assertIsNone(herdr_cli._extract_error_code("plain text failure\n"))
        self.assertIsNone(herdr_cli._extract_error_code('["PANE_NOT_FOUND"]'))
        self.assertIsNone(herdr_cli._extract_error_code('{"error": {"code": 7}}'))
        self.assertIsNone(herdr_cli._extract_error_code('{"error": "PANE_NOT_FOUND"}'))

    def test_herdr_text_returns_raw_stdout_unparsed(self):
        raw = "  line one\nline two\n"
        self.stub(result=completed(stdout=raw))
        self.assertEqual(herdr_cli.herdr_text("agent", "read", "p1"), raw)

    def test_herdr_text_propagates_failure(self):
        self.stub(result=completed(rc=2, stderr="boom"))
        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.herdr_text("agent", "read", "p1")
        self.assertEqual(ctx.exception.returncode, 2)

    def test_invocation_prefixes_binary_and_bounds_runtime(self):
        run = self.stub(result=completed(stdout="{}"))
        herdr_cli.herdr("pane", "list", "--json")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["herdr", "pane", "list", "--json"])
        self.assertEqual(kwargs["timeout"], herdr_cli.READ_TIMEOUT)

    def test_read_only_verbs_use_short_timeout(self):
        run = self.stub(result=completed(stdout="{}"))
        self.assertEqual(herdr_cli.READ_TIMEOUT, 10)
        for resource in ("agent", "workspace", "pane"):
            for verb in ("get", "list", "show"):
                with self.subTest(resource=resource, verb=verb):
                    herdr_cli.herdr(resource, verb, "w")
                    self.assertEqual(run.call_args.kwargs["timeout"], 10)
        for extra in (
            ("pane", "process-info", "w:p0"),
            ("agent", "read", "w:p0"),
            ("api", "schema", "--json"),
        ):
            with self.subTest(args=extra):
                herdr_cli.herdr(*extra)
                self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_non_read_calls_keep_full_timeout(self):
        run = self.stub(result=completed(stdout="{}"))
        self.assertEqual(herdr_cli.TIMEOUT, 60)
        for args in (
            ("agent", "prompt", "w", "text"),
            ("agent", "wait", "w"),
            ("agent", "send-keys", "w", "Enter"),
            ("agent", "start", "w"),
            ("agent", "interrupt", "w"),
            ("workspace", "create", "w"),
            ("workspace", "set", "w"),
            ("pane", "run", "w"),
            ("pane", "kill", "w"),
            ("session", "list"),
            ("agent",),
            (),
        ):
            with self.subTest(args=args):
                herdr_cli.herdr(*args)
                self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_server_not_running_names_the_running_named_sessions(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "flowops").mkdir()
        (root / "flowops" / "herdr.sock").touch()
        (root / "never-started").mkdir()
        patcher = mock.patch.object(herdr_cli, "SESSIONS_DIR", root)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.stub(result=completed(
            rc=1,
            stderr='{"error": {"code": "server_not_running", "message": "no herdr server is running at /x/herdr.sock"}}',
        ))
        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.herdr("agent", "list")
        message = str(ctx.exception)
        self.assertIn("running named sessions: flowops", message)
        self.assertIn("HERDR_SESSION=", message)
        self.assertNotIn("never-started", message)

    def test_other_failures_carry_no_session_hint(self):
        self.stub(result=completed(rc=1, stderr='{"error": {"code": "agent_not_found", "message": "nope"}}'))
        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.herdr("agent", "get", "w")
        self.assertNotIn("running named sessions", str(ctx.exception))

    def test_delivery_confirms_the_agent_left_idle(self):
        calls: list[tuple[str, ...]] = []

        def stalling(*args: str):
            calls.append(args)
            if args[:2] == ("agent", "prompt"):
                raise self.error("agent_prompt_stalled")
            return {}

        herdr_cli.deliver_prompt(stalling, "w", "text")
        self.assertEqual([call[:2] for call in calls], [
            ("agent", "prompt"),
            ("agent", "send-keys"),
            ("agent", "wait"),
        ])
        self.assertEqual(calls[1], ("agent", "send-keys", "w", "Enter"))

    def test_delivery_that_never_leaves_idle_is_agent_not_ready(self):
        def wedged(*args: str):
            if args[:2] == ("agent", "send-keys"):
                return {}
            raise self.error("agent_prompt_stalled" if args[:2] == ("agent", "prompt") else "timeout")

        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.deliver_prompt(wedged, "w", "text")
        self.assertEqual(ctx.exception.code, "agent_not_ready")
        self.assertIn("never left idle", str(ctx.exception))

    def test_a_coded_failure_is_never_read_as_a_stall(self):
        # the composed message echoes our own `--timeout` flag; only the code decides
        calls: list[tuple[str, ...]] = []

        def blocked(*args: str):
            calls.append(args)
            raise self.error("agent_blocked")

        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.deliver_prompt(blocked, "w", "text")
        self.assertEqual(ctx.exception.code, "agent_blocked")
        self.assertEqual(len(calls), 1)

    def test_uncoded_or_unrelated_failures_never_recover_from_text(self):
        for code in (None, "agent_not_found", "agent_blocked"):
            for marker in ("agent_blocked", "agent_prompt_stalled", "timeout"):
                with self.subTest(code=code, marker=marker):
                    stderr = f'{{"error": {{"message": "{marker}"}}}}'
                    if code is not None:
                        stderr = f'{{"error": {{"code": "{code}", "message": "{marker}"}}}}'
                    run = self.stub(result=completed(rc=4, stdout="partial", stderr=stderr))
                    with self.assertRaises(run_state.RunError) as ctx:
                        herdr_cli.deliver_prompt(herdr_cli.herdr, "w", f"prompt mentioning {marker}")
                    self.assertEqual(ctx.exception.code, code)
                    self.assertEqual(ctx.exception.stdout, "partial")
                    self.assertEqual(ctx.exception.stderr, stderr)
                    self.assertIn(marker, str(ctx.exception))
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(run.call_args.args[0][1:3], ["agent", "prompt"])
                    self.assertEqual(run.call_args.kwargs["timeout"], 60)

    def test_structured_stalls_recover_once_regardless_of_prompt_text(self):
        for code in ("agent_prompt_stalled", "timeout"):
            for marker in ("agent_blocked", "agent_prompt_stalled", "timeout"):
                with self.subTest(code=code, marker=marker):
                    run = self.stub(side_effect=[
                        completed(rc=1, stderr=f'{{"error": {{"code": "{code}"}}}}'),
                        completed(stdout="{}"),
                        completed(stdout="{}"),
                    ])
                    text = f"prompt mentioning {marker}"
                    herdr_cli.deliver_prompt(herdr_cli.herdr, "w", text)
                    wait = ["--until", "working", "--timeout", str(herdr_cli.PROMPT_WAIT_MS)]
                    self.assertEqual([call.args[0] for call in run.call_args_list], [
                        ["herdr", "agent", "prompt", "w", text, "--wait", *wait],
                        ["herdr", "agent", "send-keys", "w", "Enter"],
                        ["herdr", "agent", "wait", "w", *wait],
                    ])
                    self.assertEqual([call.kwargs["timeout"] for call in run.call_args_list], [60, 60, 60])

    def test_confirmed_prompt_needs_no_enter_or_second_submission(self):
        run = self.stub(result=completed(stdout="{}"))
        herdr_cli.deliver_prompt(herdr_cli.herdr, "w", "timeout agent_prompt_stalled agent_blocked")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:3], ["agent", "prompt"])

    def test_uncoded_wait_failure_preserves_context_without_another_recovery(self):
        stderr = "timeout agent_prompt_stalled"
        run = self.stub(side_effect=[
            completed(rc=1, stderr='{"error": {"code": "agent_prompt_stalled"}}'),
            completed(stdout="{}"),
            completed(rc=4, stdout="partial", stderr=stderr),
        ])
        with self.assertRaises(run_state.RunError) as ctx:
            herdr_cli.deliver_prompt(herdr_cli.herdr, "w", "text")
        self.assertIsNone(ctx.exception.code)
        self.assertEqual(ctx.exception.args_tuple[:2], ("agent", "wait"))
        self.assertEqual(ctx.exception.returncode, 4)
        self.assertEqual(ctx.exception.stdout, "partial")
        self.assertEqual(ctx.exception.stderr, stderr)
        self.assertEqual(run.call_count, 3)
    def test_core_mode_translates_agent_workspace_and_metadata_calls(self):
        env = {"HC_TERMINAL_ID": "t1d", "PATH": "/usr/bin:/bin"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(herdr_cli, "_hc_bin", return_value="/usr/local/bin/hc"),
        ):
            self.assertTrue(herdr_cli.is_core_mode())
            self.assertEqual(herdr_cli.current_holder_env(), ("t1d", "t1d"))

            run = self.stub(result=completed(stdout='{"terminals":[{"address":"t20","name":"w-abc","agent":"omp","state":"live","status":"idle"}]}'))
            listed = herdr_cli.herdr("agent", "list")
            self.assertEqual(run.call_args.args[0], ["/usr/local/bin/hc", "ls", "--json"])
            self.assertEqual(listed["result"]["agents"][0]["name"], "w-abc")
            self.assertEqual(listed["result"]["agents"][0]["agent_status"], "idle")

            run = self.stub(side_effect=[
                completed(stdout="{}"),
                completed(stdout='{"address":"t20","name":"w-abc","state":"live","labels":{"orch.provenance":"orchestrator","orch.run":"r1"}}'),
            ])
            stamped = herdr_cli.stamp_workspace_tokens("t20", "r1", tokens=("provenance=orchestrator",))
            self.assertEqual(run.call_args_list[0].args[0][:3], ["/usr/local/bin/hc", "label", "--json"])
            self.assertEqual(stamped["result"]["workspace"]["tokens"]["provenance"], "orchestrator")
            self.assertEqual(stamped["result"]["workspace"]["tokens"]["run"], "r1")

            run = self.stub(result=completed(stdout="screen text\n"))
            txt = herdr_cli.herdr_text("agent", "read", "w-abc", "--source", "recent-unwrapped", "--lines", "80")
            self.assertEqual(txt, "screen text\n")
            self.assertEqual(run.call_args.args[0], ["/usr/local/bin/hc", "read", "--lines", "80", "w-abc"])

            run = self.stub(result=completed(rc=3, stderr='{"error":{"kind":"not_found","message":"no terminal"}}'))
            with self.assertRaises(run_state.RunError) as ctx:
                herdr_cli.herdr("agent", "get", "missing")
            self.assertEqual(ctx.exception.code, "agent_not_found")

    def test_core_mode_adopts_started_worker_into_run_space_tab(self):
        env = {
            "HC_TERMINAL_ID": "t1d",
            "HERDR_ORCHESTRATOR_SPACE_ID": "sp_run1",
            "PATH": "/usr/bin:/bin",
        }
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(herdr_cli, "_hc_bin", return_value="/usr/local/bin/hc"),
            mock.patch.object(herdr_cli, "_herdr_app_bin", return_value="/usr/local/bin/herdr-app"),
            mock.patch.object(herdr_cli, "_herdr_app_run", return_value={}) as app_run,
        ):
            run = self.stub(side_effect=[
                completed(stdout='{"address":"t10","cwd":"/tmp/wt","labels":{"orch.label":"A · a1"}}'),
                completed(stdout='{"address":"t11","name":"fo-1234","state":"live","status":"idle"}'),
                completed(stdout="{}"),  # hc label herdr.space/herdr.tab
                completed(stdout="{}"),  # hc kill placeholder
                completed(stdout="{}"),  # hc rm placeholder
                completed(stdout='{"address":"t11","name":"fo-1234","state":"live","status":"idle"}'),
            ])
            res = herdr_cli.herdr("agent", "start", "fo-1234", "--kind", "omp", "--pane", "t10")
            self.assertEqual(res["result"]["workspace_id"], "t11")
            label_call = run.call_args_list[2].args[0]
            self.assertEqual(label_call[:4], ["/usr/local/bin/hc", "label", "--json", "t11"])
            self.assertIn("herdr.space=sp_run1", label_call)
            self.assertTrue(any(tok.startswith("herdr.tab=tb_") for tok in label_call))
            self.assertEqual(app_run.call_args.args[0][0:2], ["do", "tab.rename"])

    def test_core_mode_headless_fallback_skips_herdr_app_when_disabled(self):
        env = {
            "HC_TERMINAL_ID": "t1d",
            "HERDR_ORCHESTRATOR_NO_UI": "1",
            "PATH": "/usr/bin:/bin",
        }
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(herdr_cli, "_hc_bin", return_value="/usr/local/bin/hc"),
        ):
            self.assertIsNone(herdr_cli._herdr_app_bin())
            self.assertIsNone(herdr_cli.ensure_run_space("/tmp/fake-run", job="demo"))

    def test_ensure_run_space_creates_space_and_replaces_default_tab_with_graph_mod(self):
        env = {"HC_TERMINAL_ID": "t1d", "PATH": "/usr/bin:/bin"}
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "run-1"
            run_dir.mkdir()
            app_calls = []

            def fake_app(argv, **_kw):
                app_calls.append(argv)
                if argv == ["ls"]:
                    return {"spaces": []}
                if argv[:2] == ["do", "space.new"]:
                    return {"space": "sp_new", "tab": "tb_shell"}
                if argv[:2] == ["do", "tab.new"]:
                    return {"tab": "tb_graph"}
                return {}

            with (
                mock.patch.dict("os.environ", env, clear=True),
                mock.patch.object(herdr_cli, "_herdr_app_bin", return_value="/usr/local/bin/herdr-app"),
                mock.patch.object(herdr_cli, "_herdr_app_home", return_value=Path(td) / "app"),
                mock.patch.object(herdr_cli, "_herdr_app_run", side_effect=fake_app),
            ):
                ui = herdr_cli.ensure_run_space(run_dir, job="demo", repo=td, focus=False)
                self.assertEqual(ui, {"space": "sp_new", "tab": "tb_graph"})
                self.assertTrue((Path(td) / "app" / "mods" / "herdr-orchestrator").exists())
                self.assertEqual([c[:2] for c in app_calls], [
                    ["ls"],
                    ["do", "space.new"],
                    ["do", "tab.new"],
                    ["do", "tab.close"],
                ])


if __name__ == "__main__":
    unittest.main()
