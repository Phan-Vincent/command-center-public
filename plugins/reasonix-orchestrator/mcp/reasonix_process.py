"""Bounded subprocess ownership for the stdio server and its CLI wrapper."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Sequence


CLEANUP_SECONDS = 2.0


class ProcessFailure(Exception):
    def __init__(self, kind: str, stdout: str, stderr: str, process: dict) -> None:
        super().__init__(kind)
        self.kind = kind
        self.stdout = stdout
        self.stderr = stderr
        self.process = process
        self.exit_code = 124 if kind == "timeout" else 130 if kind == "cancelled" else 1


def _text(value: str | bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_owned(proc: subprocess.Popen, grouped: bool, sig: int) -> None:
    try:
        if grouped:
            os.killpg(proc.pid, sig)
        elif proc.poll() is None:
            proc.send_signal(sig)
    except ProcessLookupError:
        pass


def _cleanup(proc, grouped, stdout, stderr, seconds, term_grace):
    deadline = time.monotonic() + seconds
    drained = False
    errors = []
    # Do not let a second cancellation interrupt bounded cleanup.
    saved = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            saved[sig] = signal.signal(sig, signal.SIG_IGN)
    try:
        if term_grace:
            try:
                _signal_owned(proc, grouped, signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=min(term_grace, max(.001, deadline - time.monotonic())))
                drained = True
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = _text(exc.output), _text(exc.stderr)
            except OSError as exc:
                errors.append(type(exc).__name__)
        # The direct child can exit while an owned descendant still holds pipes.
        try:
            _signal_owned(proc, grouped, signal.SIGKILL)
        except OSError as exc:
            errors.append(type(exc).__name__)
        if not drained:
            try:
                stdout, stderr = proc.communicate(timeout=max(.001, deadline - time.monotonic()))
                drained = True
            except subprocess.TimeoutExpired as exc:
                stdout, stderr = _text(exc.output), _text(exc.stderr)
            except OSError as exc:
                errors.append(type(exc).__name__)
        # Escaped descendants can retain inherited pipes. Never drain indefinitely.
        if not drained:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            try:
                proc.wait(timeout=max(.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                errors.append("reap_timeout")
        while grouped and _group_exists(proc.pid) and time.monotonic() < deadline:
            time.sleep(min(.02, max(0, deadline - time.monotonic())))
        stopped = proc.poll() is not None and drained and not errors
        if grouped:
            stopped = stopped and not _group_exists(proc.pid)
        process = {
            "local_status": "stopped" if stopped else "unknown",
            "scope": "owned_local_process_group" if grouped else "direct_child_only",
            "pid": proc.pid,
            "pipes_drained": drained,
            "cleanup_errors": errors,
            "escaped_descendants": "not_established",
        }
        return _text(stdout), _text(stderr), process
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def guarded_run(argv: Sequence[str], *, input: str | None = None,
                timeout: float, cwd: str | None = None, env: dict | None = None,
                cleanup_seconds: float = CLEANUP_SECONDS, term_grace: float = 0):
    """Own a new local session, bound execution/draining, and retain failure evidence.

    SIGINT/SIGTERM cancel active work on the main thread. This is not a threaded
    JSON-RPC cancellation implementation and does not claim remote termination.
    """
    if timeout <= 0 or cleanup_seconds <= 0 or not 0 <= term_grace < cleanup_seconds:
        raise ValueError("positive execution/cleanup budgets and shorter termination grace required")
    deadline = time.monotonic() + timeout
    grouped = os.name == "posix"
    proc = None
    pending_signal = None
    saved = {}

    def cancel(signum, frame):
        nonlocal pending_signal
        pending_signal = signum
        # A signal during Popen creation must not lose the child identity.
        if proc is not None:
            raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            saved[sig] = signal.signal(sig, cancel)
    try:
        if pending_signal is not None:
            raise KeyboardInterrupt
        proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                                cwd=cwd, env=env, start_new_session=grouped)
        if pending_signal is not None:
            raise KeyboardInterrupt
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stdout, stderr, state = _cleanup(proc, grouped, "", "", cleanup_seconds, term_grace)
            raise ProcessFailure("timeout", stdout, stderr, state)
        try:
            stdout, stderr = proc.communicate(input=input, timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, state = _cleanup(proc, grouped, _text(exc.output), _text(exc.stderr),
                                             cleanup_seconds, term_grace)
            raise ProcessFailure("timeout", stdout, stderr, state) from None
        except KeyboardInterrupt:
            stdout, stderr, state = _cleanup(proc, grouped, "", "", cleanup_seconds, term_grace)
            raise ProcessFailure("cancelled", stdout, stderr, state) from None
        except Exception as exc:
            stdout, stderr, state = _cleanup(proc, grouped, "", "", cleanup_seconds, term_grace)
            raise ProcessFailure("communication_error", stdout,
                                 stderr + "\n" + type(exc).__name__, state) from None
        if grouped and _group_exists(proc.pid):
            stdout, stderr, state = _cleanup(proc, grouped, stdout, stderr, cleanup_seconds, term_grace)
            raise ProcessFailure("descendants_remained", stdout, stderr, state)
        completed = subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)
        completed.process = {
            "local_status": "stopped", "scope": "owned_local_process_group" if grouped else "direct_child_only",
            "pid": proc.pid, "pipes_drained": True, "escaped_descendants": "not_established",
        }
        return completed
    except KeyboardInterrupt:
        if proc is None:
            raise ProcessFailure("cancelled", "", "", {"local_status": "not_started"}) from None
        stdout, stderr, state = _cleanup(proc, grouped, "", "", cleanup_seconds, term_grace)
        raise ProcessFailure("cancelled", stdout, stderr, state) from None
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
