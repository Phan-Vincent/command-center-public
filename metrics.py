#!/usr/bin/env python3
"""metrics.py — time-series store for the Command Center (stdlib sqlite3)."""

import sqlite3
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "data" / "metrics.db"

_LOCK = threading.Lock()

# Whitelist of queryable metric columns (prevents SQL injection via metric name).
METRICS = {
    "load1", "load5", "load15",
    "mem_used_gb", "mem_total_gb",
    "disk_use_pct", "uptime_secs", "failed_services",
}


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute("""
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            host TEXT NOT NULL,
            load1 REAL, load5 REAL, load15 REAL,
            mem_used_gb REAL, mem_total_gb REAL,
            disk_use_pct REAL, uptime_secs REAL,
            failed_services INTEGER, reachable INTEGER
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_samples_host_ts ON samples(host, ts)")
    return c


def record(status, cfg=None):
    """Persist one sample per host from a status snapshot."""
    retention_hours = (cfg or {}).get("server", {}).get("metrics_retention_hours", 24)
    now = time.time()
    rows = []
    for host, h in status.get("hosts", {}).items():
        load = h.get("loadavg") or []
        mem = h.get("mem") or {}
        disk = (h.get("disk") or [{}])[0]
        failed = sum(1 for u in (h.get("services") or []) if u.get("state") == "failed")
        rows.append((
            now, host,
            load[0] if len(load) > 0 else None,
            load[1] if len(load) > 1 else None,
            load[2] if len(load) > 2 else None,
            mem.get("used_gb"), mem.get("total_gb"),
            disk.get("use_pct"), h.get("uptime_secs"),
            failed, 1 if h.get("reachable") else 0,
        ))
    with _LOCK:
        c = _conn()
        try:
            c.executemany(
                "INSERT INTO samples (ts, host, load1, load5, load15, mem_used_gb,"
                " mem_total_gb, disk_use_pct, uptime_secs, failed_services, reachable)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            c.execute("DELETE FROM samples WHERE ts < ?", (now - retention_hours * 3600,))
            c.commit()
        finally:
            c.close()


def series(host, metric, limit=120):
    """Return [{"ts": epoch, "v": value}, ...] oldest-first for a host+metric."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric: {metric}")
    limit = max(1, min(int(limit), 1000))
    c = _conn()
    try:
        rows = c.execute(
            f"SELECT ts, {metric} FROM samples WHERE host=? AND {metric} IS NOT NULL"
            " ORDER BY ts DESC LIMIT ?", (host, limit)).fetchall()
    finally:
        c.close()
    rows.reverse()
    return [{"ts": r[0], "v": r[1]} for r in rows]
