"""Regression tests for the 2026-10-18 QA findings (QA-1..QA-3, QA-6..QA-10).

Synthetic state only: notifications, SSH and metrics are mocked, state files
live in temporary directories, and no fleet host is contacted.
"""
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import alerts  # noqa: E402
import collector  # noqa: E402
import control  # noqa: E402
import healthcheck  # noqa: E402
import home_internet  # noqa: E402
import server  # noqa: E402
from agent_orchestrator.adapters import classify_reasonix_failure  # noqa: E402

WEBHOOKS = [{"id": "synthetic", "url": "https://example.invalid"}]


def _handler(path, body=None):
    h = object.__new__(server.Handler)
    h.path = path
    h.server = SimpleNamespace(cfg={"server": {}})
    h.headers = {"Content-Length": str(len(body or b""))}
    h.rfile = io.BytesIO(body or b"")
    h._send = mock.Mock()
    return h


class _AlertsCase(unittest.TestCase):
    def setUp(self):
        alerts.reset_state()
        patcher = mock.patch.object(alerts.notify, "send", return_value=True)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(alerts.reset_state)


class StaleFactsTests(_AlertsCase):
    """QA-1: rules that read facts are held while those facts are stale."""

    CFG = {"alerts": {"rules": [{"id": "pokemon-stale", "type": "pokemon_stale", "severity": "critical"}],
                      "notify": {"min_active_seconds": 60, "max_sample_gap_seconds": 120},
                      "webhooks": WEBHOOKS}}

    @staticmethod
    def _host(reachable, stale):
        return {"hosts": {"hermes": {"reachable": reachable, "stale": stale, "pokemon_scanner": {
            "available": True, "last_success_at": "2000-01-01T00:00:00Z"}}}}

    def test_stale_snapshot_never_fires_fact_rules(self):
        alerts.evaluate({"hosts": {}}, self.CFG, now=0)
        alerts.evaluate(self._host(False, True), self.CFG, now=30000)
        alerts.evaluate(self._host(False, True), self.CFG, now=31000)
        self.send.assert_not_called()

    def test_fresh_snapshot_still_fires(self):
        alerts.evaluate(self._host(True, False), self.CFG, now=0)
        alerts.evaluate(self._host(True, False), self.CFG, now=61)
        self.assertEqual(self.send.call_count, 1)

    def test_collection_keys_are_never_cached_or_restored(self):
        facts = {"reachable": True, "errors": [], "stale": False, "id": "x", "as_of": "t", "uptime": 5}
        with mock.patch.dict(collector._LAST_GOOD, {}, clear=True), \
                mock.patch.object(collector, "collect_remote", return_value=facts):
            collector._collect_one("fixture", {"connect": "ssh"}, {"server": {"collect_timeout_seconds": 1}})
            self.assertEqual(collector._LAST_GOOD["fixture"], {"uptime": 5})
        poisoned = {"fixture": {"reachable": True, "errors": [], "uptime": 5}}
        with mock.patch.dict(collector._LAST_GOOD, poisoned, clear=True), \
                mock.patch.object(collector, "collect_remote", side_effect=OSError("synthetic offline")):
            _, entry, _ = collector._collect_one("fixture", {"connect": "ssh"}, {"server": {"collect_timeout_seconds": 1}})
        self.assertFalse(entry["reachable"])
        self.assertTrue(entry["stale"])
        self.assertTrue(entry["errors"])
        self.assertEqual(entry["uptime"], 5)


class RecoveryDebounceTests(_AlertsCase):
    """QA-7: an unobservable interval restarts the recovery debounce."""

    CFG = {"alerts": {"rules": [{"id": "svc-failed", "type": "service"}],
                      "notify": {"min_active_seconds": 60}, "webhooks": WEBHOOKS}}

    @staticmethod
    def _sample(reachable, failed=False):
        services = [{"name": "fixture.service", "state": "failed"}] if failed else []
        return {"hosts": {"fixture": {"reachable": reachable, "services": services}}}

    def test_recovery_needs_a_full_observed_debounce(self):
        alerts.evaluate(self._sample(True, True), self.CFG, now=0)
        alerts.evaluate(self._sample(True, True), self.CFG, now=61)
        self.assertEqual(self.send.call_count, 1)  # fired
        alerts.evaluate(self._sample(True), self.CFG, now=62)
        alerts.evaluate(self._sample(False), self.CFG, now=63)
        alerts.evaluate(self._sample(False), self.CFG, now=130)
        alerts.evaluate(self._sample(True), self.CFG, now=131)
        self.assertEqual(self.send.call_count, 1)  # held, not resolved on the first sample back
        alerts.evaluate(self._sample(True), self.CFG, now=191)
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("RESOLVED", self.send.call_args.args[1])


