# Session-persistence document formats

Session docs are pane history. Run dirs are execution state. Keep both under the same docs root and keep both local/gitignored:

```
docs/sessions/<id>/{progress.md,HANDOFF.md,NEXT-SESSION-PROMPT.md,session.json}
docs/sessions/latest -> <id>            # informational pointer only
docs/runs/<run-id>/{run.json,graph.json,events.jsonl,state.json,controller.json,status.md,run.lock,artifacts/...}
```

Persist canonical absolute paths only. Prompts and metadata must never cite `latest` or a relative `docs/runs/...` path as authority.

## Authority

- `docs/runs/<run-id>/events.jsonl` is the append-only fact log and execution authority.
- `state.json` and `controller.json` are atomically materialized caches from replay/reconcile. Replay keeps valid pre-transfer history; post-transfer old-epoch mutations are rejected. Callers pass the current replayed epoch and never treat the cache as mutation authority.
- `status.md` is derived and disposable.
- `progress.md` is optional pane history, not the graph, ledger, success marker, worker state, or a rotation/acknowledgment prerequisite.
- Herdr `idle`/`done` is observation only. Success is a reconciled run event after collection, ownership, integration, and promotion gates pass.
- Notify once per actual edge into `blocked`, `failed`, or all-terminal. Retry can enter those completion edges again. Rotation alone is not a completion edge.
- Canonical `session.json.run` is the sole session run locator; markdown links are informational. Resolve it through `session_docs.session_run`, which validates canonical absolute paths, same-docs-root containment, and run metadata.

## Session identity

`<id>` = `YYYYMMDD-HHMMSS-<job-slug>`, plus a zero-padded `-NNN` suffix when panes start the same job within one second. The directory is claimed by `mkdir`, so concurrent starts cannot collide.

`session.json` records at least:

```json
{
  "id": "<session id>",
  "job": "<job>",
  "owner": "<workspace-or-pane-or-unknown>",
  "created": "<iso timestamp>",
  "workspace": "<workspace id or null>",
  "pane": "<pane id or null>",
  "parent": "<predecessor session or null>",
  "run": "<canonical absolute run path>"
}
```

`owner` is `HERDR_WORKSPACE_ID`, else `HERDR_PANE_ID`, else `unknown`. Reuse of an existing session requires a real workspace/pane identity, matching job, and an existing absolute `session.json.run` whose directory contains `run.json`. Outside Herdr there is no identity, so every start opens a fresh session. A fresh session may carry prose forward, but it still binds exactly one immutable absolute run path.

`init-docs.py --dir docs --job <name> [--run PATH]` prints JSON with `session` and `run`. Both are canonical absolute; `session` is the same path stored as `holder.session`. Never CWD-relative; never `latest`.

## Run binding

`session_docs.open_session(..., run=None)` returns the bound absolute run path as `run` and stores the same value in `session.json.run`.

Binding order:

1. Reuse this pane's existing same-job session only if its stored `run` is absolute and points at an existing run dir with `run.json`. A conflicting explicit `run` is refused with no new session or run writes unless `force_new` / `init-docs.py --new` or an explicit new-session `parent` (ack-handoff) authorizes a fresh session.
2. Else if this pane's current same-job session has `run` missing, null, or `""`, reuse that directory: claim or bind a canonical run, write `session.json.run` plus the exact holder, and do not rewrite progress/HANDOFF/NEXT.
3. Else bind the explicit `run` argument or predecessor metadata `run`; it must resolve to an existing run dir with `run.json`.
4. Else call `run_state.claim_run(docs_root, job)` and store the claimed absolute run path.

`--new` / `force_new` skips reuse and opens a fresh session (new claimed run, or the explicit `run` if provided). Ack-handoff passes `parent` plus the predecessor's absolute run with `defer_run_binding=True`; that writes a successor session without mutating the predecessor or calling `bind_session_run` until `lease_transfer`. Outside Herdr there is no identity, so every start opens a fresh session. A different job or different workspace/pane owner is not a conflict with this pane's binding.

A nonempty invalid or noncanonical `run` is not absence: do not migrate that session in place; open a fresh session. A predecessor used only as a prose parent is never rewritten.

