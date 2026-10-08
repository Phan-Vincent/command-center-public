#!/usr/bin/env python3
"""
control.py — allowlisted control surface for the Command Center.

Executes ONLY commands declared in config.json under host "actions", over ssh
(local hosts run directly). Every invocation is appended to audit.jsonl.

Security posture:
  * No free-form command execution — only allowlisted action ids.
  * Commands run as the SSH user (no sudo). sudo actions are intentionally
    NOT supported from the dashboard.
  * Actions are executed with a hard timeout.
  * Server binds to 127.0.0.1 only; control requires POST + UI confirmation.
"""

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
AUDIT_PATH = BASE / "audit.jsonl"
MAX_AUDIT_BYTES = 1_000_000

# Redact obvious secrets before they hit the audit log (over-redaction is safer).
REDACT_PATTERNS = [
    (re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|apikey|authorization|bearer)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), "[REDACTED KEY]"),
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "[REDACTED]"),
]

# One lock per host so two clicks can't double-restart the same service.
_LOCK_GUARD = threading.Lock()
_LOCKS = {}


def _host_lock(host):
    with _LOCK_GUARD:
        return _LOCKS.setdefault(host, threading.Lock())


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _run_local(cmd, timeout):
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
    return {"rc": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}


def _run_ssh(host_key, cmd, timeout):
    # Pass the whole command as ONE ssh argument so the remote shell parses it
    # (ssh joins separate argv entries with spaces, which breaks bash -c).
    ssh_cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        host_key, cmd,
    ]
    r = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
    return {"rc": r.returncode, "stdout": r.stdout.strip(), "stderr": r.stderr.strip()}


# ssh prints these when the remote host dies mid-command (e.g. reboot/poweroff).
_DISCONNECT_MARKERS = (
    "closed by remote host", "connection reset by peer", "broken pipe",
    "connection to", "connection timed out",
)


def _connection_dropped(res):
    """True if ssh reported the remote closing the connection on us."""
    blob = f"{res.get('stdout', '')} {res.get('stderr', '')}".lower()
    return res.get("rc") == 255 and any(m in blob for m in _DISCONNECT_MARKERS)


def _interpret_disconnect(res):
    """Reclassify a reboot-style command whose ssh session dropped mid-flight.

    `systemctl reboot`/`poweroff` terminate sshd before the command can return,
    so ssh exits 255 with "closed by remote host" even though the command
    succeeded. Without this, a successful reboot would be reported as a failure.
    """
    if res.get("rc") == 0:
        res["stderr"] = res.get("stderr") or "command accepted"
        return res
    if _connection_dropped(res):
        return {"rc": 0, "stdout": res.get("stdout", ""),
                "stderr": "connection dropped — reboot initiated"}
    return res


def _redact(s):
    for pattern, repl in REDACT_PATTERNS:
        s = pattern.sub(repl, s)
    return s


def _redact_entry(entry):
    out = dict(entry)
    for k in ("cmd", "stdout", "stderr", "label"):
        if k in out and isinstance(out[k], str):
            out[k] = _redact(out[k])
    return out


def audit(entry, path=AUDIT_PATH, max_bytes=MAX_AUDIT_BYTES):
    """Append one redacted audit line (best-effort, never raises) and rotate."""
    try:
        path = Path(path)
        line = json.dumps(_redact_entry(entry)) + "\n"
        with open(path, "a") as f:
            f.write(line)
        if path.stat().st_size > max_bytes:
            os.replace(path, path.with_name(path.name + ".1"))
    except Exception:  # noqa: BLE001
        pass


def read_audit(limit=50, path=AUDIT_PATH):
    entries = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        return []
    return entries[-limit:]


def run_action(host_key, action_id, cfg=None, timeout=None):
    """Execute an allowlisted action. Returns a result dict."""
    cfg = cfg or load_config()
    host_cfg = cfg["hosts"].get(host_key)
    if not host_cfg:
        return {"ok": False, "error": f"unknown host: {host_key}"}
    action = next((a for a in host_cfg.get("actions", []) if a["id"] == action_id), None)
    if not action:
        return {"ok": False, "error": f"unknown action: {action_id}"}
    timeout = timeout or cfg["server"].get("control_timeout_seconds", 60)

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": host_key,
        "action_id": action_id,
        "label": action["label"],
        "cmd": action["cmd"],
    }
    lock = _host_lock(host_key)
    if not lock.acquire(blocking=False):
        entry.update({"rc": -3, "error": f"another action is already running on {host_key}"})
        audit(entry)
        return {"ok": False, "error": entry["error"], "audited": True}
    try:
        try:
            if host_cfg.get("connect") == "local":
                res = _run_local(action["cmd"], timeout)
            else:
                res = _run_ssh(host_key, action["cmd"], timeout)
        except subprocess.TimeoutExpired:
            # A reboot/poweroff can kill ssh before it returns; a timeout here is
            # ambiguous but usually means the host is already going down. Report
            # it as success with a caveat rather than a false failure.
            if action.get("disconnect_ok"):
                entry.update({"rc": 0, "stdout": "",
                              "stderr": f"no reply after {timeout}s — host may be rebooting"})
                audit(entry)
                return {"ok": True, "rc": 0, "stdout": "", "stderr": entry["stderr"],
                        "audited": True, "note": "verify host reachability"}
            entry.update({"rc": -1, "error": f"timeout after {timeout}s"})
            audit(entry)
            return {"ok": False, "error": entry["error"], "audited": True}
        except Exception as e:  # noqa: BLE001
            entry.update({"rc": -2, "error": str(e)})
            audit(entry)
            return {"ok": False, "error": str(e), "audited": True}

        if action.get("disconnect_ok"):
            res = _interpret_disconnect(res)
        entry.update({"rc": res["rc"], "stdout": res["stdout"][:2000], "stderr": res["stderr"][:2000]})
        audit(entry)
        ok = res["rc"] == 0
        return {
            "ok": ok,
            "rc": res["rc"],
            "stdout": res["stdout"],
            "stderr": res["stderr"],
            "audited": True,
        }
    finally:
        lock.release()


