#!/usr/bin/env python3
"""Tailnet Lock / key-expiry collection and alert tests (no real Tailscale CLI)."""

import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import alerts  # noqa: E402
import collector  # noqa: E402
import notify  # noqa: E402


NOW = 1_800_000_000  # fixed epoch for deterministic expiry tests
T0 = float(NOW)  # notification lifecycle clock


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def date(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d")


def status(tailnet, reachable=True, stale=False):
    return {"hosts": {"macbook": {
        "reachable": reachable, "stale": stale, "tailnet": tailnet,
    }}}


def cfg(*rules):
    return {"alerts": {"rules": list(rules), "webhooks": []}}


class TailnetCollectTests(unittest.TestCase):
    def patch_tailscale(self, lock="", status_out="", exc=None, backend="Running"):
        calls = []
        original = collector._sh

        def fake(cmd, timeout=10, **kwargs):
            calls.append((list(cmd), timeout))
            if exc is not None:
                raise exc
            if "lock" in cmd:
                return lock
            if "status" in cmd:
                try:  # a valid status object gets the client's BackendState
                    data = json.loads(status_out)
                except ValueError:
                    return status_out
                if isinstance(data, dict) and backend is not None:
                    data.setdefault("BackendState", backend)
                return json.dumps(data)
            return ""

        collector._sh = fake
        self.addCleanup(setattr, collector, "_sh", original)
        return calls

    def test_lock_off_exact_shape_and_devices(self):
        lock = json.dumps({
            "SchemaVersion": "1", "Enabled": False,
            "PublicKey": "tlpub:abc", "NodeKey": "nodekey:def",
        })
        status_out = json.dumps({
            "Self": {"HostName": "macbook", "KeyExpiry": "2027-02-01T00:00:00Z",
                     "Tags": ["tag:laptop"]},
            "Peer": {
                "nodekey:peer1": {"HostName": "hermes", "KeyExpiry": "0001-01-01T00:00:00Z",
                                  "Tags": []},
                "nodekey:peer2": {"HostName": "acer", "KeyExpiry": "2027-03-01T00:00:00Z",
                                  "Tags": ["tag:server"]},
            },
        })
        calls = self.patch_tailscale(lock=lock, status_out=status_out)

        tailnet = collector._tailnet()

        self.assertIs(tailnet["lock_enabled"], False)
        self.assertIsNone(tailnet["self_signed"])
        self.assertEqual(tailnet["locked_out"], [])
        self.assertEqual(tailnet["devices"], [
            {"name": "macbook", "id": None, "self": True, "tagged": True,
             "key_expiry": "2027-02-01T00:00:00Z"},
            {"name": "hermes", "id": "nodekey:peer1", "self": False, "tagged": False,
             "key_expiry": None},
            {"name": "acer", "id": "nodekey:peer2", "self": False, "tagged": True,
             "key_expiry": "2027-03-01T00:00:00Z"},
        ])
        self.assertIsNone(tailnet["error"])
        self.assertEqual([c[0][1:4] for c in calls],
                         [["status", "--json"], ["lock", "status", "--json"]])
        self.assertTrue(all(c[1] == 10 for c in calls))

    def test_tailscale_runs_as_the_cli_outside_a_shell(self):
        # Under launchd the app-bundle binary otherwise starts as the GUI and prints no JSON.
        seen = []
        original = collector._sh

        def fake(cmd, timeout=10, **kwargs):
            seen.append(kwargs.get("env") or {})
            return ""

        collector._sh = fake
        self.addCleanup(setattr, collector, "_sh", original)
        collector._tailnet()
        collector._tailscale_ip()
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(env.get("TAILSCALE_BE_CLI") == "1" for env in seen))

    def test_lock_state_is_unknown_unless_the_client_runs(self):
        # A stopped or logged-out client may say Enabled: false; that must not read as "lock off".
        for backend in ("Stopped", "NeedsLogin", None):
            self.patch_tailscale(lock=json.dumps({"Enabled": False}),
                                 status_out=json.dumps({"Self": {"HostName": "macbook"}}),
                                 backend=backend)
            tailnet = collector._tailnet()
            self.assertIsNone(tailnet["lock_enabled"], backend)
            self.assertEqual(tailnet["devices"][0]["name"], "macbook")
            self.assertIn("tailscale is", tailnet["error"])

    def test_lock_on_filtered_peers_and_unsigned_self(self):
        lock = json.dumps({
            "SchemaVersion": "1", "Enabled": True,
            "PublicKey": "tlpub:abc", "NodeKey": "nodekey:def",
            "NodeKeySigned": False,
            "FilteredPeers": {
                "nodekey:peer1": {"Name": "hermes.tailnet.ts.net."},
                "nodekey:peer2": {"HostName": "acer"},
                "nodekey:peer3": {"StableID": "stable-1"},
                "nodekey:peer4": {"NodeKey": "nodekey:0123456789abcdefXYZ"},
                "nodekey:peer5": {},
            },
        })
        self.patch_tailscale(lock=lock, status_out=json.dumps({}))

        tailnet = collector._tailnet()

        self.assertIs(tailnet["lock_enabled"], True)
        self.assertIs(tailnet["self_signed"], False)
        self.assertEqual(tailnet["locked_out"], [
            "hermes.tailnet.ts.net.", "acer", "stable-1", "nodekey:01234567",
        ])
        self.assertEqual(tailnet["devices"], [])
        self.assertIsNone(tailnet["error"])

    def test_missing_and_wrong_typed_fields_are_defensive(self):
        lock = json.dumps({"Enabled": True})  # NodeKeySigned + FilteredPeers missing
        self.patch_tailscale(lock=lock, status_out=json.dumps({
            "Self": {"HostName": "macbook"},
            "Peer": [],
        }))

        tailnet = collector._tailnet()

        self.assertIs(tailnet["lock_enabled"], True)
        self.assertIsNone(tailnet["self_signed"])
        self.assertEqual(tailnet["locked_out"], [])
        self.assertEqual(tailnet["devices"], [
            {"name": "macbook", "id": None, "self": True, "tagged": False, "key_expiry": None},
        ])
        self.assertIsNone(tailnet["error"])

        self.patch_tailscale(lock=json.dumps({"Enabled": "yes"}),
                             status_out=json.dumps({"Self": None, "Peer": {}}))
        tailnet = collector._tailnet()
        self.assertIsNone(tailnet["lock_enabled"])
        self.assertIsNone(tailnet["self_signed"])
        self.assertEqual(tailnet["locked_out"], [])
        self.assertEqual(tailnet["devices"], [])
        self.assertIsNone(tailnet["error"])

    def test_filtered_peers_list(self):
        self.patch_tailscale(
            lock=json.dumps({"Enabled": True, "FilteredPeers": [
                {"Name": "hermes"}, {"HostName": "acer"}, None,
            ]}), status_out="{}",
        )
        self.assertEqual(collector._tailnet()["locked_out"], ["hermes", "acer"])

    def test_device_ids_prefer_stable_id_then_id_then_node_key(self):
        self.patch_tailscale(
            lock=json.dumps({"Enabled": False}),
            status_out=json.dumps({
                "Self": {"HostName": "macbook", "PublicKey": "nodekey:self"},
                "Peer": {
                    "nodekey:peer1": {"HostName": "phone", "StableID": "stable-a", "ID": "n1"},
                    "nodekey:peer2": {"HostName": "phone", "ID": "n2"},
                    "nodekey:peer3": {"HostName": "phone"},
                    "nodekey:peer4": {"HostName": "phone", "NodeKey": "nodekey:field"},
                },
            }),
        )
        ids = [d["id"] for d in collector._tailnet()["devices"]]
        self.assertEqual(ids, ["nodekey:self", "stable-a", "n2",
                               "nodekey:peer3", "nodekey:peer4"])

        # A Self object that only carries the older NodeKey spelling still identifies.
        self.patch_tailscale(
            lock=json.dumps({"Enabled": False}),
            status_out=json.dumps({"Self": {"HostName": "macbook",
                                            "NodeKey": "nodekey:self-field"}}),
        )
        self.assertEqual(collector._tailnet()["devices"][0]["id"], "nodekey:self-field")

    def test_listed_peer_uses_its_node_key_field(self):
        self.patch_tailscale(
            lock=json.dumps({"Enabled": False}),
            status_out=json.dumps({"Peer": [{"HostName": "phone",
                                             "NodeKey": "nodekey:list"}]}),
        )
        self.assertEqual(collector._tailnet()["devices"][0]["id"], "nodekey:list")

    def test_cli_failure_preserves_devices(self):
        self.patch_tailscale(status_out=json.dumps({"Self": {"HostName": "macbook"}}))
        tailnet = collector._tailnet()
        self.assertIsNone(tailnet["lock_enabled"])
        self.assertEqual(tailnet["devices"][0]["name"], "macbook")
        self.assertIn("lock status", tailnet["error"])

    def test_cli_failure_never_raises(self):
        self.patch_tailscale(lock="", status_out="")
        tailnet = collector._tailnet()
        self.assertIsNone(tailnet["lock_enabled"])
        self.assertEqual(tailnet["devices"], [])
        self.assertIn("lock status", tailnet["error"])
        self.assertIn("status --json", tailnet["error"])

        self.patch_tailscale(exc=RuntimeError("no CLI"))
        tailnet = collector._tailnet()
        self.assertIsNone(tailnet["lock_enabled"])
        self.assertEqual(tailnet["devices"], [])
        self.assertIn("RuntimeError", tailnet["error"])

    def test_bad_json_preserves_the_readable_half(self):
        self.patch_tailscale(
            lock="not json",
            status_out=json.dumps({"Self": {"HostName": "macbook"}}),
        )
        tailnet = collector._tailnet()
        self.assertIsNone(tailnet["lock_enabled"])
        self.assertEqual(tailnet["devices"][0]["name"], "macbook")
        self.assertIn("lock status", tailnet["error"])
        self.assertNotIn("tailscale status --json", tailnet["error"])

        self.patch_tailscale(
            lock=json.dumps({"Enabled": True, "NodeKeySigned": True}),
            status_out="{broken",
        )
        tailnet = collector._tailnet()
        # Without a readable status the client's state is unknown, so the lock's is too.
        self.assertIsNone(tailnet["lock_enabled"])
        self.assertIsNone(tailnet["self_signed"])
        self.assertEqual(tailnet["devices"], [])
        self.assertIn("status --json", tailnet["error"])

    def test_zero_and_missing_key_expiry_become_none(self):
        self.patch_tailscale(
            lock=json.dumps({"Enabled": False}),
            status_out=json.dumps({
                "Self": {"HostName": "macbook", "KeyExpiry": "0001-01-01T00:00:00Z"},
                "Peer": {"p": {"HostName": "hermes"}},
            }),
        )
        tailnet = collector._tailnet()
        self.assertEqual([d["key_expiry"] for d in tailnet["devices"]], [None, None])

    def test_collect_local_includes_tailnet(self):
        self.patch_tailscale(lock=json.dumps({"Enabled": False}),
                             status_out=json.dumps({"Self": {"HostName": "macbook"}}))
        original_wan, original_dns = collector.wan_check, collector.dns_check
        collector.wan_check = lambda: {"ok": True}
        collector.dns_check = lambda: {"ok": True}
        self.addCleanup(setattr, collector, "wan_check", original_wan)
        self.addCleanup(setattr, collector, "dns_check", original_dns)

        facts = collector.collect_local({"agents": [], "projects": []})

        self.assertIn("tailnet", facts)
        self.assertIs(facts["tailnet"]["lock_enabled"], False)
        self.assertEqual(facts["tailnet"]["devices"][0]["name"], "macbook")