Relative run paths are refused. Sessions and runs must both live under the same docs root; `init-docs.py` fails closed if `docs/sessions/` or `docs/runs/` is not ignored by git.


## Template contract

### `progress.md`

Optional informational pointer stub:

```markdown
# Progress — <job>

Run: <canonical absolute run path>

This file is pane notes, not the live ledger. Resolve the authoritative run locator from session.json.run; events.jsonl under that run is execution authority.
```

No slice table belongs here. On-disk slice tables are history; new `progress.md` is an informational pointer stub and may be absent for rotation/acknowledgment. The graph is `graph.json`; reconcile the canonical metadata run before reading derived `state.json`.

### `HANDOFF.md`

Carry forward decisions, constraints, blockers, cross-slice contracts, gotchas, orchestrator surface, and current focus as prose. A displayed run link is optional and informational: neither rotation nor acknowledgment parses a markdown run path or compares it with `session.json.run`. Existing notes are retained, not rewritten to inject a machine path.

`HANDOFF.md` and `NEXT-SESSION-PROMPT.md` remain required readable bootstrap resources, checked before drain/spawn. Note timestamps do not gate execution; there is no document-age option or handoff-mtime acknowledgment token. Measured context-snapshot freshness remains independently enforced by [rotation.md](rotation.md).

### `NEXT-SESSION-PROMPT.md`

Bootstrap order is fixed:

1. Confirm `HERDR_ENV=1` and preserve the selected Herdr session/socket environment.
2. Read the concrete predecessor's `HANDOFF.md` and `NEXT-SESSION-PROMPT.md` for relevant context.
3. Resolve the canonical run from the concrete session's `session.json.run`, not markdown or `latest`.
4. If spawned by `rotate.py`, run the generated `ack-handoff.py --nonce <n> --session <predecessor path>` command before dispatch. The generic template does not invent the nonce or predecessor.
5. Use the canonical successor session and run printed by acknowledgment, reconcile that run, and reattach live workers by exact recorded identity. See [rotation.md](rotation.md) and [identity-reattach.md](identity-reattach.md).

The prompt must explicitly warn not to treat `progress.md`, `docs/sessions/latest`, Herdr idle, or Herdr done as the live ledger or success state.

## Mutating run operations

Once controller identity is bound, mutating reconcile calls (`--event` or `--repo`) must include all holder fields:

```
python3 scripts/reconcile.py --run <absolute run path> --event '<json>' --holder-workspace "$HERDR_WORKSPACE_ID" --holder-pane "$HERDR_PANE_ID" --session <concrete session path>
```

Python callers pass `holder={"workspace": ..., "pane": ..., "session": ...}` to `run_state.append_event`, `run_state.reconcile`, `run_state.promote`, or `run_state.recover_promotion`.

