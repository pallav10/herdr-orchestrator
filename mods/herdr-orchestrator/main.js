// Interactive DAG workflow board for herdr-orchestrator runs.
// Reads live state via `ho.py snapshot`, watches the active run directory,
// and lets the user focus/peek worker terminals or run safe controller actions.

import { View, div } from "gpui-kit";
import { v_flex, h_flex } from "gpui-base";
import {
  appHome,
  exec,
  home,
  props,
  readTextFile,
  run,
  theme,
  terminals,
  watchFolder,
  checking,
  notify,
} from "herdr";

const STATE_COLOR = (t, state, liveStatus) => {
  if (state === "succeeded") return t.done;
  if (state === "failed" || state === "blocked" || liveStatus === "blocked") return t.blocked;
  if (state === "running") return liveStatus === "done" || liveStatus === "idle" ? t.done : t.working;
  if (state === "ready") return t.accent;
  return t.muted;
};

const STATE_BADGE = (state, liveStatus) => {
  if (state === "running" && liveStatus) return `running · ${liveStatus}`;
  return state || "planned";
};

function findHoScript() {
  const p = props() || {};
  if (typeof p.ho === "string" && p.ho) return p.ho;
  return `${home()}/.omp/agent/skills/herdr-orchestrator/scripts/ho.py`;
}

export default class HerdrOrchestratorGraph extends View {
  init(_props, cx) {
    this.data = null;
    this.selectedRun = typeof props()?.run === "string" ? props().run : null;
    this.selectedNode = null;
    this.error = null;
    this.busy = false;
    this.actionLog = null;
    this.watchedDir = null;
    this.watchHandle = null;
    this.hoScript = findHoScript();
    if (checking()) return;
    this.refresh(cx);
    cx.timer.every(3000, (cx) => this.refresh(cx));
  }

  async close() {
    if (this.watchHandle) {
      try { this.watchHandle.cancel(); } catch {}
      this.watchHandle = null;
    }
  }

  watchRun(runDir, cx) {
    if (!runDir || this.watchedDir === runDir) return;
    if (this.watchHandle) {
      try { this.watchHandle.cancel(); } catch {}
      this.watchHandle = null;
    }
    this.watchedDir = runDir;
    cx.spawn(async (cx) => {
      try {
        while (this.watchedDir === runDir) {
          this.watchHandle = watchFolder(runDir);
          await this.watchHandle;
          if (this.watchedDir === runDir) this.refresh(cx);
        }
      } catch {
        this.watchedDir = null;
      }
    });
  }

  async resolveHoScript() {
    const candidates = [props()?.ho, this.hoScript];
    try {
      const raw = await readTextFile(`${appHome()}/data/herdr-orchestrator.json`);
      if (raw) {
        const cfg = JSON.parse(raw);
        if (typeof cfg?.ho === "string" && cfg.ho) candidates.push(cfg.ho);
      }
    } catch {}
    candidates.push(
      `${home()}/.omp/agent/skills/herdr-orchestrator/scripts/ho.py`,
      `${home()}/.claude/skills/herdr-orchestrator/scripts/ho.py`,
    );
    const seen = new Set();
    for (const cand of candidates) {
      if (typeof cand !== "string" || !cand || seen.has(cand)) continue;
      seen.add(cand);
      const check = await exec("test", ["-f", cand]);
      if (check.code === 0) {
        this.hoScript = cand;
        return cand;
      }
    }
    return this.hoScript;
  }

  refresh(cx) {
    if (this.loading) return;
    this.loading = true;
    cx.spawn(async (cx) => {
      try {
        const ho = await this.resolveHoScript();
        const argv = [ho, "snapshot"];
        if (this.selectedRun) argv.push("--run", this.selectedRun);
        const res = await exec("python3", argv, { timeout: 10000 });
        if (res.code !== 0) {
          this.error = (res.stderr || res.stdout || `ho snapshot exited ${res.code}`).trim();
        } else {
          const parsed = JSON.parse(res.stdout || "{}");
          this.data = parsed;
          this.error = null;
          const active = parsed?.active;
          if (active?.run) {
            this.selectedRun = active.run;
            this.watchRun(active.run, cx);
          }
          if (active?.nodes?.length && !active.nodes.some((n) => n.id === this.selectedNode)) {
            this.selectedNode = active.nodes[0].id;
          }
        }
      } catch (err) {
        this.error = String(err);
      } finally {
        this.loading = false;
        cx.notify();
      }
    });
  }