class TailnetAlertTests(unittest.TestCase):
    def tearDown(self):
        alerts.reset_state()

    def test_lock_off_fires_with_exact_message(self):
        found = alerts.evaluate(
            status({"lock_enabled": False, "self_signed": None, "locked_out": [],
                    "devices": []}),
            cfg({"id": "tailnet-lock", "type": "tailnet_lock", "hosts": ["macbook"],
                 "expect_enabled": True, "severity": "critical"}),
            now=NOW,
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["rule"], "tailnet-lock")
        self.assertEqual(found[0]["type"], "tailnet_lock")
        self.assertEqual(found[0]["severity"], "critical")
        self.assertEqual(found[0]["message"], "macbook: Tailnet Lock is off")

    def test_lock_rule_uses_default_id(self):
        found = alerts.evaluate(
            status({"lock_enabled": False}),
            cfg({"type": "tailnet_lock", "expect_enabled": True}),
            now=NOW,
        )
        self.assertEqual(found[0]["rule"], "tailnet-lock")

    def test_lock_does_not_fire_when_disabled_expectation_or_unknown(self):
        rule = {"id": "tailnet-lock", "type": "tailnet_lock", "expect_enabled": False}
        for lock_enabled in (False, True, None):
            self.assertEqual(
                alerts.evaluate(status({"lock_enabled": lock_enabled}), cfg(rule), now=NOW),
                [],
            )
        self.assertEqual(
            alerts.evaluate(
                status({"lock_enabled": None}),
                cfg({"id": "tailnet-lock", "type": "tailnet_lock", "expect_enabled": True}),
                now=NOW,
            ),
            [],
        )

    def test_enabled_lock_does_not_fire(self):
        self.assertEqual(
            alerts.evaluate(status({"lock_enabled": True}),
                            cfg({"type": "tailnet_lock", "expect_enabled": True}), now=NOW),
            [],
        )

    def test_locked_out_names_and_unsigned_self(self):
        tailnet = {
            "lock_enabled": True, "self_signed": False,
            "locked_out": ["hermes", "acer"], "devices": [],
        }
        found = alerts.evaluate(
            status(tailnet),
            cfg({"id": "tailnet-locked-out", "type": "tailnet_locked_out",
                 "hosts": ["macbook"], "severity": "warning"}),
            now=NOW,
        )
        self.assertEqual([a["subject"] for a in found], ["hermes", "acer", "macbook"])
        self.assertEqual(found[0]["message"],
                         "macbook: hermes is locked out of the tailnet "
                         "(needs a Tailnet Lock signature)")
        self.assertEqual(found[1]["message"],
                         "macbook: acer is locked out of the tailnet "
                         "(needs a Tailnet Lock signature)")
        self.assertEqual(found[2]["message"],
                         "macbook: macbook is locked out of the tailnet "
                         "(needs a Tailnet Lock signature)")
        self.assertTrue(all(a["rule"] == "tailnet-locked-out" for a in found))

    def test_locked_out_does_not_fire_when_signed(self):
        for signed in (True, None):
            tailnet = {"lock_enabled": True, "self_signed": signed,
                       "locked_out": [], "devices": []}
            self.assertEqual(
                alerts.evaluate(
                    status(tailnet),
                    cfg({"id": "tailnet-locked-out", "type": "tailnet_locked_out"}),
                    now=NOW,
                ),
                [],
            )

    def test_key_expiry_firing_and_nonfiring(self):
        rule = {"id": "tailnet-key-expiry", "type": "tailnet_key_expiry",
                "hosts": ["macbook"], "days": 14, "severity": "warning"}
        expiring = NOW + 3 * 86400
        tailnet = {"devices": [
            {"name": "macbook", "id": "stable-macbook", "self": True, "tagged": False,
             "key_expiry": iso(expiring)},
            {"name": "hermes", "id": "stable-hermes", "self": False, "tagged": False,
             "key_expiry": iso(NOW + 30 * 86400)},
            {"name": "acer", "id": "stable-acer", "self": False, "tagged": False,
             "key_expiry": None},
            {"name": "bad", "self": False, "tagged": False, "key_expiry": "not-a-date"},
            {"name": "zero", "self": False, "tagged": False,
             "key_expiry": "0001-01-01T00:00:00Z"},
        ]}
        found = alerts.evaluate(status(tailnet), cfg(rule), now=NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["subject"], f"stable-macbook@{date(expiring)}")
        self.assertEqual(found[0]["rule"], "tailnet-key-expiry")
        self.assertEqual(found[0]["message"],
                         f"macbook's Tailscale key expires {date(expiring)}")

    def test_key_expiry_parses_z_and_fractions(self):
        expiring = NOW + 2 * 86400
        expiry = datetime.fromtimestamp(expiring, timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.123456Z")
        found = alerts.evaluate(
            status({"devices": [{"name": "macbook", "key_expiry": expiry}]}),
            cfg({"id": "tailnet-key-expiry", "type": "tailnet_key_expiry",
                 "hosts": ["macbook"], "days": 14}),
            now=NOW,
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["message"],
                         f"macbook's Tailscale key expires {date(expiring)}")

    def test_key_expiry_default_is_fourteen_days(self):
        tailnet = {"devices": [{"name": "macbook", "key_expiry": iso(NOW + 10 * 86400)}]}
        found = alerts.evaluate(
            status(tailnet),
            cfg({"id": "tailnet-key-expiry", "type": "tailnet_key_expiry"}),
            now=NOW,
        )
        self.assertEqual(len(found), 1)

        tailnet = {"devices": [{"name": "macbook", "key_expiry": iso(NOW + 15 * 86400)}]}
        self.assertEqual(
            alerts.evaluate(
                status(tailnet),
                cfg({"id": "tailnet-key-expiry", "type": "tailnet_key_expiry"}),
                now=NOW,
            ),
            [],
        )

    def test_key_expiry_is_stable_then_expired(self):
        expires = NOW + 2 * 86400
        tailnet = {"devices": [{"name": "macbook", "self": True,
                                "key_expiry": iso(expires)}]}
        rule = cfg({"id": "tailnet-key-expiry", "type": "tailnet_key_expiry",
                    "hosts": ["macbook"], "days": 14})

        first = alerts.evaluate(status(tailnet), rule, now=NOW)[0]
        later = alerts.evaluate(status(tailnet), rule, now=NOW + 3600)[0]
        self.assertEqual(first["subject"], f"macbook@{date(expires)}")  # no id: old samples
        self.assertEqual(first["key"], later["key"])
        self.assertEqual(first["message"], later["message"])
        self.assertEqual(first["since"], later["since"])

        after = alerts.evaluate(status(tailnet), rule, now=expires + 1)[0]
        self.assertEqual(after["key"], first["key"])
        self.assertEqual(after["message"], f"macbook's Tailscale key expired {date(expires)}")

    def test_two_phones_with_different_ids_are_two_expiry_alerts(self):
        expires = NOW + 3 * 86400
        tailnet = {"devices": [
            {"name": "phone", "id": "stable-a", "key_expiry": iso(expires)},
            {"name": "phone", "id": "stable-b", "key_expiry": iso(expires)},
        ]}
        found = alerts.evaluate(
            status(tailnet),
            cfg({"id": "tailnet-key-expiry", "type": "tailnet_key_expiry",
                 "hosts": ["macbook"], "days": 14}),
            now=NOW,
        )
        self.assertEqual(len(found), 2)
        self.assertEqual([a["subject"] for a in found],
                         [f"stable-a@{date(expires)}", f"stable-b@{date(expires)}"])
        self.assertEqual([a["message"] for a in found],
                         [f"phone's Tailscale key expires {date(expires)}"] * 2)
        self.assertEqual(len({a["key"] for a in found}), 2)

    def test_key_renewal_changes_the_alert_identity(self):
        rule = cfg({"id": "tailnet-key-expiry", "type": "tailnet_key_expiry",
                    "hosts": ["macbook"], "days": 14})

        def sample(days):
            return status({"devices": [{"name": "macbook", "id": "stable-1",
                                        "key_expiry": iso(NOW + days * 86400)}]})

        first = alerts.evaluate(sample(2), rule, now=NOW)[0]
        renewed = alerts.evaluate(sample(9), rule, now=NOW + 15)[0]
        self.assertNotEqual(first["key"], renewed["key"])
        self.assertNotEqual(first["subject"], renewed["subject"])
        self.assertEqual(renewed["subject"], f"stable-1@{date(NOW + 9 * 86400)}")

    def test_tailnet_unknown_fires_on_error_and_unknown_lock(self):
        rule = cfg({"id": "tailnet-unreadable", "type": "tailnet_unknown",
                    "hosts": ["macbook"], "severity": "warning"})

        found = alerts.evaluate(
            status({"error": "tailscale lock status --json failed", "lock_enabled": True}),
            rule, now=NOW,
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["rule"], "tailnet-unreadable")
        self.assertEqual(found[0]["type"], "tailnet_unknown")
        self.assertEqual(found[0]["severity"], "warning")
        self.assertIn("lock status --json failed", found[0]["message"])
        self.assertNotIn("subject", found[0], "identity must not depend on the error text")

        found = alerts.evaluate(status({"error": None, "lock_enabled": None}),
                                rule, now=NOW)
        self.assertEqual(len(found), 1)
        self.assertIn("Tailnet Lock state is unknown", found[0]["message"])

        self.assertEqual(
            alerts.evaluate(status({"error": None, "lock_enabled": True}), rule, now=NOW),
            [],
        )

    def test_tailnet_unknown_is_suppressed_by_stale_or_unreachable_facts(self):
        rule = cfg({"id": "tailnet-unreadable", "type": "tailnet_unknown",
                    "hosts": ["macbook"], "severity": "warning"})
        for reachable, stale in ((False, False), (True, True)):
            self.assertEqual(
                alerts.evaluate(
                    status({"error": "boom", "lock_enabled": None},
                           reachable=reachable, stale=stale),
                    rule, now=NOW,
                ),
                [],
            )

    def test_rules_ignore_non_dict_tailnet(self):
        rules = cfg(
            {"id": "tailnet-lock", "type": "tailnet_lock", "expect_enabled": True},
            {"id": "tailnet-locked-out", "type": "tailnet_locked_out"},
            {"id": "tailnet-key-expiry", "type": "tailnet_key_expiry"},
        )
        for tailnet in (None, [], "nope", 5):
            self.assertEqual(alerts.evaluate(status(tailnet), rules, now=NOW), [])

    def test_stale_and_unreachable_facts_suppress_tailnet_alerts(self):
        firing = {
            "lock_enabled": False, "self_signed": False,
            "locked_out": ["hermes"],
            "devices": [{"name": "macbook", "key_expiry": iso(NOW - 86400)}],
        }
        rules = cfg(
            {"id": "tailnet-lock", "type": "tailnet_lock", "expect_enabled": True},
            {"id": "tailnet-locked-out", "type": "tailnet_locked_out"},
            {"id": "tailnet-key-expiry", "type": "tailnet_key_expiry"},
        )
        for reachable, stale in ((False, False), (True, True)):
            self.assertEqual(
                alerts.evaluate(status(firing, reachable=reachable, stale=stale),
                                rules, now=NOW),
                [],
            )

    def test_evaluate_host_defaults_now_to_time_time(self):
        expiry_epoch = time.time() + 86400
        h = {"reachable": True,
             "tailnet": {"devices": [{"name": "macbook", "key_expiry": iso(expiry_epoch)}]}}
        found = alerts._evaluate_host(
            "macbook", h, [{"id": "tailnet-key-expiry", "type": "tailnet_key_expiry"}])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["subject"], f"macbook@{date(expiry_epoch)}")

    def test_rule_hosts_filter_keeps_rules_macbook_only(self):
        rule = {"id": "tailnet-lock", "type": "tailnet_lock",
                "hosts": ["macbook"], "expect_enabled": True}
        other = {"hosts": {"hermes": {
            "reachable": True, "tailnet": {"lock_enabled": False},
        }}}
        self.assertEqual(alerts.evaluate(other, cfg(rule), now=NOW), [])


