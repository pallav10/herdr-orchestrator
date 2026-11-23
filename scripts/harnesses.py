"""Harness registry and capability descriptors for multi-harness orchestration.

Supports OMP, Claude Code, Codex, Copilot CLI, Pi, Grok Build, and generic CLI
harnesses without introducing per-harness subclasses or external dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

import ioutil

DEFAULT_KIND = "omp"

_JS_HOSTS = frozenset({"bun", "node"})
_LSP_MARKERS = ("pyright", "language-server", "pylsp", "tsserver")
_OMP_CLI_ENTRY = "@oh-my-pi/pi-coding-agent/dist/cli.js"
_PI_CLI_ENTRY = "@mariozechner/pi-coding-agent/dist/cli.js"

# Canonical kind aliases accepted from CLI flags, env vars, or graph.json.
HARNESS_ALIASES: dict[str, str] = {
    "omp": "omp",
    "oh-my-pi": "omp",
    "claude": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "openai-codex": "codex",
    "copilot": "copilot",
    "copilot-cli": "copilot",
    "github-copilot": "copilot",
    "pi": "pi",
    "grok": "grok",
    "grok-build": "grok",
}


@dataclass(frozen=True)
class HarnessSpec:
    kind: str
    binary: str
    orchestrate_default: bool = False
    require_provider_model: bool = False
    model_flag: str = "--model"
    thinking_flag: str | None = None
    effort_flag: str | None = None
    effort_config_key: str | None = None
    process_names: tuple[str, ...] = ()
    js_entries: tuple[str, ...] = ()
    herdr_session_source: str | None = None
    context_source: str | None = None
    context_guidance: str | None = None


HARNESSES: dict[str, HarnessSpec] = {
    "omp": HarnessSpec(
        kind="omp",
        binary="omp",
        orchestrate_default=True,
        require_provider_model=True,
        model_flag="--model",
        thinking_flag="--thinking",
        process_names=("omp",),
        js_entries=(_OMP_CLI_ENTRY,),
        herdr_session_source="herdr:omp",
        context_source="OMP ExtensionContext.getContextUsage",
        context_guidance="/restart this OMP pane to reload the installed extension, then retry in the current controller",
    ),
    "pi": HarnessSpec(
        kind="pi",
        binary="pi",
        orchestrate_default=False,
        require_provider_model=False,
        model_flag="--model",
        thinking_flag="--thinking",
        process_names=("pi",),
        js_entries=(_PI_CLI_ENTRY,),
        herdr_session_source="herdr:pi",
        context_source="Pi ExtensionContext.getContextUsage",
        context_guidance="Reload the Pi context extension in this pane, then retry in the current controller",
    ),
    "claude": HarnessSpec(
        kind="claude",
        binary="claude",
        orchestrate_default=False,
        require_provider_model=False,
        model_flag="--model",
        effort_flag="--effort",
        process_names=("claude",),
        js_entries=("@anthropic-ai/claude-code/cli.js",),
        herdr_session_source="herdr:claude",
        context_source="Claude Code context hook",
        context_guidance="Install the herdr-orchestrator Claude Code context hook (scripts/claude-context-hook.py), then retry",
    ),
    "codex": HarnessSpec(
        kind="codex",
        binary="codex",
        orchestrate_default=False,
        require_provider_model=False,
        model_flag="-m",
        effort_config_key="model_reasoning_effort",
        process_names=("codex",),
        herdr_session_source="herdr:codex",
        context_source="Codex context snapshot",
    ),
    "copilot": HarnessSpec(
        kind="copilot",
        binary="copilot",
        orchestrate_default=False,
        require_provider_model=False,
        model_flag="--model",
        effort_flag="--reasoning-effort",
        process_names=("copilot",),
        herdr_session_source="herdr:copilot",
        context_source="Copilot CLI context snapshot",
    ),
    "grok": HarnessSpec(
        kind="grok",
        binary="grok",
        orchestrate_default=False,
        require_provider_model=False,
        model_flag="--model",
        process_names=("grok",),
        herdr_session_source="herdr:grok",
        context_source="Grok Build context snapshot",
    ),
}


def normalize_kind(raw: str | None) -> str | None:
    """Normalize a harness name or alias to its canonical kind, preserving unknown custom kinds."""
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    return HARNESS_ALIASES.get(cleaned, cleaned)


def get_spec(kind: str | None) -> HarnessSpec:
    canonical = normalize_kind(kind) or DEFAULT_KIND
    spec = HARNESSES.get(canonical)
    if spec is not None:
        return spec
    return HarnessSpec(
        kind=canonical,
        binary=canonical,
        orchestrate_default=False,
        require_provider_model=False,
        model_flag="--model",
        process_names=(canonical,),
        herdr_session_source=f"herdr:{canonical}",
    )


def default_kind(
    *,
    explicit: str | None = None,
    node_kind: str | None = None,
    graph_kind: str | None = None,
    detected_kind: str | None = None,
) -> str:
    """Resolve effective worker harness kind by precedence:
    explicit CLI flag > node.kind > graph.default_kind > HERDR_ORCHESTRATOR_KIND > detected > 'omp'.
    """
    for candidate in (
        explicit,
        node_kind,
        graph_kind,
        os.environ.get("HERDR_ORCHESTRATOR_KIND"),
        detected_kind,
    ):
        normalized = normalize_kind(candidate)
        if normalized:
            return normalized
    return DEFAULT_KIND


def state_dir(*parts: str, home: Path | None = None) -> Path:
    """Harness-neutral XDG state directory for herdr-orchestrator runtime files."""
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and os.path.isabs(xdg):
        root = Path(xdg) / "herdr-orchestrator"
    else:
        base = home if home is not None else Path.home()
        root = base / ".local" / "state" / "herdr-orchestrator"
    return root.joinpath(*parts)


def config_dir(*parts: str, home: Path | None = None) -> Path:
    """Harness-neutral XDG config directory for herdr-orchestrator user config."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        root = Path(xdg) / "herdr-orchestrator"
    else:
        base = home if home is not None else Path.home()
        root = base / ".config" / "herdr-orchestrator"
    return root.joinpath(*parts)


