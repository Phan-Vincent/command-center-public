#!/usr/bin/env python3
"""
collector.py — gathers fleet status for the Command Center.

Merges, per host:
  * config.json metadata (label, role, projects, agents, links, actions)
  * live facts:
      - macbook: macOS sysctl/vm_stat/df/lsof/pgrep/git
      - hermes/acer: probe.py piped over ssh

Emits a single JSON document consumed by server.py (/api/status).

Usage:
    python3 collector.py            # print full status JSON to stdout
    python3 collector.py --host hermes
"""

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from probe import dns_check, wan_check

BASE =Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
PROBE_PATH = BASE / "probe.py"

GB = 1024 * 1024 * 1024

# Per-host last-known-good facts, so a transient ssh failure shows the previous
# snapshot (marked `stale`) instead of blanking the whole column.
_LAST_GOOD = {}

# Config keys that are UI metadata, never overwritten by stale facts.
_CFG_META_KEYS = {"name", "label", "role", "connect", "os_hint", "agents", "links", "actions", "exec", "id"}

# Keys that describe the current collection attempt. They must never be cached
# as last-known-good facts: restoring `reachable`/`stale`/`errors` from the
# cache would report a failed collection as healthy with no errors.
_COLLECTION_KEYS = frozenset({"id", "as_of", "reachable", "stale", "errors"})
_CACHE_EXCLUDED_KEYS = _CFG_META_KEYS | _COLLECTION_KEYS


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def validate_config(cfg=None):
    """Return a list of config problems (empty = valid). Never raises."""
    cfg = cfg or load_config()
    problems = []
    hosts = cfg.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        problems.append("config: 'hosts' must be a non-empty object")
        return problems
    for key, h in hosts.items():
        if not h.get("label"):
            problems.append(f"hosts.{key}: missing 'label'")
        for i, a in enumerate(h.get("actions", [])):
            if not a.get("id") or not a.get("cmd"):
                problems.append(f"hosts.{key}.actions[{i}]: needs 'id' + 'cmd'")
        for i, e in enumerate(h.get("exec", [])):
            if not e.get("id") or not e.get("cmd"):
                problems.append(f"hosts.{key}.exec[{i}]: needs 'id' + 'cmd'")
            if e.get("arg") not in ("none", "unit", "container"):
                problems.append(f"hosts.{key}.exec[{i}]: 'arg' must be none|unit|container")
        for i, l in enumerate(h.get("links", [])):
            if not l.get("label") or not l.get("url"):
                problems.append(f"hosts.{key}.links[{i}]: needs 'label' + 'url'")
    for i, r in enumerate((cfg.get("alerts") or {}).get("rules", [])):
        if not r.get("type"):
            problems.append(f"alerts.rules[{i}]: missing 'type'")
    hi = cfg.get("home_internet") or {}
    hi_hosts = list(hi.get("hosts", []))
    if "local_host" in hi:
        hi_hosts.append(hi["local_host"])
    for key in hi_hosts:
        if key not in hosts:
            problems.append(f"home_internet: unknown host '{key}'")
    for i, wh in enumerate((cfg.get("alerts") or {}).get("webhooks", [])):
        if not (wh.get("url") or wh.get("url_file")):
            problems.append(f"alerts.webhooks[{i}]: needs 'url' or 'url_file'")
        if wh.get("type", "generic") not in ("discord", "slack", "generic"):
            problems.append(f"alerts.webhooks[{i}]: 'type' must be discord|slack|generic")
    return problems


# ---------------------------------------------------------------------------
# Local (macOS) collector
# ---------------------------------------------------------------------------

def _sh(cmd, timeout=10, shell=False, env=None):
    try:
        if shell:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=True, env=env)
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout.strip()
    except Exception:
        return ""


def _uptime_secs():
    out = _sh(["sysctl", "-n", "kern.boottime"])
    m = re.search(r"sec = (\d+)", out)
    if m:
        return max(0, time.time() - int(m.group(1)))
    return None


