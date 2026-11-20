#!/usr/bin/env python3
"""Backup units are watched in both systemd scopes and alert as critical (so Discord hears).

restic-peer-sync failed for 8 days (2026-10-18..25) as a dashboard-only warning, and the off-site
copies (restic-b2-copy, restic-append-*, restic-wekan) run as system units the probe never listed.
"""

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import alerts  # noqa: E402
import collector  # noqa: E402
import notify  # noqa: E402
import probe  # noqa: E402

USER_UNITS = """\
  restic-peer-sync.service        loaded inactive dead    Sync encrypted restic repositories
● restic-acer-config.service      loaded failed   failed  Back up acer config
  homelab-health.service          loaded inactive dead    Homelab health check
  pokemon-tcgcsv-sync.service     loaded failed   failed  Sync TCGplayer prices
  pokemon-tcgcsv-sync.timer       loaded active   waiting Daily TCGplayer sync
  pokemon-scanner.service         loaded inactive dead    Pokémon scanner (owns its own panel)
"""
SYSTEM_UNITS = """\
● restic-b2-copy.service          loaded failed   failed  Copy restic repos to B2
  restic-append-server.service    loaded active   running Append-only rest-server
  homelab-container-status.timer  loaded active   waiting Publish container states
  dpkg-db-backup.service          loaded inactive dead    Daily dpkg database backup
  hermes-gateway.service          loaded active   running Not ours at system level
"""


class ProbeUnitsTests(unittest.TestCase):
    def setUp(self):
        original = probe.sh
        self.addCleanup(setattr, probe, "sh", original)
        probe.sh = lambda cmd, *a, **k: USER_UNITS if "--user" in cmd else SYSTEM_UNITS

    def test_both_scopes_are_listed_with_their_scope(self):
        units = {(u["name"], u["scope"]): u for u in probe.systemd_units()}
        self.assertEqual(units[("restic-b2-copy.service", "system")]["state"], "failed")
        self.assertIn(("restic-append-server.service", "system"), units)
        self.assertIn(("homelab-container-status.timer", "system"), units)
        self.assertEqual(units[("restic-acer-config.service", "user")]["state"], "failed")
        self.assertIn(("homelab-health.service", "user"), units)

    def test_system_scope_takes_only_restic_and_homelab_units(self):
        names = {u["name"] for u in probe.systemd_units() if u["scope"] == "system"}
        self.assertNotIn("dpkg-db-backup.service", names)
        self.assertNotIn("hermes-gateway.service", names)
        # The TCGCSV sync and other Pokémon units are user units; system scope is unchanged.
        self.assertNotIn("pokemon-tcgcsv-sync.service", names)
        self.assertNotIn("pokemon-scanner.service", names)

    def test_tcgcsv_sync_units_are_collected_once_in_user_scope(self):
        units = [(u["name"], u["scope"]) for u in probe.systemd_units()]
        self.assertEqual(units.count(("pokemon-tcgcsv-sync.service", "user")), 1)
        self.assertEqual(units.count(("pokemon-tcgcsv-sync.timer", "user")), 1)
        # Only the bounded TCGCSV pair joins the generic collection.
        self.assertNotIn(("pokemon-scanner.service", "user"), units)


class TcgcsvSyncAlertTests(unittest.TestCase):
    """End-to-end: systemd fixture -> probe.systemd_units() -> svc-failed warning."""

    def setUp(self):
        original = probe.sh
        self.addCleanup(setattr, probe, "sh", original)
        probe.sh = lambda cmd, *a, **k: USER_UNITS if "--user" in cmd else SYSTEM_UNITS
        self.rules = collector.load_config()["alerts"]["rules"]

    def evaluate(self, units):
        h = {"reachable": True, "stale": False, "services": units}
        return alerts._evaluate_host("hermes", h, self.rules)

    def test_failed_oneshot_warns_once_with_svc_failed(self):
        found = [a for a in self.evaluate(probe.systemd_units())
                 if a.get("subject") == "pokemon-tcgcsv-sync.service"]
        self.assertEqual([(a["rule"], a["severity"]) for a in found],
                         [("svc-failed", "warning")])
        self.assertEqual(found[0]["message"], "hermes: pokemon-tcgcsv-sync.service is failed")

    def test_healthy_idle_oneshot_does_not_warn(self):
        units = probe.systemd_units()
        for u in units:
            if u["name"] == "pokemon-tcgcsv-sync.service":
                u["state"] = "inactive"  # a successful oneshot exits here
        found = [a for a in self.evaluate(units)
                 if a.get("subject") == "pokemon-tcgcsv-sync.service"]
        self.assertEqual(found, [])


class BackupAlertTests(unittest.TestCase):
    def setUp(self):
        self.cfg = collector.load_config()
        self.rules = self.cfg["alerts"]["rules"]

    def evaluate(self, services):
        h = {"reachable": True, "stale": False, "services": services}
        return alerts._evaluate_host("acer", h, self.rules)

    def test_failed_restic_unit_is_one_critical_backup_alert(self):
        found = self.evaluate([{"name": "restic-b2-copy.service", "state": "failed"}])
        self.assertEqual([(a["rule"], a["severity"]) for a in found], [("backup-failed", "critical")])
        self.assertEqual(found[0]["message"], "acer: restic-b2-copy.service is failed")

    def test_other_failed_units_stay_warnings(self):
        found = self.evaluate([{"name": "discord-interaction-handler.service", "state": "failed"}])
        self.assertEqual([(a["rule"], a["severity"]) for a in found], [("svc-failed", "warning")])

    def test_backup_alert_reaches_the_discord_webhook(self):
        discord = next(w for w in self.cfg["alerts"]["webhooks"] if w["type"] == "discord")
        alert = self.evaluate([{"name": "restic-peer-sync.service", "state": "failed"}])[0]
        self.assertTrue(notify.matches(discord, alert))

    def test_healthy_units_do_not_alert(self):
        self.assertEqual(self.evaluate([{"name": "restic-peer-sync.service", "state": "inactive"}]), [])

    def test_bad_pattern_is_reported_and_never_crashes(self):
        cfg = collector.load_config()
        cfg["alerts"]["rules"] = [{"id": "x", "type": "service", "match": "("}]
        self.assertIn("alerts.rules[0]: 'match' is not a valid regex", collector.validate_config(cfg))
        h = {"reachable": True, "services": [{"name": "restic-x.service", "state": "failed"}]}
        found = alerts._evaluate_host("acer", h, cfg["alerts"]["rules"])
        self.assertEqual(len(found), 1)  # an invalid pattern is ignored, not fatal


if __name__ == "__main__":
    unittest.main(verbosity=2)
