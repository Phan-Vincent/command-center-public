"""Offline objective-policy and adapter integration acceptance."""
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN/'mcp'))
import reasonix_engine as engine
from reasonix_ledger import Ledger

STOPPED = {'local_status': 'stopped', 'remote_status': 'not_applicable',
           'scope': 'owned_local_process_group', 'pipes_drained': True}
SUCCESS = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'OK'}
FAILURE = {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
           'error': {'message': 'fixture failed'}, 'error_code': 'TASK_FAILED'}


def completed(value, rc=0, state=None):
    out = subprocess.CompletedProcess([], rc, json.dumps(value), '')
    out.process = state or STOPPED
    return out


class EngineFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = self.root/'state'
        self.args = SimpleNamespace(mode='read', authorize_writes=False, host=None, dir=str(self.root),
            model=None, effort=None, max_steps=4, timeout_seconds=20, task_id='objective', escalation_reason=None)
        self.run = mock.Mock(return_value=completed(SUCCESS))

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, prompt='first contract'):
        return engine.run_task(self.args, prompt, runner=self.run, ledger_root=self.store)



class EngineTests(EngineFixture, unittest.TestCase):
    def test_success_is_execution_only_and_missing_usage_unknown(self):
        result, code = self.execute()
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(result['verification']['status'], 'not_run')
        self.assertEqual(result['usage']['tokens'], 'unknown')
        self.assertEqual(result['usage']['cost'], 'unknown')
        self.assertEqual(self.run.call_args.args[0][-2:], ['--model', 'deepseek-flash'])
        self.assertEqual(self.run.call_args.kwargs['stdin_text'], 'first contract')
        self.assertNotIn('first contract', self.run.call_args.args[0])

    def test_invalid_gate_and_modes_never_launch(self):
        for mode, flag in [('write', False), ('write', 1), ('read', True), ('bypassPermissions', True)]:
            with self.subTest(mode=mode, flag=flag):
                self.args.mode, self.args.authorize_writes = mode, flag
                result, code = self.execute()
                self.assertEqual(code, 2)
                self.assertFalse(result['ok'])
        self.run.assert_not_called()

    def test_pro_gates_and_uncorrected_contract(self):
        self.args.model = 'deepseek-pro'
        result, _ = self.execute()
        self.assertFalse(result['ok'])
        self.run.assert_not_called()
        self.args.escalation_reason = 'flash_verification_failed'
        result, _ = self.execute()
        self.assertFalse(result['ok'])
        self.run.assert_not_called()

    def test_flash_then_one_corrected_pro_and_no_second_pro(self):
        self.run.return_value = completed(FAILURE, 1)
        first, _ = self.execute()
        self.assertEqual(first['failure_class'], 'substantive')
        self.args.model, self.args.escalation_reason = 'deepseek-pro', 'flash_verification_failed'
        rejected, code = self.execute()
        self.assertEqual(code, 2)
        self.assertEqual(self.run.call_count, 1)
        self.args.timeout_seconds = 7200
        second, _ = self.execute('corrected contract')
        self.assertEqual(second['ledger']['deadline'], first['ledger']['deadline'])
        self.assertEqual(self.run.call_count, 2)
        self.assertLess(self.run.call_args.kwargs['timeout_seconds'], 20)
        final, _ = self.execute('third contract')
        self.assertFalse(final['ok'])
        self.assertEqual(self.run.call_count, 2)

    def test_initial_pro_has_no_second_substantive_attempt(self):
        self.args.model, self.args.escalation_reason = 'deepseek-pro', 'high_complexity'
        self.run.return_value = completed(FAILURE, 1)
        self.execute()
        final, code = self.execute('corrected contract')
        self.assertEqual(code, 2)
        self.assertFalse(final['ok'])
        self.assertEqual(self.run.call_count, 1)

    def test_one_transient_launch_retry_same_tier_no_deadline_extension(self):
        self.run.side_effect = OSError(errno.EAGAIN, 'temporarily unavailable')
        first, _ = self.execute()
        self.assertEqual(first['failure_class'], 'infrastructure')
        second, _ = self.execute()
        self.assertEqual(second['ledger']['deadline'], first['ledger']['deadline'])
        final, code = self.execute()
        self.assertEqual(code, 2)
        self.assertEqual(self.run.call_count, 2)
        self.assertFalse(final['ok'])

    def test_infrastructure_failure_does_not_escalate_or_change_contract(self):
        self.run.side_effect = OSError(errno.EAGAIN, 'temporarily unavailable')
        self.execute()
        rejected, _ = self.execute('changed')
        self.assertFalse(rejected['ok'])
        self.args.model, self.args.escalation_reason = 'deepseek-pro', 'high_complexity'
        rejected, _ = self.execute()
        self.assertFalse(rejected['ok'])
        self.assertEqual(self.run.call_count, 1)

    def test_missing_executable_and_credentials_block_without_retries(self):
        self.run.side_effect = FileNotFoundError(errno.ENOENT, 'missing', 'reasonix')
        first, _ = self.execute()
        self.assertEqual(first['failure_class'], 'blocked')
        self.execute()
        self.assertEqual(self.run.call_count, 1)
        self.args.task_id = 'credentials'
        self.run.side_effect = None
        self.run.return_value = completed({**FAILURE, 'error_code': 'missing_credential'}, 1)
        first, _ = self.execute()
        self.assertEqual(first['failure_class'], 'blocked')
        self.execute()
        self.assertEqual(self.run.call_count, 2)

    def test_malformed_envelope_cannot_escalate(self):
        self.run.return_value = completed({})
        first, _ = self.execute()
        self.assertFalse(first['ok'])
        self.assertEqual(first['failure_class'], 'unknown')
        self.args.model, self.args.escalation_reason = 'deepseek-pro', 'flash_verification_failed'
        self.execute('corrected')
        self.assertEqual(self.run.call_count, 1)

    def test_remote_success_keeps_unknown_process_and_blocks_continuation(self):
        self.args.host = 'explicit-host'
        first, _ = self.execute()
        self.assertEqual(first['process']['status'], 'unknown')
        self.execute()
        self.assertEqual(self.run.call_count, 1)

    def test_unknown_cleanup_blocks_corrected_pro(self):
        self.run.return_value = completed(FAILURE, 1, {**STOPPED, 'local_status': 'unknown'})
        self.execute()
        self.args.model, self.args.escalation_reason = 'deepseek-pro', 'flash_verification_failed'
        self.execute('corrected')
        self.assertEqual(self.run.call_count, 1)

    def test_identity_change_blocked_and_state_contains_no_prompt(self):
        self.run.return_value = completed(FAILURE, 1)
        first, _ = self.execute('private prompt not to store')
        serialized = next(self.store.glob('*.json')).read_text()
        self.assertNotIn('private prompt', serialized)
        self.assertNotIn('fixture failed', serialized)
        self.args.mode, self.args.authorize_writes = 'write', True
        final, _ = self.execute('corrected')
        self.assertFalse(final['ok'])
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(serialized, next(self.store.glob('*.json')).read_text())


    def test_permanent_launch_failure_is_not_infrastructure(self):
        self.run.side_effect = OSError(errno.EINVAL, 'invalid launch configuration')
        first, _ = self.execute()
        self.assertEqual(first['failure_class'], 'blocked')
        self.execute()
        self.assertEqual(self.run.call_count, 1)

    def test_config_error_without_code_is_blocked(self):
        self.run.return_value = completed({**FAILURE, 'error_code': '',
                                          'result': 'provider: unknown model configured-name'}, 1)
        first, _ = self.execute()
        self.assertEqual(first['failure_class'], 'blocked')
        self.execute()
        self.assertEqual(self.run.call_count, 1)

    def test_pro_infrastructure_retry_does_not_create_second_substantive_allowance(self):
        self.args.model, self.args.escalation_reason = 'deepseek-pro', 'high_complexity'
        self.run.side_effect = [OSError(errno.EAGAIN, 'transient'), completed(FAILURE, 1)]
        self.execute()
        second, _ = self.execute()
        self.assertEqual([a['kind'] for a in second['ledger']['attempts']], ['initial', 'infrastructure'])
        self.execute('new contract')
        self.assertEqual(self.run.call_count, 2)



