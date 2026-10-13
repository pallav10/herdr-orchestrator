# herdr-orchestrator

A skill and standard-library Python scripts for coordinating a dependency graph from one Herdr controller pane. Herdr supplies the terminal workspaces and agent controls; these scripts add durable run state, ownership gates, receipt-backed completion, promotion, and controller handoff.

[SKILL.md](SKILL.md) is the operator entry point. This repository is not an application, an all-in-one job runner, or a replacement for native `task`/`workflow` tools. Planning and offline checks can happen outside Herdr; live pane control belongs inside the chosen Herdr session.

## Install

Add this public catalog and install its plugin with OMP:

```sh
omp plugin marketplace add pallav10/herdr-orchestrator
omp plugin install herdr-orchestrator@pallav10
```

Installation defaults to user scope (all projects). Add `--scope project` to the install command for one project. The catalog is named `pallav10`; the plugin and skill are named `herdr-orchestrator`. See OMP's [marketplace documentation](https://omp.sh/docs/marketplace).

Use the marketplace route: OMP 18.10.1's direct Git installer does not discover this repository's root-level skill. Restart OMP after installation: `/reload-plugins` can refresh the skill, but does not initialize newly installed extension modules.

The package includes the root skill, Python scripts/references, and a context-only OMP extension. The extension exports native measured context to owner-only files under `~/.omp/run/herdr-context` for main-UI Herdr panes; it does not launch workers or replace Herdr's lifecycle integration. It requires OMP's `ExtensionContext.getContextUsage()` and main-agent identity APIs; publication checks use OMP 18.10.1.

For a fresh Herdr setup, install its stock OMP lifecycle integration:

```sh
herdr integration install omp
```

**Existing customized integrations:** this command overwrites Herdr's managed integration file. Back up local changes first. If an older augmented integration already exports context snapshots, migrate that customization back to the stock lifecycle integration before loading this plugin, so only the bundled extension writes context evidence. Never fabricate snapshots to bypass rotation's gate.

OMP loads the skill as `herdr-orchestrator`. Resolve scripts from its installed directory (or use absolute script paths), not the target repository's working directory. No npm dependencies or build step are required.

## Executors

| Executor | What it is |
| --- | --- |
| `task`, `workflow` | Bounded native harness jobs, launched by the controller's harness, not these adapters. Session-local; cannot be reattached after the controller session dies. |
| `herdr-agent` | A managed worker in an orchestrator-created workspace, with an isolated Git worktree for writers/integrators. Can survive controller rotation and be reattached by recorded identity. |
| `pane` | An existing user-owned agent pane, addressed through `prompt-agent.py`. Never retired by this flow. |

`dispatch-agent.py` routes through `spawn-agent.py`, whose contextual mode accepts only `herdr-agent` nodes. It is **not** a dispatcher for native jobs or existing panes. A contextual pane prompt requires an already-recorded `dispatch_intent` and the graph's exact live handle.

## Requirements

- `python3`, standard library only for the orchestration scripts; no build step or pinned Python minor version. The context extension runs inside OMP using Node built-ins. POSIX is required because locking uses `fcntl`.
- `git` on PATH and a target Git repository for ignore checks, ownership, worktrees, and promotion.
- `herdr` on PATH for live control, plus the executable for the selected worker kind. The default worker kind is `omp`.
- `--model-role` resolves roles through `omp config get modelRoles --json`, including when another worker kind is selected. Rotation uses role `default`; detached self-improvement uses role `task` unless overridden.
- For live work, use the controller's `HERDR_ENV=1`, `HERDR_WORKSPACE_ID`, and `HERDR_PANE_ID`; preserve its selected `HERDR_SESSION` and `HERDR_SOCKET_PATH` in child processes. Rotation additionally requires the bundled context extension to be loaded in the main UI and export fresh, identity-validated OMP context usage.

