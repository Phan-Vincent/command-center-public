#!/usr/bin/env python3
"""
End-to-end smoke test for a RUNNING Command Center server.

Usage:  python3 tests/smoke_api.py [base_url]
        (default http://127.0.0.1:9090)

Requires ./run.sh start (or `python3 server.py`) to be running first.
"""

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9090"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return r.status, r.read()


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.status, r.read()


def main():
    # 1. static pages
    for path in ("/", "/static/app.js", "/static/style.css"):
        code, body = get(path)
        assert code == 200 and len(body) > 500, (path, code, len(body))
        print(f"GET {path}: {code} ({len(body)} bytes)")

    # 2. status
    code, body = get("/api/status?force=1")
    status = json.loads(body)
    reachable = {k: h["reachable"] for k, h in status["hosts"].items()}
    print("GET /api/status?force=1:", code, "| hosts:", reachable)
    assert all(reachable.values()), f"all hosts should be reachable: {reachable}"

    # 3. config
    code, body = get("/api/config")
    cfg = json.loads(body)
    assert set(cfg["hosts"]) == {"macbook", "hermes", "acer"}
    print("GET /api/config:", code, "| hosts:", list(cfg["hosts"]))

    # 4. control: safe read-only probes
    for host, action in (("hermes", "hermes-demo-status"),
                         ("acer", "acer-demo-status"),
                         ("macbook", "macbook-demo-status")):
        code, body = post("/api/control", {"host": host, "action_id": action})
        res = json.loads(body)
        assert res["ok"], (host, action, res)
        print(f"POST control {host}/{action}: ok rc={res['rc']} out={res['stdout'][:40]!r}")

    # 5. control: rejection of unknown action
    code, body = post("/api/control", {"host": "acer", "action_id": "rm -rf /"})
    res = json.loads(body)
    assert not res["ok"] and "unknown action" in res.get("error", "")
    print("POST control bad action rejected:", res["error"])

    # 6. audit
    code, body = get("/api/audit")
    audit = json.loads(body)
    assert len(audit["entries"]) >= 3
    print("GET /api/audit:", code, "| entries:", len(audit["entries"]))

    # 7. 404
    try:
        get("/nope")
        raise SystemExit("expected 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404
        print("GET /nope: 404 (expected)")

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