class VerificationTransitionTests(EngineFixture, unittest.TestCase):
    def test_failed_check_finalizes_as_substantive_and_allows_corrected_pro(self):
        from reasonix_verification import verify
        options = SimpleNamespace(dir=str(self.root), verify=['false'], files=None)
        adjudicator = lambda payload, remaining: verify(payload, options, None, remaining)
        first, code = engine.run_task(self.args, 'first', runner=self.run,
                                     ledger_root=self.store, adjudicator=adjudicator)
        self.assertEqual(code, 1)
        self.assertEqual(first['verification']['status'], 'failed')
        self.assertEqual(first['ledger']['attempts'][-1]['classification'], 'substantive')
        self.args.model, self.args.escalation_reason = 'deepseek-pro', 'flash_verification_failed'
        result, code = self.execute('corrected')
        self.assertEqual(code, 0)
        self.assertEqual(len(result['ledger']['attempts']), 2)

    def test_unknown_verification_cleanup_blocks_next_checks_and_continuation(self):
        from reasonix_verification import verify
        import reasonix_verification as verification
        options = SimpleNamespace(dir=str(self.root), verify=['one', 'two'], files=None)
        item = {'name': 'one', 'executor': 'adapter', 'result': 'failed', 'classification': 'timeout',
                'evidence': {'process': {**STOPPED, 'local_status': 'unknown'}}}
        with mock.patch.object(verification, 'check', return_value=item) as check:
            first, code = engine.run_task(self.args, 'first', runner=self.run, ledger_root=self.store,
                adjudicator=lambda payload, remaining: verify(payload, options, None, remaining))
        self.assertEqual(check.call_count, 1)
        self.assertEqual(first['process']['status'], 'unknown')
        self.assertEqual(first['ledger']['attempts'][-1]['classification'], 'unknown')
        self.args.model, self.args.escalation_reason = 'deepseek-pro', 'flash_verification_failed'
        self.execute('corrected')
        self.assertEqual(self.run.call_count, 1)

    def test_verification_crash_leaves_active_reservation(self):
        def crash(payload, remaining):
            raise RuntimeError('fixture crash')
        with self.assertRaises(RuntimeError):
            engine.run_task(self.args, 'first', runner=self.run, ledger_root=self.store, adjudicator=crash)
        result, code = self.execute('corrected')
        self.assertEqual(code, 2)
        self.assertEqual(self.run.call_count, 1)
        data = json.loads(next(self.store.glob('*.json')).read_text())
        self.assertEqual(data['attempts'][-1]['phase'], 'active')

    def test_verification_cannot_reacquire_same_objective_while_locked(self):
        def callback(payload, remaining):
            with self.assertRaisesRegex(ValueError, 'busy'):
                with Ledger(self.store).locked('objective',
                     {'mode': 'read', 'directory': str(self.root), 'host': None}, 100): pass
        engine.run_task(self.args, 'first', runner=self.run, ledger_root=self.store, adjudicator=callback)

    def test_wall_clock_rollback_cannot_extend_current_verification_budget(self):
        import reasonix_ledger
        budgets = []
        def callback(payload, remaining):
            before = remaining()
            with mock.patch.object(reasonix_ledger.time, 'time', return_value=0):
                budgets.extend([before, remaining()])
        engine.run_task(self.args, 'first', runner=self.run, ledger_root=self.store, adjudicator=callback)
        self.assertLessEqual(budgets[1], budgets[0])

    def test_exhausted_budget_does_not_launch_check(self):
        from reasonix_verification import verify
        import reasonix_verification as verification
        options = SimpleNamespace(dir=str(self.root), verify=['one'], files=None)
        with mock.patch.object(verification, 'check') as check:
            result, code = engine.run_task(self.args, 'first', runner=self.run, ledger_root=self.store,
                adjudicator=lambda payload, remaining: verify(payload, options, None, lambda: 2.01))
        check.assert_not_called()
        self.assertEqual(result['ledger']['attempts'][-1]['classification'], 'timeout')
        self.assertEqual(result['verification']['checks'][0]['result'], 'not_run')



    def test_verification_budget_decreases_between_commands(self):
        from reasonix_verification import verify
        import reasonix_verification as verification
        options = SimpleNamespace(dir=str(self.root), verify=['one', 'two'], files=None)
        budgets = iter([10.0, 5.0])
        def callback(payload, remaining):
            verify(payload, options, None, lambda: next(budgets))
        item = {'result': 'passed', 'classification': 'success', 'evidence': {'process': STOPPED}}
        with mock.patch.object(verification, 'check', return_value=item) as check:
            engine.run_task(self.args, 'first', runner=self.run, ledger_root=self.store, adjudicator=callback)
        self.assertEqual([call.args[2] for call in check.call_args_list], [8.0, 3.0])



