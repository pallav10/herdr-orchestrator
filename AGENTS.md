# Repository Guidelines

## Project Overview

Herdr orchestration skill for one controller pane coordinating parallel workers, isolated Git worktrees, durable execution state, and controller handoffs. This is a Python script collection, not a packaged application. Native `task`/`workflow` jobs are bounded and session-local; `herdr-agent` workers can survive controller rotation; `pane` executors address existing user-owned panes.

## Architecture & Data Flow

Execution authority is the append-only `events` table in per-run SQLite WAL `<state_root>/runs/<run-id>/run.db` (default `${XDG_STATE_HOME:-~/.local/state}/herdr-orchestrator/`, overridable via `HERDR_ORCHESTRATOR_STATE_DIR`). `run_state.replay()` reconstructs state; `index.db` is a rebuildable pane/repo lookup cache. Never treat Herdr idle/done status as success.

1. `ho.py init --job <name>` creates or reuses a SQLite WAL run under XDG state and binds the pane in `index.db` (zero `.gitignore` rules or `.md` files). `run_state.accept_graph()` validates dependencies/ownership and pins the repository, base SHA, and run-owned promotion ref.
2. `dispatch-agent.py` records `dispatch_intent` with immutable input/ownership snapshots, then calls `spawn-agent.py --resume`. `prompt-agent.py` handles existing panes and blocked continuations. Launch sidecars preserve crash evidence; delivery is confirmed by observing the worker leave idle, not merely a successful CLI exit.
3. `finish-agent.py` checks worktree cleanliness and ownership, then calls `collect-agent.py` for hashed receipts. Non-integrators record success; integrators return the exact `run_state.promote()` call instead.
4. Promotion compare-and-swaps `refs/heads/herdr/<run-id>` and records promotion plus success. Delivery to a feature branch is separate and create-only; never promote into the primary checkout.
5. `rotate.py` drains native work; `ack-handoff.py` transfers the controller lease and advances its epoch. Reattach persistent workers by recorded identity and run/node/attempt tokens, not labels.

All mutations require the exact `{workspace, pane, session}` holder and appropriate lease; dispatch also requires the current epoch. `run.lock` uses `fcntl.flock`; `dispatch_guard()` keeps authorization and external launch under one lock. Do not call `append_event()` inside that guard. Concurrent writers must have disjoint `owns`, or dependency ordering must serialize them.