def run_exec(host_key, exec_id, arg="", cfg=None, timeout=None,
             allowed_units=None, allowed_containers=None):
    """Run an allowlisted READ-ONLY command from config host.exec.

    `arg` (for unit/container verbs) is validated against a whitelist of names
    derived from the live status, plus a strict character class — no shell
    metacharacters can reach the command.
    """
    cfg = cfg or load_config()
    host_cfg = cfg["hosts"].get(host_key)
    if not host_cfg:
        return {"ok": False, "error": f"unknown host: {host_key}"}
    ex = next((e for e in host_cfg.get("exec", []) if e["id"] == exec_id), None)
    if not ex:
        return {"ok": False, "error": f"unknown exec: {exec_id}"}
    cmd = ex["cmd"]
    atype = ex.get("arg", "none")
    if atype == "unit":
        if not arg or not re.fullmatch(r"[A-Za-z0-9@._:\-]+", arg):
            return {"ok": False, "error": "invalid unit name"}
        if allowed_units is not None and arg not in allowed_units:
            return {"ok": False, "error": f"unit not in live allowlist: {arg}"}
        cmd = cmd.replace("{arg}", arg)
    elif atype == "container":
        if not arg or not re.fullmatch(r"[A-Za-z0-9_.\-]+", arg):
            return {"ok": False, "error": "invalid container name"}
        if allowed_containers is not None and arg not in allowed_containers:
            return {"ok": False, "error": f"container not in live allowlist: {arg}"}
        cmd = cmd.replace("{arg}", arg)
    elif "{arg}" in cmd:
        return {"ok": False, "error": "this command requires a target"}

    timeout = timeout or cfg["server"].get("control_timeout_seconds", 60)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "host": host_key, "action_id": exec_id, "label": ex["label"], "cmd": cmd}
    lock = _host_lock(host_key)
    if not lock.acquire(blocking=False):
        entry.update({"rc": -3, "error": f"another action is already running on {host_key}"})
        audit(entry)
        return {"ok": False, "error": entry["error"], "audited": True}
    try:
        try:
            if host_cfg.get("connect") == "local":
                res = _run_local(cmd, timeout)
            else:
                res = _run_ssh(host_key, cmd, timeout)
        except subprocess.TimeoutExpired:
            entry.update({"rc": -1, "error": f"timeout after {timeout}s"})
            audit(entry)
            return {"ok": False, "error": entry["error"], "audited": True}
        except Exception as e:  # noqa: BLE001
            entry.update({"rc": -2, "error": str(e)})
            audit(entry)
            return {"ok": False, "error": str(e), "audited": True}
        entry.update({"rc": res["rc"], "stdout": res["stdout"][:2000], "stderr": res["stderr"][:2000]})
        audit(entry)
        return {"ok": res["rc"] == 0, "rc": res["rc"], "stdout": res["stdout"], "stderr": res["stderr"]}
    finally:
        lock.release()


def fetch_logs(host_key, target_type, target, tail=120, cfg=None, timeout=15):
    """Tail logs for a whitelisted unit or container (read-only)."""
    cfg = cfg or load_config()
    host_cfg = cfg["hosts"].get(host_key)
    if not host_cfg:
        return {"ok": False, "error": f"unknown host: {host_key}"}
    tail = max(1, min(int(tail), 500))
    if target_type == "unit":
        if not re.fullmatch(r"[A-Za-z0-9@._:\-]+", target):
            return {"ok": False, "error": "invalid unit name"}
        cmd = f"journalctl --user -u {target} -n {tail} --no-pager"
    elif target_type == "container":
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", target):
            return {"ok": False, "error": "invalid container name"}
        cmd = f"docker logs {target} --tail {tail} 2>&1"
    else:
        return {"ok": False, "error": "target_type must be 'unit' or 'container'"}
    try:
        if host_cfg.get("connect") == "local":
            res = _run_local(cmd, timeout)
        else:
            res = _run_ssh(host_key, cmd, timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": res["rc"] == 0, "rc": res["rc"],
            "lines": (res["stdout"] or res["stderr"]).splitlines()[-tail:],
            "stderr": res["stderr"]}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run an allowlisted control action.")
    ap.add_argument("host", help="host key (macbook|hermes|acer)")
    ap.add_argument("action_id", help="action id from config.json")
    args = ap.parse_args()
    result = run_action(args.host, args.action_id)
    print(json.dumps(result, indent=1))
