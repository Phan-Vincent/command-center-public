#!/usr/bin/env python3
"""Tailnet Lock / key-expiry collection and alert tests (no real Tailscale CLI)."""

import json
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import alerts  # noqa: E402
import collector  # noqa: E402


NOW = 1_800_000_000  # fixed epoch for deterministic expiry tests


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
            {"name": "macbook", "self": True, "tagged": True,
             "key_expiry": "2027-02-01T00:00:00Z"},
            {"name": "hermes", "self": False, "tagged": False, "key_expiry": None},
            {"name": "acer", "self": False, "tagged": True,
             "key_expiry": "2027-03-01T00:00:00Z"},
        ])
        self.assertIsNone(tailnet["error"])
        self.assertEqual([c[0][1:4] for c in calls],
                         [["status", "--json"], ["lock", "status", "--json"]])
        self.assertTrue(all(c[1] == 10 for c in calls))

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
            {"name": "macbook", "self": True, "tagged": False, "key_expiry": None},
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
            {"name": "macbook", "self": True, "tagged": False,
             "key_expiry": iso(expiring)},
            {"name": "hermes", "self": False, "tagged": False,
             "key_expiry": iso(NOW + 30 * 86400)},
            {"name": "acer", "self": False, "tagged": False, "key_expiry": None},
            {"name": "bad", "self": False, "tagged": False, "key_expiry": "not-a-date"},
            {"name": "zero", "self": False, "tagged": False,
             "key_expiry": "0001-01-01T00:00:00Z"},
        ]}
        found = alerts.evaluate(status(tailnet), cfg(rule), now=NOW)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["subject"], "macbook")
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
        self.assertEqual(first["key"], later["key"])
        self.assertEqual(first["message"], later["message"])
        self.assertEqual(first["since"], later["since"])

        after = alerts.evaluate(status(tailnet), rule, now=expires + 1)[0]
        self.assertEqual(after["key"], first["key"])
        self.assertEqual(after["message"], f"macbook's Tailscale key expired {date(expires)}")

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
        expiry = iso(time.time() + 86400)
        h = {"reachable": True,
             "tailnet": {"devices": [{"name": "macbook", "key_expiry": expiry}]}}
        found = alerts._evaluate_host(
            "macbook", h, [{"id": "tailnet-key-expiry", "type": "tailnet_key_expiry"}])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["subject"], "macbook")

    def test_rule_hosts_filter_keeps_rules_macbook_only(self):
        rule = {"id": "tailnet-lock", "type": "tailnet_lock",
                "hosts": ["macbook"], "expect_enabled": True}
        other = {"hosts": {"hermes": {
            "reachable": True, "tailnet": {"lock_enabled": False},
        }}}
        self.assertEqual(alerts.evaluate(other, cfg(rule), now=NOW), [])


class TailnetConfigTests(unittest.TestCase):
    def test_three_rules_configured_for_macbook(self):
        rules = {r["id"]: r for r in collector.load_config()["alerts"]["rules"]}

        lock = rules["tailnet-lock"]
        self.assertEqual(lock["type"], "tailnet_lock")
        self.assertEqual(lock["hosts"], ["macbook"])
        self.assertIs(lock["expect_enabled"], False)
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

    def test_webhook_carries_locked_out_and_expiry(self):
        webhooks = collector.load_config()["alerts"]["webhooks"]
        rules = webhooks[0]["rules"]
        self.assertIn("tailnet-locked-out", rules)
        self.assertIn("tailnet-key-expiry", rules)

    def test_validate_config_passes(self):
        self.assertEqual(collector.validate_config(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
