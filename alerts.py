#!/usr/bin/env python3
"""alerts.py — config-driven alert engine + webhook notifications.

Notification lifecycle (per alert key = host|rule|subject, per webhook):
  * fire once the alert has been active for `notify.min_active_seconds`;
  * never re-send while it stays active, including across server restarts
    (state persists to `notify.state_file`);
  * once it has been gone for `min_active_seconds`, send one RESOLVED message
    to every webhook that was told it fired. Alerts whose facts can't be
    confirmed — the host is unreachable, or the snapshot is a stale
    last-known-good copy — are held rather than resolved, and their recovery
    debounce restarts once real facts are back;
  * rules that read collected facts are only evaluated while those facts are
    current: an unreachable or stale host is `host`'s business alone;
  * failed sends are retried on later evaluations, after a short per-webhook
    backoff.

A gap of more than `notify.max_sample_gap_seconds` since the previous
evaluation means the MacBook was asleep. Its brief
background wakes collect before Tailscale reconnects, so every host looks
unreachable. Host and offline alerts therefore can't start in the first
evaluation after a gap, and any not yet announced are dropped: their
debounce has to run while the MacBook is awake and watching. Unannounced ones
loaded from the state file when the server starts are dropped the same way.
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import notify

BASE = Path(__file__).resolve().parent
RETRY_BACKOFF_SECONDS = 60
RESOLVED_TTL_SECONDS = 24 * 3600  # give up on undeliverable recoveries after a day
DEFAULT_MAX_SAMPLE_GAP_SECONDS = 120
SLEEP_GUARDED_TYPES = ("host", "offline")  # judged from the MacBook's own connection

# key -> {"alert": {...}, "first_seen": epoch, "notified": [webhook ids], "missing_since"?: epoch}
_STATE = {}
# key -> {"alert": {...}, "first_seen", "resolved_at", "pending": [webhook ids]}
_RESOLVED = {}
_BACKOFF = {}  # webhook id -> epoch before which it is skipped
_LOADED = {"path": None, "saved": None, "last_eval": None}
_LOCK = threading.Lock()


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _facts_unknown(h):
    """True when a host's facts can neither confirm nor deny anything right now.

    Covers both an unreachable host and the last-known-good copy a failed
    collection leaves behind (`stale`). Every rule derived from a host's facts
    must be held in that state, and recoveries can't be observed in it.
    """
    return not isinstance(h, dict) or not h.get("reachable") or bool(h.get("stale"))


def _str_list(value):
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _clean_active(raw):
    """Normalize persisted active alerts; drop anything unusable."""
    out = {}
    items = raw.items() if isinstance(raw, dict) else ()
    for key, entry in items:
        if not isinstance(entry, dict):
            continue
        alert = entry.get("alert")
        first_seen = _num(entry.get("first_seen"))
        if not isinstance(alert, dict) or not isinstance(alert.get("host"), str) or first_seen is None:
            continue
        clean = {"alert": dict(alert), "first_seen": first_seen,
                 "notified": _str_list(entry.get("notified"))}
        missing_since = _num(entry.get("missing_since"))
        if missing_since is not None:
            clean["missing_since"] = missing_since
        out[str(key)] = clean
    return out


def _clean_resolved(raw):
    """Normalize persisted pending recoveries; drop anything unusable."""
    out = {}
    items = raw.items() if isinstance(raw, dict) else ()
    for key, entry in items:
        if not isinstance(entry, dict):
            continue
        alert = entry.get("alert")
        resolved_at = _num(entry.get("resolved_at"))
        if not isinstance(alert, dict) or not isinstance(alert.get("host"), str) or resolved_at is None:
            continue
        out[str(key)] = {"alert": dict(alert), "first_seen": _num(entry.get("first_seen")),
                         "resolved_at": resolved_at,
                         "pending": _str_list(entry.get("pending"))}
    return out


def _age_seconds(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed).total_seconds()
    except (TypeError, ValueError):
        return None


def _parse_ts(value):
    """Parse an ISO timestamp (Z and fractions ok) into aware UTC, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _evaluate_host(host, h, rules, now=None):
    alerts = []
    now = time.time() if now is None else now
    h = h if isinstance(h, dict) else {}
    # Only `host` is judged from the connection attempt itself. Everything else
    # reads collected facts, which are last-known-good copies (`stale`) whenever
    # a collection failed — evaluating those repeats an old incident forever.
    facts_known = not _facts_unknown(h)
    for rule in rules:
        if rule.get("hosts") is not None and host not in rule["hosts"]:
            continue
        t = rule.get("type")
        sev = rule.get("severity", "warning")
        first = len(alerts)
        if t != "host" and not facts_known:
            continue
        if t == "host":
            if rule.get("state") == "unreachable" and not h.get("reachable"):
                alerts.append({"host": host, "rule": rule.get("id", "host-down"),
                               "severity": sev, "message": f"host {host} is unreachable"})
        elif t == "offline":
            # Answers over Tailscale but can't reach the internet, so its jobs and
            # its own alerts fail. Stale facts (host unreachable) are host-down's job.
            wan, dns = h.get("wan"), h.get("dns")
            problem = None
            if h.get("reachable") and not h.get("stale"):
                if isinstance(wan, dict) and wan.get("ok") is False:
                    problem = "has no internet"
                elif isinstance(dns, dict) and dns.get("ok") is False:
                    problem = "can't resolve DNS"
            if problem:
                alerts.append({"host": host, "rule": rule.get("id", "host-offline"),
                               "severity": sev,
                               "message": f"{host} {problem} (still reachable over Tailscale)"})
        elif t == "service":
            state = rule.get("state", "failed")
            for u in (h.get("services") or []):
                if u.get("state") == state:
                    alerts.append({"host": host, "rule": rule.get("id", "svc-failed"),
                                   "severity": sev, "subject": u.get("name"),
                                   "message": f"{host}: {u.get('name')} is {state}"})
        elif t == "disk":
            d = (h.get("disk") or [{}])[0]
            pct = _num(d.get("use_pct"))
            thr = _num(rule.get("threshold", 90))
            if pct is not None and thr is not None and pct >= thr:
                alerts.append({"host": host, "rule": rule.get("id", "disk-high"),
                               "severity": sev, "message": f"{host}: disk {pct:.0f}% (≥ {thr:.0f}%)"})
        elif t == "mem":
            mem = h.get("mem") or {}
            total = _num(mem.get("total_gb"))
            if total:
                pct = (mem.get("used_gb", 0) / total) * 100
                thr = _num(rule.get("threshold", 90))
                if thr is not None and pct >= thr:
                    alerts.append({"host": host, "rule": rule.get("id", "mem-high"),
                                   "severity": sev, "message": f"{host}: mem {pct:.0f}% (≥ {thr:.0f}%)"})
        elif t == "load":
            load = h.get("loadavg") or []
            cores = max(1, int(_num(h.get("cpu", {}).get("cores")) or 1))
            thr = _num(rule.get("threshold"))
            if thr is None:
                thr = float(cores)  # default: load1 >= all cores busy
            if load and thr is not None and _num(load[0]) is not None and load[0] >= thr:
                alerts.append({"host": host, "rule": rule.get("id", "load-high"),
                               "severity": sev, "message": f"{host}: load {load[0]:.1f} (≥ {thr:.1f})"})
        elif t == "pokemon_stale":
            scanner = h.get("pokemon_scanner") or {}
            age = _age_seconds(scanner.get("last_success_at"))
            threshold = _num(rule.get("threshold_hours", 7))
            stale = age is None or (threshold is not None and age >= threshold * 3600)
            if scanner.get("available") and threshold is not None and stale:
                detail = "has no recorded successful run" if age is None else f"stale for {age / 3600:.1f}h"
                alerts.append({"host": host, "rule": rule.get("id", "pokemon-stale"),
                               "severity": sev,
                               "message": f"{host}: Pokémon scan {detail}"})
        elif t == "pokemon_source":
            scanner = h.get("pokemon_scanner") or {}
            sources = ((scanner.get("source_health") or {}).get("sources") or {})
            min_attempts = int(_num(rule.get("min_attempts", 10)) or 10)
            min_rate = _num(rule.get("min_rate", 0.10))
            for source, sample in sources.items():
                active = max(0, int(_num(sample.get("attempted")) or 0) - int(_num(sample.get("ignored")) or 0))
                usable = int(_num(sample.get("usable")) or 0)
                rate = usable / active if active else None
                if active >= min_attempts and rate is not None and min_rate is not None and rate < min_rate:
                    alerts.append({"host": host, "rule": rule.get("id", "pokemon-source"),
                                   "severity": sev, "subject": source,
                                   "message": f"{host}: Pokémon {source} yield {usable}/{active} ({rate:.1%})"})
        elif t == "pokemon_quiet":
            scanner = h.get("pokemon_scanner") or {}
            post = scanner.get("last_discord_post")
            min_runs = int(_num(rule.get("min_runs", 8)) or 8)
            if scanner.get("available") and post and (_num(post.get("runs_since")) or 0) >= min_runs:
                # Keyed on the last post time (not the run count) so it stays one alert per streak.
                alerts.append({"host": host, "rule": rule.get("id", "pokemon-quiet"),
                               "severity": sev,
                               "message": f"{host}: no Pokémon Discord post since {post.get('at') or 'ever'}"})
        elif t == "pokemon_service":
            scanner = h.get("pokemon_scanner") or {}
            for component, state in (scanner.get("services") or {}).items():
                if state != "active":
                    alerts.append({"host": host, "rule": rule.get("id", "pokemon-service"),
                                   "severity": sev, "subject": component,
                                   "message": f"{host}: Pokémon {component} is {state or 'unknown'}"})
        elif t == "tailnet_lock":
            tailnet = h.get("tailnet")
            if not isinstance(tailnet, dict):
                continue
            if rule.get("expect_enabled") is True and tailnet.get("lock_enabled") is False:
                alerts.append({"host": host, "rule": rule.get("id", "tailnet-lock"),
                               "severity": sev,
                               "message": f"{host}: Tailnet Lock is off"})
        elif t == "tailnet_locked_out":
            tailnet = h.get("tailnet")
            if not isinstance(tailnet, dict):
                continue
            names = tailnet.get("locked_out")
            for name in (names if isinstance(names, list) else []):
                if not isinstance(name, str) or not name:
                    continue
                alerts.append({"host": host, "rule": rule.get("id", "tailnet-locked-out"),
                               "severity": sev, "subject": name,
                               "message": f"{host}: {name} is locked out of the tailnet "
                                          f"(needs a Tailnet Lock signature)"})
            if tailnet.get("self_signed") is False:
                alerts.append({"host": host, "rule": rule.get("id", "tailnet-locked-out"),
                               "severity": sev, "subject": host,
                               "message": f"{host}: {host} is locked out of the tailnet "
                                          f"(needs a Tailnet Lock signature)"})
        elif t == "tailnet_key_expiry":
            tailnet = h.get("tailnet")
            if not isinstance(tailnet, dict):
                continue
            days = _num(rule.get("days", 14))
            if days is None:
                days = 14
            cutoff = now + days * 86400
            devices = tailnet.get("devices")
            for device in (devices if isinstance(devices, list) else []):
                if not isinstance(device, dict):
                    continue
                name = device.get("name")
                if not isinstance(name, str) or not name:
                    continue
                raw_expiry = device.get("key_expiry")
                if isinstance(raw_expiry, str) and raw_expiry.startswith("0001-01-01"):
                    continue  # Tailscale's zero time means "never expires"
                expires = _parse_ts(raw_expiry)
                if expires is None:
                    continue
                expires_at = expires.timestamp()
                if expires_at > cutoff:
                    continue
                if expires_at < now:
                    message = f"{name}'s Tailscale key expired {expires.strftime('%Y-%m-%d')}"
                else:
                    message = f"{name}'s Tailscale key expires {expires.strftime('%Y-%m-%d')}"
                alerts.append({"host": host, "rule": rule.get("id", "tailnet-key-expiry"),
                               "severity": sev, "subject": name, "message": message})
        for a in alerts[first:]:
            a["type"] = t
    return alerts




