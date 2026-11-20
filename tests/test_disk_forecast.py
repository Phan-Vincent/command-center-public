"""Disk-full forecast: daily peaks in metrics.disk_daily and the disk_forecast alert rule."""
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alerts  # noqa: E402
import metrics  # noqa: E402

RULE = {"id": "disk-forecast", "type": "disk_forecast", "target_pct": 95, "days": 21,
        "window_days": 14, "min_days": 7, "severity": "warning"}


def history(values):
    """Daily peaks ending yesterday, oldest first."""
    today = datetime.now(timezone.utc).date()
    return [{"day": (today - timedelta(days=len(values) - i)).isoformat(), "use_pct": v}
            for i, v in enumerate(values)]


def host(pct, values):
    return {"reachable": True, "disk": [{"use_pct": pct}], "disk_history": history(values)}


class ForecastRule(unittest.TestCase):
    def evaluate(self, h):
        return [a for a in alerts._evaluate_host("hermes", h, [RULE]) if a["rule"] == "disk-forecast"]

    def test_steady_growth_fires_with_days_left(self):
        found = self.evaluate(host(90, [83, 84, 85, 86, 87, 88, 89]))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["message"],
                         "hermes: disk 90% and growing 1.0 points/day; about 5 days to 95%")

    def test_far_off_target_is_quiet(self):
        self.assertEqual(self.evaluate(host(50, [49.5, 49.6, 49.7, 49.8, 49.9, 50, 50])), [])

    def test_shrinking_or_flat_disk_is_quiet(self):
        self.assertEqual(self.evaluate(host(50, [93, 93, 93, 93, 93, 93, 93])), [])
        self.assertEqual(self.evaluate(host(93, [93] * 7)), [])

    def test_needs_min_days(self):
        self.assertEqual(self.evaluate(host(90, [86, 87, 88, 89])), [])

    def test_already_past_target_is_left_to_disk_high(self):
        self.assertEqual(self.evaluate(host(96, [89, 90, 91, 92, 93, 94, 95])), [])

    def test_only_window_days_are_fitted(self):
        # A cleanup 14+ days ago, then steady growth. Fitting the old 99% days too would
        # give a falling trend and no alert.
        growth = [77 + i for i in range(13)]  # 77..89 over the last 13 days
        self.assertEqual(len(self.evaluate(host(90, [99] * 6 + growth))), 1)

    def test_bad_history_rows_are_ignored(self):
        h = host(90, [83, 84, 85, 86, 87, 88, 89])
        h["disk_history"] += [{"day": "not-a-day", "use_pct": 1}, {"day": "2026-01-01", "use_pct": None}, "junk"]
        self.assertEqual(len(self.evaluate(h)), 1)
        self.assertEqual(self.evaluate({"reachable": True, "disk": [{"use_pct": 90}], "disk_history": "junk"}), [])

    def test_stale_facts_are_not_judged(self):
        h = host(90, [83, 84, 85, 86, 87, 88, 89]) | {"stale": True}
        self.assertEqual(self.evaluate(h), [])


class DailyPeaks(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="metrics-")
        self.addCleanup(t.cleanup)
        patcher = mock.patch.object(metrics, "DB_PATH", Path(t.name) / "metrics.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def status(pct, reachable=True, stale=False):
        return {"hosts": {"hermes": {"reachable": reachable, "stale": stale, "disk": [{"use_pct": pct}]}}}

    def test_keeps_the_daily_peak_only_for_live_facts(self):
        metrics.record(self.status(91))
        metrics.record(self.status(93))
        metrics.record(self.status(92))
        metrics.record(self.status(99, stale=True))
        metrics.record(self.status(99, reachable=False))
        rows = metrics.disk_daily("hermes")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["use_pct"], 93)
        self.assertEqual(rows[0]["day"], time.strftime("%Y-%m-%d", time.gmtime()))

    def test_old_days_are_pruned(self):
        old = time.time() - (metrics.DISK_DAILY_RETENTION_DAYS + 2) * 86400
        with mock.patch.object(metrics.time, "time", return_value=old):
            metrics.record(self.status(50))
        self.assertEqual(len(metrics.disk_daily("hermes")), 1)
        metrics.record(self.status(60))
        self.assertEqual([r["use_pct"] for r in metrics.disk_daily("hermes")], [60])


if __name__ == "__main__":
    unittest.main()