class PersistedStateTests(unittest.TestCase):
    """QA-2: corrupt state files degrade to a clean state instead of raising."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(alerts.reset_state)

    def _write(self, content):
        path = self.tmp / "state.json"
        path.write_text(content)
        return path

    def test_alert_state_rejects_non_objects_and_bad_entries(self):
        for content in ("null", "[]", '"x"'):
            alerts.reset_state()
            alerts._load(self._write(content))
        alerts.reset_state()
        alerts._load(self._write(json.dumps({
            "active": {"good": {"alert": {"host": "h", "rule": "r"}, "first_seen": 1, "notified": ["a", 3]},
                       "bad": [], "no_alert": {"first_seen": 1}},
            "resolved": "nope"})))
        self.assertEqual(set(alerts._STATE), {"good"})
        self.assertEqual(alerts._STATE["good"]["notified"], ["a"])
        self.assertEqual(alerts._RESOLVED, {})

    def test_internet_and_healthcheck_state_are_normalized(self):
        path = self._write("null")
        self.assertIsInstance(home_internet._load(path), dict)
        self.assertEqual(healthcheck.load_state(path), {})
        data = home_internet._load(self._write(json.dumps({
            "state": "up", "open": "x", "outages": [None, {"start": 1, "end": 2, "seconds": 1}],
            "pending": {"a": 1}, "retry_after": "soon"})))
        self.assertIsNone(data["open"])
        self.assertEqual(len(data["outages"]), 1)
        self.assertEqual(data["pending"], [])
        self.assertEqual(data["retry_after"], 0)
        # A state missing `notified` must not crash the next failing check.
        state = healthcheck.load_state(self._write(json.dumps({"failures": 5, "down_since": 1, "notified": "x"})))
        healthcheck.step(state, False, "synthetic", {}, threshold=1, now=10, send=mock.Mock(return_value=True))


class CollectingFlagTests(unittest.TestCase):
    """QA-2: a failed evaluation never leaves `collecting` set."""

    CFG = {"server": {"cache_ttl_seconds": 1}}

    def setUp(self):
        saved = {k: server.STATE[k] for k in ("status", "collecting", "last_full", "alerts")}
        self.addCleanup(server.STATE.update, saved)
        server.STATE.update(status={"cached": True}, collecting=False, last_full=0, alerts=[])
        for target, attr, kwargs in ((home_internet, "update", {"return_value": None}),
                                     (server.metrics, "record", {})):
            patcher = mock.patch.object(target, attr, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_evaluation_error_keeps_collecting_and_publishes(self):
        with mock.patch.object(collector, "collect", side_effect=lambda cfg: {"hosts": {}}) as collect, \
                mock.patch.object(alerts, "evaluate", side_effect=AttributeError("synthetic")):
            first = server.refresh_status(force=True, cfg=self.CFG)
            server.refresh_status(force=True, cfg=self.CFG)
        self.assertFalse(server.STATE["collecting"])
        self.assertEqual(collect.call_count, 2)
        self.assertEqual(first["hosts"], {})

    def test_unexpected_error_still_clears_collecting(self):
        with mock.patch.object(collector, "collect", return_value={"hosts": {}}), \
                mock.patch.object(server, "_summarize", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                server.refresh_status(force=True, cfg=self.CFG)
        self.assertFalse(server.STATE["collecting"])


class LogsAndControlTests(unittest.TestCase):
    """QA-3 and QA-9: allowlists always apply; malformed input is a 400."""

    def setUp(self):
        saved = server.STATE["status"]
        self.addCleanup(server.STATE.__setitem__, "status", saved)
        server.STATE["status"] = {"hosts": {"fixture": {"docker": [], "services": []}}}
        cfg = {"hosts": {"fixture": {"connect": "ssh"}}, "server": {}}
        for attr, kwargs in (("load_config", {"return_value": cfg}),
                             ("_run_ssh", {"return_value": {"rc": 0, "stdout": "", "stderr": ""}})):
            patcher = mock.patch.object(control, attr, **kwargs)
            setattr(self, attr.strip("_"), patcher.start())
            self.addCleanup(patcher.stop)

    def _status(self, h):
        return h._send.call_args.args[0]

    def test_empty_live_allowlist_allows_nothing(self):
        for ttype in ("container", "unit"):
            h = _handler(f"/api/logs?host=fixture&type={ttype}&target=--help")
            h.do_GET()
            self.assertEqual(self._status(h), 400)
        self.run_ssh.assert_not_called()

    def test_leading_hyphen_targets_are_rejected(self):
        self.assertFalse(control.fetch_logs("fixture", "container", "--help")["ok"])
        self.assertFalse(control.fetch_logs("fixture", "unit", "-x.service")["ok"])
        self.run_ssh.assert_not_called()
        control.fetch_logs("fixture", "unit", "fixture@12:34.service")
        self.assertIn("-u fixture@12:34.service", self.run_ssh.call_args.args[1])

    def test_malformed_tail_and_non_object_bodies_are_400(self):
        server.STATE["status"] = {"hosts": {"fixture": {"docker": [{"name": "app"}]}}}
        h = _handler("/api/logs?host=fixture&type=container&target=app&tail=abc")
        h.do_GET()
        self.assertEqual(self._status(h), 400)
        for route in ("/api/control", "/api/exec"):
            for body in (b"null", b"[]", b'"x"'):
                h = _handler(route, body)
                h.do_POST()
                self.assertEqual(self._status(h), 400, (route, body))
        self.run_ssh.assert_not_called()


class RedactionTests(unittest.TestCase):
    """QA-6: whole bearer values and PEM blocks are removed."""

    def test_bearer_and_pem_bodies_are_redacted(self):
        bearer = control._redact("Authorization: Bearer short_fixture_token")
        self.assertNotIn("short_fixture_token", bearer)
        for pem in ("-----BEGIN RSA PRIVATE KEY-----\nabc+/def+/ghi\n-----END RSA PRIVATE KEY-----",
                    "-----BEGIN OPENSSH PRIVATE KEY-----\nabc+/def+/ghi"):  # truncated block
            out = control._redact("before " + pem)
            self.assertNotIn("abc+/def+/ghi", out)
            self.assertIn("[REDACTED KEY]", out)


class FailureClassifierTests(unittest.TestCase):
    """QA-8: the engine's canonical failure code is consumed and kept."""

    def test_missing_executable_is_unavailable_not_retryable(self):
        from importlib import util
        spec = util.spec_from_file_location("qa_engine", ROOT / "plugins/reasonix-orchestrator/mcp/reasonix_engine.py")
        sys.path.insert(0, str(ROOT / "plugins/reasonix-orchestrator/mcp"))
        engine = util.module_from_spec(spec)
        spec.loader.exec_module(engine)
        raw = engine.failure_envelope({"ok": False, "error": "worker launch failed",
                                       "error_code": "EXECUTABLE_MISSING", "failure_class": "blocked"})
        out = classify_reasonix_failure(raw["error"], raw)
        self.assertEqual((out["category"], out["code"], out["retryable"]),
                         ("worker_unavailable", "EXECUTABLE_MISSING", False))

    def test_other_codes_keep_identity_without_becoming_retryable(self):
        for raw in ({"error_code": "SCOPE_CHECK_FAILED"}, {"failure": {"code": "SCOPE_CHECK_FAILED"}}):
            out = classify_reasonix_failure("Final scope snapshot failed", raw)
            self.assertEqual(out["code"], "SCOPE_CHECK_FAILED")
            self.assertFalse(out["retryable"])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class FrontendTests(unittest.TestCase):
    """QA-10: run the real web/app.js functions in a node VM with DOM stubs."""

    SCRIPT = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const src = fs.readFileSync(process.argv[1], 'utf8');
