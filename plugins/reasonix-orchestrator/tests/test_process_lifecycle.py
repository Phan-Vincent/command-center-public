from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "mcp"))
from reasonix_process import guarded_run, ProcessFailure
import reasonix_process


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrapper = load("lifecycle_wrapper", PLUGIN / "skills/reasonix-orchestrator/scripts/reasonix_delegate.py")
server = load("lifecycle_server", PLUGIN / "mcp/reasonix_mcp.py")


@unittest.skipUnless(os.name == "posix", "POSIX process-group lifecycle")
class LifecycleTests(unittest.TestCase):
    def test_launch_time_consumes_execution_budget(self):
        real_popen = subprocess.Popen
        observed = []

        def delayed_launch(*args, **kwargs):
            time.sleep(.15)
            proc = real_popen(*args, **kwargs)
            communicate = proc.communicate
            def record_communication(*args, **kwargs):
                observed.append(kwargs.get("timeout"))
                return communicate(*args, **kwargs)
            proc.communicate = record_communication
            return proc

        with mock.patch.object(reasonix_process.subprocess, "Popen", side_effect=delayed_launch):
            with self.assertRaises(ProcessFailure) as caught:
                guarded_run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=.3, cleanup_seconds=.5)
        self.assertEqual(caught.exception.kind, "timeout")
        self.assertLess(observed[0], .18)

    def test_pending_launch_cancellation_cleans_up_after_creation(self):
        real_popen = subprocess.Popen
        created = []

        def cancelled_launch(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            proc = real_popen(*args, **kwargs)
            created.append(proc.pid)
            return proc

        with mock.patch.object(reasonix_process.subprocess, "Popen", side_effect=cancelled_launch):
            with self.assertRaises(ProcessFailure) as caught:
                guarded_run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1, cleanup_seconds=.5)
        self.assertEqual(caught.exception.kind, "cancelled")
        with self.assertRaises(ProcessLookupError):
            os.kill(created[0], 0)

    def test_timeout_kills_descendant_and_retains_partial_output_once(self):
        child = "import time; print('DESCENDANT_READY',flush=True); time.sleep(30)"
        code = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-u','-c',{child!r}]); print('PARENT_READY',flush=True); time.sleep(30)"
        start = time.monotonic()
        with self.assertRaises(ProcessFailure) as caught:
            guarded_run([sys.executable, "-u", "-c", code], timeout=.4, cleanup_seconds=.8)
        failure = caught.exception
        self.assertEqual(failure.kind, "timeout")
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(failure.stdout.count("PARENT_READY"), 1)
        self.assertEqual(failure.stdout.count("DESCENDANT_READY"), 1)
        self.assertIn(failure.process["local_status"], {"stopped", "unknown"})
        self.assertTrue(failure.process["pipes_drained"])
        # Draining inherited pipes proves these fixture descendants were killed.

    def test_escaped_pipe_holder_does_not_cause_unbounded_drain(self):
        code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True); print(p.pid,flush=True); time.sleep(30)"
        start = time.monotonic()
        with self.assertRaises(ProcessFailure) as caught:
            guarded_run([sys.executable, "-u", "-c", code], timeout=.3, cleanup_seconds=.3)
        failure = caught.exception
        escaped_pid = int(failure.stdout.strip())
        try:
            self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(failure.process["local_status"], "unknown")
            self.assertFalse(failure.process["pipes_drained"])
        finally:
            os.kill(escaped_pid, signal.SIGKILL)

    def test_outer_wrapper_timeout_is_independently_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            stalled = Path(directory) / "stalled.py"
            stalled.write_text("import time; print('WRAPPER_READY',flush=True); time.sleep(30)")
            with mock.patch.object(server, "WRAPPER_PATH", stalled), mock.patch.object(server, "CLEANUP_SECONDS", .1), mock.patch.object(server, "WRAPPER_OVERHEAD_SECONDS", .1), mock.patch.object(server, "OUTER_CLEANUP_SECONDS", 2):
                start = time.monotonic()
                result = server.call_tool("probe", {"dir": directory, "timeout_seconds": 1})
        self.assertLess(time.monotonic() - start, 4)
        self.assertTrue(result["isError"])
        payload = result["structuredContent"]
        self.assertEqual(payload["stage"], "wrapper")
        self.assertEqual(payload["exit_code"], 124)
        self.assertEqual(payload["failure"]["code"], "TIMEOUT")
        self.assertIn("WRAPPER_READY", payload["stdout"])

    def test_actual_stdio_server_sigterm_cancels_owned_work(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            pidfile = Path(directory) / "pid"
            launcher = Path(directory) / "server.py"
            launcher.write_text(
                f"import sys; sys.path.insert(0,{str(PLUGIN / 'mcp')!r}); import reasonix_mcp as m; "
                f"m.WRAPPER_PATH={str(Path(directory) / 'worker.py')!r}; raise SystemExit(m.serve())"
            )
            Path(directory, "worker.py").write_text(
                f"import os,time; from pathlib import Path; Path({str(pidfile)!r}).write_text(str(os.getpid())); Path({str(ready)!r}).touch(); time.sleep(30)"
            )
            proc = subprocess.Popen([sys.executable, str(launcher)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "probe", "arguments": {"dir": directory, "timeout_seconds": 20}}}
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(ready.exists())
                worker_pid = int(pidfile.read_text())
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=6)
                payload = json.loads(stdout)["result"]["structuredContent"]
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["error_code"], "CANCELLED")
                self.assertEqual(proc.returncode, 130)
                with self.assertRaises(ProcessLookupError):
                    os.kill(worker_pid, 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=1)


