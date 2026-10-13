#!/usr/bin/env python3
"""Home internet check: probe reachability, classification, outage log, notifications."""

import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import home_internet  # noqa: E402
import notify  # noqa: E402
import probe  # noqa: E402

T0 = 1_800_000_000.0


def host(wan_ok=None, reachable=True, stale=False):
    h = {"reachable": reachable, "stale": stale}
    if wan_ok is not None:
        h["wan"] = {"ok": wan_ok, "rtt_ms": 12 if wan_ok else None}
    return h


def snap(hermes, acer, mac_ok=True):
    return {"hosts": {"hermes": hermes, "acer": acer,
                      "macbook": {"reachable": True, "wan": {"ok": mac_ok}}}}


UP = snap(host(True), host(True))
DOWN = snap(host(False), host(False), mac_ok=False)
AWAY_UNREACHABLE = snap(host(reachable=False), host(reachable=False))
UNKNOWN = snap(host(reachable=False), host(reachable=False), mac_ok=False)


class WanCheckTests(unittest.TestCase):
    def test_up_when_any_target_accepts(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead_port = closed.getsockname()[1]
        closed.close()  # nothing listens here: connection refused
        res = probe.wan_check(targets=(("127.0.0.1", dead_port), listener.getsockname()), timeout=1)
        self.assertTrue(res["ok"])
        self.assertEqual((res["reached"], res["tried"]), (1, 2))
        self.assertIsNotNone(res["rtt_ms"])

    def test_down_when_nothing_answers(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead = closed.getsockname()
        closed.close()
        res = probe.wan_check(targets=(dead,), timeout=1)
        self.assertEqual(res, {"ok": False, "rtt_ms": None, "reached": 0, "tried": 1})


class DnsCheckTests(unittest.TestCase):
    def test_up_when_any_name_resolves(self):
        def lookup(name, *args, **kwargs):
            if name == "bad.example":
                raise socket.gaierror("Temporary failure in name resolution")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))]
        with mock.patch.object(socket, "getaddrinfo", side_effect=lookup):
            res = probe.dns_check(names=("bad.example", "good.example"), timeout=1)
        self.assertEqual(res, {"ok": True, "resolved": 1, "tried": 2})

    def test_down_when_no_name_resolves(self):
        with mock.patch.object(socket, "getaddrinfo", side_effect=socket.gaierror("no resolver")):
            res = probe.dns_check(names=("a.example", "b.example"), timeout=1)
        self.assertEqual(res, {"ok": False, "resolved": 0, "tried": 2})

    def test_hung_lookup_counts_as_failed_without_blocking(self):
        release = threading.Event()
        self.addCleanup(release.set)
        with mock.patch.object(socket, "getaddrinfo", side_effect=lambda *a, **k: release.wait(5)):
            start = time.monotonic()
            res = probe.dns_check(names=("slow.example",), timeout=0.2)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(res, {"ok": False, "resolved": 0, "tried": 1})


class PathCheckTests(unittest.TestCase):
    def test_ping_parses_reply_and_treats_failure_as_none(self):
        ok = mock.Mock(returncode=0, stdout="64 bytes from 192.168.1.1: icmp_seq=1 ttl=64 time=2.47 ms\n")
        lost = mock.Mock(returncode=1, stdout="1 packets transmitted, 0 received, 100% packet loss\n")
        with mock.patch.object(probe.subprocess, "run", return_value=ok):
            self.assertEqual(probe.ping("192.168.1.1"), 2.5)
        with mock.patch.object(probe.subprocess, "run", return_value=lost):
            self.assertIsNone(probe.ping("192.168.1.1"))

    def test_path_check_reports_both_hops(self):
        replies = {"192.168.1.1": 2.5, "192.168.100.1": None}
        with mock.patch.object(probe, "default_gateway", return_value="192.168.1.1"), \
                mock.patch.object(probe, "ping", side_effect=lambda ip: replies[ip]):
            res = probe.path_check()
        self.assertEqual(res["router"], {"ip": "192.168.1.1", "ok": True, "rtt_ms": 2.5})
        self.assertEqual(res["modem"], {"ip": "192.168.100.1", "ok": False, "rtt_ms": None})


def with_path(snapshot, router, modem, hosts=("hermes", "acer")):
    for k in hosts:
        snapshot["hosts"][k]["path"] = {"router": {"ok": router}, "modem": {"ok": modem}}
    return snapshot


