#!/usr/bin/env python3
"""
probe.py — remote host status collector for the Command Center.

Piped to a remote Linux host over SSH:  ssh <host> 'python3 -' < probe.py
Self-contained (stdlib only), emits ONE JSON object on stdout. All diagnostics
go to stderr so stdout stays parseable. Nothing is written on the remote host
(other than transient /tmp files it cleans up).

Auto-discovers: host facts, load/mem/disk, tailscale IP, internet reachability, systemd user units
(services+timers filtered to fleet-relevant names), docker containers,
listening ports, key agent processes, hermes cron jobs, system crontab,
git projects, and a few known extra paths per host.
"""

import concurrent.futures
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

HOSTNAME = subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip() or "unknown"

# Extra paths worth surfacing per host (label -> path). Kept in sync with config.json.
HOST_EXTRAS = {
    "peeruser-Dell": {
        "hermes scripts": "~/.hermes/scripts",
        "cyber-intern-pipeline": "~/cyber-intern-pipeline",
        "broker_automation": "~/.broker_automation",
        "pihole": "~/pihole",
    },
    "vphomelab": {
        "hermes profile": "~/.hermes",
        "openclaw workspace": "~/workspace",
        "immich": "~/immich",
        "pihole": "~/pihole",
        "nebula-sync": "~/nebula-sync",
        "restic-repo": "~/restic-repo",
    },
}

UNIT_FILTER = re.compile(
    r"(hermes|ngrok|discord|bezos|kimi|restic|kalshi|crypto|openclaw)", re.IGNORECASE
)

PROC_MATCHES = [
    "hermes_cli.main gateway",
    "discord_interaction_handler",
    "ngrok",
    "openclaw",
    "codex",
    "kimi-webbridge",
    "ollama",
]


def sh(cmd, timeout=15, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           shell=True, cwd=cwd, executable="/bin/bash")
        return r.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return f"<err: {e}>"


def uptime_secs():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def loadavg():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            return [float(x) for x in parts[:3]]
    except Exception:
        return None


def meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                out[k.strip()] = int(v.strip().split()[0])  # kB
        total = out.get("MemTotal", 0) / 1024 / 1024
        avail = out.get("MemAvailable", out.get("MemFree", 0)) / 1024 / 1024
        return {"total_gb": round(total, 1), "avail_gb": round(avail, 1),
                "used_gb": round(total - avail, 1)}
    except Exception:
        return None


def disk():
    out = sh("df -kP 2>/dev/null | tail -n +2")
    disks = []
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 6 and p[0].startswith("/dev/"):
            disks.append({
                "mount": p[5], "size_gb": round(int(p[1]) / 1024 / 1024, 1),
                "used_gb": round(int(p[2]) / 1024 / 1024, 1),
                "use_pct": p[4].rstrip("%"),
            })
    disks.sort(key=lambda d: 0 if d["mount"] == "/" else 1)
    return disks or [{"mount": "/", "size_gb": 0, "used_gb": 0, "use_pct": "0"}]


def tailscale_ip():
    # tailscale0 interface is authoritative without needing the CLI.
    out = sh("ip -4 -o addr show tailscale0 2>/dev/null | awk '{print $4}'")
    m = re.search(r"([\d.]+)/", out)
    return m.group(1) if m else None


# Public anycast resolvers on :443 from three providers. Raw IPs, so a DNS fault
# isn't read as an outage. (Quad9 :443 is unreachable from the home network.)
WAN_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 443), ("208.67.222.222", 443))


def wan_check(targets=WAN_TARGETS, timeout=3):
    """Can this host reach the internet? Up if any target accepts a TCP connect."""
    def attempt(target):
        start = time.monotonic()
        try:
            with socket.create_connection(target, timeout=timeout):
                return round((time.monotonic() - start) * 1000)
        except OSError:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as ex:
        rtts = [r for r in ex.map(attempt, targets) if r is not None]
    return {"ok": bool(rtts), "rtt_ms": min(rtts) if rtts else None,
            "reached": len(rtts), "tried": len(targets)}


# Names the fleet's own jobs depend on. Resolved through the host's normal
# resolver, so a broken resolver shows up here even while wan_check passes.
DNS_NAMES = ("discord.com", "api.github.com", "stream.aisstream.io")