Only the bounded-acquisition/owner-diagnostics fallback is approved, not concurrent dispatch. Lock-taking entrypoints accept `--lock-timeout SECONDS` (finite positive, default 120; usage 2, timeout refusal 3); locked facade APIs accept `lock_timeout=120`. Preserve every exclusive critical section, authorization/launch lock extent and nested-append refusal. `run.lock.owner` is unverified diagnostic evidence, never authority or permission to break a lock; see [locking contract](references/run-state-api.md#events-locking-and-dispatch).

## Key Directories

- `scripts/`: executable adapters, importable shared modules, and colocated `test_*.py` suites; there is no `src/` tree.
- `references/`: run/storage contract and pane environment guidance; `references/templates/graphs/` holds built-in graph templates.
- `extensions/herdr-context.ts`: OMP-native measured context export; no Herdr lifecycle reporting or vendor integration code is bundled.
- `docs/plans/`: design notes, not execution authority.
- Runtime state lives outside target repositories under `${XDG_STATE_HOME:-~/.local/state}/herdr-orchestrator/` (`index.db`, `context/`, `runs/<run-id>/{run.db,run.lock,blobs/,artifacts/}`).

## Development Commands

Run from the skill repository root:

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m unittest discover -s scripts -p 'test_run_state.py'
# Parallel per-file stdlib discovery; any failed suite gives a nonzero exit:
python3 -c 'import concurrent.futures, glob, os, subprocess, sys; suites = sorted(glob.glob("scripts/test_*.py")); pool = concurrent.futures.ThreadPoolExecutor(max_workers=8); codes = list(pool.map(lambda p: subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "scripts", "-p", os.path.basename(p), "-q"]), suites)); pool.shutdown(); sys.exit(any(codes))'
python3 scripts/prompt_contract.py
python3 scripts/dispatch-agent.py --help
python3 scripts/ho.py --help
```

Discovery commands run full, focused, or parallel per-file suites; `prompt_contract.py` runs its assertion self-check. Utilities run directly as `python3 scripts/<name>.py`; inspect `--help` before live operations. No build/install step or configured lint, formatter, typecheck, or coverage command exists.

Live initialization uses `python3 scripts/ho.py init --job change-name`. Do not use live state directories for development smoke checks (set `HERDR_ORCHESTRATOR_STATE_DIR` to a temp dir in tests). Plain `reconcile.py --run /absolute/run` repairs projections; compact output is default and `--full` preserves the complete Python return shape.

## Code Conventions & Common Patterns

- Match existing four-space Python indentation, `from __future__ import annotations`, type hints, `snake_case` functions, `_private` helpers, and uppercase constants. Executables use hyphenated filenames; importable modules use underscores.
- Keep argparse adapters thin and import defining modules, not the controller-facing `run_state.py` facade. **Never import `run_state` into `ownership`** or introduce dependency-order cycles. Hyphenated adapters invoke siblings through `cli_common.run_child`; reuse `cli_common.run` for exception/exit handling.
- Preserve CLI contracts: generally one JSON object on stdout and one error line on stderr; monitors emit flushed JSONL. `RunError` maps to exit 1, `UsageError` to 2, `PreconditionError` to 3. Script-specific exceptions include prompt exit 4 for `agent_blocked`, monitor exit 2 for timeout, and ownership exit 3 for findings.
- Operations are synchronous; monitoring runs as a separate background observer, not an asyncio service. Route Herdr through `herdr_cli.py` and Git through existing deadline-bound helpers. Tests replace module-level invokers or patch subprocesses; no dependency-injection framework is used.
- Persist canonical absolute run/artifact paths. Reuse atomic/fsynced writers, SQLite WAL transactions, content-addressed `blobs/`, and hashed receipts. Retry creates a new attempt; `--force` receipt replacement is explicitly audited, not a history rewrite.

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
| `scripts/state_paths.py`, `scripts/run_store.py` | XDG state root, rebuildable `index.db`, per-run SQLite WAL `run.db`, `blobs/`, structured notes, and dynamic brief synthesis. |
| `scripts/dispatch-agent.py`, `scripts/finish-agent.py` | Controller-facing dispatch and completion entry points. |
| `scripts/reconcile.py`, `scripts/rotate.py`, `scripts/ack-handoff.py` | State repair and tokenized controller handoff. |
| `scripts/ho.py`, `scripts/doctor.py`, `scripts/inspect-run.py` | High-level controller facade, read-only environment/protocol preflight, and run status inspector. |
| `scripts/validate-graph.py`, `scripts/render-graph.py`, `scripts/accept-graph.py` | Graph linting, built-in template rendering, and graph acceptance CLI. |
| `scripts/dispatch-wave.py`, `scripts/run-script.py`, `scripts/promote-run.py` | Wave scheduler dispatch, deterministic script node runner, and promotion/delivery CLI. |
| `scripts/invalidate-node.py`, `scripts/verdict.py`, `scripts/run-metrics.py`, `scripts/prune-run.py` | Node invalidation, verdict recording, run metrics, and worktree/ref cleanup. |
| `scripts/context_usage.py`, `scripts/self-improve.py` | Measured OMP context snapshot validation and detached skill self-improvement launcher. |

Reference index (D14): [identity-reattach](references/identity-reattach.md), [rotation](references/rotation.md), [retire-promote](references/retire-promote.md), [run-state-api](references/run-state-api.md). Practical examples: [controller-playbook](references/controller-playbook.md). CLI consumers: [docs/herdr-cheatsheet.md](docs/herdr-cheatsheet.md).

XDG `index.db` + `run.db` are the run locators (zero `.md` handoff/progress/status files on disk). Structured handoff notes (`ho note add`) and `--focus` on `rotate.py --run` feed dynamic successor prompt synthesis. Measured context freshness (>= 50%) and exact nonce/holder transfer remain enforced; precondition refusals exit 3. Prompt-specific retry flags/count output are removed, but spawn/dispatch startup retry controls remain. Output-cost contracts (compact/full reconcile, adaptive monitoring, summary cap and read deadlines) are defined in the D14 references.

## Runtime/Tooling Preferences

Use `python3` on a POSIX platform: locking requires `fcntl`. Python modules use the standard library only. `package.json` is OMP plugin metadata and registers a TypeScript context extension that uses Node built-ins inside OMP; it adds no npm dependencies or build step. `.omp-plugin/marketplace.json` exposes the root skill with `source: "./"` and `skills: ["./"]`. Git is required; live control needs `herdr` on PATH. Model-role lookup invokes `omp config get modelRoles --json`. Keep the context exporter separate from Herdr's stock lifecycle integration and do not load duplicate snapshot writers.

The documented compatibility target is Herdr 0.9.3/protocol 22; `doctor.py` performs an explicit read-only preflight probe (`herdr api schema --json`). Trust installed CLI help; stop on protocol mismatch. Preserve `HERDR_SESSION`/`HERDR_SOCKET_PATH` and controller pane identity variables when launching child processes. Never stop the Herdr server from an active pane or close user-owned surfaces. Keep runtime ledgers and local `.idea/`, `.desloppify/`, and bytecode artifacts out of commits.

## Testing & QA

Use stdlib `unittest` discovery, not pytest. Tests use temporary directories, real temporary Git repositories, `unittest.mock`, and fake Herdr invokers; they do not require a live Herdr server. Hyphenated scripts are loaded with `importlib.util.spec_from_file_location`.

`test_executor_adapters.py` binds module-level `check_*` functions onto `ExecutorDispatchTests`; `test_prompt_contract.py` binds selected checks explicitly. Preserve those bindings so discovery actually runs new checks.

Prioritize observable boundaries: stale epochs/foreign holders must cause no side effects, delivery must be confirmed, mismatched identities must refuse, ownership and receipt hashes must gate success, and rotation/promotion must remain crash-safe. Run the focused suite while developing and full discovery before delivery. No coverage percentage is configured. Mocked adapter tests are not live Herdr end-to-end proof; use controlled runtime verification for changed live behavior.