  runHoAction(label, args, cx) {
    if (this.busy) return;
    const active = this.data?.active;
    if (!active?.run) return;
    this.busy = true;
    this.actionLog = `Running ${label}...`;
    cx.notify();
    cx.spawn(async (cx) => {
      try {
        const ho = await this.resolveHoScript();
        const argv = [ho, ...args, "--run", active.run];
        const ctrl = active.controller || {};
        if (ctrl.workspace) argv.push("--holder-workspace", ctrl.workspace);
        if (ctrl.pane) argv.push("--holder-pane", ctrl.pane);
        const res = await exec("python3", argv, { timeout: 60000 });
        if (res.code === 0) {
          this.actionLog = `${label}: ok`;
          notify({ title: `ho ${label}`, body: "Completed", level: "success", timeout: 3000 });
        } else {
          const msg = (res.stderr || res.stdout || `exit ${res.code}`).trim();
          this.actionLog = `${label} failed: ${msg}`;
          notify({ title: `ho ${label} failed`, body: msg.slice(0, 140), level: "error", timeout: 5000 });
        }
      } catch (err) {
        this.actionLog = `${label} error: ${String(err)}`;
      } finally {
        this.busy = false;
        this.refresh(cx);
        cx.notify();
      }
    });
  }

  focusTerminal(node) {
    const live = terminals();
    const match = live.find(
      (t) =>
        t.state === "live" &&
        ((node.worker_id && t.name === node.worker_id) ||
          (node.terminal && t.address === node.terminal))
    );
    const addr = match?.address || node.terminal;
    if (!addr) return;
    run("agent.focus", { terminal: addr });
  }

  peekTerminal(node) {
    const live = terminals();
    const match = live.find(
      (t) =>
        t.state === "live" &&
        ((node.worker_id && t.name === node.worker_id) ||
          (node.terminal && t.address === node.terminal))
    );
    const addr = match?.address || node.terminal;
    if (!addr) return;
    run("terminal.peek", { terminal: addr });
  }

  btn(t, id, label, onClick, primary = false) {
    return div()
      .id(id)
      .px(10)
      .py(5)
      .rounded(5)
      .text_size(12)
      .bg(primary ? t.solid : t.raised)
      .text_color(primary ? t.solid_foreground : t.foreground)
      .border_1()
      .border_color(primary ? t.solid : t.border)
      .cursor_pointer()
      .on_click(onClick)
      .child(label);
  }

  renderNodeCard(t, node, selected, relation, cx) {
    const color = STATE_COLOR(t, node.state, node.live_status);
    const hasTerm = Boolean(node.terminal || node.worker_id);
    const borderColor = selected
      ? t.accent
      : relation === "upstream"
        ? t.done
        : relation === "downstream"
          ? t.working
          : t.border;
    return v_flex()
      .id(`node-${node.id}`)
      .w(230)
      .p(10)
      .gap(6)
      .rounded(8)
      .bg(selected ? t.selected : t.surface)
      .border_1()
      .border_color(borderColor)
      .cursor_pointer()
      .on_click((_e, cx) => {
        this.selectedNode = node.id;
        cx.notify();
      })
      .child(
        h_flex()
          .items_center()
          .justify_between()
          .gap(8)
          .child(
            h_flex()
              .items_center()
              .gap(6)
              .child(div().size(8).rounded(4).bg(color))
              .child(div().text_size(13).font_semibold().text_color(t.foreground).child(node.id))
          )
          .child(div().text_size(11).text_color(color).child(STATE_BADGE(node.state, node.live_status)))
      )
      .child(
        h_flex()
          .gap(8)
          .text_size(11)
          .text_color(t.muted)
          .child(node.executor || "herdr-agent")
          .when(node.attempt > 0, (el) => el.child(`· a${node.attempt}`))
          .when(node.deps?.length > 0, (el) => el.child(`← ${node.deps.join(", ")}`))
      )
      .when(Boolean(node.reason), (el) =>
        el.child(div().text_size(11).text_color(t.blocked).child(String(node.reason).slice(0, 90)))
      )
      .when(hasTerm, (el) =>
        el.child(
          h_flex()
            .gap(6)
            .pt(2)
            .child(this.btn(t, `focus-${node.id}`, "Focus", () => this.focusTerminal(node)))
            .child(this.btn(t, `peek-${node.id}`, "Peek", () => this.peekTerminal(node)))
        )
      );
  }