def _loadavg():
    out = _sh(["sysctl", "-n", "vm.loadavg"])
    m = re.search(r"\{ ([\d.]+) ([\d.]+) ([\d.]+) \}", out)
    if m:
        return [float(x) for x in m.groups()]
    return None


def _cpu():
    return {
        "cores": _sh(["sysctl", "-n", "hw.ncpu"]) or "?",
        "model": _sh(["sysctl", "-n", "machdep.cpu.brand_string"]) or _sh(["sysctl", "-n", "hw.model"]),
    }


def _mem():
    total_bytes = int(_sh(["sysctl", "-n", "hw.memsize"]) or 0)
    out = _sh(["vm_stat"])
    if not out or not total_bytes:
        return None
    page = 16384
    m = re.search(r"page size of (\d+) bytes", out)
    if m:
        page = int(m.group(1))
    vals = {}
    for line in out.splitlines():
        mm = re.match(r"([^:]+):\s+(\d+)\.", line)
        if mm:
            vals[mm.group(1).strip()] = int(mm.group(2))
    free_pages = vals.get("Pages free", 0) or 0
    inactive = vals.get("Pages inactive", 0) or 0
    speculative = vals.get("Pages speculative", 0) or 0
    purgeable = vals.get("Pages purgeable", 0) or 0
    # macOS "available" approximation: free + inactive + speculative (+ purgeable)
    avail_bytes = (free_pages + inactive + speculative + purgeable) * page
    return {
        "total_gb": round(total_bytes / GB, 1),
        "avail_gb": round(avail_bytes / GB, 1),
        "used_gb": round((total_bytes - avail_bytes) / GB, 1),
    }


def _disk():
    st = os.statvfs("/")
    size = st.f_blocks * st.f_frsize
    used = (st.f_blocks - st.f_bfree) * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    return [{
        "mount": "/",
        "size_gb": round(size / GB, 1),
        "used_gb": round(used / GB, 1),
        "use_pct": str(round(used / size * 100)) if size else "0",
    }]


def _ports():
    out = _sh(["lsof", "-iTCP", "-sTCP:LISTEN", "-n", "-P"])
    seen = {}
    for line in out.splitlines()[1:]:
        p = line.split()
        if len(p) < 9:
            continue
        proc, addr = p[0], p[8]
        if "*:" in addr or "127.0.0.1:" in addr or "::1:" in addr or "localhost:" in addr:
            seen.setdefault(addr, proc)
    return [{"addr": a, "proc": p} for a, p in sorted(seen.items())]


def _processes(matches):
    procs = []
    for name in matches:
        pat = "[" + name[0] + "]" + name[1:]
        out = _sh(["pgrep", "-fl", pat], shell=False)
        for line in out.splitlines():
            parts = line.split(None, 1)
            if parts:
                procs.append({"name": name, "pid": parts[0],
                              "cmd": parts[1][:160] if len(parts) > 1 else ""})
    return procs


def _git_info(path):
    d = str(path)
    branch = _sh(["git", "-C", d, "branch", "--show-current"])
    short = _sh(["git", "-C", d, "log", "-1", "--format=%h %s"])
    dirty = _sh(["git", "-C", d, "status", "--porcelain"])
    return {"branch": branch, "commit": short, "dirty": len(dirty.splitlines())}


TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _tailscale_cli():
    """The Tailscale CLI to use: PATH first, then the macOS app bundle."""
    return "tailscale" if shutil.which("tailscale") else TAILSCALE_APP


# Outside a shell (launchd), the app-bundle binary starts as the GUI and prints an
# error instead of answering; this makes it act as the CLI.
TAILSCALE_ENV = {**os.environ, "TAILSCALE_BE_CLI": "1"}


def _tailscale_ip():
    out = _sh([_tailscale_cli(), "ip", "-4"], env=TAILSCALE_ENV)
    if re.match(r"^[\d.]+$", out.strip()):
        return out.strip()
    return None