class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Ledger(self.tmp.name)
        self.identity = {'mode': 'read', 'directory': tempfile.gettempdir(), 'host': None}
        self.digest = hashlib.sha256(b'contract').hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_active_reservation_survives_crash_and_blocks_continuation(self):
        with self.store.locked('task', self.identity, 10) as session:
            session.before_launch(contract_hash=self.digest)
        with self.store.locked('task', self.identity, 100) as session:
            with self.assertRaisesRegex(ValueError, 'unfinished'):
                session.before_launch(contract_hash=self.digest)

    def test_cross_process_objective_lock(self):
        with self.store.locked('task', self.identity, 10):
            script = "from reasonix_ledger import Ledger; s=Ledger(%r);\nwith s.locked('task',%r,10): pass" % (self.tmp.name, self.identity)
            env = {**os.environ, 'PYTHONPATH': str(PLUGIN/'mcp'), 'PYTHONDONTWRITEBYTECODE': '1'}
            result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('objective busy', result.stderr)

    def test_insufficient_budget_does_not_reserve_attempt(self):
        with self.store.locked('task', self.identity, .02) as session:
            with self.assertRaisesRegex(ValueError, 'budget'):
                session.before_launch(contract_hash=self.digest)
            self.assertEqual(session.snapshot()['attempts'], [])

    def test_corruption_and_symlink_fail_closed(self):
        path = Path(self.tmp.name)/(hashlib.sha256(b'task').hexdigest()+'.json')
        for content in ('[]', '{"version":1,"version":2}', '{"deadline":NaN}'):
            path.write_text(content)
            with self.assertRaises(ValueError):
                with self.store.locked('task', self.identity, 10): pass
        path.unlink()
        outside = Path(self.tmp.name)/'outside'
        outside.write_text('{}')
        path.symlink_to(outside)
        with self.assertRaises(OSError):
            with self.store.locked('task', self.identity, 10): pass
        self.assertEqual(outside.read_text(), '{}')


