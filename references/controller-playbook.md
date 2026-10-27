# Controller playbook — how a run was actually driven

Practical controller examples complement [SKILL.md](../SKILL.md) and the [reference index](../README.md#layout). Runtime contracts live in the linked references, not in historical examples.

## Native existing-pane playbook

Use this lightweight path for bounded user-authorized work in an existing pane. It is not durable graph execution: no lease, replacement-safe agent identity, receipt, promotion, or graph-success claim follows from a native prompt. If those guarantees are required, use the supported durable contract or report its missing prerequisite; do not substitute this path for a graph node.

The following commands illustrate the session's successful read-only analysis. Substitute the explicitly authorized target and absolute report path. Run them only after validating the returned JSON, not as an unattended script:

```sh
test "${HERDR_ENV:-}" = 1
test -S "${HERDR_SOCKET_PATH:?select the intended Herdr socket first}"
herdr --session flowops status server
herdr --session flowops agent get w4E:p0
```

Check that the socket is the selected `flowops` endpoint, the protocol is compatible, and `agent get` matches the exact live name (`qrp-services` in this example), workspace/pane, expected repository/cwd and readiness. Use an exact explicit target, or `--current` only where installed help supports it and the user authorized the current pane; never choose a pane from sidebar order or implicit focus. These checks establish the intended current target, not an immutable runtime ID.

```sh
herdr --session flowops agent prompt w4E:p0 \
  'Read-only analysis: analyze every PR comment, make no source edits, and write the complete report to /absolute/report.md. State coverage and unresolved items.' \
  --wait --until working --timeout 15000
```

Confirm the returned state is `working`. Start the following **once through the host's auto-delivering background-job facility**, then do independent work; do not run it synchronously here or replace it with status loops:

```sh
herdr --session flowops agent wait w4E:p0 --timeout 1200000
```

After the wait delivers a settled result, read the exact report with the file-reading tool and inspect a short pane tail:

```sh
herdr --session flowops agent read w4E:p0 --source recent --lines 40
```

Verify report coverage against the requested inventory, not merely an idle/done state or a nonempty report. The user-reported successful workflow covered all 90 PR comments without source edits; that observation does not certify future runs. One auto-delivering wait or edge notification is enough. A wait timeout is not completion: inspect the evidence once and report what remains unknown.

If delivery is uncertain, never resubmit the full prompt. Inspect the pane/report and structured errors first; inspect blocked approval/question states before sending any keys. Do not automate Enter recovery here, steal focus, restart the server, close user-owned surfaces, or reinterpret terminal/session/name/pane fields as immutable agent IDs. Cache installed help and verified facts; bare groups can return exit 2 while printing help, so independent discovery probes must not be chained with `&&`.

For implementation rather than read-only analysis, preserve the primary checkout and use an authorized feature branch or isolated worktree before edits. Verify the actual changed behavior and review the diff before committing or pushing only the work the user authorized; a request to analyze is not permission to edit, commit or push. Durable parallel writers still require graph ownership/isolation and their real supported identity contract.


## 1. Kernel bootstrap (once per controller session)

```python
import sys, os, json, subprocess, copy, fnmatch
sys.path.insert(0, os.path.expanduser("~/.omp/agent/skills/herdr-orchestrator/scripts"))
import run_state
SCRIPTS = os.path.expanduser("~/.omp/agent/skills/herdr-orchestrator/scripts")
REPO    = "/abs/path/to/repo"                       # canonical, no symlinks
RUN     = f"{REPO}/docs/runs/<stamp>-<job>"          # the ledger directory (events.jsonl is the authority)
SESSION = f"{REPO}/docs/sessions/<stamp>-<title>"
HOLDER  = {"workspace": os.environ["HERDR_WORKSPACE_ID"], "pane": os.environ["HERDR_PANE_ID"], "session": SESSION}
PROMPTS = f"{REPO}/docs/plans/prompts"
def git(cwd, *a): return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, check=True).stdout
st = run_state.reconcile(RUN, holder=HOLDER)        # -> {"state": {...,"nodes": {id: {...,"state": ...}}}, "controller": ..., "notified": ...}
graph = json.load(open(f"{RUN}/graph.json"))       # accepted graph; graph["nodes"] is a dict keyed by node id; nodes = graph["nodes"]
                                                     # RUN also holds run.json, state.json (derived), status.md, artifacts/<node>/<attempt>/, graph-revisions/
```

Keep `PROMOTED` (the last promoted sha) in the kernel: every new wave resets to it and is ownership-diffed from it.
The kernel is the only place these live; a failed cell loses in-cell writes (re-write prompt files with the `write`
tool, not from inside a cell that can fail later).

Return shapes differ (hit `KeyError: 'nodes'` twice): `reconcile()` → `{"state","controller","notified"}`;
`promote()`/`append_event()` → `{"state": ...}`; `accept_graph()` → the accepted graph document
(`schema_version`, `run`, `base_sha`, `promotion_ref`, `nodes`; `base_sha` is the authoritative run base, see §2.1.1); `load_run()` → the run document without node state. `run_state.load_state(RUN)` is the one read path for node state.

## 2. Wave lifecycle

```
design slices → write briefs → add nodes to graph → validate_graph → accept_graph → reconcile
→ per writer: dispatch-agent.py → hub monitor (--stale) → finish-agent.py → screenshot review
→ integrator: finish-agent.py → promote → push → retire
→ (optional) audit node depending on D → finish-agent.py (form none) → retire
```

### 2.1 Node shapes that validated

Writer:
```json
{"id":"X","executor":"herdr-agent","role":"run-detail-polish","phase":"wave3","depends_on":["D3"],"inputs":[],
 "owns":["studio/src/views/run-detail/**","platform/gateway/flowops_gateway/routes/runs.py"],
 "worktree":{"branch":"ux-204541/x"},
 "outputs":[{"name":"summary","type":"summary"},{"name":"vcs","type":"vcs"}],
 "model":{"requested_role":"default","requested_effort":null,"resolved":null,"thinking":null,"effort":null},
 "retry":{"max_attempts":2,"on":["failed","interrupted"],"side_effect_safe":true}}
```
Integrator: same plus `"phase":"integrate"`, `outputs` gains `{"name":"integration","type":"integration"}`,
`inputs` = `[{"alias":"vcs_x","from":"X","output":"vcs","required":true}, …]`, and **no `worktree.branch`** — the
branch is derived per attempt as `<run-id>/<node>/<attempt>` and `validate_graph` refuses a declared one. Integrator
may declare `owns: ["**"]` (effective owns = union of transitive `depends_on` owns plus declared extras such as
`platform/pyproject.toml`, `platform/uv.lock`, generated API files). A single-writer run can be one node that is
both writer and integrator (`depends_on: []`, `inputs: []`, outputs summary+vcs+integration).

`cannot rewrite active node X`: a `dispatch_intent` freezes the plan only once a side effect may exist — for
`herdr-agent`/`pane` that is `artifacts/X/<n>/launch-intent.json`; for native `task`/`workflow` the intent alone.
A Herdr node whose intent never launched may be re-accepted with a changed plan as long as `owns` is unchanged
(`ownership.json` is an immutable snapshot of it).
Review/audit node over another node's worktree: read-only shape (no `worktree`, no `owns`, no `vcs` output) and
`dispatch-agent.py … --cwd <writer worktree>`; the cwd must be the run repo or one of its linked worktrees.
`--path` is a worktree *create* flag and fails on an existing directory. Writers/integrators ignore `--cwd`.
Audit/report node: writer shape, `owns` = the report directory; if the directory is gitignored the node makes no
commits and uses `vcs.result.form: "none"` (HEAD must descend from `base_sha` and be `base_sha` or an already-promoted sha; clean tree — no sha hack).

Adding a wave: `g2 = copy.deepcopy(graph); g2["nodes"].update(new_nodes); run_state.validate_graph(g2);
run_state.accept_graph(RUN, g2, repo=REPO, holder=HOLDER); run_state.reconcile(RUN, holder=HOLDER)` → new
writers are `ready`, nodes with unmet `depends_on` are `planned`.

### 2.1.1 Accepted base and continuing a previous candidate

`accept_graph` does not use a submitted `base_sha`: it is ignored, not validated, and a mismatch is not refused. The first acceptance records `HEAD` of the supplied canonical `repo` as the run's immutable base; later acceptances return that recorded value. Contextual writer/integrator worktrees are created at the returned `base_sha`, and collection requires every commit to descend from it. After every acceptance, compare `accepted["base_sha"]` with the commit the briefs assume **before the first dispatch**. On mismatch, do not dispatch: retarget the briefs to the accepted base or start a new run from a repository at the intended commit. Never move or reset the primary checkout to change an existing run's base.

A fresh writer continues a previous verified candidate `CAND` (its `vcs.json` `result.sha`, built on `CAND_BASE`) only if its isolated worktree actually contains the candidate; `depends_on` and `inputs` order work and record consumption but never transfer code.

- Promoted in this run: the new node `depends_on` the integrator and declares its `integration` output in `inputs` (consumption is recorded and requires the promoted `integration.json`); Step 0 is `git reset --hard <promoted sha>`. The ownership baseline is then the promoted line, so `owns` covers only new edits.
- Not promoted (earlier run, failed or unpromoted attempt): cherry-pick `CAND_BASE..CAND` onto the accepted base and make `owns` cover every inherited path before acceptance. The baseline is the base, so cherry-picked paths count as this node's changes and an uncovered one fails the `finish-agent.py` ownership gate. Use `git reset --hard CAND` in the fresh isolated worktree only when `CAND_BASE == BASE` and `BASE` is an ancestor of `CAND`; ancestry alone can import unreviewed changes between `BASE` and `CAND_BASE`. `CAND` must exist in the run repository's object store (fetch it first if it came from another clone).

```python
import fnmatch

CAND, CAND_BASE = "<verified candidate sha>", "<sha it was built on>"
EXPECTED_BASE = "<committed base the briefs assume>"
inherited = git(REPO, "diff", "--name-only", "-z", CAND_BASE, CAND).split("\0")
uncovered = [path for path in inherited if path and
             not any(fnmatch.fnmatch(path, pattern) for pattern in graph["nodes"][NODE]["owns"])]
if uncovered:
    raise ValueError(f"owns does not cover inherited paths: {uncovered}")
accepted = run_state.accept_graph(RUN, graph, repo=REPO, holder=HOLDER)
BASE = accepted["base_sha"]                      # authoritative; graph["base_sha"] is not an input
if BASE != EXPECTED_BASE:
    raise ValueError("accepted base differs from the briefs; do not dispatch")
step0 = f"""set -e
test "$(git rev-parse HEAD)" = {BASE} || {{ echo 'worktree is not at the accepted base'; exit 1; }}
git cherry-pick {CAND_BASE}..{CAND}              # on conflict stop and report; do not improvise
python3 - <<'PY'
import re
import subprocess
diff = subprocess.check_output(["git", "range-diff", "--no-color", "{CAND_BASE}..{CAND}", "{BASE}..HEAD"], text=True)
print(diff, end="")
if not diff.strip() or any(not re.match(r"^\\s*\\d+:\\s+\\S+\\s+=\\s+\\d+:", row) for row in diff.splitlines()):
    raise SystemExit("candidate commit series changed; stop for review")
PY
"""
```

Put `step0` first in the brief; it fails closed if the worktree is not at the accepted base instead of repairing from the wrong commit.

### 2.2 Dispatch

Follow [SKILL.md](../SKILL.md#orchestration-loop) and [identity-reattach.md](identity-reattach.md) for dispatch, confirmed delivery, launch recovery, and exact-identity continuation. Every OMP prompt starts with the literal line `orchestrate`.

### 2.3 Monitor (never poll by hand)

Launch one auto-delivering background observer using [run-state-api.md](run-state-api.md#background-observation-and-output-cost). Adaptive polling and change-only JSONL are defaults; `--every-poll` is an explicit diagnostic opt-in. Settlement is observation, not success.

### 2.4 Collect

Follow [SKILL.md](../SKILL.md#orchestration-loop) and [retire-promote.md](retire-promote.md#collection-and-ownership) for ownership, receipt, collection, and integrator promotion gates.

Read the full contextual `summary.json` and the screenshots the brief demanded before calling the slice done. The controller reviews screenshots; a slice that "passed its tests" with a broken screenshot is not accepted.

### 2.5 Land

```python
run_state.promote(RUN, node="D5", attempt=1, new_sha=head, repo=REPO, holder=HOLDER)   # updates refs/heads/herdr/<run>
delivery = run_state.deliver_branch(RUN, repo=REPO, holder=HOLDER)                     # {"delivery_branch", "sha", "created"}
git(REPO, "push", "origin", f"{delivery['sha']}:{delivery['delivery_branch']}")          # fast-forward only
subprocess.run(["python3", f"{SCRIPTS}/retire-agents.py", *agent_names, "--run", RUN,
                "--session", SESSION, "--holder-workspace", WS, "--holder-pane", PANE])   # prints {"retired": [...], "refused": []}
PROMOTED = head
```
Controller commits made directly on the primary checkout between waves (small fixes, `.gitignore`, skill docs) must
be pushed before the next integrator starts, and the integrator's Step 0 must reset to that pushed head — otherwise
the final push is not a fast-forward. Worktrees and branches are retained until the PR merges.

## 3. Designing a wave

- Start every writer from `PROMOTED` (`git reset --hard <sha>` is the brief's first command); the worktree itself
  is created at the run base and is stale.
- Ownership is a hard boundary, fnmatch globs, disjoint across concurrent nodes. When two slices need the same
  file, split at file level and state the contract (e.g. `views/run-detail/index.tsx` → LS, `RunActionBar.tsx` +
  `RunStageNav.tsx` + `hooks/useRunRuntime.ts` → RS, "`useRunEvents` changes are additive"). Give exactly one node
  the hand-written API client; freeze generated files, global styles, the router, and the version file for
  everyone — the integrator owns those.
- Explicit lists beat globs when one file in a directory belongs elsewhere (`views/workflow/*` minus
  `MaximisedGraph.tsx`).
- Every brief carries: Step 0 (reset sha, docs to read, node_modules symlink), Ownership, Contract with concurrent
  panes, Environment (own ports, what never to start, test data, screenshot dir), Verification (exact commands,
  named screenshots, measurable assertions such as `scrollWidth === innerWidth`), Definition of done (commit scope,
  clean tree, final-reply fields — the reply is collected verbatim). Ten near-identical briefs were generated from
  one template with a per-node dict; it took minutes and produced consistent panes.
- Integrator briefs list the merge order, the seams to check by hand (each pane verified against its own base, not
  the merged tree), regeneration of generated files against the merged gateway, the version bump, every gate
  command, a browser smoke with named screenshots, and "commit gate fixes separately, never squash writer commits".
- Audit nodes run after the integrator they audit (an audit of a head that four concurrent slices are changing is
  invalidated on merge). Their output directory was gitignored; they write in place in the primary checkout and
  commit nothing.

## 4. Environment rules that came from incidents

- Rule zero: a pane must never start a worker against shared infrastructure (here an ARQ worker on the shared Redis
  queue): it steals the operator's jobs and, started without the operator's env, produces failures that look like
  product regressions. Diagnose with the job's recorded `worker_id` (`hostname:pid`) vs `pgrep`.
- Each pane runs its own gateway/UI on assigned ports; some ports are held by macOS `launchd` (8021) — tell panes to
  add 100 and report. Never verify against the operator's `:8000`/`:5173`; never restart them.
- Auth bound to corporate SSO → panes mint sessions straight into the shared Redis with the same helper the tests
  use, and delete them at the end. Leftover `*session*` keys from test suites are normal (TTL); check for the
  pane's own subjects only.
- Env files sourced by bash strip inner double quotes: JSON values must be single-quoted.

## 5. Herdr commands used by the controller

```
herdr agent list                                    # every live agent: pane_id, agent_status, cwd, session path
herdr agent get <name|pane>                         # one agent (agent_session.value = omp session jsonl)
herdr pane read <pane-id>                           # the pane screen (todo tree, subagent list, spinner)
herdr agent read <name> --source recent --lines N   # recent terminal output
herdr agent prompt <name|pane> "<text>"             # send a prompt (used to hand a late item to a working pane,
                                                    #   and to prompt an idle pane left by a failed spawn)
herdr workspace list                                # find a workspace by label; an orphaned contextual workspace
                                                    #   carries label == worker_id (fo-…) — close it before re-running
python3 SCRIPTS/spawn-agent.py <name> --cwd <dir> --kind omp --model-role default --prompt-file <f>
                                                    # plain pane outside a run; name-only response accepted;
                                                    #   a post-start failure prints the prompt-agent.py follow-up
python3 SCRIPTS/retire-agents.py <names…> --run RUN --session S --holder-workspace W --holder-pane P # never retire the user's own pane
```
Never stop Herdr; never remove worktrees/branches before landing; never retire/close the user's pane.