def _tailscale_json(args):
    """Run one read-only Tailscale JSON command; return (object|None, error|None)."""
    label = " ".join(args)
    try:
        out = _sh([_tailscale_cli()] + list(args), timeout=10, env=TAILSCALE_ENV)
    except Exception as e:  # noqa: BLE001 - collection must survive a broken CLI
        return None, f"tailscale {label} failed: {type(e).__name__}: {e}"
    if not isinstance(out, str) or not out.strip():
        return None, f"tailscale {label} failed"
    try:
        data = json.loads(out)
    except (TypeError, ValueError):
        return None, f"tailscale {label} returned invalid JSON"
    if not isinstance(data, dict):
        return None, f"tailscale {label} returned invalid JSON"
    return data, None


def _locked_out_names(filtered):
    if isinstance(filtered, dict):
        peers = filtered.values()
    elif isinstance(filtered, list):
        peers = filtered
    else:
        return []
    names = []
    for peer in peers:
        if not isinstance(peer, dict):
            continue
        for key in ("Name", "HostName", "StableID", "NodeKey"):
            value = peer.get(key)
            if isinstance(value, str) and value:
                names.append(value[:16] if key == "NodeKey" else value)
                break
    return names


def _tailnet_device(node, is_self):
    expiry = node.get("KeyExpiry")
    if not isinstance(expiry, str) or not expiry or expiry.startswith("0001-01-01"):
        expiry = None
    name = node.get("HostName")
    return {
        "name": name if isinstance(name, str) else None,
        "self": bool(is_self),
        "tagged": bool(node.get("Tags")),
        "key_expiry": expiry,
    }


def _tailnet_devices(status):
    devices = []
    self_node = status.get("Self")
    if isinstance(self_node, dict):
        devices.append(_tailnet_device(self_node, True))
    peers = status.get("Peer")
    if isinstance(peers, dict):
        peers = peers.values()
    elif not isinstance(peers, (list, tuple)):
        peers = ()
    for peer in peers:
        if isinstance(peer, dict):
            devices.append(_tailnet_device(peer, False))
    return devices


def _tailnet():
    """Tailnet Lock and key-expiry facts from the local Tailscale CLI.

    The two commands are independent: a failure in one leaves the other's
    readable parts intact and is reported in `error`. Never raises.
    """
    out = {"lock_enabled": None, "self_signed": None, "locked_out": [],
           "devices": [], "error": None}
    errors = []

    status, err = _tailscale_json(["status", "--json"])
    if err:
        errors.append(err)
    if status is not None:
        out["devices"] = _tailnet_devices(status)
    # A stopped or logged-out client can't vouch for the tailnet's lock state, so
    # lock facts count only while it runs (else "lock is off" would be a false alarm).
    running = status is not None and status.get("BackendState") == "Running"
    if status is not None and not running:
        errors.append(f"tailscale is {status.get('BackendState') or 'not running'}")

    lock, err = _tailscale_json(["lock", "status", "--json"])
    if err:
        errors.append(err)
    if lock is not None and running:
        enabled = lock.get("Enabled")
        if isinstance(enabled, bool):
            out["lock_enabled"] = enabled
            if enabled:
                signed = lock.get("NodeKeySigned")
                out["self_signed"] = signed if isinstance(signed, bool) else None
                out["locked_out"] = _locked_out_names(lock.get("FilteredPeers"))

    out["error"] = "; ".join(errors) if errors else None
    return out


def collect_local(cfg_host):
    """Collect live facts for the local MacBook."""
    facts = {
        "host": "macbook",
        "hostname": _sh(["hostname"]) or "macbook",
        "os": _sh(["sw_vers", "-productName"]) + " " + _sh(["sw_vers", "-productVersion"]),
        "uptime_secs": _uptime_secs(),
        "loadavg": _loadavg(),
        "cpu": _cpu(),
        "mem": _mem(),
        "disk": _disk(),
        "tailscale_ip": _tailscale_ip() or cfg_host.get("tailscale_ip"),
        "tailnet": _tailnet(),
        "wan": wan_check(),
        "dns": dns_check(),
        "ports": _ports(),
        "processes": _processes([a["match"] for a in cfg_host.get("agents", [])]),
        "projects": [],
        "services": [],          # macOS services are app-level; agents cover the interesting ones
        "docker": None,
        "hermes_cron_jobs": None,
        "crontab": None,
        "extras": [],
    }
    for p in cfg_host.get("projects", []):
        path = Path(p["path"]).expanduser()
        info = {"name": p["name"], "path": p["path"], "note": p.get("note", "")}
        if (path / ".git").exists():
            info.update(_git_info(path))
        else:
            info.update({"branch": "", "commit": "", "dirty": 0, "exists": path.exists()})
        facts["projects"].append(info)
    return facts


