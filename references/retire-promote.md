# Receipts, promotion and retirement

Use this after worker settlement, before recording success or closing a workspace. Herdr status is observation, not proof of controller success. Exact identity/resume mechanics: [identity-reattach](identity-reattach.md). Python shapes: [run-state-api](run-state-api.md).

## Collection and ownership

`collect-agent.py TARGET...` reports summaries/status. `--status-only` writes no attempt artifacts. Contextual collection accepts one target with `--run RUN --node NODE --attempt N --session SESSION --holder-workspace W --holder-pane P`; the exact active holder is required before any receipt write. It verifies dispatch identity and writes hashed declared receipts in `<run>/artifacts/<node>/<attempt>/`.

Printed summary is capped at 1,500 characters with `summary_truncated: true` when cut; `--full-summary` disables the cap. Contextual `summary.json` retains the complete text independently. Transcript summary extraction reads backward from EOF. Completed identical summary retries preserve the same bytes; conflicting content refuses without explicit `--force`.

`--publish-vcs` derives VCS receipts from recorded worktree/branch/base/repo: form `commit` identifies clean worktree HEAD descending from base; form `none` means clean HEAD equals base. An existing matching commit receipt remains valid even after promotion. Dirty trees, ancestry mismatch, or a differing existing receipt refuse. Integrators require a commit and refuse none before writing summary.

`dispatch_intent` freezes slices-only `ownership.json` and consumed artifact snapshots before work begins. Run-mode ownership uses actual dispatched worktree paths, not the primary checkout:

```sh
python3 scripts/check-ownership.py --run RUN --node NODE --attempt N
```

The default baseline is the writer's branch point on the promoted line, `git merge-base <promotion_ref> HEAD`, with recorded identity base as fallback; later-wave writers should not be charged for previous promotions. Explicit `--base`/`--since` overrides that baseline. Run and manifest modes are mutually exclusive; omitted attempt picks the highest numeric attempt directory. Working-tree changes and committed baseline-to-HEAD changes are classified as owned/overlap/unowned, with sorted absolute `files` in output. For an executable manifest, copy immutable slices and add `repos: [<actual writer worktree>]`; do not change the immutable snapshot.

Independent writers own disjoint paths; generated files belong to one writer or the integrator. For overlap validation, integration `owns: ["**"]` represents transitive dependency ownership plus declared extras rather than an unconditional conflict with unrelated disjoint nodes; non-integrator `**` remains global.

## Finish and forced correction

```sh
python3 scripts/finish-agent.py --run RUN --node NODE --attempt N \
  --session SESSION --holder-workspace W --holder-pane P
```

Every node kind requires the exact active holder before writes, including integration receipt creation. Finish filters collection to declared outputs, validates receipts/ownership and uses the merge-base default above. An integrator with no commit beyond the promoted line refuses before any receipt write. Dirty worktree, HEAD not descending from `since`, unowned paths, and a planned-worktree node with no commit refuse with exit 3; a read-only/report node without a planned worktree may succeed without VCS output. Collector exit codes pass through.

Finish JSON contains `node`, `attempt`, `state`, `vcs_form`, `since`, `artifacts`, `unowned`, and `promote` (exact Python call or null). Writer/read-only finish records `succeeded`; integration finish stops after collecting receipts and prints the exact promotion call with concrete holder. It does not record premature integration success.

`--force` deliberately re-collects rather than trusting receipts: re-read the pane, derive current worktree VCS, rewrite differing integration/summary/VCS bytes under lock and append one `artifact_forced` audit per changed receipt. Identical bytes are untouched/unaudited. It can deliberately record a worktree writer that committed nothing and correct mis-collected receipts even after success. Historical success hashes remain history and another success is not appended. To rerun/re-record node execution, use invalidated→retried with a new immutable attempt, not force as a retry mechanism. Consumed-hash changes invalidate consumers; identical hashes preserve success.

## Promotion and final delivery

Run the finish-provided call:

```python
run_state.promote(RUN, node=NODE, attempt=N, new_sha=HEAD, repo=REPO, holder=H)
```

Promotion validates exact holder/repository, current attempt, immutable snapshots and ordered declared VCS/integration evidence. Integration receipts carry ordered input node/attempt/artifact/hash/commit evidence plus base/HEAD/ref gates. Promotion is compare-and-swap only on the accepted run-owned ref, against the previous promoted SHA (initially graph base). Checked-out promotion refs refuse; primary/worktree checkout refs are not manually moved. Success records `promotion_intent`, `promoted`, receipt `promoted.json`, then `succeeded`. CAS ref movement records a blocked outcome rather than inventing success. Crash recovery is `reconcile.py --run RUN --repo REPO` with the exact holder triple or `run_state.recover_promotion(RUN, REPO, holder=H)`.

Refusal messages to recognize:

- `promotion requires a commit vcs result` — none cannot promote.
- `integration success requires promoted event` — terminal success before promotion is invalid.
- `already succeeded by promotion` — no duplicate terminal event after matching promoted success.

Final human-facing delivery is create-only, normally `refs/heads/feature/<change-slug>` from job text. `deliver_branch` requires the entire accepted graph succeeded/skipped plus a promoted-and-succeeded integration commit and exact active holder/repo. Same ref/SHA is idempotent; an existing different SHA refuses rather than moving it. Terminal promotion may auto-deliver; a delivery-note failure does not undo completed promotion. Worktrees/branches remain retained on promotion or delivery failure.

## Retirement boundaries

```sh
python3 scripts/retire-agents.py TARGET... --run RUN \
  --session SESSION --holder-workspace W --holder-pane P
```

`--run` and holder flags are optional **only for uncontextual retirement**. Any run/node/attempt-tagged surface requires its canonical run and all exact active-holder flags. With a run, retirement holds `run.lock` from committed graph/state/events through live identity, receipt gates, optional collection and every close. Historical attempts may be matched, but identity/context must still agree with recorded handle and dispatch sidecar.

Closure requires orchestrator provenance, exact recorded live workspace/pane/managed worker ID or runtime-id, recorded terminal contextual attempt and settled worker observation. Every `pane` executor, user-owned surface, replacement identity or ambiguous record refuses regardless of provenance/force. `--force-unsettled` bypasses only the unsettled-observation check. `--collect` collects optionally but does not authorize execution success or consumption. Neither option bypasses attempt terminality, identity or receipt gates.

Every explicit consumer of a declared VCS output (including custom declared output names) matters; dependency edges alone do not consume receipts. An integration-output consumer requires matching promoted integration evidence. A non-integrator consumer requires current same-attempt controller success and its matching immutable consumed snapshot/digest. Active, failed, skipped, invalidated, missing, malformed, stale, changed-hash or mismatched-attempt consumption evidence refuses closure. An integrator with zero consumers still requires its own promotion.

Only `herdr workspace close` closes authorized surfaces. It removes no retained Git worktree/branch/source/data and does not stop the server. A predecessor controller is closed only successor-side after the exact tokenized handoff plus applicable receipt/terminal gates; user-owned controllers stand down instead. All requested targets must pass these boundaries.
