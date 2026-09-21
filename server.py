#!/usr/bin/env python3
"""
server.py — Command Center HTTP server (stdlib only, macOS/Linux).

Serves the dashboard UI (web/) and a small JSON API:

    GET  /api/status           current fleet status (cached, background refresh)
    GET  /api/status?force=1   refresh immediately, then return
    GET  /api/config           hosts + allowlisted actions metadata
    GET  /api/audit            recent control-action audit entries
    POST /api/control          {host, action_id} -> run allowlisted action

Binds to 127.0.0.1 by default. Start with ./run.sh start (or --open to pop
the browser).

Usage:
    python3 server.py [--port 9090] [--host 127.0.0.1] [--open]
"""

import argparse
import copy
import json
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import alerts
import collector
import control
import metrics

BASE = Path(__file__).resolve().parent
WEB_DIR = BASE / "web"

# Global state ---------------------------------------------------------------
STATE = {"status": None, "lock": threading.Lock(), "last_full": 0.0,
         "collecting": False, "alerts": []}


def public_config(cfg=None):
    """Return dashboard configuration without authentication secrets.

    ``/api/config`` is intentionally readable before a user has supplied an
    auth token, so it must never return that token or secret-bearing webhook
    URLs.  Work on a deep copy so request handling cannot mutate live config.
    """
    source = collector.load_config() if cfg is None else cfg
    public = copy.deepcopy(source)
    server_cfg = public.get("server")
    if isinstance(server_cfg, dict):
        server_cfg["auth_required"] = bool(server_cfg.pop("auth_token", None))
    alerts_cfg = public.get("alerts")
    if isinstance(alerts_cfg, dict):
        alerts_cfg.pop("webhooks", None)
    return public


def _summarize(status, active_alerts):
    hosts = status.get("hosts", {})
    up = sum(1 for h in hosts.values() if h.get("reachable"))
    failed_services = sum(
        1 for h in hosts.values()
        for u in (h.get("services") or []) if u.get("state") == "failed")
    crit = sum(1 for a in active_alerts if a.get("severity") == "critical")
    if crit:
        health = "critical"
    elif up < len(hosts):
        health = "down"
    elif failed_services or active_alerts:
        health = "degraded"
    else:
        health = "ok"
    return {
        "hosts_total": len(hosts), "hosts_up": up, "hosts_down": len(hosts) - up,
        "failed_services": failed_services, "alerts_total": len(active_alerts),
        "alerts_critical": crit, "health": health,
    }