**Herdr compatibility:** CLI help and the bundled API schema were checked with installed Herdr **0.9.3 / protocol 22**. Older reference notes target 0.8.2 / protocol 20. The scripts do not probe versions or negotiate protocols; these offline checks are not proof of live compatibility with the newer protocol. Installed `herdr --help` and `herdr api schema --json` are the syntax/schema authorities.

## Quick start

Run live commands from this skill's root, inside the controller pane. The docs path belongs to the **target repository**, where workers will make changes; invoking initialization writes real state.

**1. In the target repository** (not this checkout), gitignore the runtime state. Init exits 3 if these are not ignored:

```
/docs/sessions/
/docs/runs/
```

**2. From the skill root**, initialize with an absolute target docs path. This writes runtime state; it is not a dry run. An existing valid same-job session/run may be reused.

```
python3 scripts/init-docs.py --dir /abs/path/to/target-repo/docs --job my-job
```

The JSON response contains canonical absolute `session` and `run` paths. Retain those exact values; never resolve `docs/sessions/latest` as a write target. `--new` forces a fresh session; `--run /absolute/existing/run` explicitly binds an existing run under the same docs root.

**3. Accept the graph before launching work.** Use `run_state.accept_graph(RUN, graph, repo=REPO, holder=H)`, where `H` contains the controller's workspace, pane, and concrete session path. Graph acceptance validates dependencies and ownership, pins the canonical repository and its current `HEAD`, and initializes `refs/heads/herdr/<run-id>`. Choose the intended committed base first: uncommitted changes are not part of that SHA.

