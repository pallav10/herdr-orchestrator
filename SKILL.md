---
name: herdr-orchestrator
description: >-
  Use inside a Herdr session when ONE pane should orchestrate sibling worker panes: plan a task graph, fan out work to agent panes, collect results, rotate the controller to a fresh pane when its context fills, or keep durable run/handoff state across pane exits. Also for planning a Herdr orchestration before entering Herdr. Not for: driving a single agent pane, tmux splits, generic subagents or shell & jobs, or an app's internal orchestrator.
---
# Herdr Orchestrator

## Purpose
One controller owns the task graph and durable run events; workers execute slices.
Use native `task`/`workflow` for bounded session-local work, `herdr-agent` for durable workers/isolated writers, and `pane` for existing user panes.
Planning outside Herdr is supported; live pane operations require the chosen Herdr session (`HERDR_ENV=1`).

## State authority
- Canonical `session.json.run` locates execution; session paths identify pane history, not execution authority.
- Append-only `<run>/events.jsonl` is authoritative; `state.json`, `controller.json`, and `status.md` are disposable replay projections.
- Replay preserves valid pre-transfer history; exact holder, lease, and replayed epoch authorize mutations. Truncated tails refuse mutation.
- Handoff prose supplies human context, not run location or freshness; Herdr `idle`/`done` is not controller success.
- `run.lock` exclusively spans authorization through external launch; bounded acquisition/owner diagnostics follow the approved fallback, not a concurrent-dispatch protocol.