class ResultTests(unittest.TestCase):
    def run_result(self, value, rc=0, host=None):
        args = wrapper.parser().parse_args(["run", "--mode", "read", "--dir", tempfile.gettempdir()])
        args.host = host
        completed = subprocess.CompletedProcess([], rc, json.dumps(value), "")
        completed.process = {"local_status": "stopped", "scope": "owned_local_process_group", "pipes_drained": True}
        stdout = io.StringIO()
        with mock.patch.object(wrapper, "run_process", return_value=completed), mock.patch.object(wrapper.sys, "stdin", io.StringIO("bounded task")), mock.patch.object(wrapper.sys, "stdout", stdout):
            status = wrapper.run_worker(args)
        return status, json.loads(stdout.getvalue())

    def test_empty_or_incomplete_object_is_not_success(self):
        for value in ({}, [], {"type": "result", "subtype": "success"}):
            with self.subTest(value=value):
                status, result = self.run_result(value)
                self.assertEqual(status, 1)
                self.assertFalse(result["ok"])

    def test_real_success_and_remote_process_uncertainty(self):
        value = {"type": "result", "subtype": "success", "is_error": False, "result": "READ_OK"}
        status, result = self.run_result(value, host="explicit-host")
        self.assertEqual(status, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["process"]["remote_status"], "unknown")
        status, result = self.run_result(value, rc=1)
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])

    def test_nested_worker_error_remains_failed_and_is_surfaced(self):
        value = {"type": "result", "subtype": "error_during_execution", "is_error": True, "error_code": "INVALID_ARGUMENT", "error": {"message": "fixture invalid argument"}, "result": "advisory text"}
        status, result = self.run_result(value)
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_ARGUMENT")
        self.assertEqual(result["failure"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(result["error"], "fixture invalid argument")
        self.assertEqual(result["worker_subtype"], "error_during_execution")

    def test_mcp_default_deadline_is_forwarded_and_independent(self):
        completed = subprocess.CompletedProcess([], 0, '{"ok":true}', "")
        with mock.patch.object(server, "guarded_run", return_value=completed) as run:
            result = server.call_tool("delegate_read", {"dir": tempfile.gettempdir(), "task_contract": "bounded task"})
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--timeout-seconds") + 1], "90")
        self.assertGreater(run.call_args.kwargs["timeout"], 90)
        self.assertEqual(result["structuredContent"]["execution_guard_version"], 2)

    def test_connector_generic_error_label_cannot_erase_canonical_failure(self):
        completed = subprocess.CompletedProcess([], 1,
            '{"ok":false,"error_code":"TIMEOUT","error":"worker timed out","failure":{"code":"TIMEOUT","stage":"worker","message":"worker timed out"}}', "")
        with mock.patch.object(server, "guarded_run", return_value=completed):
            result = server.call_tool("probe", {"dir": tempfile.gettempdir()})
        # The observed connector rewrites this reserved top-level key after MCP.
        result["structuredContent"]["error_code"] = "INVALID_ARGUMENT"
        self.assertEqual(result["structuredContent"]["failure"]["code"], "TIMEOUT")
        self.assertTrue(result["isError"])


@unittest.skipUnless(os.name == "posix", "POSIX process-group lifecycle")
class LayeredCancellationTests(unittest.TestCase):
    def test_real_server_wrapper_and_worker_groups_cancel_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pidfile = root / "fixture-pids.json"
            fake = root / "reasonix"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json,os,subprocess,sys,time\n"
                f"child=subprocess.Popen([sys.executable,'-u','-c',\"import time; print('CHILD_READY',flush=True); time.sleep(30)\"])\n"
                f"open({str(pidfile)!r},'w').write(json.dumps([os.getpid(),child.pid]))\n"
                "print('WORKER_READY',flush=True)\ntime.sleep(30)\n"
            )
            fake.chmod(0o755)
            env = {**os.environ, "PATH": str(root) + os.pathsep + os.environ.get("PATH", "")}
            proc = subprocess.Popen([sys.executable, str(PLUGIN / "mcp/reasonix_mcp.py")], env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "probe", "arguments": {"dir": directory, "timeout_seconds": 20}}}
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            pids = []
            try:
                deadline = time.monotonic() + 3
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(pidfile.exists())
                pids = json.loads(pidfile.read_text())
                proc.send_signal(signal.SIGTERM)
                stdout, _ = proc.communicate(timeout=7)
                result = json.loads(stdout)["result"]["structuredContent"]
                self.assertEqual(result["error_code"], "CANCELLED")
                self.assertEqual(proc.returncode, 130)
                self.assertIn("WORKER_READY", result["stdout"])
                for pid in pids:
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(.02)
                    else:
                        self.fail(f"fixture process {pid} remains after cancellation")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=1)
                for pid in pids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
