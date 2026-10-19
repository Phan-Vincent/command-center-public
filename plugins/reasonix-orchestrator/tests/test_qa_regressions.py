"""Regression tests for the 2026-10-18 QA findings QA-4 and QA-5.

Disposable fixture processes, a mocked worker and temporary ledgers only.
"""
from __future__ import annotations

import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "mcp"))
import reasonix_engine as engine  # noqa: E402
import reasonix_process  # noqa: E402
import reasonix_verification as verification  # noqa: E402

STOPPED = {"local_status": "stopped", "remote_status": "not_applicable",
           "scope": "owned_local_process_group", "pipes_drained": True}


@unittest.skipUnless(hasattr(signal, "pthread_sigmask"), "POSIX signal masks required")
class CancellationMaskTests(unittest.TestCase):
    """QA-4: an inherited blocked mask must not turn cancellation into a timeout."""

    FIXTURE = r"""
import json, os, signal, sys, threading, time
sys.path.insert(0, sys.argv[1])
from reasonix_process import guarded_run, ProcessFailure
signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])
threading.Timer(.15, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
kind = None
try:
    guarded_run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=3, cleanup_seconds=.5)
except ProcessFailure as e:
    kind = e.kind
after = signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, [])
print(json.dumps({'kind': kind, 'still_blocked_after': after}), flush=True)
os._exit(0)
"""

    def test_blocked_sigterm_still_cancels_and_mask_is_restored(self):
        p = subprocess.run([sys.executable, "-c", self.FIXTURE, str(PLUGIN / "mcp")],
                           capture_output=True, text=True, timeout=15)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"kind": "cancelled", "still_blocked_after": True})

    def test_worker_thread_keeps_its_own_mask(self):
        seen = []

        def fake_popen(*args, **kwargs):
            seen.append(signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, []))
            raise OSError("synthetic launch failure")

        def run():
            signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])
            try:
                reasonix_process.guarded_run([sys.executable, "-c", "pass"], timeout=1)
            except Exception:  # noqa: BLE001
                pass

        with mock.patch.object(reasonix_process.subprocess, "Popen", side_effect=fake_popen):
            t = threading.Thread(target=run)
            t.start()
            t.join(5)
        self.assertEqual(seen, [True])


class ScopeSnapshotAccountingTests(unittest.TestCase):
    """QA-5: snapshot errors end the attempt, fail closed, and block retries."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.tmp)

    def _args(self, task_id):
        return SimpleNamespace(mode="write", authorize_writes=True, host=None, dir=str(self.tmp), model=None,
                               effort=None, max_steps=2, timeout_seconds=5, task_id=task_id,
                               escalation_reason=None, verify=[], files="fixture.txt")

    @staticmethod
    def _runner():
        done = subprocess.CompletedProcess([], 0, json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "OK"}), "")
        done.process = dict(STOPPED)
        return mock.Mock(return_value=done)

    def _attempts(self, ledger):
        return [a for p in ledger.rglob("*.json") for a in json.loads(p.read_text()).get("attempts", [])]

    def test_final_snapshot_errors_end_the_attempt_as_unknown(self):
        for i, error in enumerate((ValueError("synthetic git failure"), OSError("synthetic vanished file"))):
            args, ledger = self._args(f"scope-{i}"), self.tmp / f"ledger-{i}"
            with mock.patch.object(verification, "snapshot", side_effect=error):
                payload, code = engine.run_task(
                    args, "synthetic", runner=self._runner(), ledger_root=ledger,
                    adjudicator=lambda p, r, a=args: verification.verify(p, a, {}, r))
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error_code"], "SCOPE_CHECK_FAILED")
            self.assertEqual(payload["outcome"], "unknown")
            self.assertEqual(payload["scope"]["changed_files"], "unknown")
            self.assertEqual(payload["process"]["local_status"], "stopped")
            self.assertEqual([(a["phase"], a["outcome"]) for a in self._attempts(ledger)], [("ended", "unknown")])
            # An unknown outcome authorizes no continuation, not even Pro.
            retry = self._args(f"scope-{i}")
            retry.escalation_reason = "flash_verification_failed"
            out, _ = engine.run_task(retry, "synthetic retry", runner=self._runner(), ledger_root=ledger)
            self.assertFalse(out["ok"])
            self.assertEqual(len(self._attempts(ledger)), 1)

    def test_initial_snapshot_failure_launches_nothing(self):
        runner, ledger = self._runner(), self.tmp / "ledger-prepare"
        payload, code = engine.run_task(self._args("prepare"), "synthetic", runner=runner, ledger_root=ledger,
                                        prepare=mock.Mock(side_effect=OSError("synthetic")))
        self.assertEqual((code, payload["error_code"], payload["outcome"]), (2, "SCOPE_CHECK_FAILED", "blocked"))
        runner.assert_not_called()
        self.assertEqual(self._attempts(ledger), [])

    def test_blank_file_entries_authorize_nothing(self):
        self.assertEqual(verification.expected_files(" a.py , ,b.py,"), ["a.py", "b.py"])
        self.assertEqual(verification.expected_files(" "), [])


if __name__ == "__main__":
    unittest.main()
