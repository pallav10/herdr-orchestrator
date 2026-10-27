# Controller run-state API

Import `run_state` with `scripts/` on the Python path. It is the explicit controller-facing facade; defining modules are `ioutil`, `graph_model`, `ledger`, `authz`, `artifacts`, `receipts`, `routing`, `liveness`, `promotion`, `scheduler`, and `cli_common`. Production scripts use their defining modules rather than private facade imports. The facade is not a second state authority or compatibility framework.

`RUN`, `SESSION` and `REPO` below are canonical absolute paths; the read APIs (`load_run`, `load_graph`, `replay`, `load_state`) accept `RUN` as `str` or `Path` and canonicalize it, and raise `UsageError` for a relative path. Holder `H` has exact `workspace`, `pane`, and canonical `session`. Errors are `UsageError`, `PreconditionError`, or `RunError`. CLI mapping is in [SKILL.md](../SKILL.md).

## Reads and initialization

| Call | Returns / contract |
|---|---|
| `claim_run(docs_root, job)` | Run metadata document (`id`, `job`, `created`, `schema_version`, canonical `path`, initially null `base_sha`/`promotion_ref`/`repo`), uniquely claimed under the gitignored docs root. |
| `load_run(RUN)` | Run metadata document; not runtime nodes. |
| `load_graph(RUN)` | Accepted graph document, including authoritative committed revision resolution. |
| `replay(RUN)` | `{state, controller, events, skipped_stale, truncated_tail}`. |
| `load_state(RUN)` | Exactly `replay(RUN)["state"]`; the state read path. |
| `bind_session_run(RUN, holder=H, lock_timeout=120)` | `{state, controller, event}` after exact initial holder binding. |
| `validate_graph(graph)` | `None` on valid graph; otherwise refusal, no accepted revision. |
| `accept_graph(RUN, graph, repo=REPO, holder=H, lock_timeout=120)` | Accepted graph document: `schema_version`, `run`, `base_sha`, `promotion_ref`, `nodes`. |

State has `schema_version`, `run`, `epoch`, `nodes`, `notified`, `dispatch`; nodes carry executor/attempt/state, reason/question, recorded routing/handle, artifacts and immutable dispatch/consumption history. Controller has `epoch`, `workspace`, `pane`, `session`, `run`, `dispatch`, `lease`. Applied `events` omit skipped stale post-transfer mutations; replay preserves valid pre-transfer history. Notification keys are sorted/unique; do not confuse the full state notification history with notifications emitted by the current call.