class TailnetNotificationTests(unittest.TestCase):
    """Fire -> rule-specific facts unknown (hold) -> observed recovery."""

    RULE_LOCK = {"id": "tailnet-lock", "type": "tailnet_lock",
                 "hosts": ["macbook"], "expect_enabled": True}
    RULE_LOCKED_OUT = {"id": "tailnet-locked-out", "type": "tailnet_locked_out",
                       "hosts": ["macbook"]}
    RULE_EXPIRY = {"id": "tailnet-key-expiry", "type": "tailnet_key_expiry",
                   "hosts": ["macbook"], "days": 14}
    RULE_UNKNOWN = {"id": "tailnet-unreadable", "type": "tailnet_unknown",
                    "hosts": ["macbook"]}

    def setUp(self):
        alerts.reset_state()
        self.addCleanup(alerts.reset_state)
        self.sent = []
        patcher = mock.patch.object(notify, "send", side_effect=self._send)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_file = Path(self.tmp.name) / "alert_state.json"

    def _send(self, wh, text, timeout=5):
        self.sent.append(text)
        return True

    def config(self, *rules, debounce=120):
        return {"alerts": {
            "rules": list(rules),
            "notify": {"min_active_seconds": debounce,
                       "max_sample_gap_seconds": 120,
                       "state_file": str(self.state_file)},
            "webhooks": [{"id": "discord", "type": "discord", "url": "http://unused"}],
        }}

    def run_evals(self, st, cfg, start, count, step=15):
        for i in range(count):
            alerts.evaluate(st, cfg, now=start + i * step)

    @staticmethod
    def healthy(*devices):
        return status({"lock_enabled": True, "self_signed": True,
                       "locked_out": [], "devices": list(devices)})

    def assert_held_then_resolved(self, rule, firing, unknown, healthy=None):
        cfg = self.config(rule)
        self.run_evals(firing, cfg, T0, 10)  # fires at T0+120
        self.assertEqual(len(self.sent), 1)

        self.run_evals(unknown, cfg, T0 + 150, 30)  # five minutes unobservable
        self.assertEqual(len(self.sent), 1, "unobservable facts must not resolve")

        recovered = healthy if healthy is not None else self.healthy()
        self.run_evals(recovered, cfg, T0 + 600, 8)
        self.assertEqual(len(self.sent), 1, "recovery must wait the full debounce")
        alerts.evaluate(recovered, cfg, now=T0 + 720)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("RESOLVED", self.sent[1])

    def test_lock_off_holds_while_lock_fact_unknown_then_resolves(self):
        off = status({"lock_enabled": False, "self_signed": None,
                      "locked_out": [], "devices": []})
        unknown = status({"lock_enabled": None, "self_signed": None,
                          "locked_out": [], "devices": [], "error": "lock status failed"})
        self.assert_held_then_resolved(self.RULE_LOCK, off, unknown)
        self.assertIn("Tailnet Lock is off", self.sent[1])

    def test_unknown_facts_restart_the_recovery_debounce(self):
        cfg = self.config(self.RULE_LOCK)
        off = status({"lock_enabled": False, "self_signed": None,
                      "locked_out": [], "devices": []})
        unknown = status({"lock_enabled": None, "self_signed": None,
                          "locked_out": [], "devices": []})
        self.run_evals(off, cfg, T0, 10)
        self.assertEqual(len(self.sent), 1)

        self.run_evals(self.healthy(), cfg, T0 + 150, 5)  # 60 s observed clean
        self.run_evals(unknown, cfg, T0 + 225, 1)         # not observable again
        self.run_evals(self.healthy(), cfg, T0 + 240, 5)  # 60 s more: under debounce
        self.assertEqual(len(self.sent), 1)
        self.run_evals(self.healthy(), cfg, T0 + 315, 5)  # full 120 s, then resolve
        self.assertEqual(len(self.sent), 2)

    def test_locked_out_holds_when_lock_state_is_unknown(self):
        firing = status({"lock_enabled": True, "self_signed": True,
                         "locked_out": ["hermes"], "devices": []})
        unknown = status({"lock_enabled": None, "self_signed": None,
                          "locked_out": [], "devices": []})
        self.assert_held_then_resolved(self.RULE_LOCKED_OUT, firing, unknown)

    def test_locked_out_holds_when_self_signature_is_unknown(self):
        firing = status({"lock_enabled": True, "self_signed": True,
                         "locked_out": ["hermes"], "devices": []})
        unknown = status({"lock_enabled": True, "self_signed": None,
                          "locked_out": [], "devices": []})
        self.assert_held_then_resolved(self.RULE_LOCKED_OUT, firing, unknown)

    def test_expiry_holds_when_the_device_list_is_unreadable(self):
        firing = status({"devices": [{"name": "macbook", "id": "stable-1",
                                      "key_expiry": iso(T0 + 2 * 86400)}]})
        # Collection failed after status --json: an error with no devices.
        unreadable = status({"devices": [], "lock_enabled": None,
                             "error": "tailscale status --json failed"})
        healthy = self.healthy({"name": "macbook", "id": "stable-1",
                                "key_expiry": iso(T0 + 30 * 86400)})
        self.assert_held_then_resolved(self.RULE_EXPIRY, firing, unreadable, healthy)

    def test_tailnet_unknown_waits_out_a_sleep_gap(self):
        cfg = self.config(self.RULE_UNKNOWN)
        healthy = status({"error": None, "lock_enabled": True})
        broken = status({"error": "tailscale status --json failed",
                         "lock_enabled": None})
        self.run_evals(healthy, cfg, T0, 4)
        self.run_evals(broken, cfg, T0 + 900, 1)  # first sample after waking: dropped
        self.assertEqual(self.sent, [])
        self.run_evals(broken, cfg, T0 + 915, 1)   # debounce starts on this sample
        self.run_evals(broken, cfg, T0 + 1020, 1)  # 120 s after waking, 105 s watched
        self.assertEqual(self.sent, [])
        self.run_evals(broken, cfg, T0 + 1035, 1)  # 120 s watched since waking
        self.assertEqual(len(self.sent), 1)
        self.assertIn("tailnet facts unreadable", self.sent[0])

    def test_tailnet_unknown_identity_is_stable_when_the_error_changes(self):
        cfg = self.config(self.RULE_UNKNOWN, debounce=0)
        first = alerts.evaluate(status({"error": "one", "lock_enabled": None}),
                                cfg, now=T0)
        self.assertEqual(len(self.sent), 1)
        second = alerts.evaluate(status({"error": "two", "lock_enabled": None}),
                                 cfg, now=T0 + 15)
        self.assertEqual(len(self.sent), 1, "same unreadable tailnet, no second alert")
        self.assertEqual(first[0]["key"], second[0]["key"])
        self.assertEqual(first[0]["since"], second[0]["since"])
        self.assertIn("two", second[0]["message"])


