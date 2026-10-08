#!/usr/bin/env python3
"""alerts.py — config-driven alert engine + optional webhook notifications."""

import json
import threading
import time
import urllib.request
from datetime import datetime, timezone

# alert key -> first-seen epoch (in-memory; resets on restart)
_STATE = {}
_NOTIFIED = set()
_LOCK = threading.Lock()


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _age_seconds(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - parsed).total_seconds()
    except (TypeError, ValueError):
        return None


def _evaluate_host(host, h, rules):
    alerts = []
    for rule in rules:
        t = rule.get("type")
        sev = rule.get("severity", "warning")
        if t == "host":
            if rule.get("state") == "unreachable" and not h.get("reachable"):
                alerts.append({"host": host, "rule": rule.get("id", "host-down"),
                               "severity": sev, "message": f"host {host} is unreachable"})
        elif t == "service":
            state = rule.get("state", "failed")
            for u in (h.get("services") or []):
                if u.get("state") == state:
                    alerts.append({"host": host, "rule": rule.get("id", "svc-failed"),
                                   "severity": sev,
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
        elif t == "scanner_stale":
            scanner = h.get("scanner_health") or {}
            age = _age_seconds(scanner.get("last_success_at"))
            threshold = _num(rule.get("threshold_hours", 7))
            stale = age is None or (threshold is not None and age >= threshold * 3600)
            if scanner.get("available") and threshold is not None and stale:
                detail = "has no recorded successful run" if age is None else f"stale for {age / 3600:.1f}h"
                alerts.append({"host": host, "rule": rule.get("id", "scanner-stale"),
                               "severity": sev,
                               "message": f"{host}: Scanner run {detail}"})
        elif t == "scanner_source":
            scanner = h.get("scanner_health") or {}
            sources = ((scanner.get("source_health") or {}).get("sources") or {})
            min_attempts = int(_num(rule.get("min_attempts", 10)) or 10)
            min_rate = _num(rule.get("min_rate", 0.10))
            for source, sample in sources.items():
                active = max(0, int(_num(sample.get("attempted")) or 0) - int(_num(sample.get("ignored")) or 0))
                usable = int(_num(sample.get("usable")) or 0)
                rate = usable / active if active else None
                if active >= min_attempts and rate is not None and min_rate is not None and rate < min_rate:
                    alerts.append({"host": host, "rule": rule.get("id", "scanner-source"),
                                   "severity": sev,
                                   "message": f"{host}: Scanner {source} yield {usable}/{active} ({rate:.1%})"})
        elif t == "scanner_service":
            scanner = h.get("scanner_health") or {}
            for component, state in (scanner.get("services") or {}).items():
                if state != "active":
                    alerts.append({"host": host, "rule": rule.get("id", "scanner-service"),
                                   "severity": sev,
                                   "message": f"{host}: Scanner {component} is {state or 'unknown'}"})
    return alerts


def evaluate(status, cfg):
    """Return the current active alerts, tracking first-seen and dispatching
    webhooks on new (rising-edge) transitions."""
    rules = (cfg.get("alerts") or {}).get("rules", [])
    now = time.time()
    raw = []
    for host, h in status.get("hosts", {}).items():
        raw.extend(_evaluate_host(host, h, rules))

    active = []
    new_alerts = []
    with _LOCK:
        current_keys = set()
        for a in raw:
            key = f"{a['host']}|{a['rule']}|{a['message']}"
            a["key"] = key
            if key not in _STATE:
                _STATE[key] = now
                new_alerts.append(a)
            a["since"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_STATE[key]))
            current_keys.add(key)
            active.append(a)
        # Drop alerts that are no longer firing so they can re-notify next time.
        for key in list(_STATE.keys()):
            if key not in current_keys:
                _STATE.pop(key, None)
                _NOTIFIED.discard(key)

    # Notify only alerts that haven't been sent yet (rising edge).
    to_send = []
    with _LOCK:
        for a in new_alerts:
            if a["key"] not in _NOTIFIED:
                _NOTIFIED.add(a["key"])
                to_send.append(a)
    for a in to_send:
        _dispatch(a, cfg)
    return active


def _dispatch(alert, cfg):
    webhooks = (cfg.get("alerts") or {}).get("webhooks", [])
    for wh in webhooks:
        url = wh.get("url")
        if not url:
            continue
        kind = wh.get("type", "generic")
        payload = None
        if kind == "discord":
            payload = json.dumps({"content": f"🚨 **{alert['severity']}** — {alert['message']}"}).encode()
        elif kind == "slack":
            payload = json.dumps({"text": f"*{alert['severity']}*: {alert['message']}"}).encode()
        else:  # ntfy / generic
            payload = alert["message"].encode()
        try:
            req = urllib.request.Request(url, data=payload, method="POST")
            if kind in ("discord", "slack"):
                req.add_header("Content-Type", "application/json")
            urllib.request.urlopen(req, timeout=5)
        except Exception:  # noqa: BLE001
            pass  # notifications are best-effort; never break the dashboard