const slice = (a, b) => src.slice(src.indexOf(a), src.indexOf(b));
const state = { status: { generated_at: 't', hosts: {} }, confirmPending: false };
const nodes = new Map();
const ctx = vm.createContext({ state, console, Date, Map, Set,
  $: id => { if (!nodes.has(id)) nodes.set(id, { hidden: false, appendChild() {} }); return nodes.get(id); } });
vm.runInContext('const SEV_RANK = {crit:0,warn:1,info:2};' + slice('function makeIssue(', '/* --- main render pipeline'), ctx);
vm.runInContext('function secId(host, key) {return `sec-${host}-${key}`;}', ctx);
vm.runInContext(slice('function isAttentionSection(', 'function filterSectionContent('), ctx);
ctx.synthetic = { hosts: { hermes: { reachable: true } }, alerts: [
  { host: 'hermes', rule: 'custom-id', type: 'pokemon_stale', severity: 'critical', message: 'stale scanner' }] };
vm.runInContext('deriveIssues(synthetic)', ctx);
assert.equal(state.issues[0].sectionKey, 'pokemon');
assert.equal(vm.runInContext('isAttentionSection({dataset:{sectionId:"sec-hermes-pokemon"}},"hermes")', ctx), true);
let drawerRenders = 0;
ctx.renderAlertsDrawer = () => drawerRenders++;
for (const n of ['renderSummary','renderInternetDrawer','renderAlertBanner','deriveIssues','renderHostChips',
  'renderMobileSelect','renderIssueQueue','restoreSortState','applyFilters','updateRefreshUI']) ctx[n] = () => {};
vm.runInContext(slice('function render()', 'function renderSummary('), ctx);
vm.runInContext('render()', ctx);
assert.equal(drawerRenders, 1);
nodes.set('exec-hermes-verb', { value: 'unit-status', selectedIndex: 0, options: [{ dataset: { arg: 'unit' } }] });
nodes.set('exec-hermes-target', { value: 'unit:fixture@12:34.service' });
let sent;
ctx.api = async (url, opts) => { sent = JSON.parse(opts.body); return { ok: true, stdout: '' }; };
ctx.setActionOutput = ctx.toast = () => {};
vm.runInContext(slice('async function runExec(', '/* --- log tail'), ctx);
vm.runInContext('runExec("hermes")', ctx).then(() => assert.equal(sent.arg, 'fixture@12:34.service'))
  .catch(e => { console.error(e); process.exitCode = 1; });
"""

    def test_pokemon_routing_drawer_refresh_and_colon_targets(self):
        p = subprocess.run(["node", "-e", self.SCRIPT, str(ROOT / "web/app.js")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