Lock-taking adapters accept `--lock-timeout SECONDS`, and locked facade calls accept `lock_timeout=120`: finite positive, default 120 seconds; invalid CLI values exit 2, acquisition timeout exits 3. The approved fallback preserves the entire exclusive critical section and records only unverified owner diagnostics; [run-state-api.md](run-state-api.md#events-locking-and-dispatch) defines the intended contract, not a full concurrent-dispatch cutover.

Contextual `spawn-agent.py` / `prompt-agent.py` require `--run/--node/--attempt` plus the caller-supplied `--session`, `--holder-workspace`, `--holder-pane`, and `--epoch`. They hold `authz.dispatch_guard` through authorization, external launch/prompt, and identity persistence; adapters do not append controller events. Recorded identity is `identity_kind: managed-name` with immutable `worker_id` for new managed workers, or legacy `identity_kind: runtime-id` using Herdr `agent_id`/`id`. Missing identity kind is legacy runtime-id, never managed-name.

`dispatch-agent.py` appends a missing `dispatch_intent`, calls `spawn-agent.py --resume` only when `launch-complete.json` is absent, and records `dispatched` only from confirmed delivery evidence, never from `dispatch-identity.json` or CLI exit alone. Blocked delivery records `blocked`, not dispatch success. Shared single-submission delivery, structured errors, sidecar ordering, ambiguous-launch refusal, and exact-identity continuation are defined once in [identity-reattach.md](identity-reattach.md).

Prompt-specific retry flags and retry-count output are removed; startup `spawn-agent.py` / `dispatch-agent.py --retries` and `--retry-delay` remain separate readiness controls.

CLI reconcile emits compact controller/node state by default; `--full` retains the complete Python return shape. Adaptive monitor cadence, change-only JSONL, three transient-read failure tolerance, and 10-second read-only versus 60-second mutation/wait deadlines are defined in [run-state-api.md](run-state-api.md#background-observation-and-output-cost). Collection stdout summaries cap at 1,500 characters (`summary_truncated: true` when cut); `--full-summary` disables the cap and contextual `summary.json` always keeps full text. See [retire-promote.md](retire-promote.md#collection-and-ownership).

## Rotation lifecycle

Measured native occupancy, its threshold, identity/freshness checks, and refusal behavior are defined in [rotation.md](rotation.md#measured-context-gate). Document age is not a substitute for measured context freshness.

The predecessor starts rotation with its concrete session path:

```
python3 scripts/rotate.py --session <predecessor session path>
```

`--session` is required (no `latest`/owner fallback). Canonical metadata and required bootstrap resources are validated before drain/spawn. `rotate.py` owns native drain, successor launch, and tokenized acknowledgment; do not run manual drain or append `lease_transfer` by hand.

`ack-handoff.py` resolves the run from canonical predecessor metadata, validates the exact predecessor/nonce/holder/lineage, opens the successor session with deferred run binding, transfers the lease once, and stamps `handoff-ack` plus successor session metadata. Missing, invalid, noncanonical, conflicting, or out-of-root session/run metadata is a precondition refusal (exit 3), not a generic runtime failure. Same-nonce stamp recovery must reuse the transferred successor, not create another session or transfer.

After acknowledgment, reconcile the concrete run and reattach live workers by exact workspace/pane/recorded identity and run/node/attempt tokens, never labels or a second dispatch intent. See [rotation.md](rotation.md#drain-acknowledgment-and-stand-down) and [identity-reattach.md](identity-reattach.md).

`latest` may move during the roll, but no script or prompt may rely on it. The predecessor keeps writing only to its own concrete session path until it stands down or is retired.

## Collection, ownership, and promotion gates

- Node ids match `[A-Za-z0-9][A-Za-z0-9._-]{0,31}` (historical letter ids stay valid; do not rename active nodes or rewrite ledgers). Fresh graphs should use unique lowercase kebab-case task ids of at most 32 characters (`research-contracts`, `implement-api`, `implement-ui`, `integrate-feature`) and keep the attempt out of the id. Integration is output `type: integration`, not a magic id. Input aliases are nonempty and unique at graph accept. Attempt dirs and snapshot files stay inside the canonical run `artifacts/` tree (resolved, no symlink escape, including the artifacts root itself).
- Active-node graph revisions may change only the metadata-only `phase`. Executor, outputs, inputs, ownership, model request, handle, worktree, dependencies, conditions, retry policy, and other accepted node fields stay fixed. Compare with the previous accepted graph, not runtime-overlaid routing or handles; unrelated-node additions remain allowed.
- Canonical model routing is `requested_role`, `requested_effort`, `resolved`, `thinking`, `effort`, plus `requested_model` only when it is a concrete non-null value. Comparisons use that normalized form. Requested fields remain the attempt request. Null `requested_role`/`requested_effort` inherits the session; do not invent resolved. Retry reuses recorded concrete routing unless a `replan` event changes the request. Non-null observed resolved/thinking/effort that differ from recorded are refused. Missing/unreported live runtime values stay null, are not disagreement, and never overwrite recorded model. New continuation events carry recorded `model` plus `live_identity.routing`; events without that field remain replayable. Installed Herdr `agent get` does not expose routing fields, so absence is unknown, not verified live model metadata.
- `collect-agent.py <name> --run <absolute run> --node <id> --attempt <n>` writes per-attempt hashed artifacts. `--publish-vcs` writes form `commit` (sha = worktree HEAD) or form `none` when HEAD == identity base and the worktree is clean; an existing commit receipt matching HEAD is reused even after promotion. `--force` re-collects instead of trusting what is on disk: it re-reads the pane and re-derives `vcs.json` from the current HEAD, skipping the promoted-sha downgrade (that rule exists only to keep an already-recorded commit receipt stable, and a forced re-derivation has no trustworthy receipt to preserve). Every receipt whose content genuinely differs is replaced and audited as one `artifact_forced` event per receipt, appended under the run lock; identical bytes are left alone and produce no event.
- `vcs.result.form` is `commit` (sha = worktree HEAD descending from recorded `base_sha` via `merge-base --is-ancestor`, plus repo/worktree/branch identity gates), `diff` (attempt `change.bin`), or `none` (HEAD must descend from `base_sha` and be `base_sha` or an already-promoted sha; clean tree; honest form for audit/report nodes that change no VCS state). A writer is classified against its branch point on the promoted line (`git merge-base <promotion_ref> HEAD`, falling back to `base_sha`), so wave-N writers that reset to the last promoted sha are gated only on their own commits. `promote` refuses `none` with `promotion requires a commit vcs result`. Ownership treats the empty `none` diff as owned. Integrators (`needs_integration`) still refuse `none` (exit 3, no `summary.json`). `finish-agent.py` refuses `none` for any node whose accepted plan declares a `worktree`: a node that asked for its own branch and committed nothing produced nothing, and in practice its prompt never ran. Inspect the pane and retry the node, or pass `--force` to record the empty result deliberately. Nodes with no `worktree` — audit/report nodes — keep succeeding with no vcs receipt at all.
- Integration `base_sha` must equal the immutable recorded base. Nonempty `commit` and `head` must equal each other and the expected promoted/VCS result. `consumed` is the ordered sequence `(producer, positive attempt, output name, hash)` matching persisted graph input order for VCS-typed inputs (type from the producer output declaration, not `name=='vcs'`). Missing, extra, duplicated, or reordered entries are refused. Alias may be omitted; if present it must match the snapshot position.
- For ownership, `artifacts/<node>/<attempt>/ownership.json` is an immutable slices-only snapshot from `dispatch_intent`. Copy it into a temporary/per-attempt executable manifest, set `repos:["<actual writer worktree>"]` (the dispatch attempt worktree, not the primary checkout), keep this node's `owns` under `slices`, then run `check-ownership.py --manifest <manifest> [--base SHA | --since SHA]`. `--since` is an alias of `--base`. Alternatively `check-ownership.py --run PATH --node ID [--attempt N]` derives `slices` from that ownership snapshot and `repos` from `dispatch-identity.json.worktree_path`; `--since` defaults to the writer's branch point on the promoted line (`git merge-base <promotion_ref> HEAD`), else the identity's `base`; `--attempt` defaults to the highest numeric attempt dir. Output includes `"files": [...]` (sorted absolute classified changed files) in both modes.
- An integration node may declare `owns: ["**"]`. For the overlap check only, effective owns = union of effective owns of its transitive `depends_on` closure plus the node's declared entries other than `"**"`. Declared `owns` is otherwise unchanged (`dispatch_intent.owns` / `ownership.json` still record the declared list). A non-integrator with `"**"` overlaps everything concurrent.
- Integration/promotion nodes call `run_state.promote(<run>, node=<id>, attempt=<n>, new_sha=<sha>, repo=<absolute repo>, holder=<holder>)`; crash recovery is `reconcile.py --run <run> --repo <repo>` with all holder flags. `promote` is the terminal step for nodes with an `integration` output: it appends `promotion_intent`, `promoted`, then `succeeded`. Writer/read-only nodes: `finish-agent.py` appends `succeeded`. Integration nodes: `finish-agent.py` stops after collection and prints the `promote` call; it does not append `succeeded`.
- The run-owned `promotion_ref` (`refs/heads/herdr/<run-id>`) stays internal bookkeeping. The human-facing final branch is produced by `run_state.deliver_branch(<run>, repo=<absolute repo>, holder=<holder>[, branch="feature/<change-slug>"])`: it requires the exact holder and matching repo, requires every node of the accepted graph to be `succeeded`/`skipped`, derives the SHA from the last `promoted` event whose node/attempt is `succeeded`, and defaults to `refs/heads/feature/<slug of the full job text>` (never a run id, timestamp, node, or attempt). Creation is create-only `git update-ref <ref> <sha> ""`; an existing ref at exactly that SHA is an idempotent no-op (`created: false`), any other value is refused (`delivery branch <ref> already exists at <sha>; deliver with branch=<another name>`) and never moved or deleted. `promote` and promotion recovery call it automatically once the whole graph is done and report `delivery_branch` (plus `delivery_note` when publication was skipped or refused); they never retry or alter `promoted`/`succeeded` semantics — a git failure or collision becomes a `delivery_note`, never a failed promotion. Recovery also ensures the branch when there is no pending intent (crash between the `succeeded` append and publication); that path stays silent for runs that are unfinished or never promoted, and refuses a wrong holder or wrong repo before touching git. Re-run `deliver_branch` with an explicit `branch=` to resolve a collision or add an alias for an already terminal run.
- Promote only the run-owned ref. Refuse a checked-out promotion ref (`git worktree list --porcelain`) before initial ref creation where relevant and before direct/recovery mutation. Do not mutate the primary checkout or update checkout/worktree refs by hand. Appending `succeeded` for an attempt that already has a matching `promoted` event and whose node state is `succeeded` raises `PreconditionError("already succeeded by promotion")` (checked before `"succeeded requires a running attempt"`). Appending `succeeded` for an integrator before `promote` keeps raising `"integration success requires promoted event"`.

## Retirement gate

Retirement is successor-driven and receipt-gated:

- User-owned surfaces are never closed. Replacement live identities are never closed.
- Bind the live workspace+pane+identity — managed `worker_id` or legacy `agent_id`/`id`, per the recorded `identity_kind` — to the exact recorded attempt handle/`dispatch-identity.json` (including historical `dispatched`/`attempt_started` for earlier attempts), then provenance/run/node/attempt tokens.
- Resolve executor and consumer rules from the event-committed graph. `graph.json` may still contain the previous revision after a crash between event append and graph materialization; that cache never authorizes retirement.
- With `--run`, hold `run.lock` while loading the committed graph/state/events and through identity/receipt checks, optional collection, and every `workspace close`. A graph revision must commit before this transaction or after close, never between validation and close.
- `executor: pane` nodes are never retired, even with orchestrator provenance, terminal artifacts, or `--force-unsettled`. Artifactless interrupted cleanup does not override this refusal.
- Any surface carrying a `run`, `node`, or `attempt` token requires `retire-agents.py --run <absolute run path>` with `--session/--holder-workspace/--holder-pane`, and closure requires the active lease. Bare retirement is only for uncontextual surfaces; omission of `--run` cannot bypass executor and receipt gates.
- Contextual retirement requires a recorded terminal attempt for that run/node/attempt (`succeeded`, `failed`, `interrupted`, `skipped`, or `invalidated`). Herdr `idle`/`done` and `--collect`/`--force-unsettled` cannot authorize terminal run state or replace controller collection/events. Historical terminal attempts remain valid.
- Every explicit VCS consumer matters (declared output `type=vcs`, using the declared `name`). Only explicit `inputs` declare consumption; ordering-only `depends_on` does not. An integration-output consumer requires the existing promoted `integration.json` gates. A non-integrator requires current same-attempt controller success and a matching immutable consumed snapshot/digest. Active, failed, skipped, invalidated, missing, malformed, stale, or changed-hash evidence refuses. Herdr `idle`/`done` and `--force-unsettled` never authorize consumption. An integrator with zero consumers still needs its own promotion, independent of downstream count.
- Workspace closure (`herdr workspace close` only) does not remove retained Git worktrees or branches and has no source or data deletion capability. Promotion failure retains those worktrees and branches.
- Herdr server is never stopped by this flow. Reconcile observation treats `server_not_running` as unknown inventory, not vanished workers; a successfully fetched empty list is authoritative emptiness. Other Herdr CLIs still fail closed on `server_not_running` (start or attach a server).
