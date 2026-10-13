"""Controller-facing API; implementations live in the defining modules."""

from ioutil import (
    RunError, UsageError, PreconditionError, new_worker_id, _now, canonical_path, slug, run_cmd, atomic_text,
    atomic_json, _read_json, _run_git, _git_ref, sha256_file, SUBPROCESS_TIMEOUT,
    first_str, payload_text, payload_field,
)
from graph_model import (
    _holder_payload, _as_list, _node_outputs, _effective_owns, validate_graph,
    _eligible_blocked_continuation, holder_exact, require_active_holder, _event_matches_controller, _check_holder,
    artifact_name, _attempt_int, _required_attempt, output_specs, validate_model, validate_continuation_routing,
    _request_fields, _condition_true, current_artifact, has_current_dispatch_intent, _herdr_token,
    parse_herdr_identity, herdr_identity_matches, herdr_command_target, _retry_allows, SCHEMA_VERSION, EXECUTORS,
    STATES, OUTPUT_TYPES, RETRY_ON, ATTEMPT_TERMINAL, SETTLED, HOLDER_KEYS, PROVENANCE_ORCHESTRATOR, LEASE_ACTIVE,
    LEASE_DRAINING, MODEL_KEYS, HERDR_EXECUTORS, RUNTIME_KEYS, REQUEST_KEYS, NODE_ID_RE, AGENT_NAME_RE,
    IDENTITY_MANAGED_NAME, IDENTITY_RUNTIME_ID, WORKER_ID_RE, _DRAIN_ALLOWED_EVENTS,
)
from ledger import (
    claim_run, load_run, bind_session_run, _revision_digest, _revision_path, _load_graph_revision,
    _committed_graph_revision, load_graph, _node_is_active, _validate_revision_against_runtime,
    _promotion_ref_checked_out, _refuse_checked_out_promotion_ref, _materialize_graph_revision_locked,
    accept_graph, with_lock, append_event, append_event_locked, append_events_locked, _events, _initial_state, _resolve_consumed,
    _complete_json, _artifacts_root, _ensure_attempt_snapshot, _repair_dispatch_snapshots_locked, _relative_to,
    attempt_dir, _artifact_file_name, read_consumed_snapshot, _authoritative_consumed, _file_hash,
    _validate_artifact_records, load_dispatch_identity, _validate_writer_gates, validate_vcs_payload,
    _consumed_receipt_tuple, vcs_typed_consumed, validate_integration_payload, _promotion_artifacts,
    _matching_promoted_event, validate_promoted_receipt, _validate_success, _validate_ready,
    _pending_promotion_intent, _normalize_dispatch_model, _normalize_blocked_continuation,
    _validate_run_scoped_event, _validate_event_append, _apply_event, _runtime_overlay, _apply_graph_revision,
    replay, load_state, recorded_herdr_identity, _previous_promotion_sha, derive_status_md, write_caches,
    _materialize_locked, _EVENT_TO,
)
from authz import (
    dispatch_guard,
)
from artifacts import (
    prepare_dispatch_prompt,
)
from routing import (
    split_spec, canonical_routing,
)
from liveness import (
    native_quiesced, _native_handle_id, _observable_orphan, maybe_interrupt_native, _herdr_live_triple,
    _herdr_live_surface, _herdr_stamp_complete, _live_herdr_records, herdr_ready_dispatch_candidate,
    maybe_interrupt_herdr,
)
from promotion import (
    delivery_branch_ref, default_delivery_branch, _delivered_sha, _graph_work_complete,
    _deliver_branch_locked, deliver_branch, _autodeliver_locked, _finalize_promotion_locked, promote,
    _pending_promotion, recover_promotion, _ensure_delivery_locked, _recover_promotion_locked,
)
from scheduler import (
    _computed_lifecycle_events, _invalidation_policy_events, invalidate_if_stale, _event_key, _same_state_repeat,
    _all_done_occurrence, _notification_edge_key, _notification_targets, _notify, _append_candidates_locked,
    reconcile,
)
from cli_common import (
    parse_child_json, token_value, token_flag, run_context, holder_from_args, validate_auth_args,
    agent_workspace_id, agent_pane_id, workspace_pane_id, TOKEN_VALUE_MAX, SCRIPTS,
)
from ownership import (
    _parse_charclass, _glob_tokens, _charset_intersect, _glob_consume, _glob_epsilon, _fnmatch_intersects,
    owns_overlap,
)