  renderWaveConnectors(t, nextLayer, nodesById, selectedId) {
    const edges = [];
    for (const nid of nextLayer) {
      const node = nodesById.get(nid);
      for (const dep of node?.deps || []) {
        edges.push({ from: dep, to: nid, fromState: nodesById.get(dep)?.state || "planned" });
      }
    }
    if (!edges.length) return null;
    return v_flex()
      .pl(12)
      .gap(4)
      .child(div().w(2).h(8).bg(t.border).ml(6))
      .child(
        h_flex()
          .gap(6)
          .flex_wrap()
          .children(
            edges.map((e, i) => {
              const activeEdge = selectedId && (e.from === selectedId || e.to === selectedId);
              const edgeColor = activeEdge
                ? t.accent
                : e.fromState === "succeeded"
                  ? t.done
                  : t.muted;
              return h_flex()
                .id(`edge-${e.from}-${e.to}-${i}`)
                .px(7)
                .py(2)
                .rounded(4)
                .bg(t.surface)
                .border_1()
                .border_color(activeEdge ? t.accent : t.border)
                .text_size(11)
                .text_color(edgeColor)
                .child(`${e.from}  →  ${e.to}`);
            })
          )
      )
      .child(div().w(2).h(8).bg(t.border).ml(6));
  }
  renderDetails(t, active, node, cx) {
    if (!node) return null;
    const color = STATE_COLOR(t, node.state, node.live_status);
    return v_flex()
      .w(300)
      .h_full()
      .p(12)
      .gap(10)
      .bg(t.chrome)
      .border_l(1)
      .border_color(t.border)
      .child(
        h_flex()
          .items_center()
          .justify_between()
          .child(div().text_size(14).font_semibold().text_color(t.foreground).child(node.id))
          .child(div().text_size(11).text_color(color).child(STATE_BADGE(node.state, node.live_status)))
      )
      .child(div().text_size(11.5).text_color(t.muted).child(`Executor: ${node.executor} · Wave ${node.wave + 1} · Attempt ${node.attempt}`))
      .when(Boolean(node.worker_id), (el) =>
        el.child(div().text_size(11).text_color(t.muted).child(`Worker: ${node.worker_id}`))
      )
      .when(Boolean(node.terminal), (el) =>
        el.child(div().text_size(11).text_color(t.muted).child(`Terminal: ${node.terminal}`))
      )
      .when(node.deps?.length > 0, (el) =>
        el.child(div().text_size(11.5).text_color(t.muted).child(`Depends on: ${node.deps.join(", ")}`))
      )
      .when(node.owns?.length > 0, (el) =>
        el.child(div().text_size(11.5).text_color(t.muted).child(`Owns: ${node.owns.join(", ")}`))
      )
      .when(Boolean(node.reason), (el) =>
        el.child(div().p(8).rounded(6).bg(t.surface).text_size(11.5).text_color(t.blocked).child(String(node.reason)))
      )
      .when(Boolean(node.question), (el) =>
        el.child(div().p(8).rounded(6).bg(t.surface).text_size(11.5).text_color(t.foreground).child(`Question: ${node.question}`))
      )
      .child(
        h_flex()
          .gap(6)
          .flex_wrap()
          .when(Boolean(node.terminal || node.worker_id), (el) =>
            el
              .child(this.btn(t, "detail-focus", "Focus Worker", () => this.focusTerminal(node), true))
              .child(this.btn(t, "detail-peek", "Peek", () => this.peekTerminal(node)))
          )
          .when(node.state === "running", (el) =>
            el.child(this.btn(t, "detail-finish", "Finish Node", () => this.runHoAction(`finish ${node.id}`, ["finish", node.id], cx)))
          )
          .when(Boolean(node.promotable), (el) =>
            el.child(this.btn(t, "detail-promote", "Promote", () => this.runHoAction(`promote ${node.id}`, ["promote", node.id], cx), true))
          )
      )
      .when((active.notes?.length || 0) > 0, (el) =>
        el.child(
          v_flex()
            .pt(8)
            .gap(4)
            .border_t(1)
            .border_color(t.border)
            .child(div().text_size(11).font_semibold().text_color(t.muted).child("Recent Notes"))
            .children(
              active.notes.slice(-4).map((n, i) =>
                div()
                  .id(`note-${i}`)
                  .text_size(11)
                  .text_color(t.muted)
                  .child(`[${n.kind}] ${String(n.body || "").slice(0, 100)}`)
              )
            )
        )
      );
  }

