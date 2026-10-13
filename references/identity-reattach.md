# Worker identity and reattachment

Read this for dispatch, continuation, worker recovery or successor reattachment. Core authority and safety rules live in [SKILL.md](../SKILL.md); promotion/retirement gates live in [retire-promote.md](retire-promote.md).

## Identity modes

| Mode | Stable token | Applicable records |
|---|---|---|
| `managed-name` | immutable `worker_id`, matching `fo-[0-9a-f]{24}` | New orchestrator-spawned workers |
| `runtime-id` | Herdr-issued `agent_id`/`id`, stored through the historical `agent` field | Legacy workers and every user-owned `pane` |

Missing `identity_kind` means legacy runtime-id; it is not permission to convert a historical name into managed identity. Managed `worker_id` is persisted in immutable `launch-intent.json` before the first Herdr side effect and is the exact target for start/get/prompt/list. It is not written into legacy `agent_id`/`id` fields. Herdr 0.8.2 (protocol 20) supplies no server-issued ID for named managed-agent addressing, so these workers do not wait for one.

Reattachment requires exact workspace **and** pane, mode-appropriate stable token, and run/node/attempt tokens. Managed workers additionally require orchestrator provenance; user-owned pane dispatch preserves user provenance and remains runtime-id-only. Recorded handle and dispatch sidecar evidence must agree; conflicting evidence refuses before metadata or prompt side effects. A matching surface with incomplete stamps stays unresolved rather than being adopted or relaunched.

Managed-name ownership assumes exclusive orchestrator ownership of the reserved `fo-` namespace. It cannot reliably detect an external replacement that perfectly copies the name and metadata; it is weaker than a server-issued runtime identity. Uncertain identity blocks.

## Node IDs and display labels

Fresh graph IDs should be unique, task-specific lowercase kebab-case, at most 32 characters (e.g. `research-contracts`, `implement-api`, `integrate-feature`). The accepted grammar also supports historical IDs matching `[A-Za-z0-9][A-Za-z0-9._-]{0,31}`. There is no reserved integrator node ID: an integration output identifies its role. Attempt context is `<run-id>/<node>/<attempt>`.

`dispatch-agent.py --name` is an optional display alias, not command identity. When NAME equals contextual node ID, the human label is `<name> · a<attempt>`; otherwise it is `<name> · <node>/a<attempt>`. Uncontextual labels remain NAME. Workspace `role` metadata is presentation NAME. The contextual label initializes workspace/pane presentation; until `dispatch-identity.json` exists, the workspace label also ends in ` · <worker_id>` for orphan discovery. Rename failures are warnings, not proof of launch failure.

## Executor routing and recovery

| Executor | Dispatch | Reattach/retry |
|---|---|---|
| `task` | Append `dispatch_intent`, inject `prepare_dispatch_prompt`, launch native task, record handle/nested model in `dispatched`. | Session-local; foreign spawning session becomes interrupted. Same-session missing handles require complete native inventory. Interrupted/failed retry requires `side_effect_safe=true`. |
| `workflow` | Same intent/prompt boundary, then record workflow `runId` and nested model. | Same native session/inventory and safe-retry rules. |
| `herdr-agent` | Use `dispatch-agent.py` with run/node, exact holder and epoch; it calls contextual spawn with `--resume`. | Reattach exact recorded surface/token. Missing identity may become interrupted; relaunch is a new approved retry attempt, not a second launch of the live attempt. |
| Integration `herdr-agent` | Same dispatch, deriving required branch/base from accepted graph. | Fresh isolated worktree, ordered VCS/integration receipts and promotion gates; do not reuse primary or previous integrator checkout. |
| `pane` | After intent, `prompt-agent.py TARGET --run RUN --node NODE --attempt N --session SESSION --holder-workspace W --holder-pane P --epoch E --prompt-file FILE`, then controller records returned handle/model. | Exact runtime-id identity and context tokens; no orchestrator provenance requirement and no orchestrator retirement. |

Contextual spawn derives writer/integrator `--base` and required `--branch`; do not add speculative overrides. Explicit mismatches are usage errors (`--base must equal graph.base_sha ...`, `--branch must equal ...`). Uncontextual independent writers must supply their own isolated branch/base as appropriate. Read-only dispatch accepts `--cwd`; worktree creation normalizes a linked-worktree cwd to its verified primary checkout, without changing that checkout. Read-only workspace creation retains requested cwd. Identity `repo` remains the accepted run repository.

## Sidecar ordering and at-most-once resume

Contextual adapters require run/node/attempt, exact holder triple and epoch. `dispatch-agent.py` verifies epoch, commits immutable consumed/ownership snapshots via `dispatch_intent` if absent, and holds authorization/launch serialization. Ordered sidecars under the attempt directory are:

1. `launch-intent.json`: immutable identity and launch plan, before creation/start.
2. `dispatch-identity.json`: exact workspace/pane, identity kind/token, repo/worktree and context, before metadata/prompt.
3. `prompt-attempt.json`: pre-send marker, before prompt submission.
4. `launch-complete.json`: confirmed outcome, not a guess from CLI success.

Without `--resume`, preexisting launch evidence refuses. With it, automatic progress is allowed only on unambiguous evidence: identity with no prompt marker proves text was not sent; intent-only recovery requires explicit structured `agent_not_found` and no workspace labeled with the worker ID before creating anything. A missing lookup alone is insufficient. A prompt marker without conclusive completion is ambiguous and fails closed (exit 3).

Refusal prints discovered workspace/pane and exact manual recovery commands. Inspect with `herdr agent read TARGET`; after checking the live pane, use the printed `prompt-agent.py TARGET --prompt-file FILE` command if manual delivery is appropriate. Only after confirming delivery may `spawn-agent.py ... --resume --delivered` acknowledge it. Orphan cleanup uses the printed workspace ID and normal safety/retirement rules. Direct low-level spawn/resume does not replace the controller's ledger handle/model recording.

