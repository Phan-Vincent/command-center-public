#!/usr/bin/env python3
"""Notification lifecycle, webhook delivery and the dashboard healthcheck (stdlib only)."""

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import alerts  # noqa: E402
import healthcheck  # noqa: E402
import notify  # noqa: E402
import server  # noqa: E402

T0 = 1_800_000_000.0


class FakeReceiver:
    """Local HTTP endpoint that records POSTs and answers with a chosen status."""

    def __init__(self):
        self.posts = []
        self.status = 204
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.posts.append({"body": self.rfile.read(length), "headers": dict(self.headers)})
                self.send_response(outer.status)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/hook"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def status(quiet_runs=None, tunnel="active", hermes_reachable=True):
    scanner = {"available": True, "services": {"discord_handler": "active", "tunnel": tunnel}}
    if quiet_runs is not None:
        scanner["last_discord_post"] = {"at": "2026-10-10T07:03:29+00:00", "runs_since": quiet_runs}
    return {"hosts": {"hermes": {"reachable": hermes_reachable, "pokemon_scanner": scanner}}}


def config(state_file=None, debounce=120, webhooks=None):
    notify_cfg = {"min_active_seconds": debounce}
    if state_file:
        notify_cfg["state_file"] = str(state_file)
    return {"alerts": {
        "rules": [
            {"id": "pokemon-service", "type": "pokemon_service", "severity": "critical"},
            {"id": "pokemon-quiet", "type": "pokemon_quiet", "min_runs": 8, "severity": "warning"},
        ],
        "notify": notify_cfg,
        "webhooks": webhooks if webhooks is not None else [
            {"id": "discord", "type": "discord", "url": "http://unused",
             "severities": ["critical"], "rules": ["pokemon-quiet"]},
        ],
    }}