def dns_check(names=DNS_NAMES, timeout=3):
    """Can this host resolve names? Up if any name resolves within the timeout."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(names))
    futures = [ex.submit(socket.getaddrinfo, name, 443, proto=socket.IPPROTO_TCP) for name in names]
    done, _ = concurrent.futures.wait(futures, timeout=timeout)
    ex.shutdown(wait=False)  # getaddrinfo can't be cancelled; don't block on a hung lookup
    resolved = sum(1 for f in done if f.exception() is None)
    return {"ok": resolved > 0, "resolved": resolved, "tried": len(names)}


MODEM_IP = "192.168.100.1"  # cable modems answer here even when their line is down


def default_gateway():
    m = re.search(r"default via ([\d.]+)", sh("ip -4 route show default 2>/dev/null"))
    return m.group(1) if m else None


def ping(ip, timeout=1):
    """One ICMP echo; rtt in ms, or None if no reply."""
    try:
        r = subprocess.run(["ping", "-n", "-c", "1", "-W", str(timeout), ip],
                           capture_output=True, text=True, timeout=timeout + 2)
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"time=([\d.]+)", r.stdout)
    return round(float(m.group(1)), 1) if r.returncode == 0 and m else None


def path_check():
    """Which hop on the way out answers: the home router, then the cable modem.

    During an outage this tells a hung router (no reply) from a dead modem
    link (router replies, modem doesn't) from a Spectrum-side fault (both reply).
    """
    hops = {"router": default_gateway(), "modem": MODEM_IP}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(hops)) as ex:
        rtts = dict(zip(hops, ex.map(lambda ip: ping(ip) if ip else None, hops.values())))
    return {name: {"ip": ip, "ok": rtts[name] is not None, "rtt_ms": rtts[name]}
            for name, ip in hops.items()}


def systemd_units():
    units = []
    out = sh("systemctl --user list-units --all --no-legend --type=service,timer 2>/dev/null")
    for raw in out.splitlines():
        line = raw.strip()
        # Failed units are prefixed with a bullet glyph ("● name.service ...").
        if line.startswith("\u25cf"):
            line = line[1:].lstrip()
        p = line.split(None, 4)
        if len(p) < 4:
            continue
        name, load, active, sub = p[0], p[1], p[2], p[3]
        desc = p[4].strip() if len(p) > 4 else ""
        if UNIT_FILTER.search(name):
            units.append({"name": name, "load": load, "state": active, "sub": sub, "desc": desc})
    units.sort(key=lambda u: u["name"])
    return units


# acer publishes container states as root, so its user needs no docker group (homelab-ansible,
# security baseline F8). Older than this, the panel shows "unavailable" rather than stale data.
CONTAINER_STATUS_PATH = "/run/homelab/container-status.json"
CONTAINER_STATUS_MAX_AGE = 15 * 60


def published_containers():
    """Containers from homelab-container-status, or None when absent, stale or malformed."""
    try:
        with open(CONTAINER_STATUS_PATH) as f:
            doc = json.load(f)
        if time.time() - doc["generated_at"] > CONTAINER_STATUS_MAX_AGE:
            return None
        return [{"name": c["name"], "image": c.get("image", ""), "status": c.get("status", ""),
                 "ports": c.get("ports", "")} for c in doc["containers"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def docker_containers():
    out = sh("docker ps -a --format '{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}' 2>/dev/null")
    if not out or "<err:" in out or "permission denied" in out.lower():
        return published_containers()  # docker absent or not usable by this user
    conts = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            conts.append({"name": parts[0], "image": parts[1], "status": parts[2], "ports": parts[3]})
    return conts


def docker_stats():
    out = sh("docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' 2>/dev/null")
    if not out or "<err:" in out:
        return None
    stats = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            stats.append({"name": parts[0], "cpu": parts[1], "mem": parts[2], "mem_pct": parts[3]})
    return stats


def top_procs():
    out = sh("ps -eo pid,pcpu,pmem,comm --sort=-pcpu 2>/dev/null | head -9")
    procs = []
    for line in out.splitlines()[1:]:
        p = line.split(None, 3)
        if len(p) == 4:
            procs.append({"pid": p[0], "cpu": p[1], "mem": p[2], "comm": p[3][:40]})
    return procs


def restic_ages():
    """Best-effort: latest snapshot time per restic repo, when available."""
    restic = sh("command -v restic 2>/dev/null")
    if not restic or "<err:" in restic:
        return None
    pw = None
    for cand in ("~/.config/restic/password", "~/.config/restic/restic-password"):
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            pw = p
            break
    if not pw:
        return None
    base = os.path.expanduser("~/restic-repo")
    if not os.path.isdir(base):
        return None
    repos = []
    for d in sorted(os.listdir(base)):
        rp = os.path.join(base, d)
        if not os.path.isdir(rp):
            continue
        out = sh(f"RESTIC_PASSWORD_FILE={pw} {restic} -r {sh_quote(rp)} snapshots --latest 1 --json 2>/dev/null")
        latest = None
        try:
            data = json.loads(out)
            snaps = data if isinstance(data, list) else []
            if snaps:
                latest = snaps[0].get("time")
        except Exception:
            pass
        repos.append({"repo": d, "latest": latest})
    return repos or None


def ports():
    out = sh("ss -tlnp 2>/dev/null")
    result = []
    for line in out.splitlines():
        if not line.startswith("LISTEN"):
            continue
        m = re.match(r"\S+\s+\S+\s+\S+\s+(\S+):(\d+)\s+(\S+)", line)
        if not m:
            continue
        addr, port = m.group(1), m.group(2)
        proc = "?"
        marker = 'users:(("'
        if marker in line:
            proc = line.split(marker, 1)[1].split('"', 1)[0]
        result.append({"addr": f"{addr}:{port}", "proc": proc})
    return result


def processes():
    procs = []
    for name in PROC_MATCHES:
        # Bracket trick: "[n]ame" won't match this pgrep's own bash wrapper cmdline.
        pat = "[" + name[0] + "]" + name[1:]
        out = sh(f"pgrep -fl '{pat}' 2>/dev/null | head -4")
        for line in out.splitlines():
            parts = line.split(None, 1)
            if parts:
                procs.append({"name": name, "pid": parts[0],
                              "cmd": parts[1][:160] if len(parts) > 1 else ""})
    return procs


def hermes_cron_jobs():
    path = os.path.expanduser("~/.hermes/cron/jobs.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        jobs = data if isinstance(data, list) else data.get("jobs", [])
        out = []
        for j in jobs:
            sched = j.get("schedule") or {}
            out.append({
                "id": j.get("id"),
                "name": j.get("name"),
                "schedule": sched.get("display") or j.get("schedule_display"),
                "enabled": bool(j.get("enabled")),
                "state": j.get("state"),
                "last_status": j.get("last_status"),
                "last_error": (j.get("last_error") or "")[:120],
                "next_run_at": j.get("next_run_at"),
            })
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def crontab():
    out = sh("crontab -l 2>/dev/null | grep -vE '^\\s*(#|$)'")
    return [l.strip() for l in out.splitlines() if l.strip()]


def projects():
    result = []
    roots = [os.path.expanduser("~/projects")]
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            d = os.path.join(root, entry)
            if os.path.isdir(os.path.join(d, ".git")) and d not in seen:
                seen.add(d)
                info = git_info(d)
                result.append({"name": entry, "path": d, **info})
    return result


def git_info(d):
    q = sh_quote(d)
    branch = sh(f"git -C {q} branch --show-current 2>/dev/null")
    short = sh(f"git -C {q} log -1 --format='%h %s' 2>/dev/null")
    dirty = sh(f"git -C {q} status --porcelain 2>/dev/null | wc -l")
    return {"branch": branch, "commit": short, "dirty": int(dirty or 0)}


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def host_extras():
    extras = HOST_EXTRAS.get(HOSTNAME, {})
    result = []
    for label, path in extras.items():
        p = os.path.expanduser(path)
        result.append({"name": label, "path": path, "exists": os.path.exists(p)})
    return result


def os_release():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return None


def _read_only_db(path):
    """Open an existing SQLite database without creating or migrating it."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _safe_tree(root):
    """Hash source/config artifacts while excluding state and credential files."""
    allowed = {".py", ".md", ".service", ".timer", ".json"}
    blocked = (".env", "auth", "token", "secret", ".db", "cache")
    result = {}
    if not os.path.isdir(root):
        return result
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", "venv"}]
        for name in files:
            lower = name.lower()
            if any(part in lower for part in blocked):
                continue
            if os.path.splitext(name)[1].lower() not in allowed:
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root)
            try:
                with open(path, "rb") as handle:
                    result[rel] = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
    return result


