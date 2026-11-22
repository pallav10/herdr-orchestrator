"""Structured worker verdict validation and rework stagnation detection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from ioutil import PreconditionError, UsageError

VERDICT_FILENAME = "verdict.json"
VERDICT_STATUSES = frozenset({"pass", "rework", "fail", "blocked"})
REWORK_STATES = frozenset({"failed", "invalidated", "retried"})


def findings_hash(findings: list[dict[str, Any]]) -> str | None:
    """Return a deterministic sha256 digest for non-empty normalized verdict findings."""
    if not findings:
        return None
    normalized = [
        {
            "severity": item["severity"],
            "message": item["message"],
            **({"path": item["path"]} if item.get("path") is not None else {}),
        }
        for item in findings
    ]
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate_verdict(data: Any) -> dict[str, Any]:
    """Validate a parsed verdict.json payload and return a normalized dict."""
    if not isinstance(data, dict):
        raise PreconditionError("verdict.json must be a JSON object")

    status = data.get("status")
    if not isinstance(status, str) or status not in VERDICT_STATUSES:
        raise PreconditionError(
            f"verdict.status must be one of {sorted(VERDICT_STATUSES)}"
        )

    summary = data.get("summary")
    if not isinstance(summary, str):
        raise PreconditionError("verdict.summary must be a string")

    raw_findings = data.get("findings")
    if not isinstance(raw_findings, list):
        raise PreconditionError("verdict.findings must be a list")

    findings: list[dict[str, Any]] = []
    for index, item in enumerate(raw_findings):
        if not isinstance(item, dict):
            raise PreconditionError(f"verdict.findings[{index}] must be an object")
        severity = item.get("severity")
        message = item.get("message")
        if not isinstance(severity, str) or not severity:
            raise PreconditionError(
                f"verdict.findings[{index}].severity must be a non-empty string"
            )
        if not isinstance(message, str) or not message:
            raise PreconditionError(
                f"verdict.findings[{index}].message must be a non-empty string"
            )
        normalized_item: dict[str, Any] = {"severity": severity, "message": message}
        if "path" in item and item["path"] is not None:
            path = item["path"]
            if not isinstance(path, str) or not path:
                raise PreconditionError(
                    f"verdict.findings[{index}].path must be a non-empty string"
                )
            normalized_item["path"] = path
        findings.append(normalized_item)

    result: dict[str, Any] = {
        "status": status,
        "summary": summary,
        "findings": findings,
    }
    if "rework_target" in data and data["rework_target"] is not None:
        target = data["rework_target"]
        if not isinstance(target, str) or not target:
            raise PreconditionError("verdict.rework_target must be a non-empty string")
        result["rework_target"] = target
    return result


def load_and_validate_verdict(attempt_dir: str | Path) -> dict[str, Any] | None:
    """Load and validate <attempt_dir>/verdict.json, or return None when absent."""
    path = Path(attempt_dir) / VERDICT_FILENAME
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise PreconditionError(f"verdict path must be a regular file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreconditionError(f"invalid verdict.json at {path}: {exc}") from exc
    return validate_verdict(raw)


def _event_findings_hash(event: dict[str, Any]) -> str | None:
    for key in ("findings_hash", "verdict_findings_hash"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    verdict = event.get("verdict")
    if isinstance(verdict, dict) and isinstance(verdict.get("findings"), list):
        try:
            validated = validate_verdict(verdict)
        except PreconditionError:
            return None
        return findings_hash(validated["findings"])
    return None


def detect_rework_stagnation(
    events: Iterable[dict[str, Any]],
    node_id: str,
    *,
    window: int = 3,
) -> dict[str, Any]:
    """Detect repeated failure reasons or identical verdict findings across consecutive attempts."""
    if isinstance(window, bool) or not isinstance(window, int) or window < 2:
        raise UsageError("window must be an integer >= 2")

    attempts: dict[int, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("node") != node_id:
            continue
        typ = event.get("type")
        if typ not in REWORK_STATES and typ not in {"succeeded", "interrupted", "skipped"}:
            continue
        raw_attempt = event.get("attempt")
        if isinstance(raw_attempt, bool) or not isinstance(raw_attempt, int) or raw_attempt < 1:
            continue

        entry = attempts.setdefault(
            raw_attempt,
            {"attempt": raw_attempt, "outcome": None, "reason": None, "findings_hash": None},
        )
        if typ not in REWORK_STATES:
            entry["outcome"] = typ
            continue

        # failed/invalidated take precedence over retried for reason/outcome attribution
        reason = event.get("reason")
        if isinstance(reason, str) and reason:
            if entry["outcome"] not in {"failed", "invalidated"} or typ in {"failed", "invalidated"}:
                entry["reason"] = reason
        fhash = _event_findings_hash(event)
        if fhash is not None:
            entry["findings_hash"] = fhash
        if typ in {"failed", "invalidated"} or entry["outcome"] is None:
            entry["outcome"] = typ

    ordered = [attempts[num] for num in sorted(attempts)]
    trailing: list[dict[str, Any]] = []
    for entry in reversed(ordered):
        if entry["outcome"] in REWORK_STATES:
            trailing.append(entry)
        else:
            break
    trailing.reverse()

    if len(trailing) < window:
        return {
            "node": node_id,
            "stagnated": False,
            "window": window,
            "streak": len(trailing),
            "attempts": [item["attempt"] for item in trailing],
            "reason": None,
            "findings_hash": None,
        }

    tail = trailing[-window:]
    reasons = [item["reason"] for item in tail]
    hashes = [item["findings_hash"] for item in tail]
    same_reason = reasons[0] if reasons[0] and all(r == reasons[0] for r in reasons) else None
    same_hash = hashes[0] if hashes[0] and all(h == hashes[0] for h in hashes) else None
    if same_reason == "verdict:rework" and not same_hash and any(hashes):
        same_reason = None
    stagnated = bool(same_reason or same_hash)

    return {
        "node": node_id,
        "stagnated": stagnated,
        "window": window,
        "streak": len(trailing),
        "attempts": [item["attempt"] for item in tail],
        "reason": same_reason,
        "findings_hash": same_hash,
    }
