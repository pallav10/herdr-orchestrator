# Repository Guidelines

## Project Overview

Herdr orchestration skill for one controller pane coordinating parallel workers, isolated Git worktrees, durable execution state, and controller handoffs. This is a Python script collection, not a packaged application. Native `task`/`workflow` jobs are bounded and session-local; `herdr-agent` workers can survive controller rotation; `pane` executors address existing user-owned panes.

## Architecture & Data Flow

Execution authority is the append-only `events.jsonl` in a target repository's run directory. `run_state.replay()` reconstructs state; `state.json`, `controller.json`, and `status.md` are derived caches. Never edit caches, session markdown, or Herdr idle/done status to declare success.

1. `init-docs.py` opens/reuses a pane session and binds a canonical run. `run_state.accept_graph()` validates dependencies/ownership and pins the repository, base SHA, and run-owned promotion ref.
2. `dispatch-agent.py` records `dispatch_intent` with immutable input/ownership snapshots, then calls `spawn-agent.py --resume`. `prompt-agent.py` handles existing panes and blocked continuations. Launch sidecars preserve crash evidence; delivery is confirmed by observing the worker leave idle, not merely a successful CLI exit.
3. `finish-agent.py` checks worktree cleanliness and ownership, then calls `collect-agent.py` for hashed receipts. Non-integrators record success; integrators return the exact `run_state.promote()` call instead.
4. Promotion compare-and-swaps `refs/heads/herdr/<run-id>` and records promotion plus success. Delivery to a feature branch is separate and create-only; never promote into the primary checkout.
5. `rotate.py` drains native work; `ack-handoff.py` transfers the controller lease and advances its epoch. Reattach persistent workers by recorded identity and run/node/attempt tokens, not labels.

All mutations require the exact `{workspace, pane, session}` holder and appropriate lease; dispatch also requires the current epoch. `run.lock` uses `fcntl.flock`; `dispatch_guard()` keeps authorization and external launch under one lock. Do not call `append_event()` inside that guard. Concurrent writers must have disjoint `owns`, or dependency ordering must serialize them.