# ---------------------------------------------------------------------------
# Remote collector (ssh + probe.py)
# ---------------------------------------------------------------------------

def ssh_probe(host_key, timeout=40):
    """Pipe probe.py to the remote host and parse its JSON."""
    probe_src = PROBE_PATH.read_text()
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        host_key, "python3", "-",
    ]
    start = time.monotonic()
    r = subprocess.run(cmd, input=probe_src, capture_output=True, text=True, timeout=timeout)
    rtt_ms = round((time.monotonic() - start) * 1000)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {host_key} failed rc={r.returncode}: {r.stderr.strip()[:200]}")
    try:
        facts = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"probe output not JSON on {host_key}: {e}") from e
    facts["rtt_ms"] = rtt_ms
    return facts


def collect_remote(host_key, cfg_host, timeout=40):
    facts = ssh_probe(host_key, timeout=timeout)
    if not facts.get("os"):
        facts["os"] = cfg_host.get("os_hint", "Linux")
    # Config paths use "~" which must expand to the REMOTE home, not the local one.
    remote_home = facts.get("home") or ""
    seen = {os.path.abspath(p.get("path", "")) for p in facts.get("projects", [])}
    for p in cfg_host.get("projects", []):
        raw = p["path"]
        norm = (remote_home + raw[1:]) if raw.startswith("~/") else raw
        if norm not in seen:
            facts.setdefault("projects", []).append({
                "name": p["name"], "path": norm, "note": p.get("note", ""),
                "exists": None,  # existence was checked remotely via extras/probe
            })
            seen.add(norm)
    return facts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _collect_one(key, cfg_host, cfg):
    """Collect a single host; returns (key, entry, error_or_None)."""
    entry = dict(cfg_host)
    entry["id"] = key
    entry["reachable"] = False
    entry["errors"] = []
    entry["stale"] = False
    err = None
    try:
        if cfg_host.get("connect") == "local":
            facts = collect_local(cfg_host)
        else:
            facts = collect_remote(key, cfg_host, timeout=cfg["server"]["collect_timeout_seconds"])
        entry["reachable"] = True
        entry.update(facts)
        _LAST_GOOD[key] = {k: v for k, v in facts.items() if k not in _CACHE_EXCLUDED_KEYS}
    except Exception as e:  # noqa: BLE001
        entry["errors"].append(f"{type(e).__name__}: {e}")
        err = str(e)
        if key in _LAST_GOOD:
            for k, v in _LAST_GOOD[key].items():
                if k not in _CACHE_EXCLUDED_KEYS:
                    entry[k] = v
            entry["stale"] = True
    entry["as_of"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return key, entry, err


def collect(cfg=None, hosts=None):
    cfg = cfg or load_config()
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cache_ttl": cfg["server"]["cache_ttl_seconds"],
        "hosts": {},
        "errors": {},
    }
    hosts = hosts or list(cfg["hosts"].keys())
    workers = max(1, int(cfg["server"].get("collect_workers", len(hosts))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_collect_one, k, cfg["hosts"][k], cfg): k for k in hosts}
        for fut in concurrent.futures.as_completed(futures):
            k, entry, err = fut.result()
            result["hosts"][k] = entry
            if err:
                result["errors"][k] = err
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", help="collect only this host key")
    args = ap.parse_args()
    cfg = load_config()
    hosts = [args.host] if args.host else None
    json.dump(collect(cfg, hosts), sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
