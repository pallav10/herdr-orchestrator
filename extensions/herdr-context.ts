import { createHash, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import { homedir } from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const SOURCE = "OMP ExtensionContext.getContextUsage";

function writeSnapshot(workspace: string, pane: string, payload: Record<string, unknown>): void {
  const directory = path.join(homedir(), ".omp", "run", "herdr-context");
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const directoryStat = fs.lstatSync(directory);
  if (!directoryStat.isDirectory() || directoryStat.isSymbolicLink() ||
      (typeof process.getuid === "function" && directoryStat.uid !== process.getuid())) {
    throw new Error("Unsafe context snapshot directory");
  }
  fs.chmodSync(directory, 0o700);
  const digest = createHash("sha256").update(`${workspace}\n${pane}`, "utf8").digest("hex");
  const destination = path.join(directory, `${digest}.json`);
  try {
    const existing = fs.lstatSync(destination);
    if (!existing.isFile() || existing.isSymbolicLink() ||
        (typeof process.getuid === "function" && existing.uid !== process.getuid())) {
      throw new Error("Unsafe context snapshot file");
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
  }
  const temporary = path.join(directory, `.${digest}.${process.pid}.${randomBytes(8).toString("hex")}.tmp`);
  try {
    fs.writeFileSync(temporary, JSON.stringify(payload), { flag: "wx", mode: 0o600 });
    fs.chmodSync(temporary, 0o600);
    fs.renameSync(temporary, destination);
  } finally {
    try {
      fs.unlinkSync(temporary);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }
}

function snapshot(ctx: ExtensionContext, reason?: string): void {
  if (ctx.hasUI !== true || ctx.agent?.kind !== "main" || process.env.HERDR_ENV !== "1") return;
  const workspace = process.env.HERDR_WORKSPACE_ID;
  const pane = process.env.HERDR_PANE_ID;
  if (!workspace || !pane) return;

  const payload: Record<string, unknown> = {
    schema_version: 1,
    source: SOURCE,
    workspace,
    pane,
    pid: process.pid,
    observed_at_ms: Date.now(),
    available: false,
    reason: reason ?? "invalid",
  };
  try {
    try {
      const file = ctx.sessionManager.getSessionFile();
      const id = ctx.sessionManager.getSessionId();
      const model = ctx.model;
      if (typeof file === "string" && path.isAbsolute(file) &&
          typeof id === "string" && id && model &&
          typeof model.provider === "string" && model.provider &&
          typeof model.id === "string" && model.id) {
        payload.omp_session_file = fs.realpathSync(file);
        payload.omp_session_id = id;
        payload.model = { provider: model.provider, id: model.id };
      }
    } catch {
      // Missing session identity leaves the measurement unavailable.
    }
    if (!reason) {
      try {
        const usage = ctx.getContextUsage();
        if (usage && typeof usage.tokens === "number" && Number.isFinite(usage.tokens) && usage.tokens >= 0 &&
            typeof usage.contextWindow === "number" && Number.isFinite(usage.contextWindow) && usage.contextWindow > 0 &&
            payload.omp_session_file && payload.omp_session_id && payload.model) {
          payload.available = true;
          payload.context_usage = { tokens: usage.tokens, context_window: usage.contextWindow };
          delete payload.reason;
        }
      } catch {
        payload.reason = "unavailable";
      }
    }
    writeSnapshot(workspace, pane, payload);
  } catch {
    // Export failures cannot disrupt OMP; the consumer refuses absent/stale evidence.
  }
}

export default function (pi: ExtensionAPI): void {
  pi.on("session_start", (_event, ctx) => snapshot(ctx));
  pi.on("session_switch", (_event, ctx) => snapshot(ctx));
  pi.on("session_branch", (_event, ctx) => snapshot(ctx));
  pi.on("session_tree", (_event, ctx) => snapshot(ctx));
  pi.on("tool_execution_start", (_event, ctx) => snapshot(ctx));
  pi.on("turn_end", (_event, ctx) => snapshot(ctx));
  pi.on("retry_fallback_applied", (_event, ctx) => snapshot(ctx));
  pi.on("auto_compaction_start", (_event, ctx) => snapshot(ctx, "compaction"));
  pi.on("session_before_compact", (_event, ctx) => snapshot(ctx, "compaction"));
  pi.on("auto_compaction_end", (_event, ctx) => snapshot(ctx));
  pi.on("session_compact", (_event, ctx) => snapshot(ctx));
  pi.on("session_shutdown", (_event, ctx) => snapshot(ctx, "session_shutdown"));
}