def refresh_status(force=False, cfg=None):
    """Collect fleet status; serve from cache unless forced or stale.

    Collection runs OUTSIDE the lock (copy-on-write): the slow ssh probes never
    block concurrent /api/status readers, and a forced refresh while a background
    collection is already running simply returns the last known snapshot.
    """
    cfg = cfg or collector.load_config()
    ttl = cfg["server"]["cache_ttl_seconds"]
    now = time.time()
    with STATE["lock"]:
        stale = STATE["status"] is None or (now - STATE["last_full"]) >= ttl
        if not (force or stale):
            return STATE["status"]
        if STATE["collecting"]:
            return STATE["status"]  # a collection is already in flight
        STATE["collecting"] = True

    fresh = None
    try:
        fresh = collector.collect(cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[collector] {e}", flush=True)

    if fresh is not None:
        # Annotate (fast, outside the swap) then persist metrics + alerts.
        active = alerts.evaluate(fresh, cfg)
        fresh["summary"] = _summarize(fresh, active)
        fresh["alerts"] = active
        with STATE["lock"]:
            STATE["status"] = fresh
            STATE["last_full"] = time.time()
            STATE["alerts"] = active
        try:
            metrics.record(fresh, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"[metrics] {e}", flush=True)

    with STATE["lock"]:
        STATE["collecting"] = False
        return STATE["status"]


def _background_loop(cfg, interval):
    while True:
        try:
            refresh_status(force=True, cfg=cfg)
        except Exception as e:  # noqa: BLE001
            print(f"[collector] {e}", flush=True)
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    server_version = "CommandCenter/1.0"

    # Paths that require the auth token when one is configured.
    _PROTECTED = {"/api/control", "/api/exec", "/api/audit", "/api/logs"}

    # -- helpers -------------------------------------------------------------
    def _authorized(self):
        token = (self.server.cfg or {}).get("server", {}).get("auth_token")
        if not token:
            return True
        return self.headers.get("X-Auth-Token") == token

    def _require_auth(self, path):
        if path in self._PROTECTED and not self._authorized():
            self._send(401, {"ok": False, "error": "unauthorized: missing/invalid X-Auth-Token"})
            return False
        return True

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, indent=1)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, rel):
        path = (WEB_DIR / rel).resolve()
        if not path.is_relative_to(WEB_DIR) or not path.is_file():
            self._send(404, {"error": "not found"})
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(path.suffix, "application/octet-stream")
        self._send(200, path.read_bytes(), ctype)

    def log_message(self, fmt, *args):  # quieter access log
        pass

    @staticmethod
    def _qdict(qs):
        out = {}
        for part in qs.split("&"):
            if "=" in part:
                k, _, v = part.partition("=")
                out[k] = urllib.parse.unquote(v)
        return out

    # -- routes --------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path == "/":
            return self._send_file("index.html")
        if path.startswith("/static/"):
            return self._send_file(path[len("/static/"):])
        if path == "/api/status":
            force = any(p == "force=1" for p in qs.split("&"))
            return self._send(200, refresh_status(force=force))
        if path == "/api/config":
            return self._send(200, public_config())
        if path == "/api/metrics":
            q = self._qdict(qs)
            try:
                series = metrics.series(q.get("host", ""), q.get("metric", "load1"),
                                        int(q.get("limit", 120)))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            return self._send(200, {"host": q.get("host", ""), "metric": q.get("metric", "load1"),
                                    "series": series})
        if path == "/api/alerts":
            return self._send(200, {"alerts": STATE["alerts"]})
        if path == "/api/logs":
            if not self._require_auth(path):
                return
            q = self._qdict(qs)
            host = q.get("host", "")
            target = q.get("target", "")
            ttype = q.get("type", "unit")
            status = STATE["status"] or {}
            h = (status.get("hosts") or {}).get(host, {})
            # validate target against the live allowlist
            if ttype == "unit" and h.get("services"):
                names = {u["name"] for u in h["services"]}
                if target not in names:
                    return self._send(400, {"ok": False, "error": f"unit not in live allowlist: {target}"})
            elif ttype == "container" and h.get("docker"):
                names = {c["name"] for c in h["docker"]}
                if target not in names:
                    return self._send(400, {"ok": False, "error": f"container not in live allowlist: {target}"})
            res = control.fetch_logs(host, ttype, target, tail=int(q.get("tail", 120)))
            return self._send(200, res)
        if path == "/api/audit":
            if not self._require_auth(path):
                return
            return self._send(200, {"entries": control.read_audit(limit=60)})
        return self._send(404, {"error": f"no route: {path}"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._require_auth(path):
            return
        if path == "/api/control":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"ok": False, "error": "bad json body"})
            host = payload.get("host")
            action_id = payload.get("action_id")
            if not host or not action_id:
                return self._send(400, {"ok": False, "error": "host and action_id required"})
            cfg = collector.load_config()
            return self._send(200, control.run_action(host, action_id, cfg=cfg))
        if path == "/api/exec":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"ok": False, "error": "bad json body"})
            host = payload.get("host")
            exec_id = payload.get("exec_id")
            arg = payload.get("arg", "")
            if not host or not exec_id:
                return self._send(400, {"ok": False, "error": "host and exec_id required"})
            status = STATE["status"] or {}
            h = (status.get("hosts") or {}).get(host, {})
            cfg = collector.load_config()
            return self._send(200, control.run_exec(
                host, exec_id, arg=arg, cfg=cfg,
                allowed_units={u["name"] for u in (h.get("services") or [])},
                allowed_containers={c["name"] for c in (h.get("docker") or [])},
            ))
        return self._send(404, {"error": f"no route: {path}"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9090)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true", help="open browser after start")
    args = ap.parse_args()

    cfg = collector.load_config()
    problems = collector.validate_config(cfg)
    if problems:
        print("[config] validation failed:", flush=True)
        for p in problems:
            print(f"  - {p}", flush=True)
        raise SystemExit(1)
    refresh_status(force=True, cfg=cfg)  # warm cache before serving
    t = threading.Thread(
        target=_background_loop,
        args=(cfg, cfg["server"]["cache_ttl_seconds"]),
        daemon=True,
    )
    t.start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.cfg = cfg
    url = f"http://{args.host}:{args.port}/"
    loopback = args.host in ("127.0.0.1", "::1", "localhost")
    if not loopback:
        print("⚠️  binding to a non-loopback address — the dashboard will be reachable"
              " on the network.", flush=True)
        if not cfg["server"].get("auth_token"):
            print("⚠️  WARNING: no auth_token set in config.json. The live control"
                  " surface will be exposed WITHOUT authentication.", flush=True)
    print(f"Command Center listening on {url}", flush=True)
    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)


if __name__ == "__main__":
    main()