For still-live workers, a successor reattaches rather than appending a second intent. A successful same-attempt blocked continuation returns an `attempt_started` descriptor for the controller to append. It retains recorded routing and resumes blocked→running; blocked/unconfirmed delivery does not emit that transition. A retried N+1 attempt gets a fresh intent.

## Delivery and routing

Both spawn and prompt use one shared `deliver_prompt` call: one full-text submission, structured `exc.code` classification only. Prompt words such as `timeout`, `agent_blocked` or `agent_prompt_stalled` do not influence behavior. Uncoded/unrelated errors remain failures. Prompt-specific outer retry flags/counts are not part of this contract; start-readiness retries are a separate operation.

Confirmation uses `herdr agent prompt ... --wait --until working --timeout 15000`. A genuine structured stall/timeout may use the existing **single Enter** recovery and bounded observation, not another full prompt. A worker that stays idle is unconfirmed (`prompted: false`, blocked/not-ready), not dispatched. True `agent_blocked` is prompt-agent exit 4; inspect the agent before manual intervention. A live `agent_not_ready` start records blocked without closing the workspace. Any post-start failure retains surface/identity evidence and prints exact follow-up instructions.

Read-only Herdr inventory/get/observation calls have a 10-second subprocess deadline; mutations retain their normal deadline and prompt waiting retains its explicit 15,000ms confirmation bound.

Resolve roles with `omp config get modelRoles --json`; use `--model-role` or concrete `--model provider/model[:thinking]`. Preserve requested role/model/effort separately from resolved model/thinking/effort. Null means unknown/inherited, not invented resolution. Canonical model routing has the five model keys plus optional non-null `requested_model`. Retry reuses concrete recorded routing unless replan changes the request. Continuation observes canonical live routing, leaves unreported fields null, and refuses known mismatch before metadata/prompt; it does not overwrite recorded routing with unknowns. Historical events without live routing remain readable.

`spawn.resolve_role(role: str) -> str` resolves the role through `omp config get modelRoles --json`. Its atomic cache is `~/.omp/run/herdr-cache/model-roles.json`, keyed by readable `~/.omp/agent/config.yml` `st_mtime_ns`. Missing/corrupt cache or unreadable config falls back to the OMP query; unknown roles refuse. This cache is routing lookup only, never run or identity authority.

Metadata tokens include `run`, `node`, `attempt`, `role`, `plan`, `provenance`, `handoff-ack`, and `session`. Values are limited to 80 characters; the `run` token is the short run ID, while durable records/prompts carry the canonical absolute run path. Keep plan tokens short/relative; store prompts durably (e.g. `docs/plans/prompts/<role>.md`) for verbatim recovery. Dispatch scripts prepend standalone `orchestrate` when enabled.

## Chosen Herdr session

On `server_not_running`, mutating controls report start-or-attach failure (exit 1), not worker loss. If the message lists every `~/.config/herdr/sessions/*/herdr.sock` but a server exists, the caller may lack `HERDR_SOCKET_PATH`/`HERDR_SESSION`; restore the chosen session environment rather than auto-attaching a different session. Reconcile treats an absent server as unknown inventory; malformed/partial inventory fails rather than authorizing worker-loss inference, while a successfully fetched empty inventory is authoritative emptiness. Resolve actual protocol mismatch before further operations.

Uncontextual spawn may report `agent=NAME` when start has no runtime-id and its returned name equals the target. This fallback does not enroll user panes or reinterpret legacy contextual identities.

## Detached self-improvement

The controller notices defects/opportunities and invokes `self-improve.py --finding TEXT`; this is an event-triggered loop, not a polling daemon. Include the observed behavior, sanitized evidence, bounded change, and an observable acceptance check. Report each distinct finding once. A detached improvement worker must not recursively trigger this loop.

`--skill-repo PATH` identifies the editable skill Git root and defaults to the launcher's checkout. Installed copies outside Git require the explicit source path; never substitute the target project or edit the installed copy. `--model-role` defaults to `task`.

The command creates a private job under `~/.omp/run/herdr-improvements/`, starts a new-session launcher with closed terminal input and file-backed output, and returns immediately. `status: queued` confirms only queueing, not pane creation or prompt delivery. The child clones committed HEAD with `--no-local`, selects a private `skill-improvement/<job-id>` branch, and removes the clone's origin remote. It neither shares source Git metadata nor copies/stashes source WIP.

The child reuses context-free `spawn-agent.py` with the private checkout as `--cwd` and `--no-orchestrate`; existing spawn creates a new no-focus workspace/pane and confirms prompt delivery. No run/node/attempt/holder/epoch or parent plan is supplied. The reporting run gets no graph node, edge, event, lease change, receipt, notification, or success prerequisite. Its code and installed copies are not updated, so later subprocesses cannot unexpectedly adopt the candidate fix.

`job.json` records queued/preparing/launching/working/blocked/failed state and returned launch identity; `launch.log` retains launch errors. Working means confirmed delivery, not completed implementation. A single-use launch claim forbids automatic re-launch or full-prompt resend after a crash or ambiguous result. Inspect the job and exact recorded identity before any manual recovery; do not treat a stale status as proof that no pane was created.

The worker implements and verifies only the finding in its private clone, producing `change.patch` and `result.json` beside the checkout. Those artifacts are not consumed by the active graph. If the finding requires uncommitted source changes absent from the clone, it reports the missing prerequisite rather than touching the source. Review, merge, commit, push, installation, and worker closure are separate authorized actions after the reporting run; none are automatic dependencies.