class AlertNotificationTests(unittest.TestCase):
    def setUp(self):
        alerts.reset_state()
        self.sent = []
        self.ok = True
        patcher = mock.patch.object(notify, "send", side_effect=self._send)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(alerts.reset_state)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "alert_state.json"

    def _send(self, wh, text, timeout=5):
        if self.ok:
            self.sent.append(text)
        return self.ok

    def run_evals(self, st, cfg, start, count, step=15):
        for i in range(count):
            alerts.evaluate(st, cfg, now=start + i * step)

    def test_fires_once_after_debounce_despite_repeated_evaluations(self):
        cfg = config(self.state_file)
        self.run_evals(status(tunnel="inactive"), cfg, T0, 8)   # 0..105s: still debouncing
        self.assertEqual(self.sent, [])
        self.run_evals(status(tunnel="inactive"), cfg, T0 + 120, 40)  # 10 more minutes active
        self.assertEqual(len(self.sent), 1)
        self.assertIn("CRITICAL", self.sent[0])
        self.assertIn("tunnel is inactive", self.sent[0])

    def test_restart_does_not_resend_and_keeps_first_seen(self):
        cfg = config(self.state_file)
        self.run_evals(status(tunnel="inactive"), cfg, T0, 10)
        self.assertEqual(len(self.sent), 1)
        alerts.reset_state()  # simulate a server restart: memory gone, state file kept
        active = alerts.evaluate(status(tunnel="inactive"), cfg, now=T0 + 3600)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(active[0]["since"], alerts._iso(T0))

    def test_resolved_sent_once_after_clear_debounce(self):
        cfg = config(self.state_file)
        self.run_evals(status(tunnel="inactive"), cfg, T0, 10)
        self.run_evals(status(), cfg, T0 + 300, 7)  # gone for 90s: not yet
        self.assertEqual(len(self.sent), 1)
        self.run_evals(status(), cfg, T0 + 420, 20)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("RESOLVED", self.sent[1])
        self.assertIn("tunnel is inactive", self.sent[1])

    def test_flap_shorter_than_debounce_is_silent(self):
        cfg = config(self.state_file)
        self.run_evals(status(tunnel="inactive"), cfg, T0, 3)
        self.run_evals(status(), cfg, T0 + 45, 20)
        self.assertEqual(self.sent, [])

    def test_unreachable_host_holds_resolution(self):
        cfg = config(self.state_file)
        self.run_evals(status(tunnel="inactive"), cfg, T0, 10)
        down = {"hosts": {"hermes": {"reachable": False}}}  # no scanner facts at all
        self.run_evals(down, cfg, T0 + 200, 40)
        self.assertEqual(len(self.sent), 1, "must not claim recovery while hermes is unreachable")
        self.run_evals(status(tunnel="inactive"), cfg, T0 + 1000, 10)
        self.assertEqual(len(self.sent), 1, "same unresolved alert must not re-notify")

    def test_failed_send_retries_after_backoff_without_duplicates(self):
        cfg = config(self.state_file)
        self.ok = False
        alerts.evaluate(status(tunnel="inactive"), cfg, now=T0)
        self.run_evals(status(tunnel="inactive"), cfg, T0 + 120, 2)
        self.assertEqual(notify.send.call_count, 1, "second attempt is inside the backoff")
        self.ok = True
        self.run_evals(status(tunnel="inactive"), cfg, T0 + 200, 20)
        self.assertEqual(len(self.sent), 1)

    def test_filters_route_critical_and_pokemon_quiet_only(self):
        cfg = config(self.state_file, debounce=0)
        cfg["alerts"]["rules"].append({"id": "mem-high", "type": "mem", "threshold": 50, "severity": "warning"})
        st = status(quiet_runs=9, tunnel="inactive")
        st["hosts"]["hermes"]["mem"] = {"total_gb": 10, "used_gb": 9}
        active = alerts.evaluate(st, cfg, now=T0)
        self.assertEqual({a["rule"] for a in active}, {"pokemon-service", "pokemon-quiet", "mem-high"})
        self.assertEqual(len(self.sent), 2)
        self.assertFalse(any("mem" in s for s in self.sent))

    def test_identity_ignores_message_changes_but_separates_subjects(self):
        cfg = config(self.state_file, debounce=0)
        alerts.evaluate(status(quiet_runs=9), cfg, now=T0)
        alerts.evaluate(status(quiet_runs=12), cfg, now=T0 + 15)
        self.assertEqual(len(self.sent), 1)
        st = status()
        st["hosts"]["hermes"]["pokemon_scanner"]["services"] = {"discord_handler": "failed", "tunnel": "failed"}
        active = alerts.evaluate(st, cfg, now=T0 + 30)
        self.assertEqual(len({a["key"] for a in active}), 2)

    def test_works_in_memory_without_state_file(self):
        cfg = config(None, debounce=0)
        alerts.evaluate(status(tunnel="inactive"), cfg, now=T0)
        alerts.evaluate(status(tunnel="inactive"), cfg, now=T0 + 15)
        self.assertEqual(len(self.sent), 1)

    def offline_status(self, wan=True, dns=True, **hermes):
        def host(**extra):
            return {"reachable": True, "wan": {"ok": wan}, "dns": {"ok": dns}, **extra}
        return {"hosts": {"hermes": host(**hermes), "acer": host()}}

    def offline_config(self, debounce=0):
        cfg = config(self.state_file, debounce=debounce)
        cfg["alerts"]["rules"] = [
            {"id": "host-offline", "type": "offline", "hosts": ["hermes"], "severity": "critical"}]
        return cfg

    def test_offline_flags_no_internet_then_dns_only_on_listed_hosts(self):
        cfg = self.offline_config()
        active = alerts.evaluate(self.offline_status(wan=False), cfg, now=T0)
        self.assertEqual([a["host"] for a in active], ["hermes"], "acer isn't in the rule's hosts")
        self.assertIn("hermes has no internet", active[0]["message"])
        active = alerts.evaluate(self.offline_status(dns=False), cfg, now=T0 + 15)
        self.assertIn("hermes can't resolve DNS", active[0]["message"])
        self.assertEqual(alerts.evaluate(self.offline_status(), cfg, now=T0 + 30), [])

    def test_offline_ignores_unreachable_stale_and_old_probe_facts(self):
        cfg = self.offline_config()
        for st in (self.offline_status(wan=False, reachable=False),
                   self.offline_status(wan=False, stale=True),  # last-good facts of a failed collection
                   {"hosts": {"hermes": {"reachable": True}}}):  # probe predates wan/dns
            self.assertEqual(alerts.evaluate(st, cfg, now=T0), [])

    def test_offline_notifies_once_per_incident_and_resolves(self):
        cfg = self.offline_config(debounce=120)
        self.run_evals(self.offline_status(wan=False), cfg, T0, 10)
        self.run_evals(self.offline_status(dns=False), cfg, T0 + 150, 10)  # cause changes, same incident
        self.assertEqual(len(self.sent), 1)
        self.assertIn("CRITICAL", self.sent[0])
        self.run_evals(self.offline_status(), cfg, T0 + 300, 10)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("RESOLVED", self.sent[1])

    def host_down_config(self):
        cfg = config(self.state_file, webhooks=[{"id": "discord", "type": "discord", "url": "http://unused"}])
        cfg["alerts"]["rules"] = [
            {"id": "host-down", "type": "host", "state": "unreachable", "severity": "critical"}]
        return cfg

    def test_dark_wakes_never_fire_host_down(self):
        cfg = self.host_down_config()
        down = {"hosts": {"hermes": {"reachable": False}, "acer": {"reachable": False}}}
        # Mac asleep: a few collections per dark wake, ~15 min apart, for hours.
        for wake in range(12):
            self.run_evals(down, cfg, T0 + wake * 900, 5, step=20)  # 80 s awake each time
        self.assertEqual(self.sent, [])

    def test_host_down_fires_once_mac_is_awake_and_watching(self):
        cfg = self.host_down_config()
        up = {"hosts": {"hermes": {"reachable": True}}}
        down = {"hosts": {"hermes": {"reachable": False}}}
        self.run_evals(up, cfg, T0, 4)
        self.run_evals(down, cfg, T0 + 60, 8)  # 60..165 s: debouncing, no gap
        self.assertEqual(self.sent, [])
        self.run_evals(down, cfg, T0 + 180, 4)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("host hermes is unreachable", self.sent[0])

    def test_first_sample_after_gap_cannot_start_host_down(self):
        cfg = self.host_down_config()
        down = {"hosts": {"hermes": {"reachable": False}}}
        self.run_evals(down, cfg, T0, 4)
        self.run_evals(down, cfg, T0 + 600, 9)  # asleep in between: starts over at T0+615
        self.assertEqual(self.sent, [], "only 105 s watched since waking")
        self.run_evals(down, cfg, T0 + 750, 1)
        self.assertEqual(len(self.sent), 1)

    def test_restart_drops_unannounced_host_down_from_state_file(self):
        cfg = self.host_down_config()
        down = {"hosts": {"hermes": {"reachable": False}}}
        self.run_evals(down, cfg, T0, 4)  # 45 s: saved, not yet announced
        alerts.reset_state()  # server restart hours later
        self.run_evals(down, cfg, T0 + 7200, 8)
        self.assertEqual(self.sent, [], "the old first_seen must not count")
        self.run_evals(down, cfg, T0 + 7320, 1)
        self.assertEqual(len(self.sent), 1)

    def test_gap_keeps_announced_host_down_and_other_rules(self):
        cfg = self.host_down_config()
        cfg["alerts"]["rules"].append({"id": "pokemon-service", "type": "pokemon_service", "severity": "critical"})
        down = {"hosts": {"hermes": {"reachable": False}}}
        self.run_evals(down, cfg, T0, 12)
        self.run_evals(status(tunnel="inactive"), cfg, T0 + 3600, 1)  # woke: hermes back, tunnel down
        active = alerts.evaluate(down, cfg, now=T0 + 3615)
        self.assertEqual({a["rule"] for a in active}, {"host-down"})
        self.assertEqual(len(self.sent), 1, "an announced host-down isn't re-sent or resolved by a gap")
        self.run_evals(status(tunnel="inactive"), cfg, T0 + 7200, 1)
        self.run_evals(status(tunnel="inactive"), cfg, T0 + 7210, 15, step=10)
        self.assertEqual(sum("CRITICAL" in s and "tunnel" in s for s in self.sent), 1,
                         "non-reachability alerts still debounce across a gap as before")


class WebhookDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.rx = FakeReceiver()
        self.addCleanup(self.rx.close)

    def test_discord_payload_blocks_mentions_and_sets_user_agent(self):
        self.assertTrue(notify.send({"type": "discord", "url": self.rx.url}, "@everyone hi"))
        post = self.rx.posts[0]
        body = json.loads(post["body"])
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.assertEqual(post["headers"]["User-Agent"], notify.USER_AGENT)

    def test_url_file_and_http_errors(self):
        with tempfile.NamedTemporaryFile("w", suffix=".url", delete=False) as f:
            f.write(self.rx.url + "\n")
        self.addCleanup(Path(f.name).unlink)
        self.assertTrue(notify.send({"type": "discord", "url_file": f.name}, "x"))
        self.rx.status = 429
        self.assertFalse(notify.send({"type": "discord", "url_file": f.name}, "x"))
        self.assertFalse(notify.send({"type": "discord", "url_file": "/nonexistent/hook"}, "x"))

    def test_end_to_end_fire_and_resolve_through_http(self):
        alerts.reset_state()
        self.addCleanup(alerts.reset_state)
        with tempfile.TemporaryDirectory() as td:
            cfg = config(Path(td) / "state.json", debounce=0,
                         webhooks=[{"id": "d", "type": "discord", "url": self.rx.url, "severities": ["critical"]}])
            for i in range(5):
                alerts.evaluate(status(tunnel="inactive"), cfg, now=T0 + i * 15)
            for i in range(5):
                alerts.evaluate(status(), cfg, now=T0 + 100 + i * 15)
        texts = [json.loads(p["body"])["content"] for p in self.rx.posts]
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith("🔴"))
        self.assertTrue(texts[1].startswith("✅"))


