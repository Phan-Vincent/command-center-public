"""Regressions from the supplied QA archive, bound to canonical source."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN/'mcp'))
import reasonix_ledger as ledger

IDENTITY = {'mode': 'read', 'directory': tempfile.gettempdir(), 'host': None}
A, B = 'a'*64, 'b'*64
STOPPED = {'local_status': 'stopped', 'remote_status': 'not_applicable',
           'scope': 'owned_local_process_group', 'pipes_drained': True}
NOT_STARTED = {'local_status': 'not_started', 'remote_status': 'not_started',
               'scope': 'not_started', 'pipes_drained': True}


class AdversarialLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ledger.Ledger(self.tmp.name)
        self.path = Path(self.tmp.name)/(hashlib.sha256(b'objective').hexdigest()+'.json')

    def lock(self, identity=IDENTITY, timeout=10):
        return self.store.locked('objective', identity, timeout)

    def finish_flash(self, evidence=STOPPED):
        with self.lock() as session:
            session.before_launch(contract_hash=A)
            session.finish({'outcome': 'failed', 'process': evidence}, 'substantive')

    def test_null_does_not_reset_active_or_exhausted_history_or_overwrite_state(self):
        for exhausted in (False, True):
            with self.subTest(exhausted=exhausted):
                if self.path.exists(): self.path.unlink()
                with self.lock() as session:
                    session.before_launch(contract_hash=A)
                    if exhausted:
                        session.finish({'outcome': 'failed', 'process': STOPPED}, 'substantive')
                if exhausted:
                    with self.lock() as session:
                        session.before_launch('pro', 'flash_verification_failed', B)
                        session.finish({'outcome': 'failed', 'process': STOPPED}, 'substantive')
                self.path.write_text('null')
                with self.assertRaises(ValueError):
                    with self.lock() as session:
                        session.before_launch(contract_hash=A)
                self.assertEqual(self.path.read_text(), 'null')

    def test_clock_forward_then_rollback_never_regrows_observed_budget(self):
        with mock.patch.object(ledger.time, 'time', return_value=1000) as wall:
            with self.lock() as session:
                session.before_launch(contract_hash=A)
                wall.return_value = 1005
                observed = session.remaining()
                wall.return_value = 900
                self.assertLessEqual(session.remaining(), observed)
                wall.return_value = 1200
                exhausted = session.remaining()
                wall.return_value = 800
                self.assertLessEqual(session.remaining(), exhausted)
                with self.assertRaises(ValueError):
                    session.before_launch(contract_hash=A)

    def test_clock_rollback_does_not_revive_exhausted_stored_budget(self):
        with mock.patch.object(ledger.time, 'time', return_value=1000) as wall:
            with self.lock() as session:
                session.before_launch(contract_hash=A)
                wall.return_value = 1011
                session.finish({'outcome': 'failed', 'process': STOPPED}, 'substantive')
                self.assertLess(session.snapshot()['attempts'][-1]['remaining_budget'], 0)
            wall.return_value = 900
            before = self.path.read_bytes()
            with self.lock() as session:
                with self.assertRaisesRegex(ValueError, 'budget'):
                    session.before_launch('pro', 'flash_verification_failed', B)
            self.assertEqual(self.path.read_bytes(), before)

    def test_contradictory_positive_cleanup_fields_are_rejected(self):
        cases = [
            {**STOPPED, 'scope': 'not_started'},
            {**STOPPED, 'scope': 'unknown'},
            {**STOPPED, 'scope': 'owned_remote_process_group'},
            {**STOPPED, 'pipes_drained': False},
            {**NOT_STARTED, 'scope': 'owned_local_process_group'},
            {**NOT_STARTED, 'pipes_drained': False},
            {'local_status': 'running', 'scope': 'not_started'},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                ledger.process(case)

    def test_local_and_remote_stop_require_the_appropriate_evidence(self):
        cases = [
            (STOPPED, None, True),
            ({**STOPPED, 'remote_status': 'unknown'}, None, True),
            (NOT_STARTED, None, True),
            ({'local_status': 'not_started'}, None, False),
            ({}, None, False),
            ({**STOPPED, 'local_status': 'unknown'}, None, False),
            ({**STOPPED, 'remote_status': 'unknown'}, 'host', False),
            ({**STOPPED, 'remote_status': 'not_applicable'}, 'host', False),
            ({**STOPPED, 'remote_status': 'stopped'}, 'host', True),
            (NOT_STARTED, 'host', True),
            ({**NOT_STARTED, 'remote_status': 'stopped'}, 'host', False),
            ({**STOPPED, 'remote_status': 'running'}, None, False),
        ]
        for evidence, host, expected in cases:
            with self.subTest(evidence=evidence, host=host):
                self.assertEqual(ledger.safe(ledger.process(evidence), host), expected)

    def test_contradictory_finish_leaves_reservation_active_without_rewriting(self):
        with self.lock() as session:
            session.before_launch(contract_hash=A)
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                session.finish({'outcome': 'failed', 'process': {**STOPPED, 'scope': 'not_started'}}, 'substantive')
            self.assertEqual(self.path.read_bytes(), before)
        with self.lock() as session:
            with self.assertRaisesRegex(ValueError, 'unfinished'):
                session.before_launch('pro', 'flash_verification_failed', B)

    def test_corrupt_completed_cleanup_blocks_without_overwriting(self):
        self.finish_flash()
        data = json.loads(self.path.read_text())
        data['attempts'][-1]['process']['scope'] = 'not_started'
        self.path.write_text(json.dumps(data))
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            with self.lock() as session:
                session.before_launch('pro', 'flash_verification_failed', B)
        self.assertEqual(self.path.read_bytes(), before)

    @unittest.skipUnless(os.name == 'posix', 'POSIX FIFO and no-follow')
    def test_nonregular_state_rejects_promptly_and_releases_objective_lock(self):
        os.mkfifo(self.path)
        child = '''import sys
sys.path.insert(0, sys.argv[1])
from reasonix_ledger import Ledger
store = Ledger(sys.argv[2])
try:
    with store.locked('objective', {'mode':'read','directory':sys.argv[3],'host':None}, .1): pass
except (ValueError, OSError):
    raise SystemExit(0)
raise SystemExit(4)
'''
        proc = subprocess.Popen([sys.executable, '-c', child, str(PLUGIN/'mcp'), self.tmp.name,
                                 IDENTITY['directory']], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            out, err = proc.communicate(timeout=1)
            self.assertEqual(proc.returncode, 0, err)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=1)
        self.assertTrue(self.path.exists())
        self.path.unlink()
        # The failed reader must release the objective lock rather than strand it.
        with self.lock() as session:
            session.before_launch(contract_hash=A)

    @unittest.skipUnless(os.name == 'posix', 'POSIX FIFO')
    def test_fifo_lock_file_rejects_promptly(self):
        lockpath = self.path.with_suffix('.lock')
        os.mkfifo(lockpath)
        code = "import sys; sys.path.insert(0,sys.argv[1]); from reasonix_ledger import Ledger;\ntry:\n with Ledger(sys.argv[2]).locked('objective', {'mode':'read','directory':sys.argv[3],'host':None}, .1): pass\nexcept (ValueError,OSError): sys.exit(0)\nsys.exit(4)"
        proc = subprocess.Popen([sys.executable, '-c', code, str(PLUGIN/'mcp'), self.tmp.name,
                                 IDENTITY['directory']], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        try:
            _, err = proc.communicate(timeout=1)
            self.assertEqual(proc.returncode, 0, err)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=1)
        self.assertTrue(lockpath.exists())

    def test_directory_and_symlink_state_reject_without_replacing(self):
        self.path.mkdir()
        with self.assertRaises((ValueError, OSError)):
            with self.lock(): pass
        self.assertTrue(self.path.is_dir())
        self.path.rmdir()
        target = Path(self.tmp.name)/'target'
        target.write_text('null')
        self.path.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            with self.lock(): pass
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), 'null')


if __name__ == '__main__':
    unittest.main()
