#!/usr/bin/env python3
"""notify.py — webhook delivery shared by alerts.py and healthcheck.py (stdlib only).

A webhook entry in config.json `alerts.webhooks`:

    { "id": "discord", "type": "discord",
      "url_file": "~/.config/command-center/discord-webhook",
      "severities": ["critical"], "rules": ["pokemon-quiet"] }

`url_file` keeps the secret URL out of git; `url` is accepted too. With no
`severities`/`rules` filter a webhook receives every alert; otherwise it gets
alerts matching any listed severity or rule id.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = "command-center/1.0"  # Discord rejects urllib's default UA


def webhook_id(wh, index):
    return str(wh.get("id") or index)


def resolve_url(wh):
    if wh.get("url"):
        return wh["url"]
    path = wh.get("url_file")
    if not path:
        return None
    try:
        return Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def matches(wh, alert):
    severities, rules = wh.get("severities"), wh.get("rules")
    if severities is None and rules is None:
        return True
    return alert.get("severity") in (severities or []) or alert.get("rule") in (rules or [])


def body(kind, text):
    """Return (bytes, content type) for one webhook flavour."""
    if kind == "discord":
        # Alert text carries host/unit names; never let it ping anyone.
        return (json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode(),
                "application/json")
    if kind == "slack":
        return json.dumps({"text": text}).encode(), "application/json"
    return text.encode(), "text/plain; charset=utf-8"  # ntfy / generic


def send(wh, text, timeout=5):
    """POST text to one webhook. True on a 2xx response; never raises."""
    url = resolve_url(wh)
    if not url:
        return False
    data, ctype = body(wh.get("type", "generic"), text)
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": ctype, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:  # e.g. 429 rate limit: retried later
        e.close()
        return False
    except Exception:  # noqa: BLE001 — notifications are best-effort
        return False


def fire_text(alert):
    icon = "🔴" if alert.get("severity") == "critical" else "🟠"
    return f"{icon} **{str(alert.get('severity', 'alert')).upper()}** · {alert.get('message', '')}"


def resolved_text(alert, active_seconds):
    return f"✅ **RESOLVED** · {alert.get('message', '')} (active {duration(active_seconds)})"


def duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h"
