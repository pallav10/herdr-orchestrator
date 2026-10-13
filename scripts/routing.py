"""Canonical model routing and model specification parsing."""

from __future__ import annotations

from typing import Any
from graph_model import MODEL_KEYS, validate_model
from ioutil import payload_field


def split_spec(spec: str) -> tuple[str, str]:
    """'provider/model[:thinking]' -> (model, thinking)."""
    model, _, think = spec.rpartition(":")
    if not model or "/" in think:
        return spec, ""
    return model, think


def canonical_routing(model: dict[str, Any]) -> dict[str, str | None]:
    payload = {key: model.get(key) for key in MODEL_KEYS}
    requested = model.get("requested_model")
    if requested is not None:
        payload["requested_model"] = requested
    return validate_model(payload)


def routing_for(
    payloads: list[dict[str, Any] | None],
    *,
    requested_model: str | None = None,
    requested_role: str | None = None,
    requested_effort: str | None = None,
    explicit_model: str = "",
    explicit_thinking: str = "",
) -> dict[str, str | None]:
    """Observe live routing, with explicit fallbacks only when supplied by spawn."""
    resolved = next(
        (value for p in payloads if (value := payload_field(p, {"model", "model_id", "resolved_model"}))),
        None,
    )
    thinking = next(
        (value for p in payloads if (value := payload_field(p, {"thinking", "resolved_thinking"}))),
        None,
    )
    effort = next(
        (value for p in payloads if (value := payload_field(p, {"effort", "reasoning_effort", "resolved_effort"}))),
        None,
    )
    if resolved:
        resolved, model_thinking = split_spec(resolved)
        thinking = thinking or model_thinking or None
    routing = {
        "requested_role": requested_role,
        "requested_effort": requested_effort,
        "resolved": resolved or explicit_model or None,
        "thinking": thinking or explicit_thinking or None,
        "effort": effort,
    }
    if requested_model is not None:
        routing["requested_model"] = requested_model
    return canonical_routing(routing)
