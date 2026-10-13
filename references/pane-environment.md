# Pane environment

Each worker pane is an isolated process tree sharing the host. Files, ports, databases, brokers, and queues on that host are shared unless a pane is given its own bind address, namespace, prefix, or database. Treat isolation as something you configure, not something the pane provides.

Controller launch and worker identity contracts are in [identity-reattach.md](identity-reattach.md); the [controller playbook](controller-playbook.md#4-environment-rules-that-came-from-incidents) gives historical environment examples. These conventions do not change exact-holder or run-lock authorization.

## Per-pane ports

Pick a distinct port per pane. Probe before binding:

```
nc -z 127.0.0.1 PORT
```

Exit 0 means taken. macOS `launchd` holds some ports even when nothing appears listening (for example 8021), so probe rather than assume. Pass the port through env or CLI, record it in the dispatch prompt, and never hardcode a shared default.

First free port in a range:

```bash
for p in $(seq 8100 8199); do
  nc -z 127.0.0.1 "$p" 2>/dev/null && continue
  echo "$p"
  break
done
```

## Shared infrastructure vs isolated processes

Shared: databases, brokers, queues, and caches that every pane can reach.

Isolated: every process a pane starts — dev servers, watchers, workers.

Never start a shared-queue worker or consumer from a pane. A worker attached to a shared queue steals or reprocesses other panes' jobs. Run consumers only from the controller or a dedicated pane. Give each pane its own namespace, prefix, or database when isolation is needed.

## Checklist

- Probe `nc -z 127.0.0.1 PORT` before bind; do not assume a default is free.
- Pass the chosen port via env or CLI; record it in the dispatch prompt.
- Never hardcode a shared default port across panes.
- Start only isolated processes in a worker pane (dev server, watcher).
- Never start a shared-queue worker or consumer from a worker pane.
- Give each pane its own namespace, prefix, or database when isolation is needed.
- Run shared consumers from the controller or one dedicated pane only.
