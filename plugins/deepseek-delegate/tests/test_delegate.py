#!/usr/bin/env python3
"""stdlib unittest suite for delegate.py, run against a fake reasonix binary.

    python3 -m unittest discover -s plugins/deepseek-delegate/tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DELEGATE = HERE.parent / "scripts" / "delegate.py"
FAKE = HERE / "fake_reasonix.py"

CALC_OK = "def add(a, b):\n    return a + b\n"
CALC_BAD = "def add(a, b):\n    return a - b\n"
VERIFY_CALC = "python3 -c \"import calc; assert calc.add(2, 3) == 5\""


class DelegateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.plan = Path(self.tmp.name) / "plan.json"
        self.calls = Path(self.tmp.name) / "calls.jsonl"
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "tester")
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        (self.repo / "README.md").write_text("hi\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers
    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True,
                              text=True, check=True).stdout.strip()

    def delegate(self, *args, plan=None, stdin=None):
        if plan is not None:
            self.plan.write_text(json.dumps(plan))
        env = dict(os.environ, FAKE_REASONIX_PLAN=str(self.plan),
                   FAKE_REASONIX_CALLS=str(self.calls),
                   DELEGATE_REASONIX_BIN=str(FAKE), PYTHONDONTWRITEBYTECODE="1")
        r = subprocess.run([sys.executable, str(DELEGATE), *args], cwd=self.repo, env=env,
                           capture_output=True, text=True, input=stdin)
        return r.returncode, r.stdout

    def run_task(self, plan, *extra):
        code, out = self.delegate("run", "--title", "add calc", "--task", "implement add()",
                                  "--verify", VERIFY_CALC, *extra, plan=plan)
        return code, json.loads(out)

    def recorded_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def log(self):
        p = self.repo / ".delegate" / "log.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()]

    # -- tests
    def test_success_first_try_commits_on_new_branch(self):
        code, out = self.run_task([{"write": {"calc.py": CALC_OK}, "cost": 0.0012}])
        self.assertEqual(code, 0, out)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["attempts"], ["flash:pass"])
        self.assertEqual(out["files_changed"], ["calc.py"])
        self.assertTrue(out["branch"].startswith("delegate/add-calc-"))
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), out["branch"])
        self.assertIn("1 file changed", out["diff_stat"])
        self.assertEqual(out["cost"], {"USD": 0.0012})
        msg = self.git("log", "-1", "--format=%B")
        self.assertTrue(msg.startswith("delegate(flash): add calc"))
        self.assertIn("Delegate-Model: deepseek-flash", msg)
        self.assertEqual(self.git("status", "--porcelain"), "")
        kinds = [r["kind"] for r in self.log()]
        self.assertEqual(kinds, ["call", "task"])
        call = self.log()[0]
        self.assertEqual((call["model"], call["input_tokens"], call["pass"]), ("flash", 1000, True))
        # Reasonix got the contract and the right flags
        c = self.recorded_calls()[0]
        self.assertNotIn("implement add()", " ".join(c["opts"].values()))  # prompt via stdin
        self.assertEqual(c["opts"]["model"], "deepseek-flash")
        self.assertEqual(c["opts"]["permission-mode"], "workspace-write")
        self.assertEqual(c["opts"]["output-format"], "json")
        self.assertIn('"open_questions"', c["prompt"])
        self.assertIn(VERIFY_CALC, c["prompt"])

    def test_escalates_flash_twice_then_pro(self):
        plan = [{"write": {"calc.py": CALC_BAD}}, {"write": {"calc.py": CALC_BAD}},
                {"write": {"calc.py": CALC_OK}}]
        code, out = self.run_task(plan)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["attempts"], ["flash:fail", "flash:fail", "pro:pass"])
        self.assertEqual(out["model"], "pro")
        calls = self.recorded_calls()
        self.assertEqual([c["opts"]["model"] for c in calls],
                         ["deepseek-flash", "deepseek-flash", "deepseek-pro"])
        self.assertNotIn("PREVIOUS ATTEMPT", calls[0]["prompt"])
        self.assertIn("PREVIOUS ATTEMPT #1 (flash) FAILED", calls[1]["prompt"])
        self.assertIn("AssertionError", calls[1]["prompt"])
        self.assertTrue(self.git("log", "-1", "--format=%s").startswith("delegate(pro):"))

    def test_ladder_exhausted_stashes_and_needs_human(self):
        code, out = self.run_task([{"write": {"calc.py": CALC_BAD}}])
        self.assertEqual(code, 1)
        self.assertEqual(out["status"], "needs_human")
        self.assertEqual(out["attempts"], ["flash:fail"] * 2 + ["pro:fail"] * 2)
        self.assertIsNone(out["commit"])
        self.assertIn("AssertionError", out["failure_tail"])
        self.assertIn("delegate-failed:", self.git("stash", "list"))
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.log()[-1]["status"], "needs_human")

    def test_credential_error_is_fatal_no_retry(self):
        plan = [{"is_error": True, "error_code": "missing_credential",
                 "raw": "provider \"deepseek-flash\": missing env DEEPSEEK_API_KEY"}]
        code, out = self.run_task(plan)
        self.assertEqual(out["status"], "error")
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_unknown_model_is_fatal(self):
        plan = [{"is_error": True, "raw": 'execution_model "x": unknown model "x"'}]
        code, out = self.run_task(plan)
        self.assertEqual(out["status"], "error")
        self.assertEqual(len(self.recorded_calls()), 1)

    def test_dirty_tree_is_blocked(self):
        (self.repo / "README.md").write_text("edited\n")
        code, out = self.run_task([{"write": {"calc.py": CALC_OK}}])
        self.assertEqual(code, 2)
        self.assertEqual(out["status"], "blocked")
        self.assertIn("README.md", out["error"])
        self.assertEqual(self.recorded_calls(), [])

    def test_empty_and_oversized_tasks_are_blocked(self):
        code, out = self.delegate("run", "--task", "   ", plan=[{}])
        self.assertEqual((code, json.loads(out)["status"]), (2, "blocked"))
        code, out = self.delegate("run", "--task-file", "-", plan=[{}], stdin="é" * 300_000)
        self.assertIn("bytes", json.loads(out)["error"])
        self.assertEqual(self.recorded_calls(), [])

    def test_security_sensitive_flagged(self):
        code_py = "import subprocess\n\ndef run(c):\n    return subprocess.run(c, shell=True)\n"
        code, out = self.delegate("run", "--task", "add runner", "--verify", "true",
                                  plan=[{"write": {"auth/runner.py": code_py}}])
        out = json.loads(out)
        self.assertTrue(out["security_sensitive"])
        self.assertTrue(any(r.startswith("path:auth/") for r in out["security_reasons"]))
        self.assertTrue(any("shell" in r for r in out["security_reasons"]))

    def test_plain_change_not_security_sensitive(self):
        code, out = self.run_task([{"write": {"calc.py": CALC_OK}}])
        self.assertFalse(out["security_sensitive"])

    def test_fenced_report_parsed_and_summary_capped(self):
        report = {"status": "done", "files_changed": ["calc.py"],
                  "summary": "\n".join(f"line {i}" for i in range(9)),
                  "test_result": "pass", "open_questions": ["keep int only?"]}
        raw = "Here you go:\n```json\n" + json.dumps(report) + "\n```"
        code, out = self.run_task([{"write": {"calc.py": CALC_OK}, "raw": raw}])
        self.assertEqual(out["summary"].splitlines(), [f"line {i}" for i in range(5)])
        self.assertEqual(out["open_questions"], ["keep int only?"])
        self.assertNotIn("report_parse_error", out)

    def test_worker_reported_failure_retries_without_verify(self):
        plan = [{"report": {"status": "failed", "summary": "stuck"}},
                {"write": {"calc.py": CALC_OK}}]
        code, out = self.run_task(plan)
        self.assertEqual(out["attempts"], ["flash:fail", "flash:pass"])

    def test_read_only_task_no_commit(self):
        head = self.git("rev-parse", "HEAD")
        code, out = self.delegate("run", "--read-only", "--task", "where is add defined?",
                                  plan=[{"report": {"status": "done", "summary": "nowhere yet"}}])
        out = json.loads(out)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["summary"], "nowhere yet")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.recorded_calls()[0]["opts"]["permission-mode"], "read-only")

    def test_pro_no_escalate_and_out_of_scope_blocks_commit(self):
        base = self.git("rev-parse", "HEAD")
        plan = [{"write": {"calc.py": CALC_OK, "extra.py": "x = 1\n"}}]
        code, out = self.run_task(plan, "--model", "pro", "--no-escalate", "--files", "calc.py")
        self.assertEqual(code, 1)
        self.assertEqual(out["attempts"], ["pro:pass"])
        self.assertEqual(out["status"], "needs_human")
        self.assertEqual(out["out_of_scope"], ["extra.py"])
        self.assertIsNone(out["commit"])
        self.assertIn("outside --files", out["error"])
        self.assertEqual(self.git("rev-parse", "HEAD"), base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertIn("delegate-out-of-scope:", self.git("stash", "list"))
        # Nothing is lost: the whole attempt comes back with one pop.
        self.git("stash", "pop", "-q")
        self.assertEqual((self.repo / "calc.py").read_text(), CALC_OK)
        self.assertEqual((self.repo / "extra.py").read_text(), "x = 1\n")
        self.assertEqual(self.log()[-1]["status"], "needs_human")

    def verify_run(self, verify, files=None, plan=None):
        args = ["run", "--title", "add calc", "--task", "implement add()", "--verify", verify]
        if files:
            args += ["--files", files]
        code, out = self.delegate(*args, plan=plan or [{"write": {"calc.py": CALC_OK}}])
        return code, json.loads(out)

    def test_verify_by_products_are_removed_not_committed(self):
        # Written before the assert, so the failed attempt 1 leaves one behind too.
        code, out = self.verify_run("echo cache > cache.json && " + VERIFY_CALC, "./calc.py",
                                    plan=[{"write": {"calc.py": CALC_BAD}},
                                          {"write": {"calc.py": CALC_OK}}])
        self.assertEqual(code, 0, out)
        self.assertEqual(out["attempts"], ["flash:fail", "flash:pass"])
        self.assertEqual(out["files_changed"], ["calc.py"])
        self.assertEqual(out["verify_removed"], ["cache.json"])
        self.assertNotIn("verify_edits", out)
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD"), "calc.py")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.repo / "cache.json").exists())

    def test_verify_by_products_removed_without_files_too(self):
        code, out = self.verify_run(VERIFY_CALC + " && echo cache > cache.json")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["files_changed"], ["calc.py"])
        self.assertEqual(out["verify_removed"], ["cache.json"])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_verify_edit_to_worker_file_is_committed_and_reported(self):
        # A formatter in verify rewrites a file the worker already changed: the
        # tests ran on the formatted version, so that is what gets committed.
        fmt = "python3 -c \"open('calc.py', 'a').write('# formatted\\n')\" && "
        code, out = self.verify_run(fmt + VERIFY_CALC, "calc.py")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["verify_edits"], ["calc.py"])
        self.assertEqual(self.git("show", "HEAD:calc.py"), CALC_OK + "# formatted")

    def test_verify_regenerated_tracked_file_is_committed_not_reverted(self):
        (self.repo / "fixture.txt").write_text("old\n")
        self.git("add", "fixture.txt")
        self.git("commit", "-q", "-m", "fixture")
        regen = "echo new > fixture.txt && grep -q new fixture.txt && " + VERIFY_CALC
        code, out = self.verify_run(regen, "calc.py,fixture.txt")
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["verify_edits"], ["fixture.txt"])
        self.assertEqual(self.git("show", "HEAD:fixture.txt"), "new")
        # The same regeneration outside --files goes to review instead.
        self.git("checkout", "-q", "main")
        code, out = self.verify_run(regen, "calc.py")
        self.assertEqual(out["status"], "needs_human")
        self.assertEqual(out["out_of_scope"], ["fixture.txt"])
        self.assertEqual(out["verify_edits"], ["fixture.txt"])
        self.assertEqual(self.git("show", "HEAD:fixture.txt"), "old")

    def test_absolute_and_dot_scope_entries(self):
        code, out = self.run_task([{"write": {"calc.py": CALC_OK}}],
                                  "--files", str(self.repo / "calc.py"))
        self.assertEqual(out["status"], "success")
        self.git("checkout", "-q", "main")
        code, out = self.run_task([{"write": {"calc.py": CALC_OK, "x.py": "X = 1\n"}}],
                                  "--files", ".")
        self.assertEqual(out["status"], "success")
        self.assertNotIn("out_of_scope", out)

    def test_directory_scope_entry_covers_its_files(self):
        code, out = self.delegate(
            "run", "--title", "add pkg", "--task", "add pkg", "--verify", "true",
            "--files", "pkg/", plan=[{"write": {"pkg/a.py": "A = 1\n", "pkg/sub/b.py": "B = 2\n"}}])
        out = json.loads(out)
        self.assertEqual(out["status"], "success")
        self.assertEqual(out["files_changed"], ["pkg/a.py", "pkg/sub/b.py"])
        self.assertNotIn("out_of_scope", out)

    def test_task_from_stdin_and_explicit_branch(self):
        code, out = self.delegate("run", "--task-file", "-", "--branch", "feat/x",
                                  "--verify", VERIFY_CALC,
                                  plan=[{"write": {"calc.py": CALC_OK}}],
                                  stdin="Add calc\nImplement add(a, b).\n")
        out = json.loads(out)
        self.assertEqual(out["branch"], "feat/x")
        self.assertEqual(out["title"], "Add calc")
        self.assertIn("Implement add(a, b).", self.recorded_calls()[0]["prompt"])

    def test_config_prices_fallback_and_stats(self):
        (self.repo / ".delegate").mkdir()
        (self.repo / ".delegate" / "config.json").write_text(json.dumps({
            "prices": {"flash": {"input": 1.0, "cache_hit": 0.1, "output": 2.0,
                                 "currency": "USD"}}}))
        self.run_task([{"write": {"calc.py": CALC_BAD}}, {"write": {"calc.py": CALC_OK}}])
        # 400 fresh * 1.0 + 600 cached * 0.1 + 200 out * 2.0 = 860 per call, / 1e6
        calls = [r for r in self.log() if r["kind"] == "call"]
        self.assertEqual([r["cost"] for r in calls], [0.00086, 0.00086])
        code, out = self.delegate("stats", "--json", "--compare", "3,15")
        s = json.loads(out)
        self.assertEqual(s["by_model"]["flash"]["calls"], 2)
        self.assertEqual(s["by_model"]["flash"]["pass"], 1)
        self.assertEqual(s["total"]["cost"], {"USD": 0.00172})
        self.assertEqual(s["tasks"]["success"], 1)
        self.assertEqual(s["tasks"]["first_try"], 0)
        self.assertEqual(s["compare"]["cost"], round((2000 * 3 + 400 * 15) / 1e6, 4))
        code, text = self.delegate("stats")
        self.assertIn("flash", text)
        self.assertIn("1/1 succeeded", text)

    def test_state_dir_is_self_ignoring(self):
        self.run_task([{"write": {"calc.py": CALC_OK}}])
        tracked = self.git("ls-files")
        self.assertNotIn(".delegate", tracked)

    def test_doctor_and_init(self):
        code, out = self.delegate("doctor")
        d = json.loads(out)
        self.assertTrue(d["ok"])
        self.assertTrue(d["on_protected_branch"])
        code, out = self.delegate("init")
        self.assertTrue((self.repo / ".delegate" / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
