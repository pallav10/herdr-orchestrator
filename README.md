# herdr-orchestrator

A skill and Python scripts for running a multi-agent job from one Herdr controller pane. The controller plans a dependency graph, records durable run events, and delegates work to sibling worker panes.

[SKILL.md](SKILL.md) defines the operational contract: graph acceptance, dispatch, monitoring, completion, promotion, rotation, and retirement. This skill is not for single-pane work, tmux, generic subagents, or an application's internal orchestration.

## Executors

| Executor | What it is |
| --- | --- |
| `task`, `workflow` | Bounded native harness jobs. Not launched by these scripts; session-local and cannot be reattached after the controller session dies. |
| `herdr-agent` | Managed worker pane created by `spawn-agent.py`. Long-lived or isolated writer; survives rotation. |
| `pane` | An existing user pane that gets prompted. Never retired by this flow. |

## Requirements

- `python3`, standard library only (no install step, no pinned minor version in this repo). POSIX only: the run lock uses `fcntl`.
- `git` on PATH (ignore checks, ownership diffs, promotion).
- `herdr` on PATH for any live control. The documented target is Herdr 0.8.2 / protocol 20; **the scripts do not check the version**. Installed `herdr --help` is the CLI authority.
- `omp` on PATH, only when `--model-role` is used.
- Run live operations inside a Herdr controller pane with `HERDR_WORKSPACE_ID` and `HERDR_PANE_ID`. Preserve the selected session's `HERDR_SESSION` / `HERDR_SOCKET_PATH` when launching child processes.

## Quick start

Run the commands below from this skill's root, inside the controller pane. The docs path belongs to the **target repository**, where workers will make changes.

**1. In the target repository** (not this checkout), gitignore the runtime state. Init exits 3 if these are not ignored:

```
/docs/sessions/
/docs/runs/
```

**2. From the skill root**, initialize with an absolute target docs path. This writes runtime state; it is not a dry run. An existing valid same-job session/run may be reused.

```
python3 scripts/init-docs.py --dir /abs/path/to/target-repo/docs --job my-job
```

It prints one JSON object with canonical absolute `session` and `run` paths. Keep those exact paths; never use `docs/sessions/latest` as a write target.

**3. Follow the [orchestration loop](SKILL.md#orchestration-loop)** to accept the graph, dispatch workers, and collect verified results. Graph acceptance, promotion, and branch delivery use the `run_state` Python API, not standalone CLI commands.

Cache repair only (atomically replaces/fsyncs missing, damaged, or changed projections; unchanged bytes are untouched). CLI output is compact by default; `--full` prints the complete Python reconcile shape:

```
python3 scripts/reconcile.py --run /abs/path/to/run
```

## State and safety

```
docs/sessions/<id>/{progress.md,HANDOFF.md,NEXT-SESSION-PROMPT.md,session.json}
docs/runs/<run-id>/{run.json,graph.json,events.jsonl,state.json,controller.json,status.md,run.lock,artifacts/}
```

- `events.jsonl` is the fact log. `state.json` / `controller.json` are replay caches; `status.md` is disposable. Session markdown is history, not proof of success. Herdr `idle`/`done` is observation only.
- A holder is workspace + pane + concrete session path. Pass the replayed epoch; do not authorize from the lease cache.
- Concurrent writers need disjoint `owns` paths; serialize overlapping work. Integrators combine dependency outputs in their own worktrees.
- Close only surfaces this flow created. Existing user panes are never retired. Never stop the Herdr server from an active session.
- Promotion updates a run-owned ref, not the primary checkout; feature-branch delivery is separate. Keep writer/integrator worktrees and branches until matching integration receipts exist, including after failed promotion.
- Only bounded lock acquisition and unverified owner diagnostics are approved, not concurrent-dispatch locking. Lock-taking adapters accept `--lock-timeout SECONDS` (finite positive, default 120; usage 2, timeout refusal 3), and locked facade calls accept `lock_timeout=120`; exclusive critical-section extent is unchanged. See the [locking contract](references/run-state-api.md#events-locking-and-dispatch).

## Detached self-improvement

While the skill is in use, concrete defects/improvements trigger `scripts/self-improve.py --finding TEXT --skill-repo /absolute/editable-skill-repo`. It queues a background launcher and returns immediately; a new no-focus worker implements the finding in an independent clone/branch. The active run gets no dependency or ledger change, and its checkout and installed skill are untouched. Candidate fixes require separately authorized review/deployment after the run. See the [operator contract](references/identity-reattach.md#detached-self-improvement).

## Safe checks

`--help` exits during argument parsing, before any Herdr, git or ledger access:

```
python3 scripts/init-docs.py --help
python3 scripts/dispatch-agent.py --help
python3 scripts/reconcile.py --help
python3 scripts/self-improve.py --help
```

Other adapters also expose `--help`. Use it for current flags rather than relying on module docstrings. These checks do not exercise a live orchestration.

## Development

```
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 scripts/prompt_contract.py     # self-check, asserts only, no I/O
# Independent stdlib suites in eight processes (nonzero if any suite fails):
python3 -c 'import concurrent.futures, glob, os, subprocess, sys; suites = sorted(glob.glob("scripts/test_*.py")); pool = concurrent.futures.ThreadPoolExecutor(max_workers=8); codes = list(pool.map(lambda p: subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "scripts", "-p", os.path.basename(p), "-q"]), suites)); pool.shutdown(); sys.exit(any(codes))'
```

## Layout

- [SKILL.md](SKILL.md): contract, toolbox, orchestration loop.
- [references/session-docs.md](references/session-docs.md): session/run layout and gates.
- [references/pane-environment.md](references/pane-environment.md): ports and shared queues (convention, not enforced).
- [references/controller-playbook.md](references/controller-playbook.md): practical controller examples.
- Situational reference index (D14): [identity/reattachment](references/identity-reattach.md), [rotation](references/rotation.md), [retirement/promotion](references/retire-promote.md), [run-state API/output](references/run-state-api.md).
- [docs/herdr-cheatsheet.md](docs/herdr-cheatsheet.md): installed Herdr CLI and adapter examples.
- [scripts/](scripts/): CLI adapters, shared modules, and colocated `test_*.py` suites. `run_state.py` is a library, not a CLI.
- Defining modules (D5): `scripts/ioutil.py` (I/O/errors/git), `graph_model.py` (graph/ownership model), `ledger.py` (events/replay/caches), `authz.py` (leases/guards), `artifacts.py` (dispatch snapshots), `receipts.py` (VCS/integration evidence), `routing.py` (model policy), `liveness.py` (identity/observation), `promotion.py` (CAS/recovery/delivery), `scheduler.py` (lifecycle/reconcile), and `cli_common.py` (adapter/child boundary). `run_state.py` is the stable controller-facing facade; adapters import defining modules.
- [visual/index.html](docs/diagrams/visual/index.html), [herdr-orchestration-loop.html](docs/diagrams/herdr-orchestration-loop.html), [herdr-dynamic-orchestration.html](docs/diagrams/herdr-dynamic-orchestration.html): optional diagrams, not procedure.
