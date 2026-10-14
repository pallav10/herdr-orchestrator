# Herdr cheatsheet

Complete reference for the installed **Herdr 0.8.2** CLI, safe everyday recipes, troubleshooting, and — in a clearly separate part — this repository's orchestration adapters.

Source and limits: static output of the installed 0.8.2 binary — `herdr --version`, `herdr --help`, `herdr <group> [<subgroup>] <command> --help` for every command (including two groups root help does not list, `terminal` and `plugin`), `herdr --default-config`, `herdr --skill`, and `herdr completion bash` (used only to cross-check command names and option names against the help pages) — plus the adapters' help/source under `scripts/` for the [adapter section](#orchestration-adapters-this-repository). **No live Herdr state was read or changed**: response shapes, error codes beyond those named in help or the skill, and the semantics of `--seq`/`--ttl-ms` are not in help and are marked **[INFERENCE]** or attributed to their source. When the binary disagrees with this file, trust the binary (see [Troubleshooting](#troubleshooting)).

**Contents**

1. [Ground rules](#ground-rules) (incl. [destructive and human-only commands](#destructive-and-human-only-commands))
2. [Mental model](#mental-model) (incl. [caller context and session selection](#caller-context-environment))
3. [CLI conventions](#cli-conventions)
4. [Root command and options](#root-command-and-options)
5. [Command index](#command-index)
6. [Status, update, completion](#status-update-completion)
7. [Server, channel, config, api](#server-channel-config-api)
8. [Workspace, worktree, tab](#workspace-worktree-tab)
9. [Notification](#notification)
10. [Agent commands](#agent-commands)
11. [Pane commands](#pane-commands)
12. [Metadata and report commands](#metadata-and-report-commands)
13. [Session and remote](#session-and-remote)
14. [Integration](#integration)
15. [Plugin (unlisted group)](#plugin-unlisted-group)
16. [Terminal (unlisted group)](#terminal-unlisted-group)
17. [Read and wait semantics](#read-and-wait-semantics) (incl. [state is not success](#state-is-not-success))
18. [Default configuration and keyboard shortcuts](#default-configuration-and-keyboard-shortcuts)
19. [Recipes](#recipes)
20. [Troubleshooting](#troubleshooting)
21. [Orchestration adapters (this repository)](#orchestration-adapters-this-repository)

---

## Ground rules

- **Humans** may launch and attach normally from any terminal: `herdr`, `herdr --session NAME`, `herdr session attach NAME`, `herdr --remote TARGET`.
- **Automation controlling a live session** (an agent running a control command) MUST run inside a Herdr-managed pane. Check first and stop when it fails:

  ```bash
  test "${HERDR_ENV:-}" = 1
  ```

  Do not inspect or control the focused session from outside Herdr.
- Never run bare `herdr` or bare `herdr server` to "discover" — the first launches or attaches the TUI, the second runs the headless server. Use `--help`.
- Commands that look like queries can execute: `herdr workspace create` with no flags creates a workspace. Never probe a mutating command by omitting arguments; use `--help`.
- Always give an explicit target: `--current`, a pane ID, a unique agent name. Omitting a pane target may resolve to the UI-focused pane, which can belong to the user or another client.
- Use `--no-focus` for background work. Pass an explicit `--cwd`.
- Parse IDs out of command responses. Never predict them from examples or sidebar order.
- Close, remove, stop, delete only what you created in this task. Never run `herdr server stop` from an active session unless the user explicitly intends to stop the server and every pane process it hosts.
- Never kill the Herdr process. Use a named test session (`--session NAME`) for experiments needing an isolated server.
- `idle` / `done` / `blocked` / `unknown` are *observations*. Neither idle nor done proves a task succeeded; `unknown` proves nothing.
- Metadata and `report-*` commands change what Herdr displays and who has lifecycle authority; see [Metadata and report commands](#metadata-and-report-commands) before using them.

### Destructive and human-only commands

Run these only when the user explicitly asked for that exact action. "Only things you created" applies to every close/remove/stop.

| Command | Why |
|---|---|
| `herdr`, `herdr --session NAME`, `herdr session attach NAME`, `herdr --remote TARGET`, `herdr agent attach`, `herdr terminal attach`, `herdr terminal session control` | Attach your terminal to the TUI or to a terminal stream; interactive, human use. `--takeover` (on the attach/control commands) has no help text. |
| `herdr server` (bare) | Runs the headless server. |
| `herdr server stop`, `herdr session stop NAME` | **Stops a server and every pane process it hosts.** |
| `herdr session delete NAME` | Deletes a stopped session (help: "Delete a stopped session"); what is deleted is not described. |
| `herdr workspace close`, `herdr tab close`, `herdr pane close`, `herdr plugin pane close` | Closes terminals and the processes in them. |
| `herdr worktree remove [--force]` | Removes a worktree checkout; `--force` has no help text. |
| `herdr update`, `herdr channel set` | Installs a new binary / changes the update channel. |
| `herdr config reset-keys` | Backs up `config.toml` and removes custom keybindings. |
| `herdr integration install`, `herdr integration uninstall` | Change how an agent integrates with Herdr; which files change is not stated in help. |
| `herdr plugin install`, `uninstall`, `link`, `unlink`, `enable`, `disable`, `action invoke` | Install, activate, or run third-party plugin code. |
| `herdr terminal title set`, `herdr terminal title clear` | Change the outer terminal's title. |
| `herdr pane report-agent`, `report-agent-session`, `release-agent`, `report-metadata`, `herdr workspace report-metadata` | Change what Herdr displays and who it believes owns an agent's lifecycle; see [Metadata and report commands](#metadata-and-report-commands). |

## Mental model

```text
session (server process; default or named)
└── workspace   w1          (a "space"; may be backed by a Git worktree)
    └── tab     w1:t1
        └── pane  w1:p1     (a terminal; may host a recognized agent)
```

| Surface | Use for |
|---|---|
| workspace / tab / pane topology commands | creating and arranging terminal locations |
| `herdr pane ...` | raw terminals: shells, servers, tests, literal input, output |
| `herdr agent ...` | the recognized coding agent currently occupying a pane; lifecycle states |

- A pane exists whether or not an agent is in it. `herdr agent start` needs an existing pane at an interactive shell prompt; it never creates, splits, or moves layout.
- Public IDs are opaque stable handles: workspace `w1`, tab `w1:t1`, pane `w1:p1`. Closed tab/pane IDs are not reused. A pane moved to another workspace gets a new workspace-qualified ID (see [`herdr pane move`](#pane-layout-and-targeting)).
- Agent commands accept a **unique live agent name** or the **pane ID currently hosting that agent**. They reject terminal IDs and bare agent-kind labels. Names match `[a-z][a-z0-9_-]{0,31}`, are unique among live agents, follow the current pane occupant, and are cleared when the agent exits, is released, or is replaced.
- Lifecycle states: `idle` (ready, tab seen in focused UI), `done` (same underlying idle after unseen background work finished), `working`, `blocked` (Herdr recognized an approval/question UI), `unknown` (agent present, not confidently classified). Focusing marks a tab seen; CLI reads do not.

### Caller context (environment)

Herdr injects these into each managed pane:

| Variable | Meaning |
|---|---|
| `HERDR_ENV` | `1` inside a Herdr-managed pane |
| `HERDR_WORKSPACE_ID`, `HERDR_TAB_ID`, `HERDR_PANE_ID` | the caller's own IDs |
| `HERDR_CONFIG_PATH` | overrides the config file path (root help) |
| `HERDR_SOCKET_PATH`, `HERDR_SESSION` | which server socket/session a CLI call targets; a process that inherited neither (for example an Eval kernel) may be looking at the wrong socket |

```bash
printf '%s\n' "$HERDR_WORKSPACE_ID" "$HERDR_TAB_ID" "$HERDR_PANE_ID"
```

**Session selection.** The default session lives in `~/.config/herdr/`; a named session's files live under `~/.config/herdr/sessions/<name>/` (its socket appeared there as `HERDR_SOCKET_PATH` in a managed pane). The printed `Logs:` line of `herdr --help` follows `HERDR_SESSION`: unset → `~/.config/herdr/herdr.log`; `HERDR_SESSION=example` → `~/.config/herdr/sessions/example/herdr.log`. Help does not state the precedence between `HERDR_SOCKET_PATH`, `HERDR_SESSION`, and `--session`, nor whether `--session` is accepted before a control subcommand (root usage shows it only for launching). To address a particular session from automation, run from a pane inside it, or set the inherited variables consistently; do not mix them.

## CLI conventions

| Topic | Rule |
|---|---|
| Output | Most control commands print JSON to stdout. Read IDs/state from it. |
| Server errors | JSON on stderr, exit status `1`. |
| Syntax errors | Exit status `2`. |
| Timeouts | Herdr CLI `--timeout` is **milliseconds**. The Python adapters use **seconds** (see [adapter units](#units-and-defaults)). |
| Missing `--timeout` | `agent wait`, `agent prompt --wait`, `pane wait-output` wait **indefinitely**. `agent start` defaults to 30000 ms (max 300000). |
| Escape key | `esc` is canonical; `escape` is accepted (`send-keys`). |
| Repeatable flags | `--until` (agent wait/prompt), `--env KEY=VALUE`. |
| Floats / counts | `--ratio`, `--amount`, `--lines`, `--seq`, `--ttl-ms` have no unit or default in help. |
| Help footer | Each `--help` ends with a pointer to `herdr.dev` resources; they are links, not part of the command syntax. |
| Unlisted groups | `herdr terminal` and `herdr plugin` exist in 0.8.2: their `--help` works and `herdr completion bash` names them, but root help does not list them (the bundled skill lists `herdr terminal`). See [Plugin](#plugin-unlisted-group) and [Terminal](#terminal-unlisted-group). |
| Exit `0` | Every `--help` page and the printers (`--version`, `--default-config`, `--skill`, `completion`) exited `0` when collected. |
| `--json` | Listed on `status`/`status server`/`status client`, `server agent-manifests`, `server update-agent-manifests`, `api schema`, `agent explain`, `session list`/`stop`/`delete`, `plugin list`. Other control commands print JSON without a flag (bundled skill). |
| Units | Herdr CLI `--timeout` and `--ttl-ms` carry milliseconds in their name/help; config `[ui.toast] delay_seconds` is seconds. `--cols`, `--rows`, `--lines`, `--seq`, `--amount`, `--ratio`, `--limit` have no unit in help. |

Creation responses (from the bundled skill): `workspace create` → `.result.workspace`, `.result.tab`, `.result.root_pane`; `tab create` → `.result.tab`, `.result.root_pane`; `pane split` → `.result.pane`; `pane move` → `.result.move_result.pane.pane_id` (new) and `.result.move_result.previous_pane_id` (old; only the moved process's inherited context still resolves it). Pane IDs are read as `.result.pane.pane_id` and `.result.root_pane.pane_id`.

## Root command and options

```text
herdr [options]
herdr --session <name> [options]
herdr --remote <ssh-target> [--session <name>]
```

| Invocation / option | Effect |
|---|---|
| `herdr` | Launch or attach to the persistent session (TUI). Human use; not for automation discovery. |
| `herdr --no-session` | Run monolithically — no server/client. Escape hatch. |
| `herdr --session NAME` | Use or create a named persistent session. |
| `herdr --remote TARGET` | Attach through SSH to a remote Herdr server. |
| `herdr --remote TARGET --session NAME` | Attach to a named session on the remote. |
| `--remote-keybindings local\|server` | Keybindings for `--remote` attach. Default `local`. |
| `--handoff` | Opt into live handoff for `update` or remote attach. |
| `herdr --default-config` | Print default configuration and exit (safe). |
| `herdr --skill` | Print the bundled agent skill and exit (safe). |
| `herdr --version`, `herdr -V` | Print version (`herdr 0.8.2`) and exit. |
| `herdr --help`, `herdr -h` | Show help. |

Files: config `~/.config/herdr/config.toml` (override with `HERDR_CONFIG_PATH`). Logs: root help prints `herdr.log` plus `herdr-client.log` and `herdr-server.log`; the directory **depends on the session** — default session `~/.config/herdr/`, session `NAME` `~/.config/herdr/sessions/NAME/` (see [Session selection](#caller-context-environment)). Per-session sockets live under `~/.config/herdr/sessions/*/herdr.sock` (this repository's adapter notes).

The config key `[experimental] allow_nested = false` ("allow launching herdr from inside a herdr-managed pane") is the setting that governs nested launch; the exact refusal behavior is not shown in help ([config](#default-configuration-and-keyboard-shortcuts)).

## Command index

17 command groups — the 15 listed by root help plus the unlisted `terminal` and `plugin` — and 95 leaf commands: 76 behind the listed groups (74 subcommands + `herdr update` + `herdr completion`) and 19 behind the unlisted ones (5 `terminal`, 14 `plugin`). `herdr status` and `herdr server` are groups that are also runnable bare. Effect legend: **R** read-only inspection, **W** changes live Herdr state, **D** disruptive/destructive (closes, removes, stops, deletes, installs), **F** writes files/config/network.

| Group | Commands (full paths) |
|---|---|
| status | `herdr status`, `herdr status server`, `herdr status client` |
| update | `herdr update` |
| completion | `herdr completion` |
| server | `herdr server`, `herdr server stop`, `herdr server reload-config`, `herdr server agent-manifests`, `herdr server update-agent-manifests`, `herdr server reload-agent-manifests` |
| channel | `herdr channel show`, `herdr channel set` |
| config | `herdr config check`, `herdr config reset-keys` |
| api | `herdr api snapshot`, `herdr api schema` |
| workspace | `herdr workspace list`, `create`, `get`, `focus`, `rename`, `report-metadata`, `close` |
| worktree | `herdr worktree list`, `create`, `open`, `remove` |
| tab | `herdr tab list`, `create`, `get`, `focus`, `rename`, `close` |
| notification | `herdr notification show` |
| agent | `herdr agent list`, `get`, `read`, `send-keys`, `prompt`, `rename`, `focus`, `wait`, `attach`, `start`, `explain` |
| pane | 25 commands — see [Pane commands](#pane-commands) |
| session | `herdr session list`, `attach`, `stop`, `delete` |
| integration | `herdr integration install`, `uninstall`, `status` |
| terminal (unlisted) | `herdr terminal attach`, `session control`, `session observe`, `title set`, `title clear` — see [Terminal](#terminal-unlisted-group) |
| plugin (unlisted) | 14 commands — see [Plugin](#plugin-unlisted-group) |

Every path with its effect:

| Path | Effect |
|---|---|
| `herdr status` | R |
| `herdr status server` | R |
| `herdr status client` | R |
| `herdr update` | D F (downloads and installs) |
| `herdr completion SHELL` | R (prints script) |
| `herdr server` | D headless server launch — never from an agent |
| `herdr server stop` | D stops server and its pane processes |
| `herdr server reload-config` | W reloads `config.toml` |
| `herdr server agent-manifests` | R |
| `herdr server update-agent-manifests` | F W (fetches and reloads manifests) |
| `herdr server reload-agent-manifests` | W (local overrides) |
| `herdr channel show` | R |
| `herdr channel set CHANNEL` | F (update channel) |
| `herdr config check` | R |
| `herdr config reset-keys` | D F (backs up `config.toml`, removes custom keybindings) |
| `herdr api snapshot` | R |
| `herdr api schema` | R; F with `--output` |
| `herdr workspace list` / `get` | R |
| `herdr workspace create` / `focus` / `rename` / `report-metadata` | W |
| `herdr workspace close` | D |
| `herdr worktree list` | R |
| `herdr worktree create` / `open` | W F (creates/opens a Git worktree workspace) |
| `herdr worktree remove` | D (removes a checkout) |
| `herdr tab list` / `get` | R |
| `herdr tab create` / `focus` / `rename` | W |
| `herdr tab close` | D |
| `herdr notification show` | W (user-visible popup) |
| `herdr agent list` / `get` / `read` / `explain` | R (do not mark seen) |
| `herdr agent send-keys` / `prompt` / `rename` / `focus` / `start` / `wait` | W (`wait` is passive but blocks) |
| `herdr agent attach` | W interactive; human terminal only |
| `herdr pane list` / `current` / `get` / `layout` / `process-info` / `neighbor` / `edges` / `read` / `wait-output` | R |
| `herdr pane focus` / `resize` / `zoom` / `rename` / `input` / `split` / `swap` / `move` / `send-text` / `send-keys` / `run` / `report-*` / `release-agent` | W |
| `herdr pane close` | D |
| `herdr session list` | R |
| `herdr session attach` | W (attaches your terminal) |
| `herdr session stop` | D |
| `herdr session delete` | D (stopped sessions only) |
| `herdr integration status` | R |
| `herdr integration install` / `uninstall` | F (changes how an agent integrates; files touched not stated in help) |
| `herdr terminal attach` / `session control` | W interactive; human terminal only |
| `herdr terminal session observe` | interactive stream view [INFERENCE: read-only; help says only "Observe a terminal stream"] |
| `herdr terminal title set` / `clear` | W (outer terminal title) |
| `herdr plugin list` / `action list` / `log list` / `config-dir` | R |
| `herdr plugin install` / `uninstall` / `link` / `unlink` / `enable` / `disable` | D F (changes installed or active plugin code) |
| `herdr plugin action invoke` | W (runs plugin code) |
| `herdr plugin pane open` / `focus` | W |
| `herdr plugin pane close` | D |

## Status, update, completion

| Command | Syntax and notes |
|---|---|
| `herdr status` | `herdr status [--json] [server\|client]` — local client and running server status. |
| `herdr status server` | `herdr status server [--json]` — running server status. |
| `herdr status client` | `herdr status client [--json]` — local client status. |
| `herdr update` | `herdr update [--handoff]` — **downloads and installs** the latest version on the configured [channel](#server-channel-config-api). `--handoff` tries live handoff after install. Human-initiated only; never from a pane you do not own. |
| `herdr completion SHELL` | `herdr completion <SHELL>`; SHELL ∈ `bash`, `elvish`, `fish`, `powershell`, `zsh`. Prints the completion script to stdout. |

`--json` has no description in help beyond being a flag.

## Server, channel, config, api

### Server

| Command | Syntax and notes |
|---|---|
| `herdr server` | Run as the headless server (advanced). Never run from a session you want to keep. |
| `herdr server stop` | **Destructive**: stops the running server through the API socket, ending every pane process it hosts. Only for a deliberately targeted server (for example a throwaway `--session`). |
| `herdr server reload-config` | Reload `config.toml` in the running server. |
| `herdr server agent-manifests` | `[--json]` — show active agent detection manifests. |
| `herdr server update-agent-manifests` | `[--json]` — fetches manifests from the network and reloads them. Governed by `[update] manifest_check`. |
| `herdr server reload-agent-manifests` | Reload local agent-detection manifest overrides. |

### Channel

| Command | Syntax |
|---|---|
| `herdr channel show` | Print the configured update channel. |
| `herdr channel set CHANNEL` | CHANNEL ∈ `stable`, `preview`. Persists the choice used by background checks and `herdr update`. |

### Config

| Command | Syntax |
|---|---|
| `herdr config check` | Validate `config.toml` and print diagnostics. Read-only. |
| `herdr config reset-keys` | Back up `config.toml` and remove custom keybindings. Destructive to local customizations. |

### API

| Command | Syntax |
|---|---|
| `herdr api snapshot` | Print the live session snapshot. |
| `herdr api schema` | `herdr api schema [--json] [--output PATH]` — print or write the bundled API schema. `--output` writes a file. |

## Workspace, worktree, tab

### Workspace

| Command | Syntax and notes |
|---|---|
| `herdr workspace list` | List workspaces. |
| `herdr workspace create` | `[--cwd PATH] [--label TEXT] [--env KEY=VALUE]... [--focus \| --no-focus]` — **executes with no flags.** Response: `.result.workspace`, `.result.tab`, `.result.root_pane`. |
| `herdr workspace get` | `<workspace_id>` |
| `herdr workspace focus` | `<workspace_id>` — focusing marks the tab seen. |
| `herdr workspace rename` | `<WORKSPACE_ID> <LABEL>...` |
| `herdr workspace report-metadata` | see [metadata](#metadata-and-report-commands) |
| `herdr workspace close` | `<workspace_id>` — destructive; only workspaces you created. |

### Worktree

Git worktree-backed workspaces.

| Command | Syntax and notes |
|---|---|
| `herdr worktree list` | `[--workspace ID] [--cwd PATH]` |
| `herdr worktree create` | `[--workspace ID] [--cwd PATH] [--branch NAME] [--base REF] [--path PATH] [--label TEXT] [--focus \| --no-focus]` — creates and opens a Git worktree. Help does not say what an omitted `--branch`, `--base`, or `--path` resolves to; pass them explicitly. |
| `herdr worktree open` | `[--workspace ID] [--cwd PATH] [--path PATH] [--branch NAME] [--label TEXT] [--focus \| --no-focus]` — open an existing worktree. |
| `herdr worktree remove` | `[--workspace ID] [--force]` — **removes a checkout**; help names no path or branch flag, so the target is chosen by `--workspace` (or the default target if omitted — not stated). Always pass `--workspace` for a worktree you created and have no unmerged work in. `--force` has no help text. |

Worktree directory is configurable via `[worktrees] directory` (default shown commented as `~/.herdr/worktrees`).

### Tab

| Command | Syntax and notes |
|---|---|
| `herdr tab list` | `[--workspace WORKSPACE_ID]` |
| `herdr tab create` | `[--workspace WORKSPACE_ID] [--cwd PATH] [--label TEXT] [--env KEY=VALUE]... [--focus \| --no-focus]` — response `.result.tab`, `.result.root_pane`. |
| `herdr tab get` | `<tab_id>` |
| `herdr tab focus` | `<tab_id>` |
| `herdr tab rename` | `<TAB_ID> <LABEL>...` |
| `herdr tab close` | `<tab_id>` — destructive. |

## Notification

| Command | Syntax and notes |
|---|---|
| `herdr notification show` | `herdr notification show <TITLE> [--body TEXT] [--position top-left\|top-right\|bottom-left\|bottom-right] [--sound none\|done\|request]` — user-visible; use sparingly. |

## Agent commands

Target (`TARGET`) is a unique live agent name or the pane ID hosting that agent.

| Command | Syntax and notes |
|---|---|
| `herdr agent list` | List agents. |
| `herdr agent get` | `<target>` — one agent's state. |
| `herdr agent read` | `<TARGET> [--source visible\|recent\|recent-unwrapped\|detection] [--lines N] [--format text\|ansi] [--ansi]` — default source `recent`. |
| `herdr agent send-keys` | `<TARGET> <KEY>...` — logical keys (`esc`, `ctrl+c`, `Enter`). All keys validated before any bytes are written. |
| `herdr agent prompt` | `<TARGET> <TEXT> [--wait] [--until STATUS]... [--timeout MS]` — see below. |
| `herdr agent rename` | `<TARGET> <NAME>` or `<TARGET> --clear` |
| `herdr agent focus` | `<target>` — marks the tab seen. |
| `herdr agent wait` | `<TARGET> [--until STATUS]... [--timeout MS]` |
| `herdr agent attach` | `<TARGET> [--takeover]` — attach directly to the agent terminal. Interactive; human terminal only. `--takeover` has no help text; do not guess. Raw terminal streams are addressed by [`herdr terminal`](#terminal-unlisted-group), which takes a terminal ID rather than an agent target. |
| `herdr agent start` | `<NAME> --kind KIND --pane ID [--timeout MS] [-- AGENT_ARG...]` |
| `herdr agent explain` | `[TARGET] [--file PATH] [--agent LABEL] [--json] [--format text\|json] [-v\|--verbose]` — explain agent detection state. `TARGET` is optional; help does not say what `--file` or `--agent` do. `--json` and `--format json` are both listed. |

`STATUS` ∈ `idle`, `working`, `blocked`, `done`, `unknown`.

**`agent start`.** The pane must be at its interactive shell prompt (shell in the foreground; no editor, command, or agent). Success means the expected agent was detected in that terminal and is ready for input. `--timeout` defaults to 30000 ms, max 300000. Everything after `--` goes to the agent itself. A name is bound to the pane occupant. `agent start` never creates layout.

`--kind` values: `pi`, `claude`, `codex`, `gemini`, `cursor`, `devin`, `agy`, `cline`, `omp`, `mastracode`, `opencode`, `copilot`, `kimi`, `kiro`, `droid`, `amp`, `grok`, `hermes`, `kilo`, `qodercli`, `qwen`, `maki`.

**`agent prompt`.**
- Submits the text plus an encoded Enter after a short delay, honoring the pane's live bracketed-paste mode (bundled skill).
- Already `blocked` → rejected with `agent_blocked` **before any input is sent**.
- Without `--wait` the call is submission only; help does not describe what its exit proves about delivery.
- `--wait` returns on "the first matching state observed after submission". Default match set: `idle`, `done`, `blocked`. `--until STATUS` (repeatable; `idle`, `working`, `blocked`, `done`, `unknown`) names the exact states instead; do not repeat the defaults with `--until`.
- When an accepted submission starts from a non-working state, `--wait` first requires an observed state change within **5000 ms**, else `agent_prompt_stalled` (text may be unsubmitted). A shorter `--timeout` returns `timeout` instead.
- It tracks lifecycle state, **not turns**: if the agent is already working, completion of that active turn may match.
- Without `--timeout` the settled-state wait is indefinite; `--timeout` is milliseconds.
- A successful exit means a matching **state was observed**, not that the task succeeded; see [State is not success](#state-is-not-success).

**`agent wait`.** Passive. Without `--until` it matches `idle`, `done`, `blocked`; `unknown` must be requested explicitly (`--until unknown`). Without `--timeout` it waits indefinitely. Help does not say whether a state that already holds matches at once.

**Blocked agents.** If a wait ends `blocked`, or `agent start` returns `agent_not_ready` (the name stays live), inspect with `agent get` and `agent read` before sending any key. Ask the user before answering approval dialogs.

## Pane commands

`PANE_ID` is a positional; most targeting commands also accept `--pane ID` or `--current`.

### Pane inspection

| Command | Syntax |
|---|---|
| `herdr pane list` | `[--workspace WORKSPACE_ID]` |
| `herdr pane current` | `[--pane ID] [--current]` |
| `herdr pane get` | `<pane_id>` |
| `herdr pane layout` | `[--pane ID] [--current]` |
| `herdr pane process-info` | `[--pane ID] [--current]` |
| `herdr pane neighbor` | `--direction left\|right\|up\|down [--pane ID] [--current]` |
| `herdr pane edges` | `[--pane ID] [--current]` |

### Pane layout and targeting

| Command | Syntax and notes |
|---|---|
| `herdr pane focus` | `--direction left\|right\|up\|down [--pane ID] [--current]` — focus a neighboring pane. |
| `herdr pane resize` | `--direction left\|right\|up\|down [--amount FLOAT] [--pane ID] [--current]` |
| `herdr pane zoom` | `[PANE_ID] [--pane ID] [--current] [--toggle \| --on \| --off]` — default when none given is not stated in help. |
| `herdr pane rename` | `<PANE_ID> [LABEL]... [--clear]` |
| `herdr pane input` | `--right-click herdr\|pane [PANE_ID] [--pane ID] [--current]` — "set pane input routing". Help lists the two targets only; meaning (who receives right-click) is [INFERENCE] from `right_click_passthrough_modifier`. |
| `herdr pane split` | `[PANE_ID] [--pane ID] [--current] [--direction right\|down] [--ratio FLOAT] [--cwd PATH] [--env KEY=VALUE]... [--right-click herdr\|pane] [--focus \| --no-focus]` — does **not** run a command. Response `.result.pane.pane_id`. |
| `herdr pane swap` | `[--direction left\|right\|up\|down] [--pane ID] [--current] [--source-pane ID] [--target-pane ID]` |
| `herdr pane move` | `<PANE_ID> [--tab TAB_ID] [--split right\|down] [--target-pane ID] [--ratio FLOAT] [--new-tab] [--workspace ID] [--new-workspace] [--label TEXT] [--tab-label TEXT] [--focus \| --no-focus]` — continue with `.result.move_result.pane.pane_id` afterward. When it reports `changed=false` with `reason=zoomed_tab`, unzoom, move, then optionally re-zoom (orchestrator notes). |
| `herdr pane close` | `<pane_id>` — destructive; only panes you created. |

### Pane input and output

| Command | Syntax and notes |
|---|---|
| `herdr pane read` | `<PANE_ID> [--source visible\|recent\|recent-unwrapped\|detection] [--lines N] [--format text\|ansi] [--ansi] [--raw]` — default source `recent`. |
| `herdr pane send-text` | `<PANE_ID> <TEXT>` — literal text, **no Enter**. |
| `herdr pane send-keys` | `<PANE_ID> <KEY>...` — `esc` canonical, `escape` accepted. Help gives no key vocabulary; skill examples are `esc`, `ctrl+c`, `Enter`. All keys are validated before any bytes are written (skill). |
| `herdr pane run` | `<PANE_ID> <COMMAND>...` — text **plus Enter** in one call. Quote the whole command as one argument. |
| `herdr pane wait-output` | `<--match TEXT \| --regex PATTERN> [--source visible\|recent\|recent-unwrapped] [--lines N] [--timeout MS] [--raw] <PANE_ID>` |

Agent/metadata reporters (`herdr pane report-agent`, `herdr pane report-agent-session`, `herdr pane release-agent`, `herdr pane report-metadata`) are in [Metadata and report commands](#metadata-and-report-commands).

Pane count: list, current, get, layout, process-info, neighbor, edges, focus, resize, zoom, read, rename, input, split, swap, move, close, send-text, send-keys, wait-output, run, report-agent, report-agent-session, release-agent, report-metadata = 25.

## Metadata and report commands

**Advanced.** These do not run work; they change what Herdr shows or who it believes owns an agent's lifecycle. Use them only for a pane/workspace you own, with a stable `--source ID` that names your reporter. Do not use them to fake `idle`/`done`.

| Command | Required | Optional |
|---|---|---|
| `herdr workspace report-metadata` | `--source ID`, `<WORKSPACE_ID>` | `--token NAME=VALUE`, `--clear-token NAME`, `--seq N`, `--ttl-ms N` |
| `herdr pane report-metadata` | `--source ID`, `<PANE_ID>` | `--agent LABEL`, `--applies-to-source ID`, `--title TEXT`, `--clear-title`, `--display-agent TEXT`, `--clear-display-agent`, `--state-label STATUS=TEXT`, `--clear-state-labels`, `--token NAME=VALUE`, `--clear-token NAME`, `--seq N`, `--ttl-ms N` |
| `herdr pane report-agent` | `--source ID`, `--agent LABEL`, `--state idle\|working\|blocked\|unknown`, `<PANE_ID>` | `--message TEXT`, `--seq N`, `--agent-session-id ID`, `--agent-session-path PATH` |
| `herdr pane report-agent-session` | `--source ID`, `--agent LABEL`, `<PANE_ID>` | `--seq N`, `--agent-session-id ID`, `--agent-session-path PATH`, `--session-start-source SOURCE` |
| `herdr pane release-agent` | `--source ID`, `--agent LABEL`, `<PANE_ID>` | `--seq N` |

- Metadata is described as **display-only**; `report-agent` reports pane agent lifecycle state; `release-agent` "releases pane agent lifecycle authority" — after it, the agent name is cleared.
- `--seq` and `--ttl-ms`: help gives no semantics or units. `--seq N` presumably orders updates from one `--source`; `--ttl-ms` suggests a time-to-live in milliseconds [INFERENCE]. Exercise them on a throwaway pane before relying on either.
- `report-agent` accepts only `idle|working|blocked|unknown` (no `done`). `done` is derived by Herdr.
- Tokens are the machine-readable channel this repository uses for reattachment; token values are capped at **80 characters** in 0.8.2 (spawn-agent help).
- Sidebar rows can show custom `$name` tokens reported through metadata (see config `[ui.sidebar.agents]` and `[ui.sidebar.spaces]`).

## Session and remote

| Command | Syntax and notes |
|---|---|
| `herdr session list` | `[--json]` |
| `herdr session attach` | `<NAME>` — attach your terminal to a named session (same as `herdr --session NAME`). |
| `herdr session stop` | `<NAME> [--json]` — **destructive**: stops that session's server. |
| `herdr session delete` | `<NAME> [--json]` — deletes a **stopped** session. |
| `herdr --remote TARGET` | Attach over SSH to a remote server. See root options for `--remote-keybindings` and `--handoff`. |

- `[remote] manage_ssh_config = true` (default shown): Herdr runs remote ssh through a generated config that includes `~/.ssh/config` first and adds keepalive fallbacks plus a private control socket. `false` runs plain ssh against your config.
- `remote_image_paste` binding (`ctrl+v`) is only active in `--remote`.
- Do not stop, delete, or attach to a session you did not create from automation.

## Integration

| Command | Syntax and notes |
|---|---|
| `herdr integration install` | `<TARGET>` — installs the built-in integration for that agent. Help does not say which files change. |
| `herdr integration uninstall` | `<TARGET>` |
| `herdr integration status` | `[--outdated-only]` |

`TARGET` ∈ `pi`, `omp`, `claude`, `codex`, `copilot`, `devin`, `droid`, `kimi`, `opencode`, `kilo`, `hermes`, `qodercli`, `qwen`, `cursor`, `mastracode`, `antigravity-cli`, `grok`.

The integration target list differs from the `agent start --kind` list: it adds `antigravity-cli` and lacks `gemini`, `agy`, `cline`, `kiro`, `amp`, `maki`. Official integrations that report session refs enable `[session] resume_agents_on_restore`.

## Plugin (unlisted group)

`herdr plugin` ("Install and run workflow plugins") is absent from root help but answers `--help` and appears in `herdr completion bash`. Plugins are third-party code: treat install/enable/link/invoke as [human-only](#destructive-and-human-only-commands). Nothing here was executed.

| Command | Syntax and notes |
|---|---|
| `herdr plugin install` | `<OWNER/REPO[/SUBDIR]> [--ref REF] [-y \| --yes]` — "Install a plugin from GitHub". `--ref` and `--yes` have no help text; `--yes` presumably skips a confirmation [INFERENCE]. |
| `herdr plugin uninstall` | `<PLUGIN>` |
| `herdr plugin link` | `<PATH> [--disabled \| --enabled]` — link a local plugin. |
| `herdr plugin unlink` | `<PLUGIN_ID>` |
| `herdr plugin enable` / `disable` | `<PLUGIN_ID>` |
| `herdr plugin list` | `[--plugin ID] [--json]` |
| `herdr plugin config-dir` | `<PLUGIN_ID>` — print a plugin config directory. |
| `herdr plugin action list` | `[--plugin ID]` |
| `herdr plugin action invoke` | `<ACTION_ID> [--plugin ID]` — runs the action. |
| `herdr plugin log list` | `[--plugin ID] [--limit N]` — plugin command logs. The `log` group also answers to the alias `logs` (per `herdr plugin --help`). |
| `herdr plugin pane open` | `[--plugin ID] [--entrypoint ID] [--placement overlay\|split\|tab\|zoomed] [--workspace ID] [--target-pane PANE] [--direction right\|down] [--cwd PATH] [--env KEY=VALUE]... [--focus \| --no-focus]` |
| `herdr plugin pane focus` / `close` | `<PANE_ID>` (a plugin-owned pane) |

## Terminal (unlisted group)

`herdr terminal` ("Attach to or observe raw terminal streams") is absent from root help, but the bundled skill lists it and `--help` works. These commands address a **terminal** (stream) rather than an agent target; the help does not describe the terminal ID format or where to obtain it. Interactive; human-only unless the user asked.

| Command | Syntax and notes |
|---|---|
| `herdr terminal attach` | `<TERMINAL_ID> [--takeover]` — attach directly to a terminal stream. |
| `herdr terminal session control` | `<TARGET> [--takeover] [--cols N] [--rows N]` — control a terminal stream. |
| `herdr terminal session observe` | `<TARGET> [--cols N] [--rows N]` — observe a terminal stream. |
| `herdr terminal title set` | `<TITLE>` — set the outer terminal title. |
| `herdr terminal title clear` | clear the outer terminal title. |

`--takeover`, `--cols`, `--rows` have no help text; `--cols`/`--rows` presumably size the terminal in cells [INFERENCE]. Config `[ui] window_title` governs the title Herdr writes by default ([config](#default-configuration-and-keyboard-shortcuts)).

## Read and wait semantics

| Source | Meaning |
|---|---|
| `visible` | the currently rendered viewport |
| `recent` | recent rendered output including soft wraps (default) |
| `recent-unwrapped` | recent output with soft wraps joined; best for logs and transcripts |
| `detection` | plain-text bottom-buffer snapshot used for agent detection (`read` only; not allowed by `wait-output`) |

- `--format text` (default) vs `--format ansi` (or `--ansi`) when colors matter. `pane read --raw` and `pane wait-output --raw` keep ANSI escape sequences (wait-output: while matching).
- `pane wait-output` needs exactly one of `--match` (literal substring) or `--regex` (Rust regular expression). It searches the selected snapshot immediately — existing output can match — then polls. `--lines N` restricts the searched snapshot; `--timeout MS` fails after that long; no timeout waits forever.
- `--lines` asks for more rows from the pane's screen plus host scrollback. A full-screen agent on the **alternate screen** loses departed rows: they never enter host scrollback, so larger `--lines` cannot recover them. Enlarge the pane, ask for concise output, use the agent's transcript controls, or scroll inside the agent and read `--source visible`.
- Fallback for a lost long response (bundled skill): ask the agent to write the full response as Markdown to a temporary directory and reply with only the path, then read the file. Not for the first prompt.
- `agent wait` / `agent prompt --wait`: see [Agent commands](#agent-commands).
- Reads never mark a tab seen; `focus` does.

### State is not success

| Observation | What it proves | What it does not prove |
|---|---|---|
| `agent prompt --wait` exits OK / `agent wait` matches `idle` | Herdr saw the agent in a ready state (tab seen in the focused UI) | The task finished, was correct, or was even received if the agent never left `idle` |
| matches `done` | The same ready state after unseen background work finished | Success, quality, or that tests pass |
| matches `blocked` | Herdr recognized an approval/question UI | Anything about the work; inspect before answering |
| matches `working` (`--until working`) | The agent left its settled state | That the work will finish or succeed |
| matches `unknown` | An agent is present but not confidently classified | Completion of any kind |
| `agent_prompt_stalled`, `timeout` | No observed change in time | That nothing happened; text may sit unsubmitted in the composer |

Confirm outcomes from evidence outside the lifecycle state: read the output (`agent read`/`pane read`), inspect Git state, run the check.

## Default configuration and keyboard shortcuts

From `herdr --default-config`. Every setting line in the printed file is **commented out** — except `pane_history = false` — so the values below are the built-in defaults, and your `config.toml` can override them. Most `[section]` headers are printed uncommented (`[theme]`, `[terminal]`, `[update]`, `[keys]`, `[server]`, `[ui]`, `[ui.toast]`, `[ui.toast.herdr]`, `[ui.toast.clipboard]`, `[ui.sound]`, `[session]`, `[remote]`, `[experimental]`, `[advanced]`); `[theme.custom]`, `[[keys.command]]`, `[keys.indexed]`, `[worktrees]`, `[ui.sidebar.agents]`, `[ui.sidebar.agents.rows_by_agent]`, `[ui.sidebar.spaces]`, and `[ui.sound.agents]` are printed commented. After editing: `herdr config check`, then `herdr server reload-config` (or the `reload_config` shortcut). `herdr config reset-keys` backs up and removes custom keybindings.

### Key syntax

- `prefix` defaults to `ctrl+b`. `prefix+KEY` requires prefix mode; `ctrl+alt+n` is a direct terminal-mode shortcut.
- Plain keys, `ctrl`/`shift`/`alt`/`cmd`/`super` modifiers, and `enter`/`tab`/`esc`/`left`/`right`/`up`/`down` are accepted. Named punctuation (`minus`, `comma`, `ampersand`, `plus`, `backtick`) is accepted.
- Most reliable direct bindings are `ctrl+letter`, function keys, and explicit modified chords. `alt+...`, `cmd`/`super`, and punctuation with modifiers may depend on the terminal/tmux.

### Default prefix-mode bindings (prefix = `ctrl+b`)

| Action | Default |
|---|---|
| `help` | `prefix+?` |
| `settings` | `prefix+s` |
| `detach` | `prefix+q` |
| `reload_config` | `prefix+shift+r` |
| `open_notification_target` | `prefix+o` |
| `workspace_picker` | `prefix+w` |
| `goto` | `prefix+g` |
| `new_workspace` | `prefix+shift+n` |
| `new_worktree` | `prefix+shift+g` |
| `rename_workspace` | `prefix+shift+w` |
| `close_workspace` | `prefix+shift+d` |
| `new_tab` | `prefix+c` |
| `rename_tab` | `prefix+shift+t` |
| `previous_tab` | `prefix+p` |
| `next_tab` | `prefix+n` |
| `switch_tab` | `prefix+1..9` |
| `close_tab` | `prefix+shift+x` |
| `rename_pane` | `prefix+shift+p` |
| `edit_scrollback` | `prefix+e` |
| `focus_pane_left` / `down` / `up` / `right` | `prefix+h` / `j` / `k` / `l` |
| `cycle_pane_next` | `prefix+tab` |
| `cycle_pane_previous` | `prefix+shift+tab` |
| `split_vertical` | `prefix+v` |
| `split_horizontal` | `prefix+minus` |
| `close_pane` | `prefix+x` |
| `zoom` (legacy alias `fullscreen`) | `prefix+z` |
| `resize_mode` | `prefix+r` |
| `toggle_sidebar` | `prefix+b` |
| `remote_image_paste` | `ctrl+v` (only in `herdr --remote`; empty disables) |

**Unset by default** (optional): `open_worktree`, `remove_worktree` (opens confirmation), `previous_workspace`, `next_workspace`, `previous_agent`, `next_agent`, `focus_agent` (indexed, for example `prefix+alt+1..9`), `move_tab_previous`, `move_tab_next`, `switch_workspace` (indexed, for example `prefix+shift+1..9`), `last_pane`, `resize_pane_left` / `down` / `up` / `right`.

### Navigate mode

Local shortcuts that win while navigate mode is open; independent from `focus_pane_*`. Do not include `prefix+`, `esc`, `enter`, `tab`, or `1..9`.

| Action | Default |
|---|---|
| `navigate_workspace_up` / `down` | `up` / `down` |
| `navigate_pane_left` / `down` / `up` / `right` | `h` / `j` / `k` / `l` (arrow left/right always focus left/right panes) |

### Custom commands

```toml
[[keys.command]]
key = "prefix+alt+g"
type = "popup"      # shell | pane | popup
command = "lazygit"
width = "80%"       # popup only; cells or percent
height = "80%"
```

`shell` runs detached in the background; `pane` opens a temporary pane that closes when the command exits; `popup` opens a session-modal terminal without changing the tab layout. The legacy `[keys.indexed]` (`tabs`, `workspaces`, `agents`) is still parsed; prefer `switch_tab`, `switch_workspace`, `focus_agent`.

### Other configuration (defaults as printed)

| Section | Settings (default) |
|---|---|
| top level | `onboarding = true` (missing also shows onboarding) |
| `[theme]` | `name = "catppuccin"` (built-ins: catppuccin, terminal, tokyo-night, dracula, nord, gruvbox, one-dark, solarized, kanagawa, rose-pine, vesper); `auto_switch = false`, `dark_name = "catppuccin"`, `light_name = "catppuccin-latte"`; `[theme.custom]` color tokens (`sidebar_bg`, `active_row_bg`, `selection_bg`, `panel_bg`, `accent`, `red`, `green`) |
| `[terminal]` | `default_shell = ""` (empty → `$SHELL`, then `/bin/sh`); `shell_mode = "auto"` (`auto`/`login`/`non_login`); `new_cwd = "follow"` (`follow`, `home`, `current`, or a fixed path) |
| `[update]` | `channel = "stable"`; `version_check = true`; `manifest_check = true` |
| `[server]` | `headless_cols = 120`, `headless_rows = 40` (virtual terminal when no client attached) |
| `[worktrees]` | `directory = "~/.herdr/worktrees"` |
| `[ui]` sidebar | `sidebar_width = 26`, `sidebar_min_width = 18`, `sidebar_max_width = 36`, `sidebar_start_collapsed = false`, `sidebar_collapsed_mode = "compact"` (`compact`/`hidden`), `mobile_width_threshold = 64` |
| `[ui]` input | `mouse_capture = true`, `copy_on_select = true`, `right_click_passthrough_modifier = ""`, `mouse_scroll_lines = 3`, `host_cursor = "auto"` (`auto`/`native`/`drawn`), `redraw_on_focus_gained = true` |
| `[ui]` prompts | `confirm_close = true`, `prompt_new_tab_name = true`, `prompt_new_workspace_name = false` |
| `[ui]` panes/tabs | `pane_borders = true`, `pane_outer_borders = true`, `pane_scrollbars = true`, `pane_gaps = true`, `show_agent_labels_on_pane_borders = false`, `hide_tab_bar_when_single_tab = false`, `tab_bar_position = "top"`, `tab_bar_right = []`, `tab_bar_right_separator = " "`, `window_title = "{hostname}: {workspace}"` |
| `[ui]` agents | `agent_panel_sort = "spaces"` (or `priority`), `status_indicators = "dots"` (or `symbols`), `accent = "cyan"` |
| `[ui.sidebar.agents]` | `row_gap = 0`, `rows = [["state_icon", "workspace", "tab"], ["agent"]]`, per-agent `rows_by_agent` |
| `[ui.sidebar.spaces]` | `row_gap = 0`, `rows = [["state_icon", "workspace"], ["branch", "git_status"]]` |
| `[ui.toast]` | `delivery = "off"` (`off`/`herdr`/`terminal`/`system`), `delay_seconds = 1`; `[ui.toast.herdr] position = "bottom-right"`; `[ui.toast.clipboard] enabled = true, position = "bottom-center"` |
| `[ui.sound]` | `enabled = true`; optional `path`, `done_path`, `request_path` (mp3); `[ui.sound.agents]` overrides `default`/`on`/`off` (droid is muted by default) |
| `[session]` | `resume_agents_on_restore = true` |
| `[remote]` | `manage_ssh_config = true` |
| `[experimental]` | `allow_nested = false`, `kitty_graphics = false`, `pane_history = false` (the one uncommented line), `switch_ascii_input_source_in_prefix = false`, `reveal_hidden_cursor_for_cjk_ime = false`, `cjk_ime_agents = []`, `cjk_ime_cursor_shape = "steady_block"` |
| `[advanced]` | `scrollback_limit_bytes = 10000000` |

Accepted values and notes printed alongside those defaults:

- **Colors** (`[theme.custom]`, `accent`): hex `#rrggbb`, named colors, or `rgb(r,g,b)`; `[theme.custom] panel_bg = "reset"` is also accepted. Sidebar inline token styles accept strict `#RGB`/`#RRGGBB` foregrounds plus `bold`/`dim` booleans.
- **Sidebar rows.** Agent-row built-ins: `state_icon`, `state_text`, `workspace`, `tab`, `pane`, `agent`, `terminal_title`, `terminal_title_stripped`. Space-row built-ins: `state_icon`, `state_text`, `workspace`, `branch`, `git_status`. Custom values reported through pane/workspace metadata use a `$name` token (for example `$jj_status`). A token may be styled as `{ token = "workspace", fg = "#89b4fa", bold = true, dim = false }`; omitted style fields keep the contextual default. `rows_by_agent` keys are canonical agent IDs (for example `claude`).
- **`tab_bar_right`**: ordered entries; supported types `zoom`, `hostname`, `datetime`, `text`, `command`; hostname/datetime/command resolve on the Herdr server. `tab_bar_right_separator` defaults to `" "`.
- **`window_title`**: tokens `{hostname}`, `{workspace}`, `{tab}`, `{pane}`, `{terminal_title}`; `{{` and `}}` are literal braces; `""` leaves the outer terminal title alone; it renders on the server, so `{hostname}` is the host the panes run on even for a remote client.
- **`agent_panel_sort`**: `"spaces"` (alias `"workspaces"`) or `"priority"`. **`status_indicators`**: `"dots"` or `"symbols"`.
- **`[ui.toast]` `delivery`**: `off` disables pop-ups; `herdr` in-app toasts; `terminal` asks the outer terminal for a desktop notification; `system` asks the OS notification service directly. `delay_seconds` is in seconds.
- **`[ui.sound]`**: `path`/`done_path`/`request_path` are optional mp3 files, relative paths resolved from the config file's directory; `[ui.sound.agents]` values are `default`, `on`, or `off` (droid is muted by default).
- **`new_cwd`** applies when no explicit `--cwd` is given: `follow` (inherit source pane/workspace), `home`, `current` (Herdr's own process directory), or a fixed path such as `"~/Projects"`.
- **`shell_mode`**: `auto` uses login shells on macOS and keeps current behavior elsewhere. `[update] channel`: stable builds default to `stable`; Windows preview builds default to `preview`.
- **Custom key commands** (`[[keys.command]]`): `type = "shell"` (detached, background), `"pane"` (temporary pane closed on exit), `"popup"` (session-modal; `width`/`height` in cells or percent); on Windows commands run through `cmd.exe /d /c`.
- **`[experimental]`**: `pane_history` ("save recent pane screen history across full server restarts") is the only uncommented setting; `cjk_ime_agents` accepts `pi, claude, codex, gemini, cursor, devin, cline, opencode, copilot, kimi, kiro, droid, amp, grok, hermes, kilo, qodercli, qoder, qwen, qwen-code, maki` (empty = any focused pane; a list with no valid names disables the reveal); `cjk_ime_cursor_shape` values `block`, `steady_block` (default), `underline`, `steady_underline`, `bar`, `steady_bar`. `switch_ascii_input_source_in_prefix` is macOS/Windows only.
- **`[ui]` apply-on-launch**: `sidebar_start_collapsed` takes effect on the next launch. `right_click_passthrough_modifier`: empty/off disables it; Shift is intentionally unsupported. `mobile_width_threshold` is a terminal width in columns.
- **`[advanced] scrollback_limit_bytes`**: maximum scrollback retained per pane terminal, in bytes.

## Recipes

These are **syntax prototypes, not verified runs** — nothing here was executed against a live session. They assume a script run inside a Herdr-managed pane. Placeholders (`/path/you/choose`, `NAME`, `reviewer`, the sample task text and test command) are yours to replace. Rules for every block: quote every variable; never `eval` or splice text read from a pane, agent, or response into a command; capture a response and check its exit status before parsing; close, remove, or stop only what this task created.

```bash
set -eu
test "${HERDR_ENV:-}" = 1 || { echo "not inside Herdr" >&2; exit 1; }

# Optional helper: print one dotted field of the JSON on stdin; fails when it is absent.
# Copying the ID by hand from the printed JSON is equally fine.
herdr_field() {
  python3 -c 'import json, sys
d = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d[k]
print(d)' "$1"
}
```

### Preflight and discovery (read-only)

```bash
herdr status --json
herdr workspace list
herdr tab list --workspace "$HERDR_WORKSPACE_ID"
herdr pane list --workspace "$HERDR_WORKSPACE_ID"
herdr pane current --current
herdr pane layout --current
herdr agent list
```

### Run a command in a background pane

`pane split` creates an empty shell pane (it runs nothing); `pane run` types the command and Enter.

```bash
resp=$(herdr pane split --current --direction right --cwd "$PWD" --no-focus)
pane=$(printf '%s' "$resp" | herdr_field result.pane.pane_id)
herdr pane run "$pane" "just test"                                      # sample command
herdr pane wait-output "$pane" --match "test result" --timeout 120000   # milliseconds
herdr pane read "$pane" --source recent-unwrapped --lines 120
```

A `wait-output` match proves the text appeared (existing output can match); it does not prove the command passed — read the output and check the exit evidence yourself. Use `down` instead of `right` for a narrow or tall caller pane (check `pane layout --current`) and avoid repeated same-direction splits. Close the pane (`herdr pane close "$pane"`, destructive) only when you created it and want it gone. `pane send-text` types without Enter; stage input that way, then `herdr pane send-keys "$pane" Enter` to submit.

### Start an agent in a new sibling pane

```bash
resp=$(herdr pane split --current --direction right --cwd "$PWD" --no-focus)
pane=$(printf '%s' "$resp" | herdr_field result.pane.pane_id)
herdr agent start reviewer --kind omp --pane "$pane"          # returns once ready; default timeout 30000 ms
task='Review the current diff and report only actionable findings.'
if herdr agent prompt reviewer "$task" --wait --timeout 120000; then
  : # a settled state (idle, done, or blocked) was OBSERVED — not proof the work succeeded
else
  herdr agent get reviewer      # server error exit 1 or syntax error exit 2: inspect before retrying
fi
herdr agent get reviewer
herdr agent read reviewer --source recent-unwrapped --lines 120
```

Native agent arguments go after `--`: `herdr agent start reviewer --kind omp --pane "$pane" -- --model provider/model`. Use the kind the user asked for (`herdr agent start --help` lists them). The returned state and the transcript are observations; verify the work itself ([State is not success](#state-is-not-success)).

### Wait for or handle a blocked agent

```bash
herdr agent wait reviewer --until blocked --timeout 120000
herdr agent get reviewer
herdr agent read reviewer --source recent-unwrapped --lines 120
```

Decide from what the read shows before sending input. For interactive UI controls: `herdr agent send-keys reviewer esc`. Ask the user before answering an approval dialog. Do not blindly send Enter as generic recovery (the adapters send exactly one Enter in one narrow case; see [adapter delivery](#delivery-confirmation)).

### New workspace for a task

Only when the user asked for a separate workspace; the default topology is a sibling pane.

```bash
resp=$(herdr workspace create --cwd "$PWD" --label "scratch" --no-focus)
pane=$(printf '%s' "$resp" | herdr_field result.root_pane.pane_id)
```

Use that pane for `pane run` or `agent start`. The response also carries `.result.workspace` and `.result.tab`.

### Git worktree workspace

```bash
base=$(git rev-parse HEAD)
resp=$(herdr worktree create --cwd "$PWD" --branch "feature/example" --base "$base" --label "example" --no-focus)
herdr worktree list --cwd "$PWD"
```

Read the new worktree's pane from the response (the adapters read `.result.root_pane.pane_id`). `herdr worktree remove --workspace <WORKSPACE_ID>` is destructive — only for a worktree you created and have no unmerged work in.

### Move a pane

```bash
herdr pane layout --pane "$pane"
resp=$(herdr pane move "$pane" --new-tab --label "build" --no-focus)
pane=$(printf '%s' "$resp" | herdr_field result.move_result.pane.pane_id)   # the old ID may stop working
```

A `changed=false` / `zoomed_tab` result means unzoom first: `herdr pane zoom "$pane" --off`.

### Notify the user

```bash
herdr notification show "Build finished" --body "See the build pane" --position bottom-right --sound done
```

### Report display-only metadata (advanced; only for a pane you own)

```bash
herdr pane report-metadata "$pane" --source my-tool --title "Build" --token stage=compile --ttl-ms 60000
herdr pane report-metadata "$pane" --source my-tool --clear-title --clear-token stage
```

`--source` must be a stable ID naming your reporter. `--ttl-ms`/`--seq` semantics are not in help (see [metadata](#metadata-and-report-commands)); do not use `report-agent` to fake lifecycle state.

### Sessions and remote (human terminal)

```bash
herdr session list --json
herdr --session scratch                         # create or attach a named session
herdr session attach scratch
herdr --remote user@host --session scratch --remote-keybindings local
```

Stopping or deleting is a separate, deliberate step for a session you created: `herdr session stop scratch`, then `herdr session delete scratch` (stopped sessions only).

### Configuration and administration

Read-only or local-output checks:

```bash
herdr --default-config > /path/you/choose/default-config.toml   # reference copy
herdr config check
herdr integration status --outdated-only
herdr api schema --output /path/you/choose/herdr-api-schema.json
herdr completion zsh > /path/you/choose/_herdr
```

Applies a change to the running server (after `config check` passes):

```bash
herdr server reload-config
```

**Human decision only — do not paste** (see [destructive and human-only commands](#destructive-and-human-only-commands)): `herdr update`, `herdr channel set <stable|preview>`, `herdr config reset-keys`, `herdr integration install|uninstall <TARGET>`, `herdr server stop`, `herdr session stop|delete NAME`, any `herdr plugin install|link|enable|action invoke`.

### Detection debugging

```bash
herdr agent explain reviewer --verbose
herdr agent explain reviewer --format json
herdr pane read "$pane" --source detection
herdr server agent-manifests --json
```

### Inspect plugins (read-only)

```bash
herdr plugin list --json
herdr plugin action list
herdr plugin log list --limit 20
```

## Troubleshooting

Sources are marked: **(help)** from `--help`, **(skill)** from `herdr --skill`, **(adapters)** from this repository's scripts and their tests.

| Symptom | Cause / action |
|---|---|
| `HERDR_ENV` unset | Not in a Herdr-managed pane **(skill)**. Say so and stop; do not control a session from outside. |
| `server_not_running` | **(adapters)** No server on the socket you reached. Start or attach deliberately (human `herdr`). An inherited-env mismatch (`HERDR_SOCKET_PATH`, `HERDR_SESSION`) can point at the wrong socket; the adapters list every `~/.config/herdr/sessions/*/herdr.sock` with a live server. Not "workers vanished". |
| Exit `1` + JSON on stderr | Server error **(skill)**. Read the JSON error body; the adapters read `error.code` (field names are not given in help). |
| Exit `2` | CLI syntax error **(skill)**; fix the command. |
| `agent_blocked` | **(help)** The agent is at an approval/question UI; nothing was sent. Inspect `agent read`; ask before answering. |
| `agent_prompt_stalled` | **(help)** No observed lifecycle change within 5000 ms of submission. Text may be sitting unsubmitted in the composer. Inspect `agent read`; do not retry blindly. |
| `timeout` | **(help)** Your `--timeout` elapsed first (milliseconds). |
| `agent_not_ready` | **(skill)** `agent start` found the agent blocked during startup; the name is live. Inspect with `agent read`; wait until idle before prompting. |
| `agent_pane_busy` | **(adapters)** Named as a retryable start failure; presumably the pane is not an available shell prompt [INFERENCE]. The adapters retry on it. |
| `agent_not_found` | **(adapters)** No live agent has that name/pane. Names clear on exit, release, replace. |
| `protocol_mismatch` | **(adapter tests)** Treated as a hard error. Stop spawning; restart the session from outside so the current binary serves it. Do not `server stop` from the pane. |
| Old pane ID stops working | After `pane move` use `.result.move_result.pane.pane_id` or the agent name **(skill)**. |
| `pane move` says `changed=false`, `zoomed_tab` | **(adapters/orchestrator notes)** Unzoom, move, optionally re-zoom. |
| `pane read` is missing earlier output | Alternate-screen agent; see [read semantics](#read-and-wait-semantics). |
| Wait never returns | No `--timeout` ⇒ indefinite **(help)**. Add a millisecond timeout. |
| `agent wait` returns `idle`/`done` but work looks wrong | Lifecycle observations, not success. See [State is not success](#state-is-not-success). |
| Where are the logs? | Root help prints `herdr.log`, `herdr-client.log`, `herdr-server.log` under `~/.config/herdr/` (default session) or `~/.config/herdr/sessions/NAME/` (named; `HERDR_SESSION=NAME`). |
| Config change has no effect | `herdr config check`, then `herdr server reload-config`; some `[ui]` options apply on next launch (`sidebar_start_collapsed`). |
| Keys do nothing | Defaults can be overridden in `config.toml`; modified/punctuation chords depend on the terminal. |
| `herdr terminal` / `herdr plugin` "not in help" | Both exist but root help omits them; use their `--help` ([Plugin](#plugin-unlisted-group), [Terminal](#terminal-unlisted-group)). |

## Orchestration adapters (this repository)

Everything below is **not Herdr**. These Python 3 adapters wrap the Herdr CLI for a controller pane. Authority is [SKILL.md](../SKILL.md); session/run contracts are [session-docs.md](../references/session-docs.md), practical examples [controller-playbook.md](../references/controller-playbook.md), and isolation guidance [pane-environment.md](../references/pane-environment.md). Situational contracts: [identity/reattachment](../references/identity-reattach.md), [rotation](../references/rotation.md), [receipts/promotion/retirement](../references/retire-promote.md), and [run-state/output API](../references/run-state-api.md).

All lock-taking adapter syntaxes below additionally accept `--lock-timeout SECONDS`: `init-docs`, `dispatch-agent`, `spawn-agent`, `prompt-agent`, `collect-agent`, `finish-agent`, `retire-agents`, `rotate`, `ack-handoff`, and `reconcile`. The approved fallback requires finite positive seconds (default 120; invalid input exit 2, acquisition timeout refusal exit 3), preserves every exclusive critical section including launch/close, and treats `run.lock.owner` as unverified diagnostics only. It does not implement concurrent dispatch; see the [locking contract](../references/run-state-api.md#events-locking-and-dispatch).

### Rules that never bend

- State authority: `docs/runs/<run-id>/events.jsonl` is append-only and authoritative. `state.json`, `controller.json`, `status.md` are caches. Session prose, `progress.md`, and Herdr `idle`/`done` are **not** success. See [Authority](../references/session-docs.md#authority).
- Persist and pass the canonical absolute `run` and `session` paths printed by `scripts/init-docs.py`. Never use `latest` or a relative `docs/...` path; never write through `latest`. Ignore `docs/sessions` and `docs/runs` in the target repo (they must be Git-ignored; `init-docs.py` checks).
- Contextual dispatch needs `--run` plus the exact holder triple (`--session`, `--holder-workspace`, `--holder-pane`) and the current `--epoch` — **necessary, not sufficient**: the holder must also be the exact **active** lease holder, dispatch must be enabled, the attempt must match, and the node must be ready. While the lease is `draining` (rotation) `dispatch_intent`/`dispatched` are refused, and `collect`/`finish`/`retire` also refuse a draining lease (they require the active holder) even though the library lets a few event types through when draining (`blocked`, `failed`, `interrupted`, `invalidated`, `notification_sent`, `promoted`, `promotion_intent`, `skipped`, `succeeded`). `lease_transfer` **increments the epoch**: after a handoff never reuse the predecessor's epoch. Holder keys are exactly `workspace`, `pane`, `session` (the canonical absolute session path).
- Concurrent writers own **disjoint paths**; overlapping slices serialize. Writers run in Herdr worktrees; integrators in separate worktrees; promotion targets the run-owned ref only. Every explicit VCS consumer matters before close: promoted `integration.json` for integration-output consumers; current same-attempt controller success and a matching immutable consumed snapshot/digest for others. Workspace close retains Git worktrees and branches.
- Managed workers are addressed by `worker_id` (`fo-` + 24 hex), never by the friendly `--name` alias, label, or sidebar position. Identity uncertain ⇒ block, never adopt.
- Native `task`/`workflow` jobs are session-local and cannot be reattached after the controller session dies.
- Documented target: Herdr 0.8.2 (protocol 20) — a statement in the skill docs and script docstrings, **not a runtime check**: no adapter probes the version or protocol. A Herdr error such as `protocol_mismatch` surfaces as a runtime failure (exit 1). Operator doctrine, not script enforcement: on a protocol mismatch stop spawning, do not substitute native `task` for Herdr roles, and do not `herdr server stop` from the pane.
- `scripts/run_state.py` is the stable controller-facing Python facade, not a CLI. Adapters import defining D5 modules; see [run-state-api.md](../references/run-state-api.md) and the [module index](../README.md#layout).
- Failure output is not uniform: handlers print the error text once on stderr, but `parser.error` prints usage plus the message (exit 2); `dispatch-agent.py` and `finish-agent.py` forward the child script's stderr and exit code; `spawn-agent.py` may print `warn: ...` and continue; `monitor-agents.py` notify failures appear as JSON on stdout. Do not parse stderr as exactly one line.
- Overlap is enforced at graph acceptance (`owns overlap without serialization`); the promotion ref is the run-owned `refs/heads/herdr/<run-id>`. Native `task`/`workflow` jobs are interrupted when their `spawning_session` differs from the current session; vanished-job detection runs only with a complete native inventory. The scripts never launch native jobs.

### Units and defaults

| Item | Unit / default |
|---|---|
| Herdr CLI `--timeout` | milliseconds |
| `herdr_cli.PROMPT_WAIT_MS` | 15000 ms passed to `agent prompt/wait --timeout` (longer than Herdr's own 5000 ms state-change window, so a stalled composer reports `agent_prompt_stalled`, not `timeout`) |
| Read-only Herdr subprocesses (`agent`/`workspace`/`pane` × `get`/`list`/`show`) | 10 seconds (`READ_TIMEOUT`); other calls retain 60 seconds (`SUBPROCESS_TIMEOUT`), including prompt/wait |
| `monitor-agents.py --interval` / `--min-interval` / `--grace` / `--timeout` | float seconds; defaults 30 maximum / 2 initial / 8 / 3600; unchanged polls double the interval up to the maximum, changes reset it |
| `monitor-agents.py` regular output | change-only by default, including initial/terminal snapshots; `--every-poll` restores per-poll lines; up to three consecutive transient agent-list failures tolerated |
| `monitor-agents.py --stale` | seconds, default unset |
| `monitor-agents.py --notify-on` | `final` (default) or `edge` |
| `spawn-agent.py --retries` | default `10` = **10 start attempts total**, not 10 extra; sleeps only on `agent_pane_busy`; `agent_not_ready` is not retried |
| `spawn-agent.py --retry-delay` | seconds, default `2.0` |
| `dispatch-agent.py --retries` / `--retry-delay` | default none: omitted flags are not passed, so spawn's defaults apply |
| `rotate.py --timeout` | seconds, default 300, must be `> 0`; bounds the native-quiescence wait **and** the ack poll (5 s poll interval) |
| `rotate.py --label` / `--model-role` | defaults `successor` / `default`; label must match `^[a-z][a-z0-9_-]{0,31}$` |
| `init-docs.py --dir` / `--job` | defaults `docs` / working-directory name |
| `spawn-agent.py --kind` | default `omp`; a leading bare `orchestrate` line is prepended iff kind is `omp`, unless `--orchestrate`/`--no-orchestrate` is given. `prompt-agent.py`: prepend is on by default. `dispatch-agent.py`: passes the flag only when you set it |
| `collect-agent.py` summary stdout | 1,500 characters; `summary_truncated: true` when cut; `--full-summary` disables cap; contextual `summary.json` retains full text |
| worktree creation in `spawn-agent.py` | happens when **any** of `--branch`, `--base`, `--path` is given |
| metadata token values | capped at 80 characters (`TOKEN_VALUE_MAX`) |

### Delivery confirmation

Spawn and prompt share one `deliver_prompt` call and one full-text submission. Only structured `exc.code` classifies errors: prompt text containing `agent_blocked`, `agent_prompt_stalled`, or `timeout` never changes behavior. Uncoded/unrelated failures propagate. Confirmed delivery and the single Enter recovery are defined in [identity-reattach.md](../references/identity-reattach.md#delivery-and-routing); prompt-specific retry flags/count output are removed, while spawn/dispatch startup retries remain.

### Adapter index

| Adapter | Purpose | Exit codes |
|---|---|---|
| `scripts/init-docs.py` | open/reuse session docs and bind a run | 0 ok, 1 runtime, 2 usage, 3 precondition/refusal |
| `scripts/reconcile.py` | replay and repair caches; observe/derive lifecycle | 0, 1, 2, 3 |
| `scripts/dispatch-agent.py` | spawn a ready `herdr-agent` node (idempotent) | 0 ok, 1 runtime, 2 usage, 3 precondition |
| `scripts/spawn-agent.py` | create workspace/worktree, pane, agent, deliver prompt | 0 ok, 1 herdr/omp failure, 2 bad arguments, 3 precondition/refusal |
| `scripts/prompt-agent.py` | prompt a live agent once through confirmed-delivery boundary | 0 ok, 1 runtime, 2 bad arguments, 3 precondition, 4 `agent_blocked` |
| `scripts/monitor-agents.py` | background poller until named agents settle | 0 settled, 1 runtime, 2 timeout **and** argparse usage, 3 vanished |
| `scripts/collect-agent.py` | status/summary snapshots, hashed receipts | 0 ok, 1 runtime, 2 usage, 3 names/artifacts refused |
| `scripts/check-ownership.py` | classify changes as owned/overlap/unowned (3-line delegator to `ownership.py`) | 0 clean, 1 runtime, 2 usage/manifest, 3 findings |
| `scripts/finish-agent.py` | ownership gates, collect, success or promote call | 0 ok, 1 runtime, 2 usage, 3 precondition |
| `scripts/retire-agents.py` | close orchestrator-owned workspaces | 0 all retired, 1 runtime, 2 usage, 3 one or more refused |
| `scripts/rotate.py` | measured controller rotation; see [rotation gate](../references/rotation.md#measured-context-gate) | 0, 1, 2, 3 domain condition (including context refusal and unacked) |
| `scripts/ack-handoff.py` | successor validates predecessor and transfers lease | 0, 1 runtime, 2 usage, 3 metadata/holder/nonce/lineage precondition |

Shared modules without a CLI: `scripts/herdr_cli.py`, `scripts/session_docs.py`, and `scripts/run_state.py`. `scripts/prompt_contract.py` has a runnable assertion self-check. `scripts/ownership.py` is also executable; `check-ownership.py` delegates to the same `main()` and is the documented controller entry point.

### Per-adapter syntax

#### scripts/init-docs.py

```text
python3 scripts/init-docs.py [--dir DIR] [--job JOB] [--new] [--run RUN]
```

Opens a session under `DIR/sessions/<id>/` (`--dir` default `docs`, `--job` default the working-directory name). Reuses a session only for this pane's workspace/pane identity and job; outside Herdr every run opens a fresh session; `--new` always does. A conflicting or relative `--run` is refused (exit 3). Prints one JSON line: `session`, `id`, `job`, `owner`, `parent`, `reused`, `source`, `run`, `files`. Save the printed absolute `session` and `run`. `docs/sessions/latest` is an informational pointer that nothing reads: never resolve or write through it.

#### scripts/reconcile.py

```text
python3 scripts/reconcile.py --run RUN [--event EVENT] [--notify] [--require-native-quiesced]
    [--session CURRENT_SESSION] [--holder-workspace ID] [--holder-pane ID] [--repo REPO]
    [--live-native-id ID]... [--native-inventory-complete] [--full]
```

Plain `--run` repairs missing/damaged/changed replay caches and leaves unchanged bytes untouched; it appends no events. Compact JSON is default (`epoch`, `dispatch`, `lease`, exact `holder`, per-node `state`/`attempt`, optional `reason`/`question`, `notified`, and `truncated_tail` only when true). `--full` prints the complete Python return shape. Holder/native inventory and server-unknown semantics are in [run-state-api.md](../references/run-state-api.md#reconcile-and-inventory-semantics).

#### scripts/dispatch-agent.py

```text
python3 scripts/dispatch-agent.py --run RUN --node N [--attempt A] --session S
    --holder-workspace W --holder-pane P --epoch E (--prompt-file F | --prompt TEXT)
    [--name ALIAS] [--model-role R | --model M] [--requested-effort E] [--plan P]
    [--kind K] [--cwd DIR] [--branch B] [--path P] [--orchestrate | --no-orchestrate]
    [--retries N] [--retry-delay S]
```

Idempotent, **`herdr-agent` nodes only** (`spawn-agent.py` refuses other executors). Refuses a wrong `--epoch`, reconciles, then: a ready node appends `dispatch_intent` when missing, runs `spawn-agent.py --resume` only when `launch-complete.json` is absent, and appends `dispatched` from the identity (a missing `launch-complete.json` after spawn is exit 3; a spawn failure propagates its exit and appends no `blocked`); a `planned` node, or a `blocked` node without `dispatch-identity.json`, is exit 3; any other state is authoritative and does not spawn. It appends `blocked` once only for a `running` attempt whose launch record says blocked and which has no `blocked` event after `dispatched` (reason `agent_not_ready` if not prompted, else `agent_blocked`). `--cwd` is the workspace cwd for read-only (non-worktree) nodes (present in the parser usage, absent from the module docstring). Omit `--name`; it is a display alias only. Prints `{"node","attempt","agent","workspace","worktree","model","state","prompted","blocked"}`. Ready `pane` nodes and blocked `herdr-agent`/`pane` continuations go through `prompt-agent.py`, not dispatch.

#### scripts/spawn-agent.py

```text
python3 scripts/spawn-agent.py NAME [--cwd CWD] [--kind KIND] [--branch BRANCH] [--base BASE] [--path PATH]
    [--model-role ROLE | --model provider/model[:thinking]] [--prompt TEXT | --prompt-file PATH]
    [--plan PATH] [--run RUN --node NODE --attempt N --session S --holder-workspace W
     --holder-pane P --epoch E] [--requested-effort E] [--resume [--delivered]]
    [--retries N] [--retry-delay S] [--orchestrate | --no-orchestrate]
```

With any of `--branch`/`--base`/`--path` it runs `herdr worktree create --label LABEL --no-focus ...`; otherwise `herdr workspace create --cwd DIR --label LABEL --no-focus`. The adapter's own `--branch` help says an omitted branch lets Herdr generate one (an adapter statement; Herdr's help is silent). It reads `.result.root_pane.pane_id`, starts the agent (retrying on `agent_pane_busy`; `agent_not_ready` is not retried — the name stays live, the prompt is skipped, the result is blocked), stamps metadata tokens, then delivers the prompt. Contextual mode generates `worker_id` (`fo-` + 24 hex), persists `launch-intent.json` before any Herdr side effect, labels the workspace `<title> · <worker_id>` until `dispatch-identity.json` exists (that suffix is how an orphan is found), and leaves sidecars `launch-intent.json` → `dispatch-identity.json` → `prompt-attempt.json` → `launch-complete.json`. `--resume` is at-most-once and fails closed (exit 3) on ambiguity (including a sent-but-unacknowledged prompt); `--resume --delivered` (requires `dispatch-identity.json`) records a hand-delivered prompt without Herdr calls. Prints one JSON line: `name` (the `worker_id` when contextual), `workspace`, `pane`, `agent`, `model`, `thinking`, `routing`, `prompted`, `blocked`, `worktree`, `tokens`, `resumed`; contextual dispatch adds `identity_kind`, `worker_id`, `alias`, `repo` (and `delivered` after `--resume --delivered`).

Model selection: `--model-role` and `--model` are alternatives. A role is looked up in `omp config get modelRoles --json`; a `provider/model[:thinking]` spec is split into `--model` plus `--thinking`, passed to the agent after `--`. Omitting both flags inherits the pane's model default.

#### scripts/prompt-agent.py

```text
python3 scripts/prompt-agent.py NAME (--prompt TEXT | --prompt-file PATH)
    [--run RUN --node ID --attempt N --session S --holder-workspace W --holder-pane P --epoch E]
    [--orchestrate | --no-orchestrate]
```

`--run/--node/--attempt` are all-or-none and require the exact holder triple plus `--epoch`. Uncontextual success prints `{"name": NAME, "prompted": true}` with no retry-count field; contextual success retains `workspace`, `pane`, `agent`, `routing`, `tokens`, `run`, `node`, and `attempt`, plus managed `worker_id`/`identity_kind` when applicable. Confirmed blocked continuation returns an `attempt_started` event descriptor for the controller, never appends it. Structured `agent_blocked` prints `{"name": NAME, "prompted": false, "blocked": true}` and exits 4; preconditions exit 3. Exact identity/routing checks and unknown live values remain governed by [identity-reattach.md](../references/identity-reattach.md).

#### scripts/monitor-agents.py

```text
python3 scripts/monitor-agents.py NAME [NAME ...] [--interval SECS] [--min-interval SECS] [--grace SECS] [--timeout SECS]
    [--notify-pane PANE_ID] [--notify-on final|edge] [--label TEXT] [--stale SECS] [--every-poll]
```

Background observer only; never a foreground polling loop. Adaptive cadence starts/resets at `--min-interval` (2 seconds), doubles unchanged up to `--interval` (30 seconds). Regular JSONL emits on states/pending/stale-set changes, including initial/terminal snapshots; `--every-poll` explicitly restores every-poll output. Up to three consecutive transient agent-list failures are tolerated. Wake the controller through an auto-delivering job and/or `--notify-pane`; settlement (`idle`/`done`/`blocked`) is observation, not success. Activity, stale notifications and final/edge notification rules are in [run-state-api.md](../references/run-state-api.md#background-observation-and-output-cost).

#### scripts/collect-agent.py

```text
python3 scripts/collect-agent.py NAME [NAME ...] [--status-only] [--force] [--full-summary]
    [--run RUN --node ID --attempt N --session S --holder-workspace W --holder-pane P [--publish-vcs]]
```

Bare mode prints `{"agents":[{"name","state","summary","source","session_path"}]}`. Per-attempt mode takes exactly one name and requires the **active** lease holder; it creates/checks hashed `summary.json`, writes `vcs.json` only with `--publish-vcs` (form `commit`, or `none` when HEAD equals base and the tree is clean), and hashes an existing `integration.json` (erroring if an integration output lacks one) — it does **not** create `integration.json`; `finish-agent.py` writes that before calling collect. `--force` replaces differing receipts and appends `artifact_forced`. `--status-only` never writes.

Stdout summaries are capped at 1,500 characters with `summary_truncated: true` when cut. `--full-summary` disables the cap; contextual `summary.json` still stores the complete text. Transcript extraction reads backward from EOF.

#### scripts/check-ownership.py

```text
python3 scripts/check-ownership.py (--manifest FILE | --run RUN [--node N]) [--attempt A] [--base SHA | --since SHA]
```

Exactly one of `--manifest` or `--run` (with `--node`). `--since` is an alias of `--base`. With `--manifest`, omitting `--base`/`--since` classifies only working-tree changes (a writer that committed and left the tree clean shows nothing). With `--run --node`, an omitted base defaults to `git merge-base <promotion_ref> HEAD`, else the identity base, and `BASE..HEAD` files are classified too; `--attempt` defaults to the highest all-digit attempt directory. Prints `{"clean","unowned","overlaps","by_slice","files"}`; exit 3 on findings.

#### scripts/finish-agent.py

```text
python3 scripts/finish-agent.py --run RUN --node N --attempt A --session S
    --holder-workspace W --holder-pane P [--since SHA] [--force]
```

Refuses a dirty tree or a HEAD not descending from `--since` (default: merge-base of promotion_ref and HEAD, else identity base), classifies changed files against `dispatch_intent.owns` (working-tree only for integrators), writes `integration.json` for integrators from consumed receipts, runs `collect-agent.py --publish-vcs`, then appends `succeeded` for writers/read-only nodes or prints the exact `run_state.promote(...)` call for integrators (no `succeeded`). A **writer** that declared a worktree and finished at `vcs.result.form: none` committed nothing and is refused unless `--force`; integrators still need a commit vcs result and `--force` does not make `none` succeed for them. `--force` also re-collects an already-succeeded attempt (auditing `artifact_forced`). Requires the active lease holder. Prints `{"node","attempt","state","vcs_form","artifacts","unowned","promote","since"}`.

#### scripts/retire-agents.py

```text
python3 scripts/retire-agents.py NAME [NAME ...] [--collect] [--force-unsettled]
    [--run RUN --session S --holder-workspace W --holder-pane P]
```

Closes only orchestrator-created workspaces (`herdr workspace close` is the only mutation). Invoke from the active run controller with exact holder and recorded identity. User-owned panes, replacements, ambiguous identities, nonterminal attempts, and missing/stale/mismatched consumer receipts refuse. `--force-unsettled` bypasses only unsettled observation; `--collect` never authorizes terminality or consumption. Workspace close retains Git worktrees/branches/source/data and never stops Herdr. Prints `{"retired":[...],"refused":[...]}`. All receipt, consumer, provenance and historical-attempt gates are defined in [retire-promote.md](../references/retire-promote.md#retirement-boundaries).

#### scripts/rotate.py

```text
python3 scripts/rotate.py --session PATH [--label LABEL] [--model-role ROLE] [--timeout N]
```

Spawns and verifies a successor without retiring the predecessor. Requires exact predecessor surface identity, canonical same-docs-root run metadata, and readable `HANDOFF.md`/`NEXT-SESSION-PROMPT.md` before drain/spawn. Metadata is the sole run locator: no markdown Run equality, progress prerequisite, document-age flag, or handoff-mtime token. Measured context freshness and occupancy gate remain enforced; see [rotation.md](../references/rotation.md). Rotation owns native drain and tokenized ack; do not pre-drain. An unacked successor leaves the lease draining, prints `"acked": false`, and exits 3. Preserve exact nonce transfer and same-nonce recovery; reattach live workers rather than redispatching.

#### scripts/ack-handoff.py

```text
python3 scripts/ack-handoff.py --nonce N --session PATH
```

Run inside the successor pane with the exact nonce and concrete predecessor path from rotation. Canonical predecessor session/run metadata and required HANDOFF/NEXT bootstrap resources must be valid; missing/invalid/noncanonical/conflicting/out-of-root metadata is exit 3. No progress prerequisite, parsed markdown run locator/equality, note-age gate, or handoff-mtime stamp remains. Lease must be draining and exact holder, nonce and lineage must match. A single `lease_transfer` advances epoch; same-nonce recovery reuses the transferred successor without a second session or transfer. Stamp `handoff-ack` and successor `session` metadata. Prints `acked`, `workspace`, `nonce`, canonical `session`, `session_id`, `parent`, and `run`. See [rotation.md](../references/rotation.md#drain-acknowledgment-and-stand-down).

### run_state library calls

Python APIs in `scripts/run_state.py` (not a CLI) that the flow below calls:

| Call | Notes |
|---|---|
| `accept_graph(run, graph, *, repo, holder=None, lock_timeout=120)` | Accepts the graph; sets `promotion_ref` to `refs/heads/herdr/<run-id>`; requires the exact holder once bound. The returned `base_sha` is authoritative (`HEAD` of the supplied `repo` at first acceptance); a `base_sha` in the submitted graph is ignored — check it before dispatch. |
| `promote(run, *, node, attempt, new_sha, repo, holder=None, lock_timeout=120)` | Returns `{state, controller, event}` plus either no delivery keys (ref moved → blocked), `{delivery_branch, sha, created}`, or `{delivery_branch: None, delivery_note}`. Refuses a checked-out promotion ref. |
| `deliver_branch(run, *, repo, holder=None, branch=None, lock_timeout=120)` | Create-only human branch after the whole graph is `succeeded`/`skipped`; refuses `refs/heads/herdr/<run-id>`. |
| `replay(run)` | `{state, controller, events, skipped_stale, truncated_tail}` |
| `append_event(run, event, *, holder=None, lock_timeout=120)` | `{state, controller, event}` |
| `claim_run(docs_root, job, *, created=None)` | Used by session binding, not a controller step. |

Promotion recovery is `scripts/reconcile.py --run RUN --repo REPO` plus the holder flags, not a separate CLI.

### Typical controller flow

1. `scripts/init-docs.py` → keep the printed `session` and `run`.
2. Accept the graph through `run_state.accept_graph(...)` (library call; see [`../SKILL.md`](../SKILL.md)).
3. `scripts/reconcile.py --run RUN` (add holder flags to observe).
4. `scripts/dispatch-agent.py ...` per ready `herdr-agent` node; ready `pane` nodes and blocked continuations use `scripts/prompt-agent.py` (same attempt, no second launch intent).
5. `scripts/monitor-agents.py ... --notify-pane CONTROLLER_PANE` in the background.
6. `scripts/finish-agent.py ...` (and `scripts/check-ownership.py` as needed); integrators then `run_state.promote(...)`; after the graph is done, `run_state.deliver_branch(...)` creates the human branch.
7. `scripts/retire-agents.py ...` only after receipts, and only orchestrator-created surfaces.
8. `scripts/rotate.py` / `scripts/ack-handoff.py` use the [measured rotation gate](../references/rotation.md#measured-context-gate); do not bypass refusal or redispatch still-live workers.

Syntax prototype with placeholders only (all values come from `init-docs.py`, the graph, and the rotate prompt; never guess a path or epoch):

```text
python3 scripts/init-docs.py --dir docs --job JOB
python3 scripts/reconcile.py --run "$RUN" --session "$SESSION" \
  --holder-workspace "$HERDR_WORKSPACE_ID" --holder-pane "$HERDR_PANE_ID"
python3 scripts/dispatch-agent.py --run "$RUN" --node NODE --session "$SESSION" \
  --holder-workspace "$HERDR_WORKSPACE_ID" --holder-pane "$HERDR_PANE_ID" \
  --epoch "$EPOCH" --prompt-file brief.md
python3 scripts/prompt-agent.py "$WORKER_ID" --prompt-file brief.md \
  --run "$RUN" --node NODE --attempt 1 --session "$SESSION" \
  --holder-workspace "$HERDR_WORKSPACE_ID" --holder-pane "$HERDR_PANE_ID" --epoch "$EPOCH"
python3 scripts/monitor-agents.py "$WORKER_ID" --notify-pane "$HERDR_PANE_ID" --notify-on final
python3 scripts/finish-agent.py --run "$RUN" --node NODE --attempt 1 --session "$SESSION" \
  --holder-workspace "$HERDR_WORKSPACE_ID" --holder-pane "$HERDR_PANE_ID"
python3 scripts/check-ownership.py --run "$RUN" --node NODE
python3 scripts/retire-agents.py "$WORKER_ID" --run "$RUN" --session "$SESSION" \
  --holder-workspace "$HERDR_WORKSPACE_ID" --holder-pane "$HERDR_PANE_ID"
python3 scripts/rotate.py --session "$SESSION"
python3 scripts/ack-handoff.py --nonce "$NONCE" --session "$PREDECESSOR_SESSION"
```

Port and shared-infrastructure isolation: [Per-pane ports](../references/pane-environment.md#per-pane-ports) and [Shared infrastructure vs isolated processes](../references/pane-environment.md#shared-infrastructure-vs-isolated-processes). Practical commands: [Controller playbook](../references/controller-playbook.md#5-herdr-commands-used-by-the-controller).
