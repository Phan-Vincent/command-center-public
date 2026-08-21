# Command Center

A unified view + control surface for the three-machine fleet, hosted on **this
MacBook** (the primary dashboard intentionally lives here — not on hermes or
acer). Pure Python 3 standard library backend + vanilla-JS frontend — **zero
dependencies, no build step**.

```
MacBook (this machine)  ──┐
                         ├──►  Command Center (127.0.0.1:9090)
hermes (peeruser-Dell)  ─────┤        │
                         │        ├── collector.py  ── ssh ──► probe.py (remote)
acer (vphomelab)  ────────┘        ├── metrics.py    ── sqlite time-series
                                   ├── alerts.py     ── rules + webhooks
                                   ├── control.py    ── allowlisted actions + exec
                                   └── web/          ── vanilla-JS dashboard
```

## Quickstart

```bash
cd ~/command-center
./run.sh start          # http://127.0.0.1:9090  (logs/server.log)
./run.sh open           # open in browser
./run.sh stop
./run.sh test           # run the unittest suite
./run.sh health         # is the server responding?
```

Or directly: `python3 server.py --port 9090 --open`

## What you see

Per host column (MacBook / hermes / acer):

| Section | Contents |
|---|---|
| Fleet strip (top) | health pill (ok/degraded/down/critical), hosts up, failed services, alert count |
| System | uptime, load, CPU, memory + disk bars, **sparklines** for load/mem/disk history |
| systemd user units | active/failed chips, sortable, per-unit **log** button |
| docker containers | healthy states, **live CPU/mem%**, per-container **log** button |
| top processes | top-by-CPU processes |
| agents | live agents with ports |
| hermes cron jobs | schedule, enabled/paused, last status + system crontab |
| listening ports | addr → process |
| restic backups | latest snapshot per repo (best-effort) |
| projects | git branch/commit/dirty + deployment notes |
| links | reachable web UIs (immich, wekan, pihole, ngrok URLs) |
| control | allowlisted action buttons + **read-only exec console** |

Plus: global search filter, collapsible sections, sortable tables, alerts
banner + drawer, control audit-log drawer, and a live **log-tail drawer**.

Auto-refreshes every 15 s (toggleable). Alerts are evaluated on every
collection and dispatched to configured webhooks on first fire.

## API

| Route | Purpose |
|---|---|
| `GET /api/status` | full fleet status + summary + alerts (cached, background refresh) |
| `GET /api/status?force=1` | force a fresh collection |
| `GET /api/config` | hosts + actions + exec metadata |
| `GET /api/metrics?host=&metric=&limit=` | time-series (load1/5/15, mem_used_gb, disk_use_pct, …) |
| `GET /api/alerts` | active alerts |
| `GET /api/logs?host=&type=unit|container&target=&tail=` | tail journalctl / docker logs (whitelisted) |
| `POST /api/control` | `{host, action_id}` → run allowlisted action |
| `POST /api/exec` | `{host, exec_id, arg}` → run allowlisted read-only command |
| `GET /api/audit` | recent control/exec audit entries |

## Control surface & safety

* Every control action and exec command must be declared in `config.json`
  (`hosts.<h>.actions` and `hosts.<h>.exec`). **No free-form command execution** —
  unknown ids are rejected; exec `arg` is validated against a live whitelist of
  unit/container names plus a strict character class.
* Commands run over ssh **as the SSH user, without sudo**.
* Control actions are serialized per host (no double-restarts) and each is
  confirmed in the UI and appended to `audit.jsonl` — which is **redacted** for
  secrets and **rotated** past 1 MB.
* Server binds `127.0.0.1` by default. Set `server.auth_token` to require an
  `X-Auth-Token` header on control/exec/audit/logs; the UI prompts for it. A
  loud warning is printed if you bind a non-loopback address without a token.
* Collection runs in parallel per host with a copy-on-write cache (readers are
  never blocked) and last-known-good fallback (a transient ssh blip shows the
  previous snapshot, marked `stale`, instead of blanking the column).

