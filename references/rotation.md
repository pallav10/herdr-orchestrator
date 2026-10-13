# Controller rotation

Use `python3 scripts/rotate.py --session <canonical predecessor session>` from the current controller. Optional controls include `--label`, `--model-role`, `--timeout`, and the approved fallback `--lock-timeout` described in [run-state-api.md](run-state-api.md#events-locking-and-dispatch). Rotation owns drain, successor creation and acknowledgment waiting; manually pre-draining bypasses the intended ordering.

## Measured context gate

The sole accepted source is the bundled `extensions/herdr-context.ts` main-UI extension's atomic `ExtensionContext.getContextUsage` snapshot (the live SessionStatsTracker used by RPC `get_state`). The snapshot must match the workspace, pane, owning PID, OMP session file/ID, and model and pass freshness/shape validation. Rotation requires `tokens * 2 >= context_window`: exactly 50% qualifies.

Footer percentages, estimates, cumulative/session totals, guessed CLI percentages and fabricated snapshots cannot substitute. Unknown, malformed, ambiguous, unavailable, stale or below-threshold readings refuse with exit 3 before drain, lifecycle mutation, spawn or prompt. Refusal evidence includes fraction/source (fraction may be null); success JSON includes `context_usage`. A fresh reading below 50% means continue in the same pane without restart until enough context is used. Measurement problems are resolved in this pane, not by silently launching another controller.

Only a missing/unloaded context export calls for `/restart` in the existing OMP pane to reload the bundled extension; rotation does not auto-restart or bypass a missing measurement. Preserve the chosen Herdr environment and safe controller procedure. There is no CLI threshold override.

Herdr's stock OMP integration remains responsible for lifecycle/session reporting; it is not assumed to export measured context. The public plugin supplies that exporter separately. Older locally augmented integrations may already write the same snapshot path: back up their changes and restore stock lifecycle reporting before loading this plugin to avoid competing writers. See [installation](../README.md#install).

## Metadata and bootstrap resources

`session_docs.session_run` resolves `session.json.run` to a canonical absolute run under the same docs root, validating run metadata. Session/run paths are concrete values, not `latest` symlinks. Wrong, noncanonical, out-of-root or malformed metadata refuses before side effects; rotate and ack use exit 3 for these domain preconditions.

`HANDOFF.md` and `NEXT-SESSION-PROMPT.md` remain required readable successor bootstrap resources, checked before drain/spawn. The successor must actually read relevant handoff context (decisions, constraints, blockers, current focus), not merely replay a ledger and discard the notes. An optional run link in markdown is informational; there is no markdown run locator/comparison. `progress.md` is optional pane history, not a rotation/ack prerequisite. Note age is not an execution gate: old timestamps do not refuse rotation. There is no document-age option or handoff-mtime acknowledgment token; context-snapshot freshness above is independent and remains enforced. Existing/historical notes are retained, not deleted or rewritten to migrate execution authority.

Initializer reuse/backfill follows [session-docs](session-docs.md): a same-pane same-job missing/null/empty run can be canonically bound without rewriting existing notes; a nonempty invalid binding is not absence. Conflicting explicit run reuse refuses unless a new session was requested. A prose parent is not rewritten as a current controller. Successor creation defers initial run binding until the exact transfer succeeds.

## Drain, acknowledgment and stand-down

1. Preserve human handoff context and verify both successor resources. Run `rotate.py --session SESSION --label successor --model-role default` in the exact predecessor holder.
2. After metadata, identity and context gates, rotate appends `lease_drain_started` (or resumes an existing exact-holder drain). It stops new dispatch and waits for provable native quiescence; unresolved native work refuses/times out without spawning. Native state cannot be inferred empty from an absent harness inventory. Rotation does not claim a complete native inventory it does not possess. Unknown Herdr observation for active owned workers also prevents unsafe progress.
3. Rotate spawns the successor with a fresh nonce and exact predecessor path. The successor reads the handoff/bootstrap context and runs `python3 scripts/ack-handoff.py --nonce NONCE --session PREDECESSOR_SESSION`. It uses the canonical run from metadata and the acknowledgment result, not a parsed handoff path.
4. Ack validates the draining exact predecessor holder, creates one canonical successor session with correct lineage, and appends `lease_transfer` naming the exact successor workspace/pane/session and nonce. Transfer advances the epoch and restores active dispatch. It stamps `handoff-ack=NONCE` plus successor `session` metadata. A same-nonce retry after stamp failure recovers the transferred successor without another session or transfer. Wrong nonce, foreign surface, holder or lineage refuses.
5. The predecessor stands down only when the matching ledger transfer **and** matching ack/session tokens identify the actually spawned successor. An ack token alone is insufficient. The successor reconciles the concrete acknowledged run under its new exact holder, then reattaches existing workers by [identity-reattach](identity-reattach.md).

`ack-handoff.py` returns `acked`, `workspace`, `nonce`, canonical `session`, `session_id`, `parent`, and `run`. Rotation's result identifies predecessor/successor, predecessor session ID, run, retirement mode and `context_usage`. The new controller passes the replayed new epoch; stale predecessor epochs/holders cause no side effects.

## Reattachment and retirement

Still-live Herdr workers keep the same dispatch intent/attempt. Exact workspace/pane plus managed worker ID or legacy runtime-id, context and required provenance authorize reattachment. Same-attempt blocked continuation records the returned `attempt_started` only after confirmed delivery; a new approved retry attempt gets a new intent. Native task/workflow handles cannot be reattached across controller sessions; interruption/retry rules and complete-inventory semantics are in [run-state-api](run-state-api.md).

Rotation-created predecessor surfaces are eligible only for successor-driven retirement after tokenized acknowledgment and the normal run terminal/receipt gates. User-owned controller surfaces stand down without closure. Rotation is not a worker success event or completion-notification edge; receipt/promotion boundaries remain in [retire-promote](retire-promote.md).

If an old controller has imported old modules, reload/restart safely before adopting this bootstrap contract. Do not mix old/new bootstrap rules or rewrite an active ledger to migrate notes.
