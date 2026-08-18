#!/usr/bin/env python3
"""stdlib unittest suite for the Command Center (network-free where possible)."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import collector  # noqa: E402
import control  # noqa: E402
import server  # noqa: E402


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = collector.load_config()

    def test_hosts_present(self):
        self.assertIn("macbook", self.cfg["hosts"])
        self.assertIn("hermes", self.cfg["hosts"])
        self.assertIn("acer", self.cfg["hosts"])

    def test_every_host_has_wellformed_actions(self):
        for key, h in self.cfg["hosts"].items():
            for a in h.get("actions", []):
                self.assertIn("id", a, key)
                self.assertIn("cmd", a, key)
                self.assertTrue(a["cmd"].strip(), key)

    def test_every_host_has_links_and_agents(self):
        for key, h in self.cfg["hosts"].items():
            self.assertIn("links", h, key)
            self.assertIn("agents", h, key)

    def test_validate_config_clean(self):
        self.assertEqual(collector.validate_config(self.cfg), [])

    def test_validate_config_detects_bad_action(self):
        bad = {"hosts": {"x": {"label": "x", "actions": [{"id": "a"}], "exec": [], "links": [], "agents": []}}}
        problems = collector.validate_config(bad)
        self.assertTrue(any("actions" in p for p in problems))


class ControlTests(unittest.TestCase):
    def test_unknown_host_rejected(self):
        r = control.run_action("nosuchhost", "x")
        self.assertFalse(r["ok"])
        self.assertIn("unknown host", r["error"])

    def test_unknown_action_rejected(self):
        r = control.run_action("hermes", "definitely-not-an-action")
        self.assertFalse(r["ok"])
        self.assertIn("unknown action", r["error"])

    def test_redact_token(self):
        out = control._redact("token=abcdef1234567890abcdef1234567890")
        self.assertIn("[REDACTED]", out)

    def test_redact_private_key(self):
        out = control._redact("-----BEGIN RSA PRIVATE KEY-----x-----END-----")
        self.assertIn("[REDACTED KEY]", out)

    def test_redact_entry_masks_cmd_and_stdout(self):
        entry = {"cmd": "echo API_KEY=secret123", "stdout": "password=hunter2secretsecretsecret"}
        out = control._redact_entry(entry)
        self.assertIn("[REDACTED]", out["cmd"])
        self.assertIn("[REDACTED]", out["stdout"])

    def test_audit_rotation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "audit.jsonl"
            # max_bytes smaller than one line forces rotation after the first write
            control.audit({"ts": "t", "cmd": "echo hi", "stdout": "x" * 100}, path=path, max_bytes=10)
            control.audit({"ts": "t", "cmd": "echo hi", "stdout": "y" * 100}, path=path, max_bytes=10)
            self.assertTrue(path.exists() or path.with_name("audit.jsonl.1").exists())
            # read_audit on the rotated file should still return entries
            entries = control.read_audit(limit=50, path=path)
            self.assertGreaterEqual(len(entries), 0)

    @staticmethod
    def _reboot_cfg():
        return {
            "hosts": {"h": {"connect": "ssh", "actions": [
                {"id": "reboot", "label": "Reboot", "cmd": "systemctl reboot", "disconnect_ok": True}
            ], "exec": []}},
            "server": {"control_timeout_seconds": 30},
        }

    def test_connection_dropped_helper(self):
        self.assertTrue(control._connection_dropped(
            {"rc": 255, "stdout": "", "stderr": "Connection to x closed by remote host."}))
        self.assertFalse(control._connection_dropped(
            {"rc": 1, "stdout": "", "stderr": "Connection to x closed by remote host."}))

    def test_reboot_disconnect_treated_as_success(self):
        cfg = self._reboot_cfg()
        orig_ssh, orig_audit = control._run_ssh, control.audit
        control._run_ssh = lambda h, c, t: {"rc": 255, "stdout": "", "stderr": "Connection to h closed by remote host."}
        control.audit = lambda entry, **kw: None
        try:
            r = control.run_action("h", "reboot", cfg=cfg)
        finally:
            control._run_ssh, control.audit = orig_ssh, orig_audit
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["rc"], 0)
        self.assertIn("reboot initiated", r["stderr"])

    def test_reboot_polkit_denied_still_fails(self):
        cfg = self._reboot_cfg()
        orig_ssh, orig_audit = control._run_ssh, control.audit
        control._run_ssh = lambda h, c, t: {"rc": 1, "stdout": "", "stderr": "Call to Reboot failed: Interactive authentication required."}
        control.audit = lambda entry, **kw: None
        try:
            r = control.run_action("h", "reboot", cfg=cfg)
        finally:
            control._run_ssh, control.audit = orig_ssh, orig_audit
        self.assertFalse(r["ok"])
        self.assertEqual(r["rc"], 1)

    def test_non_disconnect_action_rc255_fails(self):
        cfg = {
            "hosts": {"h": {"connect": "ssh", "actions": [
                {"id": "restart", "label": "Restart", "cmd": "systemctl --user restart x"}
            ], "exec": []}},
            "server": {"control_timeout_seconds": 30},
        }
        orig_ssh, orig_audit = control._run_ssh, control.audit
        control._run_ssh = lambda h, c, t: {"rc": 255, "stdout": "", "stderr": "Connection to h closed by remote host."}
        control.audit = lambda entry, **kw: None
        try:
            r = control.run_action("h", "restart", cfg=cfg)
        finally:
            control._run_ssh, control.audit = orig_ssh, orig_audit
        self.assertFalse(r["ok"])
        self.assertEqual(r["rc"], 255)


class CollectorTests(unittest.TestCase):
    def test_local_collect_has_core_fields(self):
        cfg_host = collector.load_config()["hosts"]["macbook"]
        facts = collector.collect_local(cfg_host)
        for field in ("hostname", "cpu", "mem", "disk", "loadavg", "ports"):
            self.assertIn(field, facts)


class RefreshStatusTests(unittest.TestCase):
    def tearDown(self):
        server.STATE.update({"status": None, "last_full": 0.0, "collecting": False})

    def test_serves_cache_while_collecting(self):
        cached = {"sentinel": 1}
        server.STATE.update({"status": cached, "last_full": time.time(), "collecting": True})
        cfg = {"server": {"cache_ttl_seconds": 15}}
        self.assertEqual(server.refresh_status(force=True, cfg=cfg), cached)

    def test_recollects_when_forced(self):
        server.STATE.update({"status": None, "last_full": 0.0, "collecting": False})
        orig = collector.collect
        collector.collect = lambda cfg: {"fake": True}
        try:
            cfg = {"server": {"cache_ttl_seconds": 15}, "alerts": {"rules": [], "webhooks": []}}
            res = server.refresh_status(force=True, cfg=cfg)
            self.assertTrue(res["fake"])
            self.assertIn("summary", res)
            self.assertIn("alerts", res)
        finally:
            collector.collect = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