def reset_state():
    """Forget all in-memory alert state (tests)."""
    with _LOCK:
        _STATE.clear()
        _RESOLVED.clear()
        _BACKOFF.clear()
        _LOADED.update(path=None, saved=None, last_eval=None)


def _key(a):
    return f"{a['host']}|{a['rule']}|{a.get('subject') or ''}"


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _state_path(notify_cfg):
    path = notify_cfg.get("state_file")
    if not path:
        return None
    path = Path(os.path.expanduser(path))
    return path if path.is_absolute() else BASE / path


def _load(path):
    if path == _LOADED["path"]:
        return
    _STATE.clear()
    _RESOLVED.clear()
    _LOADED.update(path=path, saved=None)
    if path is None:
        return
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("alert state must be a JSON object")
        _STATE.update(_clean_active(data.get("active")))
        _RESOLVED.update(_clean_resolved(data.get("resolved")))
        _LOADED["saved"] = raw
    except (OSError, ValueError):
        pass  # first run or unreadable: start clean


def _save(path):
    if path is None:
        return
    raw = json.dumps({"active": _STATE, "resolved": _RESOLVED}, indent=1, sort_keys=True)
    if raw == _LOADED["saved"]:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(raw, encoding="utf-8")
        os.replace(tmp, path)
        _LOADED["saved"] = raw
    except OSError as e:
        print(f"[alerts] could not save state: {e}", flush=True)