class HealthcheckTests(unittest.TestCase):
    WH = [{"id": "discord", "type": "discord", "url": "http://unused", "severities": ["critical"]}]

    def setUp(self):
        self.sent = []
        self.ok = True

    def send(self, wh, text, timeout=5):
        if self.ok:
            self.sent.append(text)
        return self.ok

    def step(self, state, healthy, now):
        return healthcheck.step(state, healthy, "no response", self.WH, 2, now=now, send=self.send)

    def test_single_failure_is_tolerated(self):
        state = self.step({}, False, T0)
        state = self.step(state, True, T0 + 120)
        self.assertEqual(self.sent, [])

    def test_down_once_then_recovered_once(self):
        state = {}
        for i in range(6):
            state = self.step(state, False, T0 + i * 120)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("dashboard is down", self.sent[0])
        for i in range(3):
            state = self.step(state, True, T0 + 1000 + i * 120)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("RESOLVED", self.sent[1])
        self.assertIn("down 16m", self.sent[1])

    def test_failed_deliveries_are_retried(self):
        self.ok = False
        state = {}
        for i in range(3):
            state = self.step(state, False, T0 + i * 120)
        self.ok = True
        state = self.step(state, False, T0 + 360)
        self.assertEqual(len(self.sent), 1)
        self.ok = False
        state = self.step(state, True, T0 + 480)
        self.ok = True
        state = self.step(state, True, T0 + 600)
        state = self.step(state, True, T0 + 720)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("RESOLVED", self.sent[1])

    def test_probe_and_health_endpoint(self):
        self.assertFalse(healthcheck.probe(1, 1, 300)[0], "nothing listens on port 1")
        server.STATE.update({"last_full": 0.0})
        self.assertIsNone(server.health()["collection_age_seconds"])
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.cfg = {"server": {}}
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        port = httpd.server_address[1]
        self.assertEqual(healthcheck.probe(port, 5, 300), (False, "no collection has completed yet"))
        server.STATE.update({"last_full": time.time() - 30})
        self.addCleanup(server.STATE.update, {"last_full": 0.0})
        self.assertEqual(healthcheck.probe(port, 5, 300), (True, "ok"))
        server.STATE.update({"last_full": time.time() - 900})
        healthy, detail = healthcheck.probe(port, 5, 300)
        self.assertFalse(healthy)
        self.assertIn("collector stuck", detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