## Toolbox
Run `python3 scripts/<name>.py --help` for flags; scripts print JSON, monitor prints flushed JSONL. Exit codes: 0 success, 1 runtime, 2 usage, 3 refusal; prompt additionally 4 blocked, monitor 2 timeout, ownership 3 findings.
Lock-taking entrypoints accept `--lock-timeout SECONDS`: finite positive, default 120; invalid input exits 2, acquisition timeout refuses with exit 3. See [locking contract](references/run-state-api.md#events-locking-and-dispatch).

- `init-docs.py` — open/reuse the exact pane's gitignored session and canonical run binding.
- `dispatch-agent.py` — controller-facing Herdr dispatch, immutable intent, confirmed launch, ledger transition; `--cwd` is for read-only workers.
- `spawn-agent.py` — low-level create/start/resume with persisted identity and launch sidecars.
- `self-improve.py` — queue a detached improvement pane in a private skill clone, outside the reporting run.
- `prompt-agent.py` — initial pane dispatch or exact-identity blocked-attempt continuation; controller records returned transition.
- `reconcile.py` — repair projections or holder-authorized observation/lifecycle/notifications; compact default, `--full` for complete state.
- `monitor-agents.py` — change-only adaptive polling by default; `--every-poll` prints each poll, `--min-interval` sets initial/reset cadence.
- `collect-agent.py` — status/summaries and immutable receipts; stdout summary capped at 1,500 characters, `--full-summary` uncaps it.
- `check-ownership.py` — classify slice changes; run mode derives the writer's merge-base, manifest mode accepts `--base`/`--since`.
- `finish-agent.py` — collect, validate ownership/receipts, and record success or print integrator promotion call; optional `--since`/`--force`.
- `retire-agents.py` — receipt-gated closure of orchestrator-created surfaces; optional `--collect`/`--force-unsettled` do not bypass run gates.
- `rotate.py` — measured-context gate, lease drain, native quiescence, successor bootstrap and exact acknowledgment.
- `ack-handoff.py` — successor session creation/recovery and exact nonce/holder lease transfer.
- `run_state.py` — controller Python facade; `load_state(run)` returns replayed state (see API reference).

## Orchestration loop
Use absolute `RUN`, `SESSION`, repository paths and concrete holder `H={"workspace": W, "pane": P, "session": SESSION}`; `E` is the replayed epoch. Commands below use `python3 scripts/`.

1. Open: `init-docs.py --dir docs --job JOB`; use returned canonical session/run, not informational symlinks.
2. Accept: `run_state.accept_graph(RUN, graph, repo=REPO, holder=H)` after planning dependencies, ownership, outputs, executors and routing; wait for acceptance before dispatch.
3. Reconcile: `reconcile.py --run RUN`; add `--session SESSION --holder-workspace W --holder-pane P` for observation/lifecycle and `--notify` for edge notifications.
4. Dispatch: `dispatch-agent.py --run RUN --node NODE --session SESSION --holder-workspace W --holder-pane P --epoch E --prompt-file FILE`; contextual writers derive branch/base from the graph, read-only workers may add `--cwd DIR`.
5. Monitor: `monitor-agents.py TARGET... --notify-pane P --label RUN_ID`; use an auto-delivering harness job and/or pane notification, not a detached file-only process.
6. Finish: `finish-agent.py --run RUN --node NODE --attempt N --session SESSION --holder-workspace W --holder-pane P`; default ownership baseline is `git merge-base <promotion_ref> HEAD`, not the original graph base for later waves.
7. Promote integrator: execute the exact `run_state.promote(...)` call printed by finish; it records terminal success. Recover with holder-authorized `reconcile.py --run RUN --repo REPO`.
8. Retire: `retire-agents.py TARGET... --run RUN --session SESSION --holder-workspace W --holder-pane P` after the reference's terminal/consumption/promotion gates.

For native launches, record intent, inject `prepare_dispatch_prompt`, launch, then record handle/model in `dispatched`. Supply `--native-inventory-complete` only for a genuinely complete harness inventory, with one `--live-native-id` per live handle; omission is unknown, explicit empty is authoritative. Full semantics: [run-state-api](references/run-state-api.md).

## Self-improvement loop
While using this skill, automatically queue each distinct, evidenced skill defect or concrete improvement:
`python3 /absolute/skill/scripts/self-improve.py --skill-repo /absolute/editable-skill-repo --finding "Observed issue; evidence; proposed change; acceptance check"`

Continue the ongoing run immediately. The launcher returns a queued-job receipt; it does not wait for cloning, pane startup, or implementation. Never add this worker to the run graph, dependencies, inventory, receipts, or promotion path, and never wait on its result. A maintenance-launch failure is not a failure of the reporting run; existing safety refusals still apply.

The worker uses an independent clone/branch, not the active checkout or installed skill. It must not spawn another improvement worker. Fixes remain private candidates: no automatic merge, push, installation, or reload during the ongoing run. If the editable source is unavailable, report that prerequisite without modifying the installation. Details: [detached improvements](references/identity-reattach.md#detached-self-improvement).

## Rotation trigger
Rotate only at fresh, identity-validated measured context occupancy **>=50%**, using `rotate.py --session SESSION`; low usage means continue in this pane.
Unknown/stale/unavailable measurements refuse before side effects; [rotation procedure and extension recovery](references/rotation.md) covers bootstrap, acknowledgment and worker reattachment.

## Safety
- Never run overlapping writers concurrently; serialize shared paths, and remember non-integrator `**` overlaps everything.
- Never authorize from caches, sidebar order, labels, focused panes, or ambiguous identity; parse exact IDs/tokens from JSON.
- Never use `latest`, relative run paths, or another pane's session as execution/holder authority.
- Never resubmit full prompt text after unconfirmed delivery or classify errors from prose; structured-only single delivery permits only the bounded Enter recovery described in the identity reference.
- Never retire user-owned `pane` executors or replacement surfaces; predecessor closure is successor-driven after tokenized ack and receipt gates.
- Never mutate the primary checkout or checked-out promotion refs by hand; closure retains worktrees/branches and does not delete source/data.
- Never stop the active Herdr server or substitute native jobs for Herdr roles after protocol mismatch; stop operations and resolve the chosen session's mismatch.
- Never pre-drain or hand-append lease drain/transfer events to bypass rotation's measured gate.
Use background/no-focus operations unless the user requests focus.

## Read when
- Identity, routing, resume, blocked delivery or worker recovery: [identity-reattach](references/identity-reattach.md).
- Context threshold, successor bootstrap, lease transfer or extension reload: [rotation](references/rotation.md).
- Finishing writers, promotion, consumption or workspace closure: [retire-promote](references/retire-promote.md).
- Python controller calls, replay shapes or exact inventory semantics: [run-state-api](references/run-state-api.md).
- Session/run layout, receipts and metadata: [session-docs](references/session-docs.md).
- Concrete operational history and failure examples: [controller-playbook](references/controller-playbook.md).
- Pane ports and shared infrastructure: [pane-environment](references/pane-environment.md).