def _drift_counts(left, right):
    left_keys, right_keys = set(left), set(right)
    shared = left_keys & right_keys
    return {
        "identical": sum(left[key] == right[key] for key in shared),
        "divergent": sum(left[key] != right[key] for key in shared),
        "repository_only": len(left_keys - right_keys),
        "runtime_only": len(right_keys - left_keys),
    }


def _last_discord_post(conn):
    """Latest sent Discord post and how many completed runs have posted nothing since."""
    last = conn.execute(
        "SELECT MAX(sent_at) FROM discord_outbox WHERE status = 'sent'"
    ).fetchone()[0]
    runs_since = conn.execute(
        """
        SELECT COUNT(*) FROM scanner_runs
        WHERE ended_at IS NOT NULL AND dry_run = 0 AND started_at > ?
        """,
        (last or "",),
    ).fetchone()[0]
    return {"at": last, "runs_since": runs_since}


def pokemon_scanner_health():
    """Best-effort, read-only scanner health for the hermes dashboard."""
    if HOSTNAME != "peeruser-Dell":
        return None
    home = os.path.expanduser("~")
    runtime = os.path.join(home, ".hermes", "scripts")
    # Deploy source for ~/.hermes/scripts (branch fix/scanner-identity-purpose);
    # the main ~/projects/pokemon-scanner checkout is on a stale branch.
    repo = os.path.join(home, "projects", "pokemon-scanner-wt", "identity-purpose")
    status_script = os.path.join(runtime, "scanner_status.py")
    python = os.path.join(home, ".hermes", "hermes-agent", "venv", "bin", "python3")
    result = {"available": False, "errors": []}
    try:
        proc = subprocess.run(
            [python, status_script, "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip()[:200])
        raw = json.loads(proc.stdout)
        source = raw.get("source_health") or {}
        result.update({
            "available": True,
            "generated_at": raw.get("generated_at"),
            "last_run": raw.get("last_run"),
            "last_success_at": raw.get("last_success_at"),
            "next_scheduled_run": sh(
                "systemctl --user show pokemon-scanner.timer "
                "-p NextElapseUSecRealtime --value 2>/dev/null"
            ) or raw.get("next_scheduled_run"),
            "browser": raw.get("browser"),
            "ebay_auth": raw.get("ebay_auth"),
            "pending_outbox": raw.get("pending_outbox"),
            "source_health": {
                "ok": source.get("ok"),
                "detail": source.get("detail"),
                "window_hours": source.get("window_hours"),
                "sources": source.get("sources") or {},
                "latest": {
                    key: {
                        "status": value.get("status"),
                        "attempted_at": value.get("attempted_at"),
                    }
                    for key, value in (source.get("latest") or {}).items()
                },
            },
        })
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"status: {exc}")

    ops_path = os.path.join(runtime, "scanner_ops.db")
    try:
        conn = _read_only_db(ops_path)
        conn.row_factory = sqlite3.Row
        last_run = result.get("last_run") or {}
        since = last_run.get("started_at") or (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).isoformat()
        failures = conn.execute(
            """
            SELECT source, status, COUNT(*) AS count
            FROM source_attempts
            WHERE attempted_at >= ?
              AND status NOT IN ('success','cache_hit','cache','disabled','skipped','not_needed')
            GROUP BY source, status ORDER BY source, status
            """,
            (since,),
        ).fetchall()
        result["source_failures"] = [dict(row) for row in failures]
        result["last_discord_post"] = _last_discord_post(conn)
        conn.close()
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"ops: {exc}")

    paper_path = os.path.join(
        home, ".hermes", "data", "pokemon-scanner", "paper-backtest", "paper_trades.db"
    )
    try:
        conn = _read_only_db(paper_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT experiment_id, started_at, discovery_ends_at, finalizes_at,
                   status, completed_at
            FROM experiments ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        result["paper_experiment"] = dict(row) if row else None
        conn.close()
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"paper: {exc}")

    result["services"] = {
        "discord_handler": sh("systemctl --user is-active discord-interaction-handler.service 2>/dev/null"),
        "tunnel": sh("systemctl --user is-active pokemon-ngrok.service 2>/dev/null"),
    }
    try:
        result["drift"] = {
            "repository": repo,
            "repository_runtime": _drift_counts(
                _safe_tree(os.path.join(repo, "scripts")), _safe_tree(runtime)
            ),
        }
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"drift: {exc}")
    return result


def main():
    payload = {
        "host": HOSTNAME,
        "ts": subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
                             capture_output=True, text=True).stdout.strip(),
        "hostname": HOSTNAME,
        "home": os.path.expanduser("~"),
        "os": os_release(),
        "uptime_secs": uptime_secs(),
        "loadavg": loadavg(),
        "cpu": {"cores": sh("nproc 2>/dev/null"),
                "model": sh("grep -m1 'model name' /proc/cpuinfo | cut -d: -f2 | xargs")},
        "mem": meminfo(),
        "disk": disk(),
        "tailscale_ip": tailscale_ip(),
        "wan": wan_check(),
        "path": path_check(),
        "dns": dns_check(),
        "services": systemd_units(),
        "docker": docker_containers(),
        "docker_stats": docker_stats(),
        "top_procs": top_procs(),
        "ports": ports(),
        "processes": processes(),
        "hermes_cron_jobs": hermes_cron_jobs(),
        "crontab": crontab(),
        "projects": projects(),
        "extras": host_extras(),
        "restic_ages": restic_ages(),
        "pokemon_scanner": pokemon_scanner_health(),
    }
    json.dump(payload, sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