class WhereTests(unittest.TestCase):
    settings = {"hosts": ["hermes", "acer"], "local_host": "macbook"}

    def where(self, s):
        return home_internet.where(s, self.settings)

    def test_verdicts(self):
        self.assertEqual(self.where(with_path(snap(host(False), host(False)), False, False)), "router")
        self.assertEqual(self.where(with_path(snap(host(False), host(False)), True, False)), "modem")
        self.assertEqual(self.where(with_path(snap(host(False), host(False)), True, True)), "upstream")
        self.assertIsNone(self.where(snap(host(False), host(False))))  # probe without path facts

    def test_any_host_reaching_the_router_clears_it(self):
        s = with_path(snap(host(False), host(False)), False, False, hosts=("hermes",))  # hermes lost Wi-Fi
        s = with_path(s, True, True, hosts=("acer",))                                 # wired acer is fine
        self.assertEqual(self.where(s), "upstream")


class ClassifyTests(unittest.TestCase):
    settings = {"hosts": ["hermes", "acer"], "local_host": "macbook"}

    def state(self, s):
        return home_internet.classify(s, self.settings)[0]

    def test_states(self):
        self.assertEqual(self.state(UP), "up")
        self.assertEqual(self.state(snap(host(False), host(True))), "up")  # one home host is enough
        self.assertEqual(self.state(DOWN), "down")
        self.assertEqual(self.state(AWAY_UNREACHABLE), "unreachable")
        self.assertEqual(self.state(UNKNOWN), "unknown")

    def test_stale_and_wanless_facts_are_not_evidence(self):
        # collector marks a failed host stale and keeps its last-good facts, old wan included
        self.assertEqual(self.state(snap(host(True, reachable=False, stale=True), host(reachable=False))),
                         "unreachable")
        self.assertEqual(self.state(snap(host(), host())), "unknown")  # probe predates the check

    def test_best_rtt_reported(self):
        s = snap(host(True), host(True))
        s["hosts"]["acer"]["wan"]["rtt_ms"] = 7
        self.assertEqual(home_internet.classify(s, self.settings), ("up", 7))


class OutageLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sent = []
        self.ok = True
        patcher = mock.patch.object(notify, "send", side_effect=self._send)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = {
            "home_internet": {"hosts": ["hermes", "acer"], "local_host": "macbook",
                              "state_file": str(Path(self.tmp.name) / "hi.json"),
                              "min_unconfirmed_seconds": 60, "notify_min_minutes": 10},
            "alerts": {"webhooks": [
                {"id": "discord", "url": "http://unused", "severities": ["critical"],
                 "rules": ["home-internet"]},
                {"id": "other", "url": "http://unused", "severities": ["critical"]},
            ]},
        }

    def _send(self, wh, text, timeout=5):
        if self.ok:
            self.sent.append((wh["id"], text))
        return self.ok

    def feed(self, snapshot, start, count, step=15):
        view = None
        for i in range(count):
            view = home_internet.update(snapshot, self.cfg, now=start + i * step)
        return view

    def test_disabled_without_config_section(self):
        self.assertIsNone(home_internet.update(UP, {"alerts": {}}, now=T0))
        self.assertFalse(Path(self.cfg["home_internet"]["state_file"]).exists())

    def test_short_confirmed_drop_is_logged_but_not_notified(self):
        self.feed(UP, T0, 2)
        view = self.feed(DOWN, T0 + 30, 10)            # 150s outage
        self.assertEqual(view["state"], "down")
        self.assertEqual(view["open"]["start"], T0 + 30)
        view = self.feed(UP, T0 + 180, 1)
        self.assertIsNone(view["open"])
        self.assertEqual(view["outages"], [{"start": T0 + 30, "end": T0 + 180,
                                            "seconds": 150, "confirmed": True}])
        self.assertEqual((view["count_24h"], view["count_7d"], view["downtime_7d_seconds"]), (1, 1, 150))
        self.assertEqual(self.sent, [])

    def test_long_outage_notifies_once_after_recovery_only_matching_webhooks(self):
        self.feed(UP, T0, 1)
        self.feed(DOWN, T0 + 15, 60)                    # 15 minutes down: nothing can be sent yet
        self.assertEqual(self.sent, [])
        self.feed(UP, T0 + 15 + 900, 5)
        self.assertEqual(len(self.sent), 1)
        wid, text = self.sent[0]
        self.assertEqual(wid, "discord")
        self.assertIn("Home internet was down for 15m", text)
        self.assertNotIn("not confirmed", text)

    def test_failed_delivery_retries_after_backoff(self):
        self.feed(DOWN, T0, 1)
        self.ok = False
        self.feed(UP, T0 + 700, 1)
        self.ok = True
        self.feed(UP, T0 + 715, 1)                      # inside backoff
        self.assertEqual(self.sent, [])
        self.feed(UP, T0 + 700 + 61, 1)
        self.assertEqual(len(self.sent), 1)
        self.feed(UP, T0 + 800, 3)
        self.assertEqual(len(self.sent), 1)

    def test_unknown_neither_opens_nor_closes(self):
        self.feed(UP, T0, 1)
        self.assertIsNone(self.feed(UNKNOWN, T0 + 15, 4)["open"])
        self.feed(DOWN, T0 + 75, 1)
        view = self.feed(UNKNOWN, T0 + 90, 10)          # MacBook lost the LAN mid-outage
        self.assertEqual(view["open"]["start"], T0 + 75)
        view = self.feed(UP, T0 + 300, 1)
        self.assertEqual(view["outages"][0]["seconds"], 225)

    def test_brief_unconfirmed_blip_is_dropped_longer_one_kept(self):
        self.feed(UP, T0, 1)
        self.feed(AWAY_UNREACHABLE, T0 + 15, 2)         # 30s: a Tailscale/ssh hiccup
        self.assertEqual(self.feed(UP, T0 + 45, 1)["outages"], [])
        self.feed(AWAY_UNREACHABLE, T0 + 60, 13)        # 195s while away
        view = self.feed(UP, T0 + 255, 1)
        self.assertEqual(len(view["outages"]), 1)
        self.assertFalse(view["outages"][0]["confirmed"])

    def test_confirmation_sticks_for_mixed_outage(self):
        self.feed(AWAY_UNREACHABLE, T0, 4)
        self.feed(DOWN, T0 + 60, 1)
        self.feed(AWAY_UNREACHABLE, T0 + 75, 40)
        view = self.feed(UP, T0 + 700, 1)
        self.assertTrue(view["outages"][0]["confirmed"])

    def test_outage_records_failing_hop_and_reports_it(self):
        self.feed(UP, T0, 1)
        self.feed(with_path(snap(host(False), host(False), mac_ok=False), True, True), T0 + 15, 2)
        self.feed(with_path(snap(host(False), host(False), mac_ok=False), True, False), T0 + 45, 40)
        view = self.feed(UP, T0 + 45 + 600, 1)
        outage = view["outages"][0]
        self.assertEqual(outage["where"], "modem")
        self.assertEqual(outage["where_samples"], {"upstream": 2, "modem": 40})
        self.assertIn("the router answered but the modem didn't", self.sent[0][1])

    def test_mac_dark_wakes_do_not_log_outages(self):
        # Asleep at home: a brief background wake every few minutes runs one
        # collection before Tailscale reconnects, so the home hosts look unreachable.
        self.feed(UP, T0, 2)
        for i in range(10):
            self.feed(AWAY_UNREACHABLE, T0 + 300 + i * 240, 1)
        view = self.feed(UP, T0 + 300 + 10 * 240, 3)    # full wake
        self.assertEqual(view["outages"], [])
        self.assertEqual(self.sent, [])

    def test_unconfirmed_outage_open_across_sleep_is_discarded(self):
        self.feed(UP, T0, 1)
        self.feed(AWAY_UNREACHABLE, T0 + 15, 10)        # seen while awake...
        self.feed(UP, T0 + 15 + 9 * 15 + 3600, 1)       # ...then an hour asleep
        view = self.feed(UP, T0 + 15 + 9 * 15 + 3615, 1)
        self.assertEqual(view["outages"], [])
        self.assertEqual(self.sent, [])

    def test_confirmed_outage_survives_sleep(self):
        self.feed(UP, T0, 1)
        self.feed(DOWN, T0 + 15, 4)                     # the home hosts said so
        view = self.feed(UP, T0 + 15 + 3 * 15 + 3600, 1)
        self.assertEqual(len(view["outages"]), 1)
        self.assertTrue(view["outages"][0]["confirmed"])

    def test_state_survives_restart_and_log_is_capped(self):
        self.cfg["home_internet"]["keep_outages"] = 3
        t = T0
        for _ in range(5):
            self.feed(DOWN, t, 1)
            self.feed(UP, t + 15, 1)
            t += 100
        self.feed(DOWN, t, 1)                           # open outage at "restart"
        view = home_internet.update(DOWN, self.cfg, now=t + 15)
        self.assertEqual(view["open"]["start"], t)
        self.assertEqual(len(view["outages"]), 3)
        self.assertEqual(view["outages"][0]["start"], T0 + 400)  # newest first


if __name__ == "__main__":
    unittest.main(verbosity=2)
