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
./run.sh install        # run under launchd: start at login, restart on crash
./run.sh uninstall      # remove the launchd agent
```

Or directly: `python3 server.py --port 9090 --open`

## What you see

Per host column (MacBook / hermes / acer):

| Section | Contents |
|---|---|
| Fleet strip (top) | health pill, hosts up, failed services, alert count, **home internet** pill (click for outage history), refresh countdown |
| Issues | deduplicated alerts + unreachable/stale hosts, collection errors, failed services, unhealthy containers |
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
| control | per-host action drawer with grouped probes, **read-only exec**, service actions, confirmations, and retained results |

Plus: All/Attention and host filters, global search across every section,
remembered density/collapse/sort/auto-refresh preferences, sortable tables,
alerts + audit drawers, and a live **log-tail drawer**. The responsive mobile
layout shows one selected host at a time while keeping the global issue queue.

Auto-refreshes every 15 s (toggleable and paused while the page is hidden);
manual refresh forces a fresh fleet collection. Alerts are evaluated on every
collection and sent to configured webhooks (see Notifications).

## API

| Route | Purpose |
|---|---|
| `GET /api/status` | full fleet status + summary + alerts (cached, background refresh) |
| `GET /api/status?force=1` | force a fresh collection |
| `GET /api/config` | hosts + actions + exec metadata (auth/webhook secrets omitted) |
| `GET /api/metrics?host=&metric=&limit=` | time-series (load1/5/15, mem_used_gb, disk_use_pct, …) |
| `GET /api/alerts` | active alerts |
| `GET /api/health` | liveness + seconds since the last completed collection (used by `healthcheck.py`) |
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
`phanvincent` — no other privilege escalation.

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
    { "id": "host-offline", "type": "offline", "hosts": ["hermes"],    "severity": "critical" },
    { "id": "svc-failed",   "type": "service", "state": "failed",      "severity": "warning" },
    { "id": "disk-high",    "type": "disk",    "threshold": 90,        "severity": "critical" },
    { "id": "mem-high",     "type": "mem",     "threshold": 90,        "severity": "warning" },
    { "id": "load-high",    "type": "load",    "threshold": null,      "severity": "warning" }
  ],
  "notify": { "min_active_seconds": 120, "state_file": "data/alert_state.json" },
  "webhooks": [ { "id": "discord", "type": "discord",
                  "url_file": "~/.config/command-center/discord-webhook",
                  "severities": ["critical"], "rules": ["pokemon-quiet"] } ]
},
"healthcheck": { "interval_seconds": 120, "fail_threshold": 2, "stale_after_seconds": 300 },
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

Alert `type`: `host` (unreachable), `offline` (answers over Tailscale but its
probe can't reach the internet or resolve DNS, so its jobs and its own alerts
fail), `service` (a systemd state like `failed`), `disk`/`mem` (percent ≥
threshold), `load` (load1 ≥ threshold, or ≥ cores when null). Any rule can take
`"hosts": [...]` to apply only to those hosts. `webhooks` support `discord`,
`slack`, and generic (ntfy) bodies.

### Notifications

A webhook receives every alert unless it lists `severities` and/or `rules`, in
which case it gets alerts matching either. Keep the URL out of git with
`url_file` (a one-line file, `chmod 600`); `url` also works.

Each alert (host + rule + unit/source) notifies **once** after it has been
active for `notify.min_active_seconds`, and sends one **RESOLVED** message once
it has been gone that long. Delivery state is saved to `notify.state_file`, so
restarts don't re-send; failed sends retry after a minute. Alerts on an
unreachable host are held until the host answers rather than reported as
resolved.

`./run.sh install` also installs `healthcheck.py` as a second launchd job. It
polls `/api/health` every `healthcheck.interval_seconds` and, after
`fail_threshold` failures in a row (no answer, or no completed collection for
`stale_after_seconds`), sends one critical "dashboard is down" message to the
webhooks, then a RESOLVED message when it answers again. `./run.sh stop`
unloads both jobs, so a deliberate stop doesn't notify.

Discord setup: Server Settings → Integrations → Webhooks → New Webhook, copy
the URL, then:

```bash
mkdir -p ~/.config/command-center && umask 077 && pbpaste > ~/.config/command-center/discord-webhook
python3 healthcheck.py --test   # sends a test message to every webhook
```

The dashboard runs only while you're logged in (it's a LaunchAgent), so it
starts after a reboot at login, not before.

### Home internet

Every collection, hermes and acer (both on the home LAN) and this MacBook
TCP-connect to 1.1.1.1, 8.8.8.8 and 208.67.222.222 on :443 (`probe.wan_check`).
`home_internet.py` turns that into `up`, `down` (both home hosts report no
internet), `unreachable` (no home host answers but the MacBook is online, as
when you're away), or `unknown`. Runs of down/unreachable are logged as outages
in `data/home_internet.json`. Unconfirmed blips under `min_unconfirmed_seconds`
are dropped. So are unconfirmed outages that span a gap of more than
`max_sample_gap_seconds` between collections: that gap means the MacBook slept,
and its brief background wakes collect before Tailscale reconnects. When an outage of at least `notify_min_minutes` ends, one summary
goes to webhooks matching rule `home-internet`. It's sent after the outage
because nothing can be delivered while the home connection is down. Outages are
only seen while this MacBook is running. `status.home_internet` carries the
state and the last 20 outages.

### Off-site heartbeat (hermes)

Everything above runs on this MacBook, so nothing alerts while it's asleep or
on the home network during an outage. `setup/hermes-heartbeat.{service,timer}`
covers hermes from outside: every 5 minutes hermes POSTs its `uptime` to its
own healthchecks.io check, which alerts through the account's integrations when
pings stop. A ping needs DNS and internet, so a silent check means hermes is
down or offline. The ping URL lives only on hermes in
`~/.config/hermes-heartbeat/ping-url` (mode 600); until it exists the service is
skipped. It matches the dashboard's unit filter, so a failed ping shows up as
`svc-failed`. (Bezos tracker's healthchecks.io check can't serve as this: it
pings `/fail` whenever that job is degraded.)

```bash
scp setup/hermes-heartbeat.service setup/hermes-heartbeat.timer hermes:.config/systemd/user/
ssh hermes 'systemctl --user daemon-reload && systemctl --user enable --now hermes-heartbeat.timer'
```

In healthchecks.io the check uses period 5 min and grace 10 min.

## Files

```
config.json      fleet definition (hosts, actions, exec, alerts, server)
probe.py         remote status probe (piped over ssh, stdlib only)
collector.py     local macOS facts + ssh orchestration → JSON
metrics.py       sqlite time-series store (data/metrics.db)
alerts.py        rule engine + notification lifecycle (dedupe, resolve, persistence)
notify.py        webhook delivery shared by alerts.py and healthcheck.py
home_internet.py home WAN state + outage log (data/home_internet.json)
healthcheck.py   independent launchd job: notifies when the dashboard is down
control.py       allowlisted action/exec execution + redacted audit log
server.py        stdlib HTTP server + JSON API
web/             dashboard UI (index.html, app.js, style.css)
tests/           unittest suite + live smoke test
run.sh           start/stop/status/install/open/test/health helper
setup/           one-time host setup (polkit reboot rule, no sudo)
plugins/         Claude Code plugins: deepseek-delegate (/delegate → DeepSeek via Reasonix),
                 reasonix-orchestrator (Mac stdio MCP server; also a Codex plugin)
.claude-plugin/  marketplace manifest for the plugins above
audit.jsonl      control audit trail (created at runtime)
data/            metrics.db (created at runtime)
logs/            server log + pidfile (created at runtime)
```

See `INVENTORY.md` for the full three-system inventory this dashboard was
scaffolded from.

## Claude Code plugin: deepseek-delegate

`plugins/deepseek-delegate/` makes Claude plan and review while DeepSeek
(via the Reasonix CLI) writes the code. Each task is verified, committed on its
own and cost-logged. See [its README](plugins/deepseek-delegate/README.md) for
setup (`/plugin marketplace add phan-vincent/command-center-public`) and a cost-check
example. `plugins/reasonix-orchestrator/` is the Mac-local Reasonix MCP server.
It installs from the same marketplace for Claude Code, and it's also the Codex
plugin that `./orchestrator` uses.