def validate_model_spec(kind: str, model: str, *, origin: str) -> None:
    """Validate a model identifier against the target harness's requirements."""
    spec = get_spec(kind)
    if not spec.require_provider_model:
        if not model.strip():
            raise ValueError(f"{origin} must be a non-empty model identifier")
        return
    provider, sep, leaf = model.partition("/")
    if not (sep and provider and leaf):
        raise ValueError(
            f"{origin} must be provider/model[:thinking]; "
            "bare aliases like 'slow'/'smol' are not models"
        )


def build_model_argv(
    kind: str,
    *,
    model: str = "",
    thinking: str = "",
    effort: str = "",
) -> list[str]:
    """Build post-`--` CLI flags for the target harness."""
    spec = get_spec(kind)
    argv: list[str] = []
    if model:
        argv.extend([spec.model_flag, model])
    if thinking:
        if spec.thinking_flag:
            argv.extend([spec.thinking_flag, thinking])
        elif spec.effort_flag and not effort:
            argv.extend([spec.effort_flag, thinking])
        elif spec.effort_config_key and not effort:
            argv.extend(["-c", f"{spec.effort_config_key}={thinking}"])
    if effort:
        if spec.effort_flag:
            argv.extend([spec.effort_flag, effort])
        elif spec.effort_config_key:
            argv.extend(["-c", f"{spec.effort_config_key}={effort}"])
        elif spec.thinking_flag and not thinking:
            argv.extend([spec.thinking_flag, effort])
    return argv


def resolve_role_for_kind(
    kind: str,
    role: str,
    *,
    home: Path | None = None,
    run_cmd=None,
) -> str:
    """Resolve a logical model role (e.g. default/slow/smol) for the given harness.

    - For `omp`: queries `omp config get modelRoles --json` with mtime-checked cache.
    - For all other harnesses: reads `~/.config/herdr-orchestrator/roles.json`
      (either `{"<kind>": {"<role>": "<model>"}}` or flat `{"<role>": "<model>"}`).
    """
    canonical = normalize_kind(kind) or DEFAULT_KIND
    base_home = home if home is not None else Path.home()
    runner = run_cmd if run_cmd is not None else ioutil.run_cmd

    if canonical == "omp":
        config = base_home / ".omp/agent/config.yml"
        cache = base_home / ".omp/run/herdr-cache/model-roles.json"
        try:
            with config.open("rb"):
                mtime = config.stat().st_mtime_ns
        except OSError:
            mtime = None
        roles = None
        if mtime is not None:
            try:
                saved = json.loads(cache.read_text(encoding="utf-8"))
                if (
                    isinstance(saved, dict)
                    and saved.get("config_mtime_ns") == mtime
                    and isinstance(saved.get("roles"), dict)
                    and all(isinstance(value, str) for value in saved["roles"].values())
                ):
                    roles = saved["roles"]
            except (OSError, ValueError, AttributeError):
                pass
        if roles is None:
            proc = runner(["omp", "config", "get", "modelRoles", "--json"], capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"omp config get modelRoles failed: {proc.stderr.strip()}")
            roles = json.loads(proc.stdout)["value"]
            if mtime is not None:
                try:
                    if config.stat().st_mtime_ns == mtime:
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        ioutil.atomic_json(cache, {"config_mtime_ns": mtime, "roles": roles})
                except OSError:
                    pass
        if role not in roles:
            raise RuntimeError(f"model role {role!r} not in modelRoles {sorted(roles)}")
        return roles[role]

    roles_path = config_dir("roles.json", home=base_home)
    if not roles_path.is_file():
        raise RuntimeError(
            f"model role {role!r} cannot be resolved for harness {canonical!r}: "
            f"pass --model explicitly or configure {roles_path}"
        )
    try:
        raw = json.loads(roles_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read roles config {roles_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError(f"roles config {roles_path} must be a JSON object")
    scoped = raw.get(canonical)
    table = scoped if isinstance(scoped, dict) else raw
    value = table.get(role)
    if not isinstance(value, str) or not value.strip():
        available = sorted(k for k, v in table.items() if isinstance(v, str))
        raise RuntimeError(
            f"model role {role!r} not configured for harness {canonical!r} in {roles_path} (available: {available})"
        )
    return value.strip()


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


def is_harness_process(proc: dict[str, Any], kind: str) -> bool:
    """Return True if a foreground process entry matches the expected harness."""
    spec = get_spec(kind)
    names = _process_basenames(proc)
    if any(any(marker in item.lower() for marker in _LSP_MARKERS) or item.lower().endswith("lsp") for item in names):
        return False
    argv = proc.get("argv")
    cmdline = proc.get("cmdline")
    if spec.kind == "omp":
        if (isinstance(argv, list) and any(isinstance(item, str) and "__omp_worker" in item for item in argv)) or (
            isinstance(cmdline, str) and "__omp_worker" in cmdline
        ):
            return False
    if any(pname in names for pname in spec.process_names):
        return True
    if not names & _JS_HOSTS or not isinstance(argv, list) or len(argv) < 2 or not isinstance(argv[1], str) or not argv[1]:
        return False
    entry = argv[1].replace("\\", "/")
    entry_name = Path(entry).name
    if entry_name in spec.process_names:
        return True
    return any(entry == js_entry or entry.endswith("/" + js_entry) for js_entry in spec.js_entries)
