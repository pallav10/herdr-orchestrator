---
name: herdr-orchestrator
description: >-
  Use for bounded user-directed control of existing Herdr panes, or for ONE controller coordinating durable parallel workers, isolated worktrees, and handoffs. Native pane prompting and validated durable graph execution are distinct workflows. Not for tmux splits, generic subagents, or an app's internal orchestrator.
license: MIT
---
# Herdr Orchestrator

One controller (any supported harness: `omp`, `claude`, `codex`, `copilot`, `pi`, `grok`, or custom CLI) owns the task graph and durable run ledger (`events.jsonl`); workers execute slices.
Use the host harness's native subagent/task tool (`task`/`workflow`) for bounded session-local work, `herdr-agent` for durable workers/isolated writers, and `pane` for existing user panes.
## Choose the workflow first
- **Bounded existing-pane work:** use native Herdr controls (`herdr agent prompt` in legacy Herdr, or `hc send`/`hc wait` in `herdr-core`). No graph or session scaffolding needed. See [native pane playbook](references/controller-playbook.md#native-existing-pane-playbook).
- **Durable coordinated execution:** use `scripts/ho.py` below. It auto-resolves legacy Herdr (`HERDR_WORKSPACE_ID`/`HERDR_PANE_ID`) or `herdr-core` (`HC_TERMINAL_ID` via `hc`), canonical `session`/`run`, and current `epoch`, and drives the entire run through `ho next`.

Resolve `ho.py` relative to this skill's installed directory (use absolute path when `cwd` is the target repo).

## The 4-Step `ho.py` Loop
Let `HO="python3 /absolute/skill/scripts/ho.py"`.

1. **Initialize** a durable run (stored in XDG SQLite WAL under `~/.local/state/herdr-orchestrator/runs/<run-id>/run.db` — no repo `.gitignore` or `.md` files needed; add `--pane TARGET` for any planned user-owned pane):
   ```sh
   $HO init --job change-name
   ```
2. **Accept** the task graph (accepts `schema_version` default `1` and `nodes` as dict or `[{id, ...}]` list):
   ```sh
   $HO accept --graph /tmp/graph.json
   ```
   Verify the returned `base_sha` matches the commit your worker briefs assume.
3. **Loop on `ho next`** until `"phase": "done"`:
   ```sh
   $HO next
   ```
   `ho next` inspects replayed + live state and returns ONE JSON object:
   `{"phase": "...", "summary": "...", "nodes": {...}, "commands": [...]}`.
   Execute the exact verbatim commands returned in `commands`:
   - `dispatch`: `$HO dispatch NODE --prompt-file FILE [--kind K] [--cwd DIR] [--model M | --model-role R] [--requested-effort E]`
   - `monitor`: `$HO monitor [--timeout S]` (run in background so controller stays free)
   - `finish`: `$HO finish NODE [--attempt A] [--force]`
   - `promote`: `$HO promote NODE [--attempt A]` (auto-resolves `new_sha` from receipts; no Python heredoc)
   - `retire`: `$HO retire --all`
   - `status`: `$HO status` (`--md` renders a human-readable markdown brief to stdout; `$HO next --reconcile` runs holder-authorized reconcile first)
   - `note` / `brief`: `$HO note add --kind decision|contract|blocker|focus "..."` records structured handoff memory in `run.db`; `$HO brief [--md]` synthesizes the full run + handoff brief on demand
   - `rotate` / `ack`: `$HO rotate --focus "..."` drains and spawns a successor with a dynamically synthesized prompt; successor runs `$HO ack --run <run> --nonce <n>`
4. **Stop** when `ho next` reports `"phase": "done"`.

## Multi-Harness Support
Worker harness selection resolves in order: `--kind` CLI flag → `node.kind` in `graph.json` → `graph.default_kind` → `HERDR_ORCHESTRATOR_KIND` → `omp`.
- Supported built-in kinds/aliases: `omp` (`oh-my-pi`), `claude` (`claude-code`), `codex` (`openai-codex`), `copilot` (`copilot-cli`), `pi`, `grok` (`grok-build`), plus any custom binary name.
- Model/effort flags are translated per harness (`omp`/`pi`: `--model` + `--thinking`; `claude`: `--model` + `--effort`; `codex`: `-m` + `-c model_reasoning_effort=…`; `copilot`: `--model` + `--reasoning-effort`; `grok`: `--model`).
- Role lookup (`--model-role`) uses `omp config get modelRoles --json` for `omp` and `~/.config/herdr-orchestrator/roles.json` for other harnesses (or pass `--model` directly).
- Context rotation supports OMP (`extensions/herdr-context.ts`) and Claude Code (`scripts/claude-context-hook.py` writing schema v2 snapshots to `~/.local/state/herdr-orchestrator/context/`).

## Companion & Low-Level Scripts
Use `ho.py` as the primary front-door. Specialized tools and all underlying low-level scripts remain directly callable when needed:
- **Planning & preflight:** `validate-graph.py` (Tarjan SCC cycles, ownership overlaps, waves), `render-graph.py` (`references/templates/graphs/`), `doctor.py` (env/git/Herdr preflight).
- **Waves, scripts & control:** `dispatch-wave.py` (critical-path/fan-out wave dispatch), `run-script.py` (deterministic `script` nodes), `invalidate-node.py` (selective cascade rerun + `feedback.md`).
- **Inspection & lifecycle:** `inspect-run.py` (timeline + next actions), `run-metrics.py` (durations/retries/tokens), `prune-run.py` (fail-closed worktree/ref GC).
- **Core adapters:** `init-docs.py`, `accept-graph.py`, `dispatch-agent.py`, `spawn-agent.py`, `prompt-agent.py`, `reconcile.py`, `monitor-agents.py`, `collect-agent.py`, `check-ownership.py`, `finish-agent.py`, `promote-run.py`, `retire-agents.py`, `rotate.py`, `ack-handoff.py`, `run_state.py`.
Exit codes: 0 ok, 1 runtime, 2 usage, 3 refusal/precondition (all lock-taking commands accept `--lock-timeout SECONDS`, default 120).

## State authority & Safety
- Default storage is per-run SQLite WAL (`~/.local/state/herdr-orchestrator/runs/<run-id>/run.db`, overridable via `HERDR_ORCHESTRATOR_STATE_DIR`) + content-addressed `blobs/<sha256>` + rebuildable `index.db` for O(1) pane/worktree auto-discovery. Zero files are written into the target repo working tree. Legacy `--dir <repo>/docs` mode remains supported for existing runs.
- The append-only event log (`events` table in `run.db`, or `events.jsonl` in legacy runs) is authoritative. Never treat Herdr `idle`/`done` as controller success.
- Never run overlapping writers concurrently; serialize shared `owns` paths (non-integrator `**` overlaps everything).
- Never retire user-owned `pane` executors, mutate the primary checkout/promotion ref by hand, or stop the active Herdr server.
- Rotate only at fresh measured context occupancy **>=50%** via `$HO rotate --focus "..."` (or `python3 scripts/rotate.py --run RUN`); see [rotation](references/rotation.md).
## Self-improvement loop
Queue distinct, evidenced skill defects in a detached private clone without blocking the current run:
`python3 /absolute/skill/scripts/self-improve.py --skill-repo /absolute/editable-skill-repo --finding "Observed issue; evidence; proposed change; acceptance check"`

## References
- Identity, routing, resume, blocked delivery, worker recovery: [identity-reattach](references/identity-reattach.md)
- Context threshold, successor bootstrap, lease transfer: [rotation](references/rotation.md)
- Finishing writers, promotion, consumption, workspace closure: [retire-promote](references/retire-promote.md)
- Python controller calls, replay shapes, locking contract: [run-state-api](references/run-state-api.md)
- Session/run layout, receipts, metadata: [session-docs](references/session-docs.md)
- Operational history & failure examples: [controller-playbook](references/controller-playbook.md)
- Pane ports & shared infrastructure: [pane-environment](references/pane-environment.md)
