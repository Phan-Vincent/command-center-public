"""Offline compatibility acceptance for single-attempt, no-Git-mutation defaults."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
ENTRY = PLUGIN/'scripts/delegate.py'
FAKE = PLUGIN/'tests/fake_reasonix.py'


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root/'repo'
        self.repo.mkdir()
        self.plan, self.calls = self.root/'plan.json', self.root/'calls.jsonl'
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        (self.repo/'user.txt').write_text('initial')
        self.git('add', 'user.txt')
        self.git('commit', '-qm', 'fixture baseline')
        self.head = self.git('rev-parse', 'HEAD')
        self.index = (self.repo/'.git/index').read_bytes()
        self.env = {**os.environ, 'DELEGATE_REASONIX_BIN': str(FAKE),
                    'REASONIX_LEDGER_DIR': str(self.root/'state'),
                    'FAKE_REASONIX_PLAN': str(self.plan), 'FAKE_REASONIX_CALLS': str(self.calls),
                    'PYTHONDONTWRITEBYTECODE': '1'}

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def run_task(self, steps, *args):
        self.plan.write_text(json.dumps(steps))
        result = subprocess.run([sys.executable, str(ENTRY), 'run', '--task-file', '-', *args],
                                input='implement the bounded task', cwd=self.repo, env=self.env,
                                capture_output=True, text=True, timeout=10)
        return result.returncode, json.loads(result.stdout)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def assert_no_git_mutation(self):
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        self.assertEqual(self.git('stash', 'list'), '')
        self.assertEqual((self.repo/'.git/index').read_bytes(), self.index)

    def test_authorized_dirty_work_preserved_without_commit_or_branch(self):
        (self.repo/'user.txt').write_text('user dirty edit')
        (self.repo/'untracked.txt').write_text('user untracked edit')
        code, result = self.run_task([{'write': {'new.txt': 'worker'}}], '--authorize-writes', '--files', 'new.txt')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['outcome'], 'partial')
        self.assertEqual(result['scope']['changed_files'], ['new.txt'])
        self.assertEqual((self.repo/'user.txt').read_text(), 'user dirty edit')
        self.assertEqual((self.repo/'untracked.txt').read_text(), 'user untracked edit')
        self.assert_no_git_mutation()
        self.assertEqual(self.recorded()[0]['opts']['permission-mode'], 'auto')

    def test_missing_write_gate_never_launches(self):
        code, result = self.run_task([{'write': {'new.txt': 'no'}}])
        self.assertEqual(code, 2)
        self.assertEqual(result['outcome'], 'blocked')
        self.assertEqual(self.recorded(), [])
        self.assertFalse((self.repo/'new.txt').exists())
        self.assert_no_git_mutation()

    def test_read_mode_stdin_no_automatic_verification(self):
        code, result = self.run_task([{}], '--read-only')
        self.assertEqual(code, 0, result)
        call = self.recorded()[0]
        self.assertEqual(call['opts']['permission-mode'], 'dontAsk')
        self.assertEqual(call['prompt'], 'implement the bounded task')
        self.assertEqual(result['verification']['status'], 'not_run')
        self.assert_no_git_mutation()

    def test_old_repeat_and_branch_flags_rejected_without_launch(self):
        for args in (['--attempts', '2'], ['--branch', 'new-branch']):
            code, result = self.run_task([{}], '--authorize-writes', *args)
            self.assertEqual(code, 2)
        self.assertEqual(self.recorded(), [])
        self.assert_no_git_mutation()

    def test_blank_files_rejected_without_launch(self):
        for files in (' , ', ' ', ''):
            code, result = self.run_task([{'write': {'new.txt': 'no'}}], '--authorize-writes', '--files', files)
            self.assertEqual((code, result['outcome']), (2, 'blocked'), result)
            self.assertIn('--files must name at least one path', result['error'])
        self.assertEqual(self.recorded(), [])
        self.assertFalse((self.repo/'new.txt').exists())
        self.assert_no_git_mutation()

    def test_failure_retains_changes_without_reset_or_stash_or_retry(self):
        code, result = self.run_task([{'is_error': True, 'write': {'partial.txt': 'retained'},
                                      'error_code': 'TASK_FAILED'}], '--authorize-writes')
        self.assertNotEqual(code, 0)
        self.assertEqual((self.repo/'partial.txt').read_text(), 'retained')
        self.assertEqual(len(self.recorded()), 1)
        self.assert_no_git_mutation()

    def test_extra_file_detected_and_retained(self):
        code, result = self.run_task([{'write': {'expected.txt': 'ok', 'extra.txt': 'review'}}],
                                    '--authorize-writes', '--files', 'expected.txt')
        self.assertEqual(code, 1)
        self.assertEqual(result['failure']['code'], 'SCOPE_DEVIATION')
        self.assertEqual(result['scope']['deviations'], ['extra.txt'])
        self.assertEqual((self.repo/'extra.txt').read_text(), 'review')
        self.assert_no_git_mutation()

    def test_all_required_checks_run_and_failure_reported(self):
        code, result = self.run_task([{}], '--authorize-writes', '--verify', 'true', '--verify', 'false')
        self.assertEqual(code, 1)
        self.assertEqual([c['result'] for c in result['verification']['checks']], ['passed', 'failed'])
        self.assertEqual(result['failure']['code'], 'VERIFICATION_FAILED')
        self.assert_no_git_mutation()

    def test_verification_file_deviation_detected(self):
        code, result = self.run_task([{}], '--authorize-writes', '--files', 'expected.txt',
                                    '--verify', 'printf extra > unexpected.txt')
        self.assertEqual(code, 1)
        self.assertEqual(result['scope']['deviations'], ['unexpected.txt'])
        self.assert_no_git_mutation()

    def test_instrumented_git_invokes_no_mutating_subcommand(self):
        bins = self.root/'bin'
        bins.mkdir()
        log = self.root/'git-calls'
        real = shutil.which('git')
        shim = bins/'git'
        shim.write_text(f'#!{sys.executable}\nimport os,sys\nopen({str(log)!r},"a").write(sys.argv[1]+"\\n")\nos.execv({real!r},[{real!r}]+sys.argv[1:])\n')
        shim.chmod(0o755)
        self.env['PATH'] = str(bins)+os.pathsep+os.environ.get('PATH', '')
        code, result = self.run_task([{}], '--authorize-writes', '--files', 'expected.txt')
        self.assertEqual(code, 0, result)
        self.assertEqual(log.read_text().splitlines(), ['ls-files', 'ls-files'])
        self.assert_no_git_mutation()

    def test_standalone_package_has_resolving_core_and_identical_bundle(self):
        core = PLUGIN.parent/'reasonix-orchestrator'
        manifest = json.loads((PLUGIN/'bundle-manifest.json').read_text())
        import hashlib
        for name, digest in manifest.items():
            self.assertEqual((PLUGIN/name).read_bytes(), (core/name).read_bytes(), name)
            self.assertEqual(hashlib.sha256((PLUGIN/name).read_bytes()).hexdigest(), digest)
        skill = PLUGIN/'skills/deepseek-delegation'
        self.assertTrue((skill/'../reasonix-orchestrator/SKILL.md').is_file())


if __name__ == '__main__':
    unittest.main()