Only the bounded-acquisition/owner-diagnostics fallback is approved, not concurrent dispatch. Lock-taking entrypoints accept `--lock-timeout SECONDS` (finite positive, default 120; usage 2, timeout refusal 3); locked facade APIs accept `lock_timeout=120`. Preserve every exclusive critical section, authorization/launch lock extent and nested-append refusal. `run.lock.owner` is unverified diagnostic evidence, never authority or permission to break a lock; see [locking contract](references/run-state-api.md#events-locking-and-dispatch).

## Key Directories

- `scripts/`: executable adapters, importable shared modules, and colocated `test_*.py` suites; there is no `src/` tree.
- `references/`: session/run contract and pane environment guidance; `references/templates/` seeds handoff and session documents.
- `docs/plans/`: design notes, not execution authority.
- In the **target repository**, `docs/runs/<run-id>/` holds the ledger and `artifacts/<node>/<attempt>/`; `docs/sessions/<session-id>/` holds pane history. Initialization requires both runtime directories to be Git-ignored. This skill repository's `.gitignore` does not add those rules.

## Development Commands

Run from the skill repository root:

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m unittest discover -s scripts -p 'test_run_state.py'
# Parallel per-file stdlib discovery; any failed suite gives a nonzero exit:
python3 -c 'import concurrent.futures, glob, os, subprocess, sys; suites = sorted(glob.glob("scripts/test_*.py")); pool = concurrent.futures.ThreadPoolExecutor(max_workers=8); codes = list(pool.map(lambda p: subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "scripts", "-p", os.path.basename(p), "-q"]), suites)); pool.shutdown(); sys.exit(any(codes))'
python3 scripts/prompt_contract.py
python3 scripts/dispatch-agent.py --help
python3 scripts/init-docs.py --help
```

Discovery commands run full, focused, or parallel per-file suites; `prompt_contract.py` runs its assertion self-check. Utilities run directly as `python3 scripts/<name>.py`; inspect `--help` before live operations. No build/install step or configured lint, formatter, typecheck, or coverage command exists.

Live initialization uses `python3 scripts/init-docs.py --dir /absolute/target/docs --job change-name` after the target's ignore rules are present. Do not use live run directories for development smoke checks. Plain `reconcile.py --run /absolute/run` repairs missing/damaged/changed projections, leaving unchanged bytes untouched; compact output is default and `--full` preserves the complete Python return shape.

## Code Conventions & Common Patterns

- Match existing four-space Python indentation, `from __future__ import annotations`, type hints, `snake_case` functions, `_private` helpers, and uppercase constants. Executables use hyphenated filenames; importable modules use underscores.
- Keep argparse adapters thin and import defining modules, not the controller-facing `run_state.py` facade. **Never import `run_state` into `ownership`** or introduce dependency-order cycles. Hyphenated adapters invoke siblings through `cli_common.run_child`; reuse `cli_common.run` for exception/exit handling.
- Preserve CLI contracts: generally one JSON object on stdout and one error line on stderr; monitors emit flushed JSONL. `RunError` maps to exit 1, `UsageError` to 2, `PreconditionError` to 3. Script-specific exceptions include prompt exit 4 for `agent_blocked`, monitor exit 2 for timeout, and ownership exit 3 for findings.
- Operations are synchronous; monitoring runs as a separate background observer, not an asyncio service. Route Herdr through `herdr_cli.py` and Git through existing deadline-bound helpers. Tests replace module-level invokers or patch subprocesses; no dependency-injection framework is used.
- Persist canonical absolute run/session/artifact paths; never resolve `docs/sessions/latest` as a write target. Reuse atomic/fsynced writers and hashed receipts. Retry creates a new attempt; `--force` receipt replacement is explicitly audited, not a history rewrite.

New managed workers use `identity_kind: managed-name` and immutable `worker_id` values matching `fo-[0-9a-f]{24}`. Missing identity kind means legacy runtime-id, not managed-name. User-owned panes remain runtime-id-only and must never be retired. Preserve fail-closed identity, holder, ownership, and promotion checks.

## Important Files

| Path | Role |
| --- | --- |
| `SKILL.md`, `references/session-docs.md` | Operator interface and durable session/run contract; cross-check claims against implementation. |
| `scripts/run_state.py` | Stable controller-facing facade; implementation lives in defining modules below. |
| `scripts/ioutil.py`, `scripts/graph_model.py` | Shared I/O/errors/git helpers and graph/ownership model. |
| `scripts/ledger.py`, `scripts/authz.py` | Event replay/materialization and exact-holder leases/dispatch guards. |
| `scripts/artifacts.py`, `scripts/receipts.py` | Immutable dispatch snapshots and VCS/integration receipt evidence. |
| `scripts/routing.py`, `scripts/liveness.py` | Model-routing policy and live identity/observation. |
| `scripts/promotion.py`, `scripts/scheduler.py`, `scripts/cli_common.py` | Promotion/recovery/delivery, lifecycle/reconcile, and adapter/child process boundary. |
| `scripts/herdr_cli.py`, `scripts/prompt_contract.py` | Shared Herdr boundary, confirmed prompt delivery, and standalone `orchestrate` prefix. |
| `scripts/ownership.py`, `scripts/check-ownership.py` | Ownership classification and its CLI entry point. |
| `scripts/session_docs.py`, `scripts/init-docs.py` | Session binding, canonical paths, and template rendering. |
| `scripts/dispatch-agent.py`, `scripts/finish-agent.py` | Controller-facing dispatch and completion entry points. |
| `scripts/reconcile.py`, `scripts/rotate.py`, `scripts/ack-handoff.py` | State repair and tokenized controller handoff. |

Reference index (D14): [identity-reattach](references/identity-reattach.md), [rotation](references/rotation.md), [retire-promote](references/retire-promote.md), [run-state-api](references/run-state-api.md). Practical examples: [controller-playbook](references/controller-playbook.md). CLI consumers: [docs/herdr-cheatsheet.md](docs/herdr-cheatsheet.md).

Session metadata is the sole run locator. Required HANDOFF/NEXT bootstrap resources remain; markdown run equality, progress existence, and note-age/mtime gates do not. Measured context freshness and exact nonce/holder transfer remain. Acknowledgment metadata preconditions exit 3. Prompt-specific retry flags/count output are removed, but spawn/dispatch startup retry controls remain. Output-cost contracts (compact/full reconcile, adaptive monitoring, summary cap and read deadlines) are defined in the D14 references.

## Runtime/Tooling Preferences

Use `python3` on a POSIX platform: locking requires `fcntl`. Python modules use the standard library only; no Python version pin, dependency manifest, package manager, or Node/Bun runtime is configured. Git is required; live control needs `herdr` on PATH. Model-role lookup invokes `omp config get modelRoles --json`.

The documented compatibility target is Herdr 0.8.2/protocol 20, not a runtime version probe. Trust installed CLI help; stop on protocol mismatch. Preserve `HERDR_SESSION`/`HERDR_SOCKET_PATH` and controller pane identity variables when launching child processes. Never stop the Herdr server from an active pane or close user-owned surfaces. Keep runtime ledgers and local `.idea/`, `.desloppify/`, and bytecode artifacts out of commits.

## Testing & QA

Use stdlib `unittest` discovery, not pytest. Tests use temporary directories, real temporary Git repositories, `unittest.mock`, and fake Herdr invokers; they do not require a live Herdr server. Hyphenated scripts are loaded with `importlib.util.spec_from_file_location`.

`test_executor_adapters.py` binds module-level `check_*` functions onto `ExecutorDispatchTests`; `test_prompt_contract.py` binds selected checks explicitly. Preserve those bindings so discovery actually runs new checks.

Prioritize observable boundaries: stale epochs/foreign holders must cause no side effects, delivery must be confirmed, mismatched identities must refuse, ownership and receipt hashes must gate success, and rotation/promotion must remain crash-safe. Run the focused suite while developing and full discovery before delivery. No coverage percentage is configured. Mocked adapter tests are not live Herdr end-to-end proof; use controlled runtime verification for changed live behavior.
