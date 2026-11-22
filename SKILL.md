---
name: herdr-orchestrator
description: >-
  Use for bounded user-directed control of existing Herdr panes, or for ONE controller coordinating durable parallel workers, isolated worktrees, and handoffs. Native pane prompting and validated durable graph execution are distinct workflows. Not for tmux splits, generic subagents, or an app's internal orchestrator.
license: MIT
---
# Herdr Orchestrator

One controller owns the task graph and durable run ledger (`events.jsonl`); workers execute slices.
Use native `task`/`workflow` for bounded session-local work, `herdr-agent` for durable workers/isolated writers, and `pane` for existing user panes.

## Choose the workflow first
- **Bounded existing-pane work:** use native Herdr controls (`herdr agent prompt` / background wait). No graph or session scaffolding needed. See [native pane playbook](references/controller-playbook.md#native-existing-pane-playbook).
- **Durable coordinated execution:** use `scripts/ho.py` below. It auto-resolves `HERDR_WORKSPACE_ID`, `HERDR_PANE_ID`, canonical `session`/`run`, and current `epoch`, and drives the entire run through `ho next`.

Resolve `ho.py` relative to this skill's installed directory (use absolute path when `cwd` is the target repo).

## The 4-Step `ho.py` Loop
Let `HO="python3 /absolute/skill/scripts/ho.py"`.

1. **Initialize** the pane's gitignored session and canonical run (add `--pane TARGET` for any planned user-owned pane):
   ```sh
   $HO init --dir /absolute/repo/docs --job change-name
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
   - `dispatch`: `$HO dispatch NODE --prompt-file FILE [--cwd DIR] [--model-role R]`
   - `monitor`: `$HO monitor [--timeout S]` (run in background so controller stays free)
   - `finish`: `$HO finish NODE [--attempt A] [--force]`
   - `promote`: `$HO promote NODE [--attempt A]` (auto-resolves `new_sha` from receipts; no Python heredoc)
   - `retire`: `$HO retire --all`
   - `status`: `$HO status` (or `$HO next --reconcile` to run holder-authorized reconcile first)
4. **Stop** when `ho next` reports `"phase": "done"`.

## Companion & Low-Level Scripts
Use `ho.py` as the primary front-door. Specialized tools and all underlying low-level scripts remain directly callable when needed:
- **Planning & preflight:** `validate-graph.py` (Tarjan SCC cycles, ownership overlaps, waves), `render-graph.py` (`references/templates/graphs/`), `doctor.py` (env/git/Herdr preflight).
- **Waves, scripts & control:** `dispatch-wave.py` (critical-path/fan-out wave dispatch), `run-script.py` (deterministic `script` nodes), `invalidate-node.py` (selective cascade rerun + `feedback.md`).
- **Inspection & lifecycle:** `inspect-run.py` (timeline + next actions), `run-metrics.py` (durations/retries/tokens), `prune-run.py` (fail-closed worktree/ref GC).
- **Core adapters:** `init-docs.py`, `accept-graph.py`, `dispatch-agent.py`, `spawn-agent.py`, `prompt-agent.py`, `reconcile.py`, `monitor-agents.py`, `collect-agent.py`, `check-ownership.py`, `finish-agent.py`, `promote-run.py`, `retire-agents.py`, `rotate.py`, `ack-handoff.py`, `run_state.py`.
Exit codes: 0 ok, 1 runtime, 2 usage, 3 refusal/precondition (all lock-taking commands accept `--lock-timeout SECONDS`, default 120).

## State authority & Safety
- `session.json.run` locates execution; append-only `<run>/events.jsonl` is authoritative. Never edit caches (`state.json`, `controller.json`, `status.md`) or treat Herdr `idle`/`done` as controller success.
- Never run overlapping writers concurrently; serialize shared `owns` paths (non-integrator `**` overlaps everything).
- Never use `latest`, relative run paths, or another pane's session as holder authority.
- Never retire user-owned `pane` executors, mutate the primary checkout/promotion ref by hand, or stop the active Herdr server.
- Rotate only at fresh measured context occupancy **>=50%** via `python3 scripts/rotate.py --session SESSION`; see [rotation](references/rotation.md).

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