  render(_window, cx) {
    const t = theme();
    const active = this.data?.active;
    const runs = this.data?.runs || [];
    const nodesById = new Map((active?.nodes || []).map((n) => [n.id, n]));
    const selected = active?.nodes?.find((n) => n.id === this.selectedNode) || null;
    const waves = active?.waves || [];

    return v_flex()
      .size_full()
      .bg(t.canvas)
      .text_color(t.foreground)
      .child(
        h_flex()
          .px(14)
          .py(10)
          .gap(10)
          .items_center()
          .justify_between()
          .bg(t.chrome)
          .border_b(1)
          .border_color(t.border)
          .child(
            h_flex()
              .items_center()
              .gap(10)
              .child(div().text_size(14).font_semibold().child(active ? `Graph · ${active.job}` : "Herdr Orchestrator"))
              .when(Boolean(active?.phase), (el) =>
                el.child(div().px(8).py(2).rounded(4).bg(t.surface).text_size(11.5).text_color(t.accent).child(`phase: ${active.phase}`))
              )
              .when(Boolean(active?.controller?.epoch), (el) =>
                el.child(div().text_size(11.5).text_color(t.muted).child(`epoch ${active.controller.epoch}`))
              )
          )
          .child(
            h_flex()
              .gap(6)
              .child(this.btn(t, "btn-refresh", "Refresh", (_e, cx) => this.refresh(cx)))
              .when(Boolean(active?.run), (el) =>
                el
                  .child(this.btn(t, "btn-reconcile", "Reconcile", () => this.runHoAction("next --reconcile", ["next", "--reconcile"], cx)))
                  .child(this.btn(t, "btn-retire", "Retire Done", () => this.runHoAction("retire --all", ["retire", "--all"], cx)))
              )
          )
      )
      .when(runs.length > 1, (el) =>
        el.child(
          h_flex()
            .px(14)
            .py(6)
            .gap(6)
            .bg(t.surface)
            .border_b(1)
            .border_color(t.border)
            .child(div().text_size(11).text_color(t.muted).child("Runs:"))
            .children(
              runs.slice(0, 6).map((r) =>
                div()
                  .id(`run-${r.run_id}`)
                  .px(8)
                  .py(3)
                  .rounded(4)
                  .text_size(11.5)
                  .cursor_pointer()
                  .bg(active?.run === r.run_dir ? t.selected : t.raised)
                  .text_color(active?.run === r.run_dir ? t.foreground : t.muted)
                  .on_click((_e, cx) => {
                    this.selectedRun = r.run_dir;
                    this.selectedNode = null;
                    this.refresh(cx);
                  })
                  .child(r.job || r.run_id)
              )
            )
        )
      )
      .when(Boolean(this.error), (el) =>
        el.child(div().px(14).py(8).bg(t.surface).text_size(12).text_color(t.blocked).child(this.error))
      )
      .when(Boolean(this.actionLog), (el) =>
        el.child(div().px(14).py(6).bg(t.surface).border_b(1).border_color(t.border).text_size(11.5).text_color(t.muted).child(this.actionLog))
      )
      .child(
        h_flex()
          .flex_1()
          .min_h(0)
          .items_start()
          .child(
            v_flex()
              .flex_1()
              .h_full()
              .p(14)
              .gap(12)
              .when(!active, (el) =>
                el.child(div().text_size(13).text_color(t.muted).child("No active orchestrator run found. Run `ho init --job <name>` to start one."))
              )
              .when(Boolean(active?.summary), (el) =>
                el.child(div().text_size(12.5).text_color(t.muted).child(active.summary))
              )
              .when(Boolean(active) && waves.length === 0, (el) =>
                el.child(div().text_size(12.5).text_color(t.muted).child("Run initialized. Accept a graph with `ho accept --graph <file.json>` to populate waves."))
              )
              .children(
                waves.map((layer, idx) => {
                  const nextLayer = waves[idx + 1] || [];
                  return v_flex()
                    .id(`wave-${idx}`)
                    .gap(8)
                    .child(div().text_size(11.5).font_semibold().text_color(t.muted).child(`WAVE ${idx + 1}`))
                    .child(
                      h_flex()
                        .gap(10)
                        .flex_wrap()
                        .children(
                          layer
                            .map((nid) => nodesById.get(nid))
                            .filter(Boolean)
                            .map((node) => {
                              let relation = null;
                              if (selected) {
                                if ((selected.deps || []).includes(node.id)) relation = "upstream";
                                else if ((node.deps || []).includes(selected.id)) relation = "downstream";
                              }
                              return this.renderNodeCard(t, node, node.id === this.selectedNode, relation, cx);
                            })
                        )
                    )
                    .when(nextLayer.length > 0, (el) => {
                      const conn = this.renderWaveConnectors(t, nextLayer, nodesById, this.selectedNode);
                      return conn ? el.child(conn) : el;
                    });
                })
              )
          )
          .when(Boolean(active && selected), (el) => el.child(this.renderDetails(t, active, selected, cx)))
      );
  }
}