## Reboot (hermes / acer)

The `hermes-reboot` / `acer-reboot` actions run `systemctl reboot` **as the SSH
user, without sudo**. Because a remote (SSH) session is *not* trusted by polkit,
reboot needs a **one-time** polkit rule on each host so the SSH user is allowed
to reboot without an interactive password:

```bash
# run ON hermes and ON acer (from this repo):
sudo install -m 0644 setup/50-command-center-reboot.rules /etc/polkit-1/rules.d/
```

polkitd reloads `rules.d` automatically. The rule grants *only* reboot
(`org.freedesktop.login1.reboot[-multiple-sessions]`) to `peeruser` and
`phanvincent2` — no other privilege escalation.

A reboot kills ssh before the command can return, so these actions carry
`"disconnect_ok": true`: `control.py` treats ssh's "connection dropped" exit
(rc 255) as success and reports "reboot initiated" rather than a failure.

After reboot both hosts come back unattended: hermes auto-logs-in via GDM
(`AutomaticLogin=peeruser`), and acer uses `loginctl enable-linger` so its user
services (hermes-gateway, cron, …) start headlessly. Neither has disk
encryption, so no boot-time passphrase is required.

## Config (config.json)

```jsonc
"server": { "host": "127.0.0.1", "port": 9090, "cache_ttl_seconds": 15,
            "collect_workers": 3, "metrics_retention_hours": 24,
            "audit_max_bytes": 1000000, "auth_token": null },
"alerts": {
  "rules": [
    { "id": "host-down",    "type": "host",    "state": "unreachable", "severity": "critical" },
    { "id": "svc-failed",   "type": "service", "state": "failed",      "severity": "warning" },
    { "id": "disk-high",    "type": "disk",    "threshold": 90,        "severity": "critical" },
    { "id": "mem-high",     "type": "mem",     "threshold": 90,        "severity": "warning" },
    { "id": "load-high",    "type": "load",    "threshold": null,      "severity": "warning" }
  ],
  "webhooks": [ { "url": "https://discord.com/api/webhooks/...", "type": "discord" } ]
},
"hosts": {
  "myhost": {
    "label": "myhost", "role": "homelab", "connect": "ssh",
    "projects": [ { "name": "repo", "path": "~/repo", "note": "" } ],
    "agents":   [ { "name": "svc", "match": "svc", "note": "" } ],
    "links":    [ { "label": "UI", "url": "http://…", "reach": "tailscale" } ],
    "actions":  [ { "id": "restart-svc", "label": "Restart svc", "cmd": "systemctl --user restart svc" } ],
    "exec":     [ { "id": "unit-log", "label": "journalctl", "cmd": "journalctl --user -u {arg} -n 80 --no-pager", "arg": "unit" } ]
  }
}
```

Alert `type`: `host` (unreachable), `service` (a systemd state like `failed`),
`disk`/`mem` (percent ≥ threshold), `load` (load1 ≥ threshold, or ≥ cores when
null). `webhooks` support `discord`, `slack`, and generic (ntfy) bodies.

## Files

```
config.json      fleet definition (hosts, actions, exec, alerts, server)
probe.py         remote status probe (piped over ssh, stdlib only)
collector.py     local macOS facts + ssh orchestration → JSON
metrics.py       sqlite time-series store (data/metrics.db)
alerts.py        rule engine + webhook dispatch
control.py       allowlisted action/exec execution + redacted audit log
server.py        stdlib HTTP server + JSON API
web/             dashboard UI (index.html, app.js, style.css)
tests/           unittest suite + live smoke test
run.sh           start/stop/status/open/test/health helper
setup/           one-time host setup (polkit reboot rule, no sudo)
audit.jsonl      control audit trail (created at runtime)
data/            metrics.db (created at runtime)
logs/            server log + pidfile (created at runtime)
```

See `INVENTORY.md` for the full three-system inventory this dashboard was
scaffolded from.