class CrossAdapterTests(unittest.TestCase):
    def test_cli_and_real_stdio_mcp_share_one_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root/'reasonix'
            calls = root/'calls'
            fake.write_text(f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\np=Path({str(calls)!r}); p.write_text((p.read_text() if p.exists() else "")+"call\\n")\nprint(json.dumps({FAILURE!r}))\n')
            fake.chmod(0o755)
            env = {**os.environ, 'PATH': directory+os.pathsep+os.environ.get('PATH', ''),
                   'REASONIX_LEDGER_DIR': str(root/'state'), 'PYTHONDONTWRITEBYTECODE': '1'}
            cli = PLUGIN/'skills/reasonix-orchestrator/scripts/reasonix_delegate.py'
            run = subprocess.run([sys.executable, str(cli), 'run', '--mode', 'read', '--dir', directory,
                  '--task-id', 'same', '--timeout-seconds', '20'], input='first', env=env, capture_output=True, text=True)
            first = json.loads(run.stdout)
            def mcp(prompt):
                request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'delegate_read',
                           'arguments': {'dir': directory, 'task_contract': prompt, 'task_id': 'same',
                           'worker_tier': 'pro', 'escalation_reason': 'flash_verification_failed', 'timeout_seconds': 7200}}}
                run = subprocess.run([sys.executable, str(PLUGIN/'mcp/reasonix_mcp.py')], input=json.dumps(request)+'\n',
                                     env=env, capture_output=True, text=True, timeout=5)
                return json.loads(run.stdout)['result']['structuredContent']
            second = mcp('corrected')
            self.assertEqual(first['ledger']['deadline'], second['ledger']['deadline'])
            self.assertEqual(len(second['ledger']['attempts']), 2)
            third = mcp('third')
            self.assertEqual(third['outcome'], 'blocked')
            self.assertEqual(calls.read_text().splitlines(), ['call', 'call'])


if __name__ == '__main__':
    unittest.main()