class TailnetConfigTests(unittest.TestCase):
    def test_four_rules_configured_for_macbook(self):
        rules = {r["id"]: r for r in collector.load_config()["alerts"]["rules"]}

        lock = rules["tailnet-lock"]
        self.assertEqual(lock["type"], "tailnet_lock")
        self.assertEqual(lock["hosts"], ["macbook"])
        self.assertIs(lock["expect_enabled"], True)  # the lock has been on since 2026-10-24
        self.assertEqual(lock["severity"], "critical")
        self.assertIn("Phase C", lock["_comment"])

        locked_out = rules["tailnet-locked-out"]
        self.assertEqual(locked_out["type"], "tailnet_locked_out")
        self.assertEqual(locked_out["hosts"], ["macbook"])
        self.assertEqual(locked_out["severity"], "warning")

        expiry = rules["tailnet-key-expiry"]
        self.assertEqual(expiry["type"], "tailnet_key_expiry")
        self.assertEqual(expiry["hosts"], ["macbook"])
        self.assertEqual(expiry["days"], 14)
        self.assertEqual(expiry["severity"], "warning")

        unreadable = rules["tailnet-unreadable"]
        self.assertEqual(unreadable["type"], "tailnet_unknown")
        self.assertEqual(unreadable["hosts"], ["macbook"])
        self.assertEqual(unreadable["severity"], "warning")

    def test_webhook_carries_tailnet_warnings(self):
        webhooks = collector.load_config()["alerts"]["webhooks"]
        rules = webhooks[0]["rules"]
        self.assertIn("tailnet-locked-out", rules)
        self.assertIn("tailnet-key-expiry", rules)
        self.assertIn("tailnet-unreadable", rules)

    def test_validate_config_passes(self):
        self.assertEqual(collector.validate_config(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
