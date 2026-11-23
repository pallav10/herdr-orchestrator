"""Measured harness context usage and same-process anti-spoof validation."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import time
from typing import Any, NoReturn

import harnesses
import ioutil
import session_docs
from herdr_cli import herdr, is_core_mode

CONTEXT_SOURCE = "OMP ExtensionContext.getContextUsage"
CONTEXT_MAX_AGE_MS = 5000
SNAPSHOT_MAX_BYTES = 16384
SESSION_HEADER_MAX_BYTES = 8192
CONTEXT_DIRECTORY = Path(".omp") / "run" / "herdr-context"
CONTEXT_GUIDANCE = "/restart this OMP pane to reload the installed extension, then retry in the current controller"
_SNAPSHOT_KEYS = {
    "schema_version",
    "source",
    "workspace",
    "pane",
    "pid",
    "observed_at_ms",
    "available",
    "omp_session_file",
    "omp_session_id",
    "model",
    "context_usage",
}
_GENERIC_SNAPSHOT_KEYS = {
    "schema_version",
    "harness",
    "source",
    "workspace",
    "pane",
    "pid",
    "observed_at_ms",
    "available",
    "model",
    "context_usage",
}
_GENERIC_OPTIONAL_KEYS = {"session_file", "session_id"}
_OMP_CLI_ENTRY = "@oh-my-pi/pi-coding-agent/dist/cli.js"
_JS_HOSTS = frozenset({"bun", "node"})

class ContextUsageError(ioutil.PreconditionError):
    """Measured context usage is missing, invalid, or below 50%."""

    def __init__(
        self,
        reason: str,
        *,
        fraction: float | None = None,
        source: str | None = None,
        guidance: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.fraction = fraction
        self.source = source
        self.guidance = guidance or "Resolve the measurement refusal and retry in the same pane."


def context_snapshot_path(workspace: str, pane: str, *, prefer_neutral: bool = False) -> Path:
    digest = hashlib.sha256(f"{workspace}\n{pane}".encode("utf-8")).hexdigest()
    neutral = harnesses.state_dir("context") / f"{digest}.json"
    legacy = Path.home() / CONTEXT_DIRECTORY / f"{digest}.json"
    if prefer_neutral:
        return neutral
    if neutral.exists() and not legacy.exists():
        return neutral
    return legacy

def _refuse(
    reason: str,
    *,
    fraction: float | None = None,
    source: str | None = None,
    guidance: str | None = None,
) -> NoReturn:
    raise ContextUsageError(reason, fraction=fraction, source=source, guidance=guidance)


def _known_source(payload: dict[str, Any]) -> str | None:
    source = payload.get("source")
    return source if isinstance(source, str) and source else None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _require_chosen_herdr() -> None:
    if is_core_mode():
        if not os.environ.get("HC_TERMINAL_ID"):
            _refuse("HC_TERMINAL_ID is required")
        return
    if os.environ.get("HERDR_ENV") != "1":
        _refuse("HERDR_ENV=1 is required")
    if not os.environ.get("HERDR_SOCKET_PATH") or not os.environ.get("HERDR_SESSION"):
        _refuse("HERDR_SOCKET_PATH and HERDR_SESSION must already be set; refusing to auto-attach")


def _holder_identity(session: Path, holder: dict[str, str]) -> tuple[str, str]:
    try:
        workspace, pane = session_docs.holder_identity(session, holder)
    except (ioutil.PreconditionError, ioutil.UsageError) as exc:
        _refuse(str(exc))
    if not is_core_mode() and not pane.startswith(f"{workspace}:"):
        _refuse("pane id is not an exact workspace prefix")
    return workspace, pane

def _herdr_result(*args: str) -> dict[str, Any]:
    try:
        payload = herdr(*args)
    except ioutil.RunError as exc:
        _refuse(f"context source unavailable: {exc}")
    if not isinstance(payload, dict):
        _refuse("ambiguous herdr payload")
    result = payload.get("result")
    if not isinstance(result, dict):
        _refuse("ambiguous herdr payload")
    return result


def _pane_view(result: dict[str, Any], workspace: str, pane: str, *, expected_harness: str = "omp") -> tuple[Any, ...]:
    if result.get("type") != "pane_info":
        _refuse("ambiguous pane get")
    live = result.get("pane")
    if not isinstance(live, dict):
        _refuse("ambiguous pane get")
    if live.get("pane_id") != pane or live.get("workspace_id") != workspace:
        _refuse("live pane identity mismatch")
    live_agent = harnesses.normalize_kind(live.get("agent")) if isinstance(live.get("agent"), str) else None
    if live_agent != expected_harness:
        _refuse(f"live agent is not {expected_harness}")
    if expected_harness == "omp":
        info = live.get("agent_session")
        if not isinstance(info, dict):
            _refuse("live agent_session missing")
        if info.get("kind") != "path" or info.get("source") != "herdr:omp" or info.get("agent") != "omp":
            _refuse("live agent_session is not a herdr:omp path")
        value = info.get("value")
        if not isinstance(value, str) or not value:
            _refuse("live agent_session path missing")
        return (
            live.get("pane_id"),
            live.get("workspace_id"),
            live.get("agent"),
            info.get("kind"),
            info.get("source"),
            info.get("agent"),
            value,
        )
    info = live.get("agent_session")
    value = info.get("value") if isinstance(info, dict) and isinstance(info.get("value"), str) and info.get("value") else None
    return (
        live.get("pane_id"),
        live.get("workspace_id"),
        live_agent,
        value,
    )

def _process_basenames(proc: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    name = proc.get("name")
    if isinstance(name, str) and name:
        found.add(Path(name).name)
    argv0 = proc.get("argv0")
    if isinstance(argv0, str) and argv0:
        found.add(Path(argv0).name)
    argv = proc.get("argv")
    if isinstance(argv, list) and argv and isinstance(argv[0], str) and argv[0]:
        found.add(Path(argv[0]).name)
    cmdline = proc.get("cmdline")
    if isinstance(cmdline, str) and cmdline.strip():
        found.add(Path(cmdline.split()[0]).name)
    return found


def _is_omp_process(proc: dict[str, Any]) -> bool:
    return harnesses.is_harness_process(proc, "omp")


def _owning_pid(result: dict[str, Any], pane: str, *, expected_harness: str = "omp") -> tuple[Any, ...]:
    if result.get("type") != "pane_process_info":
        _refuse("ambiguous process-info")
    info = result.get("process_info")
    if not isinstance(info, dict) or info.get("pane_id") != pane:
        _refuse("process-info pane mismatch")
    group = info.get("foreground_process_group_id")
    if isinstance(group, bool) or not isinstance(group, int) or group <= 0:
        _refuse("foreground process group is unavailable")
    procs = info.get("foreground_processes")
    if not isinstance(procs, list):
        _refuse("foreground process list is unavailable")
    leader = next((proc for proc in procs if isinstance(proc, dict) and proc.get("pid") == group), None)
    if not isinstance(leader, dict) or not harnesses.is_harness_process(leader, expected_harness):
        label = "OMP" if expected_harness == "omp" else expected_harness
        _refuse(f"owning pid is not the {label} foreground process")
    return (info.get("pane_id"), group, procs)


def _read_snapshot_bytes(path: Path) -> bytes:
    directory = path.parent
    try:
        dir_stat = os.lstat(directory)
    except FileNotFoundError:
        _refuse("context snapshot missing", guidance=CONTEXT_GUIDANCE)
    if stat.S_ISLNK(dir_stat.st_mode) or not stat.S_ISDIR(dir_stat.st_mode):
        _refuse("context snapshot directory is not a secure directory")
    # ponytail: POSIX mode+owner only; check ACLs if a mode-0700 dir is still shared
    if dir_stat.st_uid != os.getuid() or (dir_stat.st_mode & 0o777) != 0o700:
        _refuse("context snapshot directory is not owner-only")
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        _refuse("context snapshot missing", guidance=CONTEXT_GUIDANCE)
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        _refuse("context snapshot is not a regular file")
    if file_stat.st_uid != os.getuid() or (file_stat.st_mode & 0o777) != 0o600:
        _refuse("context snapshot permissions are not owner-only")
    if file_stat.st_size > SNAPSHOT_MAX_BYTES:
        _refuse("context snapshot is malformed")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        _refuse("context snapshot is not a regular file")
    try:
        opened = os.fstat(fd)
        if opened.st_ino != file_stat.st_ino or opened.st_dev != file_stat.st_dev:
            _refuse("context snapshot changed during read")
        blob = os.read(fd, file_stat.st_size + 1)
    finally:
        os.close(fd)
    if len(blob) != file_stat.st_size:
        _refuse("context snapshot changed during read")
    return blob


def _parse_snapshot(blob: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _refuse("context snapshot is malformed")
    if not isinstance(parsed, dict):
        _refuse("context snapshot is malformed")
    return parsed


def _finite_usage(value: Any, *, name: str, positive: bool, source: str | None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _refuse(f"context {name} is not a finite number", source=source)
    if isinstance(value, float) and not math.isfinite(value):
        _refuse(f"context {name} is not a finite number", source=source)
    if positive and not value > 0:
        _refuse(f"context {name} must be positive", source=source)
    if not positive and value < 0:
        _refuse(f"context {name} must be non-negative", source=source)
    return value


def _canonical_session_file(raw: Any, source: str | None) -> str:
    if not isinstance(raw, str) or not os.path.isabs(raw):
        _refuse("omp session file is not canonical", source=source)
    try:
        real = os.path.realpath(raw)
    except OSError:
        _refuse("omp session file is unavailable", source=source)
    if raw != real or os.path.islink(raw) or not os.path.isfile(real):
        _refuse("omp session file is not canonical", source=source)
    return real


def _session_header_id(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        _refuse(f"omp session file is unreadable: {exc}", source=CONTEXT_SOURCE)
    try:
        blob = os.read(fd, SESSION_HEADER_MAX_BYTES)
    finally:
        os.close(fd)
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        _refuse("omp session header is malformed", source=CONTEXT_SOURCE)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            _refuse("omp session header is malformed", source=CONTEXT_SOURCE)
        if not isinstance(entry, dict):
            _refuse("omp session header is malformed", source=CONTEXT_SOURCE)
        kind = entry.get("type")
        if kind == "title":
            continue
        session_id = entry.get("id")
        if kind == "session" and isinstance(session_id, str) and session_id.strip():
            return session_id
        _refuse("omp session header is malformed", source=CONTEXT_SOURCE)
    _refuse("omp session header is malformed", source=CONTEXT_SOURCE)


def read_context_usage(session: Path, holder: dict[str, str]) -> dict[str, Any]:
    """Return validated same-process context evidence or raise ContextUsageError."""
    _require_chosen_herdr()
    workspace, pane = _holder_identity(session, holder)
    path = context_snapshot_path(workspace, pane)
    blob = _read_snapshot_bytes(path)
    parsed = _parse_snapshot(blob)
    source = _known_source(parsed)
    version = parsed.get("schema_version")
    is_generic = version == 2 and isinstance(parsed.get("harness"), str)

    if is_generic:
        harness = harnesses.normalize_kind(parsed.get("harness"))
        if not harness:
            _refuse("context snapshot harness is malformed", source=source)
        expected_source = harnesses.get_spec(harness).context_source or source
        if not source or (expected_source and source != expected_source):
            _refuse(f"context snapshot source is not {expected_source}", source=source)
        if parsed.get("available") is not True:
            reason = parsed.get("reason")
            _refuse(
                reason if isinstance(reason, str) and reason else "context snapshot unavailable",
                source=source,
            )
        keys = set(parsed)
        if not _GENERIC_SNAPSHOT_KEYS.issubset(keys) or not keys.issubset(_GENERIC_SNAPSHOT_KEYS | _GENERIC_OPTIONAL_KEYS):
            _refuse("context snapshot is malformed", source=source)
    else:
        harness = "omp"
        if parsed.get("source") != CONTEXT_SOURCE:
            _refuse("context snapshot source is not ExtensionContext.getContextUsage", source=source)
        source = CONTEXT_SOURCE
        if parsed.get("available") is not True:
            reason = parsed.get("reason")
            _refuse(
                reason if isinstance(reason, str) and reason else "context snapshot unavailable",
                source=CONTEXT_SOURCE,
            )
        if (
            set(parsed) != _SNAPSHOT_KEYS
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version != 1
        ):
            _refuse("context snapshot is malformed", source=CONTEXT_SOURCE)

    if parsed.get("workspace") != workspace or parsed.get("pane") != pane:
        _refuse("context snapshot identity mismatch", source=source)
    pid = parsed.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        _refuse("context snapshot pid is malformed", source=source)
    observed = parsed.get("observed_at_ms")
    if isinstance(observed, bool) or not isinstance(observed, int):
        _refuse("context snapshot timestamp is malformed", source=source)
    _require_fresh(observed, source=source)
    model = parsed.get("model")
    if not isinstance(model, dict) or set(model) != {"provider", "id"}:
        _refuse("context snapshot model is malformed", source=source)
    provider = model.get("provider")
    model_id = model.get("id")
    if not isinstance(provider, str) or not provider or not isinstance(model_id, str) or not model_id:
        _refuse("context snapshot model is malformed", source=source)
    usage = parsed.get("context_usage")
    if not isinstance(usage, dict) or set(usage) != {"tokens", "context_window"}:
        _refuse("context snapshot usage is malformed", source=source)
    tokens = _finite_usage(usage.get("tokens"), name="tokens", positive=False, source=source)
    window = _finite_usage(usage.get("context_window"), name="context_window", positive=True, source=source)

    session_file: str | None = None
    session_id: str | None = None
    if not is_generic:
        session_file = _canonical_session_file(parsed.get("omp_session_file"), CONTEXT_SOURCE)
        session_id = parsed.get("omp_session_id")
        if not isinstance(session_id, str) or not session_id:
            _refuse("context snapshot session id is malformed", source=CONTEXT_SOURCE)
        if _session_header_id(session_file) != session_id:
            _refuse("omp session id mismatch", source=CONTEXT_SOURCE)
    else:
        raw_sf = parsed.get("session_file")
        if raw_sf is not None:
            session_file = _canonical_session_file(raw_sf, source)
        raw_sid = parsed.get("session_id")
        if raw_sid is not None:
            if not isinstance(raw_sid, str) or not raw_sid:
                _refuse("context snapshot session id is malformed", source=source)
            session_id = raw_sid

    first_pane = _herdr_result("pane", "get", pane)
    first_proc = _herdr_result("pane", "process-info", "--pane", pane)
    pane_token = _pane_view(first_pane, workspace, pane, expected_harness=harness)
    proc_token = _owning_pid(first_proc, pane, expected_harness=harness)
    if proc_token[1] != pid:
        label = "OMP" if harness == "omp" else harness
        _refuse(f"owning pid is not the {label} foreground process", source=source)
    if not is_generic:
        try:
            live_file = os.path.realpath(pane_token[-1])
        except OSError:
            _refuse("live omp session file is unavailable", source=CONTEXT_SOURCE)
        if live_file != session_file:
            _refuse("omp session file mismatch", source=CONTEXT_SOURCE)
    elif session_file is not None and pane_token[-1] is not None:
        try:
            live_file = os.path.realpath(pane_token[-1])
        except OSError:
            _refuse("live session file is unavailable", source=source)
        if live_file != session_file:
            _refuse("session file mismatch", source=source)
    if _read_snapshot_bytes(path) != blob:
        _refuse("context snapshot changed during read", source=source)
    if (
        _pane_view(_herdr_result("pane", "get", pane), workspace, pane, expected_harness=harness) != pane_token
        or _owning_pid(_herdr_result("pane", "process-info", "--pane", pane), pane, expected_harness=harness) != proc_token
    ):
        _refuse("pane identity changed during read", source=source)
    _require_fresh(observed, source=source)
    result: dict[str, Any] = {
        "harness": harness,
        "source": source,
        "tokens": tokens,
        "context_window": window,
        "fraction": tokens / window,
        "snapshot": str(path),
        "observed_at_ms": observed,
        "model": {"provider": provider, "id": model_id},
    }
    if session_file is not None:
        result["session_file"] = session_file
        if harness == "omp":
            result["omp_session_file"] = session_file
    if session_id is not None:
        result["session_id"] = session_id
        if harness == "omp":
            result["omp_session_id"] = session_id
    return result

def require_context_usage(session: Path, holder: dict[str, str]) -> dict[str, Any]:
    evidence = read_context_usage(session, holder)
    if evidence["tokens"] * 2 < evidence["context_window"]:
        raise ContextUsageError(
            "measured current context usage is below 50% of the live context window",
            fraction=evidence["fraction"],
            source=evidence["source"],
            guidance="Continue work in the same pane; retry rotation when measured current context usage reaches at least 50%.",
        )
    return evidence


def _require_fresh(observed: int, *, source: str | None = CONTEXT_SOURCE) -> None:
    age = _now_ms() - observed
    if age < 0:
        _refuse("context snapshot is in the future", source=source)
    if age > CONTEXT_MAX_AGE_MS:
        _refuse("context snapshot is stale", source=source)