The controller-facing [run_state.py](scripts/run_state.py) is an import facade, not a CLI; make `scripts/` importable when using it outside the harness. Graph acceptance, promotion, and explicit branch delivery are Python APIs. See the [orchestration loop](SKILL.md#orchestration-loop) and [API contract](references/run-state-api.md).

**4. Reconcile and dispatch a managed node.** Set `RUN` and `SESSION` to the initializer's returned paths; set `NODE` to a graph node and `EPOCH` to the replayed epoch printed by reconciliation.

```sh
python3 scripts/reconcile.py --run "$RUN"
python3 scripts/dispatch-agent.py \
  --run "$RUN" --node "$NODE" --session "$SESSION" \
  --holder-workspace "$HERDR_WORKSPACE_ID" --holder-pane "$HERDR_PANE_ID" \
  --epoch "$EPOCH" --prompt-file /abs/path/to/worker-prompt.txt
```

With only `--run`, reconciliation repairs replay projections without calling Herdr or appending lifecycle events; it does not advance planned nodes to ready. `dispatch-agent.py` performs holder-authorized readiness reconciliation before launch. Unchanged cache bytes are untouched. Output is compact by default; `--full` returns the complete scheduler result. Add the complete holder triple for live observation/lifecycle work, and `--notify` for edge notifications.

## Controller operations

Use `python3 scripts/<name>.py --help` for current flags. The adapters are synchronous; monitoring is a separate background observer.

| Operation | Interface and boundary |
| --- | --- |
| Launch/recover a managed attempt | `dispatch-agent.py` records intent and invokes `spawn-agent.py --resume`. Writers/integrators get isolated worktrees at the accepted base; `--cwd` applies to read-only nodes. Read-only is a task convention, not a filesystem sandbox. |
| Prompt an existing pane or blocked attempt | `prompt-agent.py` checks the recorded live identity and run/node/attempt tokens. Initial managed-worker launch belongs to `spawn-agent.py`, not this adapter. |
| Observe workers | `monitor-agents.py` emits change-only JSONL; cadence starts/resets at 2 seconds and backs off to 30 seconds by default. Use an auto-delivering background job or `--notify-pane` so settlement wakes the controller. `--every-poll` disables output suppression. |
| Collect evidence | `collect-agent.py` reads the OMP session transcript, with terminal-output fallback. Stdout summaries are capped at 1,500 characters; `--full-summary` uncaps them. Contextual receipts retain the full summary. |
| Check and finish | `check-ownership.py` classifies changes. `finish-agent.py` verifies cleanliness, ownership, declared outputs, and receipts; it records non-integrator success or prints the exact `run_state.promote(...)` call for an integrator. |
| Retire workers | `retire-agents.py` closes only eligible orchestrator-owned workspaces. Live settlement alone is insufficient; run, identity, completion, and integration-consumption gates still apply. |
| Rotate the controller | `rotate.py` requires fresh measured OMP context occupancy **at least 50%**, drains the lease, and waits for native work to quiesce. The successor uses `ack-handoff.py` to transfer the exact nonce/holder and advance the epoch; the predecessor is not automatically closed. |

Prompt delivery goes through [herdr_cli.deliver_prompt](scripts/herdr_cli.py): submit once with `--wait --until working`; only structured stalled/timeout responses allow one Enter-and-wait recovery. Prompt text is never resubmitted as a retry. Launch sidecars preserve enough evidence to refuse ambiguous resume rather than create a second worker.

`run_state.promote(...)` compare-and-swaps the run-owned ref and records promotion plus integrator success. Once the whole graph is succeeded/skipped, it also attempts create-only delivery to a separate feature branch. A delivery refusal is returned as `delivery_note` without undoing promotion; `run_state.deliver_branch(...)` can explicitly retry with another branch name. Neither operation checks out or updates the primary branch.

## State and safety

```text
docs/sessions/<id>/
  session.json, progress.md, HANDOFF.md, NEXT-SESSION-PROMPT.md
docs/runs/<run-id>/
  run.json, graph.json, events.jsonl
  graph-revisions/, artifacts/<node>/<attempt>/
  state.json, controller.json, status.md, run.lock
```

- `session.json.run` is the run locator. `events.jsonl` is execution authority; accepted graph revisions are immutable payloads referenced by `graph_accepted` events. `state.json` / `controller.json` / `status.md` are replay projections. Do not edit caches, graph files, session prose, or Herdr `idle`/`done` state to declare success.
- Authorization uses the exact workspace + pane + concrete session holder and replayed lease. Dispatch and contextual prompting also require the replayed epoch; never authorize from `controller.json`, labels, focus, or sidebar order.
- Concurrent writers need disjoint `owns`; serialize overlapping work. Keep writer/integrator worktrees and branches until matching integration receipts exist, including after failed promotion.
- Close only surfaces this flow created. User-owned panes are never retired; never stop the Herdr server from an active session. Native inventory omission means unknown, not empty; `--native-inventory-complete` is an explicit completeness assertion.
- Lock-taking adapters accept finite-positive `--lock-timeout SECONDS` (default 120); locked facade APIs accept `lock_timeout=120`. Invalid values exit 2; acquisition timeout refuses with exit 3. The exclusive authorization-through-launch critical section is preserved. `run.lock.owner` is unverified diagnostic evidence, never permission to break a lock. See the [locking contract](references/run-state-api.md#events-locking-and-dispatch).

## Detached self-improvement

While using the skill, queue a distinct evidenced defect or concrete improvement without adding it to the reporting run:

```sh
python3 scripts/self-improve.py \
  --skill-repo /absolute/editable-skill-repo \
  --finding "Observed issue; evidence; bounded change; acceptance check"
```

The command returns a queued-job receipt immediately. Its detached launcher clones the source's **committed HEAD** into `~/.omp/run/herdr-improvements/<job>/checkout`, removes the clone's origin, and starts a no-focus worker on an independent branch. Source-checkout WIP is not copied. A queued receipt does not prove worker startup or completion; launcher evidence is kept in the job directory.

This path makes no active-run graph/ledger changes and does not merge, push, install, or reload a candidate fix. The worker is instructed to leave the source checkout, installed skill, reporting run, and its panes untouched, and to produce `change.patch` and `result.json` for separately authorized review/deployment. See the [operator contract](references/identity-reattach.md#detached-self-improvement).

## Safe checks

Static Herdr inspection does not require attaching to or controlling a session:

```sh
herdr --version
herdr --help
herdr api schema --json
```

Adapter `--help` exits during argument parsing, before Herdr, Git, or ledger access:

```sh
python3 scripts/init-docs.py --help
python3 scripts/dispatch-agent.py --help
python3 scripts/finish-agent.py --help
python3 scripts/reconcile.py --help
python3 scripts/self-improve.py --help
```

Do not run bare `herdr` for discovery: it launches/attaches the TUI. CLI help, bundled-schema inspection, and mocked adapter suites are not live orchestration proof. Use disposable repositories/run directories for development smoke checks, never an active run.

Most adapters emit one JSON object; monitoring emits flushed JSONL. General exits are 0 success, 1 runtime failure, 2 usage, and 3 refusal. `prompt-agent.py` additionally exits 4 for `agent_blocked`; monitor exits 2 for timeout and 3 for a vanished agent; ownership exits 3 for findings.

## Development

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
python3 -m unittest discover -s scripts -p 'test_run_state.py'
python3 scripts/prompt_contract.py
```

## Layout

- [SKILL.md](SKILL.md): operator contract and orchestration loop.
- [references/session-docs.md](references/session-docs.md) and [references/controller-playbook.md](references/controller-playbook.md): session/run layout and operational examples.
- Situational contracts: [identity/reattachment](references/identity-reattach.md), [rotation](references/rotation.md), [retirement/promotion](references/retire-promote.md), [run-state API](references/run-state-api.md), and [pane environment](references/pane-environment.md).
- [docs/herdr-cheatsheet.md](docs/herdr-cheatsheet.md): CLI/adapter examples; check installed help before live use.
- [scripts/](scripts/): executable adapters, importable modules, and colocated stdlib `test_*.py` suites.

Implementation boundaries:

| Modules | Responsibility |
| --- | --- |
| [run_state.py](scripts/run_state.py) | Stable controller-facing import facade; adapters import defining modules directly. |
| [session_docs.py](scripts/session_docs.py) | Canonical session paths, metadata/run binding, templates, and carry-forward. |
| [ledger.py](scripts/ledger.py), [authz.py](scripts/authz.py) | Event/revision validation, replay, receipt validation, immutable dispatch snapshots, caches, locks, and exact-holder dispatch guards. |
| [graph_model.py](scripts/graph_model.py), [ownership.py](scripts/ownership.py) | Graph/identity schema and path-ownership classification. |
| [artifacts.py](scripts/artifacts.py), [receipts.py](scripts/receipts.py) | Attempt prompt/consumed-artifact handling and receipt interfaces; receipt validators are defined in `ledger.py`. |
| [routing.py](scripts/routing.py), [liveness.py](scripts/liveness.py), [context_usage.py](scripts/context_usage.py) | Model policy, identity/live observation, native inventory, and measured OMP context validation. |
| [promotion.py](scripts/promotion.py), [scheduler.py](scripts/scheduler.py) | Promotion/recovery/create-only delivery and lifecycle reconciliation/notifications. |
| [ioutil.py](scripts/ioutil.py), [cli_common.py](scripts/cli_common.py), [herdr_cli.py](scripts/herdr_cli.py) | Atomic I/O and deadline-bound Git, adapter/child boundaries, and the single Herdr subprocess/confirmed-prompt boundary. |
| [extensions/herdr-context.ts](extensions/herdr-context.ts) | Native main-UI context snapshot export for the unchanged Python rotation gate; separate from Herdr's lifecycle integration. |

Optional visuals, not execution procedures: [index](docs/diagrams/visual/index.html), [orchestration loop](docs/diagrams/herdr-orchestration-loop.html), and [dynamic orchestration](docs/diagrams/herdr-dynamic-orchestration.html).

## License

[MIT](LICENSE). Plugin metadata lives in [package.json](package.json) and the publisher catalog in [.omp-plugin/marketplace.json](.omp-plugin/marketplace.json).
