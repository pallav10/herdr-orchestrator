# Orchestrator bootstrap — {{JOB}}

_Date: {{DATE}} · Session: {{SESSION}} · Prepared from pane {{PANE}}_

You are the main orchestrator pane for {{JOB}}. Read the handoff context, acknowledge any pending rotation, then reconcile the canonical run before dispatch.

## Grounding (run these, confirm each)
- `test "${HERDR_ENV:-}" = 1`
- Read `{{SESSION_DIR}}/HANDOFF.md`
- Read `{{SESSION_DIR}}/session.json`, then use its `run` value as the canonical absolute run path under the gitignored runtime `docs/runs/` dir; session docs live under gitignored `docs/sessions/`
- If `rotate.py` generated an ack command for this pane, run that exact `ack-handoff.py --nonce <n> --session <predecessor path>` command now; the generic template does not invent nonce/predecessor values
- Reconcile using the ack JSON's absolute `run` and successor `session` if acknowledgment ran; otherwise use this session's metadata run: `python3 scripts/reconcile.py --run <absolute run path> --session <absolute session path> --holder-workspace "$HERDR_WORKSPACE_ID" --holder-pane "$HERDR_PANE_ID"`.
- Do not resume dispatch until any required nonce ack has succeeded
- Reattach live Herdr workers by their exact workspace, pane, agent, and context/provenance tokens; do not redispatch them.
- Do not treat `progress.md`, `docs/sessions/latest`, Herdr idle, or Herdr done as the live ledger or success state

## Current focus
- 

## Do not
- Do not rotate unless measured current native occupancy is >= 50% (`tokens * 2 >= context_window`). Unknown or lower usage must leave the run untouched. Do not bypass `rotate.py` with a footer percentage, estimate, or CLI guess. `/restart` this OMP pane to reload the installed extension if the export is missing; `/reload-plugins` is insufficient, and `rotate.py` does not auto-restart.
- 