def evaluate(status, cfg, now=None):
    """Return the currently active alerts and deliver any due notifications."""
    acfg = cfg.get("alerts") or {}
    rules = acfg.get("rules", [])
    webhooks = acfg.get("webhooks") or []
    notify_cfg = acfg.get("notify") or {}
    debounce = _num(notify_cfg.get("min_active_seconds", 0)) or 0
    max_gap = _num(notify_cfg.get("max_sample_gap_seconds", DEFAULT_MAX_SAMPLE_GAP_SECONDS))
    now = time.time() if now is None else now
    hosts = status.get("hosts") if isinstance(status, dict) else None
    if not isinstance(hosts, dict):
        hosts = {}
    raw = []
    for host, h in hosts.items():
        raw.extend(_evaluate_host(host, h, rules, now))

    with _LOCK:  # held across sends: evaluations are serialized, so nothing double-sends
        path = _state_path(notify_cfg)
        _load(path)
        last, _LOADED["last_eval"] = _LOADED["last_eval"], now
        woke = last is not None and max_gap is not None and now - last > max_gap
        if woke or last is None:  # asleep, or the server was down: unannounced ones start over
            for key, entry in list(_STATE.items()):
                if not entry["notified"] and entry["alert"].get("type") in SLEEP_GUARDED_TYPES:
                    del _STATE[key]

        current = {}
        for a in raw:
            key = a["key"] = _key(a)
            if woke and a.get("type") in SLEEP_GUARDED_TYPES and key not in _STATE:
                continue  # just woke: Tailscale may not have reconnected yet
            entry = _STATE.setdefault(key, {"first_seen": now, "notified": []})
            entry["alert"] = {k: a.get(k) for k in ("host", "rule", "type", "severity", "message")}
            entry.pop("missing_since", None)
            a["since"] = _iso(entry["first_seen"])
            current[key] = a

        for key, entry in list(_STATE.items()):
            if key in current:
                continue
            if not entry["notified"]:
                del _STATE[key]  # never announced, so nothing to resolve
                continue
            alert = entry["alert"]
            if alert.get("type") != "host" and _facts_unknown(hosts.get(alert.get("host"))):
                # Can't confirm recovery while the host is unreachable or showing
                # last-known-good facts: hold the alert and restart the recovery
                # debounce from the next sample that can actually observe it.
                entry.pop("missing_since", None)
                continue
            entry.setdefault("missing_since", now)
            if now - entry["missing_since"] < debounce:
                continue
            del _STATE[key]
            _RESOLVED[key] = {"alert": alert, "first_seen": entry["first_seen"],
                              "resolved_at": now, "pending": list(entry["notified"])}

        for key, r in list(_RESOLVED.items()):
            if now - r["resolved_at"] > RESOLVED_TTL_SECONDS:
                del _RESOLVED[key]

        by_id = {notify.webhook_id(wh, i): wh for i, wh in enumerate(webhooks)}
        sends = []  # (kind, key, webhook id, text)
        for key, entry in _STATE.items():
            if key not in current or now - entry["first_seen"] < debounce:
                continue
            for wid, wh in by_id.items():
                if wid not in entry["notified"] and notify.matches(wh, entry["alert"]):
                    sends.append(("fire", key, wid, notify.fire_text(entry["alert"])))
        for key, r in _RESOLVED.items():
            r["pending"] = [wid for wid in r["pending"] if wid in by_id]
            for wid in r["pending"]:
                sends.append(("resolved", key, wid,
                              notify.resolved_text(r["alert"], r["resolved_at"] - r["first_seen"])))

        for kind, key, wid, text in sends:
            if _BACKOFF.get(wid, 0) > now:
                continue
            if not notify.send(by_id[wid], text):
                _BACKOFF[wid] = now + RETRY_BACKOFF_SECONDS
                continue
            if kind == "fire":
                _STATE[key]["notified"].append(wid)
            else:
                _RESOLVED[key]["pending"].remove(wid)
        for key in [k for k, r in _RESOLVED.items() if not r["pending"]]:
            del _RESOLVED[key]

        _save(path)
    return list(current.values())
