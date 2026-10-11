#!/usr/bin/env python3
"""healthcheck.py — independent liveness check for the dashboard itself.

launchd runs this every `healthcheck.interval_seconds` as its own job, so it
keeps working when the dashboard process is dead or hung. It asks
/api/health whether the server answers and whether collection is still
advancing. After `fail_threshold` consecutive failures it sends one DOWN
notification to the configured webhooks, then one RECOVERED notification
when the dashboard is healthy again. State lives in data/healthcheck_state.json.

    python3 healthcheck.py [--port 9090]   # one check; exit 0 healthy, 1 not
    python3 healthcheck.py --test          # send a test message to every webhook
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import notify

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "data" / "healthcheck_state.json"
ALERT = {"rule": "dashboard-down", "severity": "critical"}
DEFAULTS = {"fail_threshold": 2, "stale_after_seconds": 300, "timeout_seconds": 10}


def probe(port, timeout, stale_after):
    """Return (healthy, detail)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as resp:
            body = json.load(resp)
    except Exception as e:  # noqa: BLE001
        return False, f"no response on :{port} ({type(e).__name__})"
    age = body.get("collection_age_seconds")
    if age is None:
        return False, "no collection has completed yet"
    if age > stale_after:
        return False, f"last collection finished {notify.duration(age)} ago (collector stuck?)"
    return True, "ok"


def load_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def step(state, healthy, detail, webhooks, threshold, now=None, send=notify.send):
    """Advance the up/down state machine and deliver due notifications."""
    now = time.time() if now is None else now
    targets = {notify.webhook_id(wh, i): wh for i, wh in enumerate(webhooks)
               if notify.matches(wh, ALERT)}
    if healthy:
        if state.get("down_since") is not None:
            text = f"✅ **RESOLVED** · command-center dashboard is answering again (down {notify.duration(now - state['down_since'])})"
            state["recovery_pending"] = sorted(set(state.get("recovery_pending", [])) | set(state.get("notified", [])))
            state.update(down_since=None, notified=[], recovery_text=text)
        state["failures"] = 0
        pending = []
        for wid in state.get("recovery_pending", []):
            if wid in targets and not send(targets[wid], state["recovery_text"]):
                pending.append(wid)
        state["recovery_pending"] = pending
        return state

    if not state.get("failures"):
        state["first_failure"] = now
    state["failures"] = state.get("failures", 0) + 1
    state["detail"] = detail
    if state["failures"] < threshold:
        return state
    if state.get("down_since") is None:
        state["down_since"] = state["first_failure"]
        state["notified"] = []
    text = f"🔴 **CRITICAL** · command-center dashboard is down: {detail}"
    for wid, wh in targets.items():
        if wid not in state["notified"] and send(wh, text):
            state["notified"].append(wid)
    return state


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int)
    ap.add_argument("--test", action="store_true", help="send a test message to every webhook")
    args = ap.parse_args(argv)

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    webhooks = (cfg.get("alerts") or {}).get("webhooks") or []
    if args.test:
        if not webhooks:
            print("no webhooks configured")
            return 1
        ok = True
        for i, wh in enumerate(webhooks):
            sent = notify.send(wh, "🧪 command-center test notification")
            ok &= sent
            print(f"{notify.webhook_id(wh, i)}: {'sent' if sent else 'FAILED (missing URL file or rejected)'}")
        return 0 if ok else 1

    hc = {**DEFAULTS, **(cfg.get("healthcheck") or {})}
    port = args.port or cfg["server"]["port"]
    healthy, detail = probe(port, hc["timeout_seconds"], hc["stale_after_seconds"])
    state = load_state(STATE_PATH)
    was_down = state.get("down_since") is not None
    state = step(state, healthy, detail, webhooks, hc["fail_threshold"])
    save_state(STATE_PATH, state)
    if not healthy or was_down:  # log failures and the recovery, stay quiet otherwise
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"{stamp} {'healthy' if healthy else 'UNHEALTHY'}: {detail} (failures={state['failures']})", flush=True)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
