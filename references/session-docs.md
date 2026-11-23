# Run storage, state authority, and handoff contract (v2)

All orchestration runtime state lives outside target repositories under the XDG state directory (overridable via `HERDR_ORCHESTRATOR_STATE_DIR`):

```text
${XDG_STATE_HOME:-~/.local/state}/herdr-orchestrator/
├── context/                          # measured context snapshots (OMP / Claude Code)
├── index.db                          # rebuildable SQLite WAL lookup: (ws, pane) -> active run
└── runs/
    └── <run-id>/
        ├── run.db                    # SQLite WAL: sole authoritative store for this run
        ├── run.lock                  # POSIX fcntl lock strictly for external side-effect sections
        ├── blobs/<sha256>            # write-once content-addressed payloads
        └── artifacts/<node>/<n>/     # per-attempt receipts and dispatch identities
```

No files are written inside target repository working trees, no `.gitignore` rules are required, and no `.md` handoff/progress/status files are persisted on disk.

## Authority

- `<run>/run.db` (`events` table, append-only) is the sole execution authority. Every state mutation appends to `events` and updates `kv` (`run`, `controller`, `state`, `graph`) and `notes` atomically in one SQLite transaction.
- `index.db` is a disposable, rebuildable lookup index mapping `(workspace, pane) -> (run_id, run_dir, session_id)` and `repo_common_dir -> runs`. Delete and rebuild anytime via `state_paths.rebuild_index()`.
- `run.lock` (`fcntl.flock`) guards external side-effect critical sections (`herdr` / `git`). SQLite transactions are always short-lived and never held across subprocesses.
- Herdr `idle`/`done` is observation only. Success is a reconciled run event after collection, ownership, integration, and promotion gates pass.
- Notify once per actual edge into `blocked`, `failed`, or all-terminal. Retry can enter those completion edges again. Rotation alone is not a completion edge.

## Run initialization and pane binding

`ho.py init --job <name> [--repo PATH] [--new] [--run PATH] [--pane TARGET ...]` creates or reuses a run under `${XDG_STATE_HOME:-~/.local/state}/herdr-orchestrator/runs/<run-id>/`:

- `<run-id>` = `YYYYMMDD-HHMMSS-<job-slug>`, with a `-NNN` suffix on same-second collisions (claimed atomically via `mkdir`).
- Controller session IDs are epoch-scoped (`epoch-1`, `epoch-2`, ...), stored in `run.db` and `index.db`.
- Same pane + same job reuses the active run via `index.db` unless `--new` is passed.
- Works identically from the primary checkout or any linked Git worktree (resolved via `git rev-parse --git-common-dir`).
- For user-owned pane plans, supply every target with repeatable `--pane`: read-only stable-ID preflight must pass for all targets before run creation or binding. Missing `agent_id`/`id` refuses with exit 3; CLI ambiguity/protocol errors propagate with exit 1.

## Structured handoff notes and on-demand briefs

Instead of mutable `.md` files (`HANDOFF.md`, `NEXT-SESSION-PROMPT.md`, `progress.md`, `status.md`), controller memory is recorded as structured `note_recorded` events in `run.db`:

```sh
ho.py note add --kind decision "Keep promotion ref clean"
ho.py note add --kind contract "API returns ISO-8601 UTC timestamps"
ho.py note add --kind blocker  "Waiting on schema approval"
ho.py note add --kind focus    "Finish slice-a then integrate"
ho.py note list [--kind KIND] [--epoch N]
```

Valid note kinds: `decision`, `contract`, `blocker`, `focus`, `gotcha`, `handoff`.

Synthesize a complete JSON or markdown brief on demand at any time:

```sh
ho.py brief           # JSON with run metadata, notes, nodes, and rendered markdown
ho.py brief --md      # Markdown to stdout (nothing written to disk)
ho.py status --md     # Same on-demand markdown brief
```

## Mutating run operations

Once controller identity is bound, mutating operations require the exact holder triple `{workspace, pane, session}` (auto-resolved by `ho.py` from Herdr env + `index.db` + `run.db`):

```sh
python3 scripts/reconcile.py --run <absolute run path> --event '<json>' \
  --holder-workspace "$HERDR_WORKSPACE_ID" --holder-pane "$HERDR_PANE_ID" --session epoch-1
```

Lock-taking adapters accept `--lock-timeout SECONDS` (finite positive, default 120; invalid values exit 2, timeout exits 3). See [run-state-api.md](run-state-api.md#events-locking-and-dispatch).

## Rotation lifecycle

See [rotation.md](rotation.md) for the measured context gate (>= 50%), native drain, dynamic successor prompt synthesis (`rotate.py --run RUN [--focus "..."]`), and tokenized epoch transfer (`ack-handoff.py --run RUN --nonce N`).

## Collection, ownership, promotion, and retirement gates

- Node IDs match `[A-Za-z0-9][A-Za-z0-9._-]{0,31}`. Integration is output `type: integration`, not a magic ID.
- Attempt dirs and snapshot files stay inside the canonical run `artifacts/` tree (resolved, no symlink escape). Large immutable payloads may be stored in content-addressed `blobs/<sha256>` via `run_store.put_blob` / `get_blob`.
- Collection, ownership, promotion, delivery, and retirement invariants are unchanged; see [retire-promote.md](retire-promote.md) and [identity-reattach.md](identity-reattach.md).
