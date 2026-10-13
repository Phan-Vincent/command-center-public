#!/usr/bin/env python3
"""home_internet.py — is the home internet up, and when did it last drop?

hermes and acer sit on the home LAN, and each probe reports whether that host
can reach the internet (probe.wan_check). Every collection is classified as:

  up           a home host reached the internet
  down         every home host that answered reports no internet (confirmed)
  unreachable  no home host answered but the MacBook is online: home internet
               (or Tailscale to home) is down, as seen from away
  unknown      nothing to go on (old probe output, or the MacBook is offline too)

Runs of down/unreachable become outages, logged to `state_file` so they survive
restarts. `unknown` neither opens nor closes an outage. Unconfirmed outages
shorter than `min_unconfirmed_seconds` are dropped as Tailscale/ssh blips.

A gap of more than `max_sample_gap_seconds` since the previous collection means
the MacBook was asleep. Its brief background wakes run a collection before
Tailscale reconnects, which looks exactly like `unreachable`. So the first
sample after a gap can't open an unconfirmed outage, and an unconfirmed outage
still open across a gap is discarded: nobody was watching. Confirmed outages
come from the home hosts themselves and are kept.
Once an outage of at least `notify_min_minutes` ends — when the home internet
is back and the message can actually get out — one summary goes to every
webhook matching rule `home-internet`.
"""

import json
import os
import threading
import time
from pathlib import Path

import notify

BASE = Path(__file__).resolve().parent
RULE = {"rule": "home-internet", "severity": "warning"}
RETRY_BACKOFF_SECONDS = 60

DEFAULTS = {
    "hosts": ["hermes", "acer"],
    "local_host": "macbook",
    "state_file": "data/home_internet.json",
    "keep_outages": 200,
    "min_unconfirmed_seconds": 60,
    "max_sample_gap_seconds": 120,
    "notify_min_minutes": 10,
}

_LOCK = threading.Lock()


def _settings(cfg):
    return {**DEFAULTS, **cfg["home_internet"]}


def _state_path(settings):
    path = settings.get("state_file")
    if not path:
        return None
    path = Path(os.path.expanduser(path))
    return path if path.is_absolute() else BASE / path


def _load(path):
    if path is not None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass  # first run or unreadable: start clean
    return {"state": None, "since": None, "open": None, "outages": [],
            "pending": [], "retry_after": 0}


def _save(path, data):
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        print(f"[home_internet] could not save state: {e}", flush=True)


def classify(status, settings):
    """Return (state, best rtt_ms) for one fleet snapshot."""
    hosts = status.get("hosts", {})
    reports = []
    for key in settings["hosts"]:
        h = hosts.get(key) or {}
        wan = h.get("wan")
        if h.get("reachable") and not h.get("stale") and isinstance(wan, dict):
            reports.append(wan)
    if reports:
        rtts = [w["rtt_ms"] for w in reports if w.get("ok") and w.get("rtt_ms") is not None]
        if any(w.get("ok") for w in reports):
            return "up", min(rtts) if rtts else None
        return "down", None
    if any((hosts.get(key) or {}).get("reachable") for key in settings["hosts"]):
        return "unknown", None  # answered without wan facts (probe predates the check)
    local = (hosts.get(settings["local_host"]) or {}).get("wan") or {}
    return ("unreachable" if local.get("ok") else "unknown"), None


def _clock(epoch):
    return time.strftime("%b %d %H:%M", time.localtime(epoch))


def outage_text(o):
    span = f"{_clock(o['start'])}–{time.strftime('%H:%M', time.localtime(o['end']))}"
    how = "" if o["confirmed"] else " (home hosts unreachable from the MacBook; not confirmed from inside)"
    return f"🌐 Home internet was down for {notify.duration(o['seconds'])} ({span}){how}"


def _close(data, settings, now):
    o = data["open"]
    data["open"] = None
    seconds = now - o["start"]
    if not o["confirmed"] and seconds < settings["min_unconfirmed_seconds"]:
        return
    outage = {"start": o["start"], "end": now, "seconds": round(seconds),
              "confirmed": o["confirmed"]}
    data["outages"] = (data["outages"] + [outage])[-int(settings["keep_outages"]):]
    if seconds >= float(settings["notify_min_minutes"]) * 60:
        data["pending"].append(outage)


def _deliver(data, cfg, now):
    if not data["pending"] or now < data.get("retry_after", 0):
        return
    webhooks = [wh for wh in ((cfg or {}).get("alerts") or {}).get("webhooks") or []
                if notify.matches(wh, RULE)]
    remaining = []
    for outage in data["pending"]:
        # Re-sent to every webhook if one fails; a duplicate beats a lost report.
        if not all(notify.send(wh, outage_text(outage)) for wh in webhooks):
            remaining.append(outage)
    data["pending"] = remaining
    if remaining:
        data["retry_after"] = now + RETRY_BACKOFF_SECONDS


def _view(data, rtt, now):
    outages = data["outages"]
    recent = [o for o in outages if now - o["end"] <= 7 * 86400]
    return {
        "state": data["state"],
        "since": data["since"],
        "rtt_ms": rtt,
        "open": data["open"],
        "outages": list(reversed(outages[-20:])),  # newest first
        "count_24h": sum(1 for o in recent if now - o["end"] <= 86400),
        "count_7d": len(recent),
        "downtime_7d_seconds": sum(o["seconds"] for o in recent),
    }


def update(status, cfg, now=None):
    """Fold one snapshot into the outage log; return the dashboard view.

    Returns None (and touches nothing) when config has no `home_internet` section.
    """
    if not (cfg or {}).get("home_internet"):
        return None
    settings = _settings(cfg)
    now = time.time() if now is None else now
    state, rtt = classify(status, settings)
    with _LOCK:
        path = _state_path(settings)
        data = _load(path)
        last = data.get("last_sample")
        data["last_sample"] = now
        if last is not None and now - last > settings["max_sample_gap_seconds"]:
            if state == "unreachable":
                state = "unknown"  # just woke: Tailscale may not have reconnected yet
            if data["open"] is not None and not data["open"]["confirmed"]:
                data["open"] = None
        if state != data["state"]:
            data["state"], data["since"] = state, now
        if state in ("down", "unreachable"):
            if data["open"] is None:
                data["open"] = {"start": now, "confirmed": False}
            data["open"]["confirmed"] = data["open"]["confirmed"] or state == "down"
        elif state == "up" and data["open"] is not None:
            _close(data, settings, now)
        _deliver(data, cfg, now)
        _save(path, data)
        return _view(data, rtt, now)