Acceptance validates IDs (`[A-Za-z0-9][A-Za-z0-9._-]{0,31}`), unique aliases, dependency graph, serialized ownership overlaps and contained artifact paths, exact holder and canonical repository. It records the immutable base — `HEAD` of the supplied canonical `repo` at first acceptance, the recorded value afterwards — and initializes the run-owned promotion ref once through recoverable committed graph revisions. A `base_sha` submitted in the graph does not select or validate the base and a mismatch is not refused; read the returned `base_sha` and compare it with the commit the briefs assume before any dispatch (see [continuing a previous candidate](controller-playbook.md#211-accepted-base-and-continuing-a-previous-candidate)). Active nodes retain all accepted fields except metadata-only `phase`; revisions compare accepted plans rather than concrete runtime model/handle overlays. Acceptance must finish before any dispatch.

Unlaunched Herdr plans may be revised: planned/ready handles are plan selectors, so accepted graph changes must also appear in `load_state`/replay. A ready `dispatch_intent` freezes `owns` but permits correction of `handle.name` until `launch-intent.json` or `prompt-attempt.json` exists. Pane prompting persists launch intent before any metadata/prompt mutation; either marker freezes the plan even if delivery fails before a `dispatched` event. Do not delete markers or rewrite projections to bypass this boundary. Running/blocked plans and historical runtime handles remain protected.

`session_docs.session_run(session)` is the shared metadata run resolver (not a markdown parser): canonical absolute binding, same-docs-root containment, valid run metadata. Initializer absence/backfill and deferred successor binding have distinct policies documented in [session-docs](session-docs.md) and [rotation](rotation.md).

## Events, locking and dispatch

`append_event(RUN, event, holder=H, lock_timeout=120)` returns `{state, controller, event}`. Events are validated before durable append against immutable attempt ownership/consumption snapshots, then flushed/fsynced and replayed after append. The post-append replay is retained; no global ledger memoizer or cache-based authorization. Inconsistent event `to`, stale epoch, foreign holder, lease violation, corrupt history and truncated tails refuse mutation.

Defining-module API `ledger.append_events_locked(run_dir, events, *, holder)` validates the sequential batch under an already-held lock and returns its post-append replay snapshot. Invalid input appends nothing; valid batching shares one durable ledger append/fsync and projection materialization. `append_event_locked` is its one-event result adapter. Do not acquire the lock recursively.

```python
with run_state.dispatch_guard(RUN, NODE, N, holder=H, epoch=E, lock_timeout=120):
    # Adapter-authorized launch side effects only; no append_event here.
    ...
```

The exclusive `fcntl.flock` on `run.lock` spans authorization **and external launch**. The guard requires exact holder, expected replayed epoch, active lease+dispatch, intact ledger, current graph node/attempt and ready dispatch intent, or eligible blocked Herdr/pane continuation when `continuation=True`. Stale holders must produce no side effects. Only the bounded-acquisition/diagnostics fallback is approved; lease/node locks, reservations, inherited authorization descriptors and external-phase lock release are not approved or implemented.

The requested fallback keeps every existing exclusive critical section and `_DISPATCH_GUARD_DEPTH` nested-append refusal. `ledger.with_lock(run_dir, *, lock_timeout=120, node=None)` bounds acquisition, not time spent inside the critical section. Locked facade APIs `bind_session_run`, `accept_graph`, `append_event`, `dispatch_guard`, `reconcile`, `promote`, `deliver_branch`, and `recover_promotion` accept keyword `lock_timeout=120` without changing their return shapes. Already-locked append helpers do not reacquire the lock.

Lock-taking CLI entrypoints `init-docs.py`, `dispatch-agent.py`, `spawn-agent.py`, `prompt-agent.py`, `collect-agent.py`, `finish-agent.py`, `retire-agents.py`, `rotate.py`, `ack-handoff.py`, and `reconcile.py` accept `--lock-timeout SECONDS`: finite positive, default 120; invalid values are usage errors (2), acquisition timeout is a precondition refusal (3). Forward the same bound to lock-taking child adapters. A child trying to reacquire a parent's held lock refuses within the bound; fallback does not make nested collection concurrent.

After acquisition, `<run>/run.lock.owner` records `pid`, `script`, `node`, `since`, canonical `run`, `lock: "run.lock"`, `mode: "exclusive"`, nullable `epoch`, and unique `token`. Timeout diagnostics name the lock/run and mark this owner record **unverified** (missing/malformed records are unknown). Waiters never delete records, infer authority from PID/liveness, or break a lock. Owner-write failure refuses before protected mutation; cleanup removes only its own matching token while still locked. Kernel locking plus replayed holder/epoch checks remain authority. Diagnostics are atomically replaced without fsync; authority and changed-cache durability remain unchanged.

`attempt_started` resumes only a blocked Herdr/pane attempt; canonical known live routing mismatches refuse and unknowns remain null. Historical events without live routing remain replayable. Initial dispatch/continuation ordering and controller-versus-adapter responsibilities are in [identity-reattach](identity-reattach.md).

Caches are atomically replaced and fsynced only when bytes change. Missing/corrupt projections repair from replay; unchanged projections cause no writes. Snapshot immutability and receipt checks are not weakened by batching or cache suppression. `derive_status_md` retains its positional interface; status is disposable.

## Reconcile and inventory semantics

```python
run_state.reconcile(
    RUN, event=None, notify=False, require_native_quiesced=False,
    current_session=SESSION, live_native_ids=None,
    native_inventory_complete=False, holder=H, repo=None, live_herdr=None,
    lock_timeout=120,
)
```

Return is always `{state, controller, notified}`; `notified` contains keys emitted by **this invocation**. Holderless plain reconcile only replays and repairs three projections; it appends no lifecycle events or attempt artifact repairs. Event, notification, observation/lifecycle or promotion-recovery mutations require exact holder. `repo` requests promotion recovery. `require_native_quiesced` checks quiescence rather than asserting an inventory is complete. Notify once per actual edge to blocked/failed/all-terminal; retry can produce a later edge, rotation alone cannot.

Under one unchanged run lock, reconcile reuses its replay snapshot and validated graph, appends each fixed-point iteration as one sequentially validated batch, and sends all pending notification keys in one batch. Any append or promotion recovery refreshes authority.

Native semantics are exact:

- Without `current_session`, native interruption inference is disabled.
- Running/blocked native handles whose `spawning_session` differs from current session become interrupted, even when inventory is unknown; handles cannot cross session boundaries.
- Same-session missing/unknown handles become interrupted only with `native_inventory_complete=True` and an absent ID in the supplied complete live set.
- A ready node with a current dispatch intent is an observable launch orphan and follows that same complete-inventory missing-handle rule.
- IDs are the recorded native `job`, `job_id`, or workflow `runId`; omitted/partial inventory is unknown, explicit complete empty set is authoritative emptiness.

`native_quiesced(state)` returns a bool: native `task`/`workflow` running or blocked nodes fail; a nonterminal native node with dispatch intent also fails. Herdr state is not native drain proof. Failed/interrupted native retry additionally needs the accepted `side_effect_safe=true` policy.

Herdr observation `None` means unknown, including `server_not_running`; successful `[]` is authoritative empty. Partial/malformed inventory refuses rather than silently treating missing records as lost workers. Stable identity is not filled from CLI names. See [identity-reattach](identity-reattach.md) for managed/runtime-id matching.

CLI `reconcile.py --run RUN` prints compact output by default:

```json
{"epoch": 1, "dispatch": true, "lease": "active", "holder": {"workspace": "W", "pane": "P", "session": "SESSION"}, "nodes": {"NODE": {"state": "ready", "attempt": 1}}, "notified": []}
```

`reason`/`question` appear per node only when present; `truncated_tail` appears only when true. `--full` prints the complete Python return shape unchanged. CLI native observation uses `--native-inventory-complete` with one `--live-native-id` per live job; omission never asserts emptiness. `--session --holder-workspace --holder-pane` is the concrete holder triple for mutating CLI calls.

## Promotion and delivery shapes

| Call | Returns |
|---|---|
| `promote(RUN, node=NODE, attempt=N, new_sha=SHA, repo=REPO, holder=H, lock_timeout=120)` | Always `{state, controller, event}`; successful auto-delivery adds `{delivery_branch, sha, created}`, skipped/refused delivery adds `{delivery_branch: None, delivery_note}`, CAS-blocked promotion has no delivery keys. |
| `deliver_branch(RUN, repo=REPO, holder=H, branch="feature/change-slug", lock_timeout=120)` | `{delivery_branch, sha, created}`; create-only/idempotent at identical SHA, never moves an existing different ref. Omit branch for job-derived default. |
| `recover_promotion(RUN, REPO, holder=H, lock_timeout=120)` | `None`; repairs interrupted promotion under lock. |

Promotion's terminal success is not duplicated by a controller append. Ordered receipt evidence, primary/checked-out ref refusal, terminal delivery and retirement boundaries are fully described in [retire-promote](retire-promote.md).

## Background observation and output cost

`monitor-agents.py` is a background observer, not authority. Default regular JSONL emits on states/pending/stale-set change (initial/terminal snapshots included). `--every-poll` restores every-poll lines. `--interval` is the maximum cadence; `--min-interval` defaults to 2 seconds, starts/resets there, doubles while unchanged up to the maximum, resets on changes. Up to three consecutive transient agent-list failures are tolerated before nonzero termination. Settlement reaches the controller through an auto-delivering harness job and/or `--notify-pane`; file-only detached polling does not wake it.

Regular lines include `t`, `states`, `pending`, `last_activity`, `idle_for`. Activity is maximum mtime of the OMP session JSONL and direct sibling `<stem>/*.jsonl` files. `--stale` emits STALE once per unsettled idle threshold crossing and ACTIVE on recovery; optional pane notifications accompany it. `--notify-on edge` adds first-settlement notifications; default final sends terminal outcome. Observation does not authorize `succeeded` or workspace retirement.

Collect stdout caps summaries while full contextual receipts remain durable; see [retire-promote](retire-promote.md). Read-only (`agent`/`workspace`/`pane` × `get`/`list`/`show`) calls use 10-second deadlines; other Herdr calls retain the 60-second deadline, including agent wait/prompt, so explicit prompt confirmation waits remain supported.
