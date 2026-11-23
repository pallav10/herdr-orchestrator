# Controller rotation

Use `python3 scripts/ho.py rotate [--focus "..."]` (or `python3 scripts/rotate.py --run <canonical run path> [--focus "..."]`) from the current controller. Optional controls include `--label`, `--kind`, `--model-role`, `--model`, `--timeout`, and `--lock-timeout`. Rotation owns drain, dynamic successor prompt synthesis, successor spawn, and tokenized acknowledgment; do not manually pre-drain or append `lease_transfer` by hand.

## Measured context gate

The sole accepted source is a validated same-process context snapshot (`extensions/herdr-context.ts` for OMP, `scripts/claude-context-hook.py` for Claude Code, or a schema v2 harness snapshot). The snapshot must match the workspace, pane, owning PID, session file/ID (when applicable), and model and pass freshness/shape validation. Rotation requires `tokens * 2 >= context_window`: exactly 50% qualifies.

Footer percentages, estimates, cumulative/session totals, guessed CLI percentages, and fabricated snapshots cannot substitute. Unknown, malformed, ambiguous, unavailable, stale, or below-threshold readings refuse with exit 3 before drain, lifecycle mutation, spawn, or prompt. Refusal evidence includes fraction/source; success JSON includes `context_usage`.

Only a missing/unloaded context export calls for `/restart` in the existing OMP pane to reload the bundled extension; rotation does not auto-restart or bypass a missing measurement. There is no CLI threshold override.

## Zero-markdown handoff and dynamic prompt synthesis

Rotation requires no `HANDOFF.md`, `NEXT-SESSION-PROMPT.md`, or `progress.md` files on disk:

- Record decisions, contracts, blockers, gotchas, and current focus into `run.db` via `ho.py note add --kind <kind> "..."`.
- Pass `--focus "..."` to `ho.py rotate` / `rotate.py --run` to attach the final handoff summary directly to `lease_drain_started`.
- `rotate.py` replays `run.db`, synthesizes a self-contained successor bootstrap prompt containing the exact `ack-handoff.py --nonce <n> --run <run>` command, `ho.py next --reconcile` command, and the rendered run brief (all recorded notes + node states), and passes it straight to the spawned successor.
- The successor can re-print the brief anytime via `ho.py brief --md --run <run>`.

## Drain, acknowledgment, and stand-down

1. Run `ho.py rotate --focus "..."` (or `rotate.py --run <run> --focus "..."`) in the exact active controller holder.
2. After holder and measured context gates pass, `rotate.py` appends `lease_drain_started` (resuming an identical in-progress drain if already draining), stops new dispatch, and waits for provable native quiescence. Unresolved native work or unobservable active Herdr workers refuse/time out without spawning.
3. `rotate.py` spawns the successor with a fresh nonce and the synthesized bootstrap prompt.
4. The successor runs `python3 scripts/ack-handoff.py --nonce NONCE --run RUN` (or `ho.py ack --nonce NONCE --run RUN`).
5. `ack-handoff.py` validates the draining predecessor holder, advances the epoch (`epoch-<N+1>`), appends `lease_transfer` with the successor's workspace/pane/session/nonce, updates XDG `index.db` for the successor pane, and stamps `handoff-ack=NONCE` plus `session=epoch-<N+1>` on the successor workspace. A same-nonce retry after a stamp failure is idempotent and does not advance the epoch again. Wrong nonce, foreign surface, or lineage mismatch refuses (exit 3).
6. The predecessor stands down only when both the matching ledger `lease_transfer` **and** matching workspace `handoff-ack`/`session` tokens identify the actually spawned successor. The successor reconciles (`ho.py next --reconcile`) and reattaches live workers by recorded identity; see [identity-reattach.md](identity-reattach.md).

## Reattachment and retirement

Still-live Herdr workers keep the same dispatch intent/attempt. Exact workspace/pane plus managed `worker_id` or legacy `runtime-id`, context, and required provenance authorize reattachment. Native `task`/`workflow` handles cannot be reattached across controller epochs.

Rotation-created predecessor surfaces are eligible only for successor-driven retirement after tokenized acknowledgment and normal run terminal/receipt gates. User-owned controller surfaces stand down without closure.
