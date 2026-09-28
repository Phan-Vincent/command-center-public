#!/usr/bin/env python3
"""Safely invoke a local or SSH-hosted Reasonix headless worker.

The worker prompt is accepted only on stdin so user-controlled task text never
becomes part of a shell command. The wrapper deliberately exposes no Reasonix
permission-bypass mode.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Sequence


SCHEMA_VERSION = 1
MAX_PROMPT_BYTES = 512 * 1024
MAX_CAPTURE_CHARS = 2_000_000
HOST_PATTERN = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+$")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def bounded(text: str | bytes) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) <= MAX_CAPTURE_CHARS:
        return text
    return text[:MAX_CAPTURE_CHARS] + "\n...[truncated by reasonix-orchestrator]"


def validate_host(host: str | None) -> None:
    if host is None:
        return
    if not HOST_PATTERN.fullmatch(host) or host.startswith("-"):
        raise ValueError("unsafe SSH host or alias syntax")


def run_process(
    argv: Sequence[str],
    *,
    stdin_text: str | None,
    timeout_seconds: int,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        input=stdin_text,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        cwd=cwd,
        check=False,
        env=os.environ.copy(),
    )


def local_or_remote_argv(host: str | None, remote_argv: Sequence[str]) -> list[str]:
    if host is None:
        return list(remote_argv)
    login_shell_argv = ["sh", "-lc", 'exec "$@"', "reasonix-remote", *remote_argv]
    remote_command = shlex.join(login_shell_argv)
    return ["ssh", "-o", "BatchMode=yes", "--", host, remote_command]


def target_label(host: str | None) -> str:
    return host if host is not None else "local"


def probe(args: argparse.Namespace) -> int:
    validate_host(args.host)
    directory = args.dir

    if args.host is None:
        resolved = Path(directory).expanduser().resolve()
        if not resolved.is_dir():
            emit({
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "operation": "probe",
                "target": "local",
                "error": "project directory does not exist",
                "dir": str(resolved),
            })
            return 1
        directory = str(resolved)
        argv = ["reasonix", "--version"]
        cwd = directory
    else:
        command = [
            "sh",
            "-lc",
            "test -d \"$1\" && command -v reasonix >/dev/null && reasonix --version",
            "reasonix-probe",
            directory,
        ]
        argv = local_or_remote_argv(args.host, command)
        cwd = None

    try:
        completed = run_process(
            argv,
            stdin_text=None,
            timeout_seconds=args.timeout_seconds,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        emit({
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "operation": "probe",
            "target": target_label(args.host),
            "error": f"executable not found: {exc.filename}",
        })
        return 127
    except subprocess.TimeoutExpired:
        emit({
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "operation": "probe",
            "target": target_label(args.host),
            "error": "probe timed out",
        })
        return 124

    ok = completed.returncode == 0
    emit({
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "operation": "probe",
        "target": target_label(args.host),
        "dir": directory,
        "exit_code": completed.returncode,
        "version": completed.stdout.strip() if ok else None,
        "stderr": bounded(completed.stderr.strip()),
    })
    return 0 if ok else 1


def run_worker(args: argparse.Namespace) -> int:
    validate_host(args.host)
    if args.mode == "write" and not args.authorize_writes:
        raise ValueError("write mode requires --authorize-writes")
    if args.mode == "read" and args.authorize_writes:
        raise ValueError("--authorize-writes is valid only with --mode write")

    prompt = sys.stdin.read()
    prompt_size = len(prompt.encode("utf-8"))
    if not prompt.strip():
        raise ValueError("worker prompt on stdin is empty")
    if prompt_size > MAX_PROMPT_BYTES:
        raise ValueError(f"worker prompt exceeds {MAX_PROMPT_BYTES} bytes")

    directory = args.dir
    if args.host is None:
        resolved = Path(directory).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"project directory does not exist: {resolved}")
        directory = str(resolved)

    permission_mode = "dontAsk" if args.mode == "read" else "auto"
    max_steps = args.max_steps
    if max_steps is None:
        max_steps = 12 if args.mode == "read" else 24

    reasonix_argv = [
        "reasonix",
        "run",
        "--dir",
        directory,
        "--permission-mode",
        permission_mode,
        "--output-format",
        "json",
        "--max-steps",
        str(max_steps),
    ]
    if args.model:
        reasonix_argv.extend(["--model", args.model])
    if args.effort:
        reasonix_argv.extend(["--effort", args.effort])

    argv = local_or_remote_argv(args.host, reasonix_argv)
    try:
        completed = run_process(
            argv,
            stdin_text=prompt,
            timeout_seconds=args.timeout_seconds,
            cwd=directory if args.host is None else None,
        )
    except FileNotFoundError as exc:
        emit({
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "operation": "run",
            "mode": args.mode,
            "target": target_label(args.host),
            "error": f"executable not found: {exc.filename}",
        })
        return 127
    except subprocess.TimeoutExpired as exc:
        emit({
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "operation": "run",
            "mode": args.mode,
            "target": target_label(args.host),
            "error": "Reasonix run timed out",
            "stdout": bounded(exc.stdout or ""),
            "stderr": bounded(exc.stderr or ""),
        })
        return 124

    parsed: Any = None
    parse_error: str | None = None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        parse_error = str(exc)

    reasonix_success = (
        completed.returncode == 0
        and isinstance(parsed, dict)
        and parsed.get("is_error") is not True
        and parsed.get("subtype") != "error_during_execution"
    )
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": reasonix_success,
        "operation": "run",
        "mode": args.mode,
        "target": target_label(args.host),
        "dir": directory,
        "exit_code": completed.returncode,
        "reasonix": parsed,
        "stderr": bounded(completed.stderr.strip()),
    }
    if parse_error is not None:
        payload["parse_error"] = parse_error
        payload["stdout"] = bounded(completed.stdout)
    emit(payload)
    return 0 if reasonix_success else 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Safely delegate a bounded task to Reasonix locally or over SSH."
    )
    subparsers = root.add_subparsers(dest="command", required=True)

    probe_parser = subparsers.add_parser("probe", help="Check target directory and Reasonix CLI.")
    probe_parser.add_argument("--host", help="Explicit SSH host or configured alias.")
    probe_parser.add_argument("--dir", default=".", help="Project directory on the target.")
    probe_parser.add_argument("--timeout-seconds", type=int, default=20)
    probe_parser.set_defaults(handler=probe)

    run_parser = subparsers.add_parser("run", help="Run one bounded Reasonix task from stdin.")
    run_parser.add_argument("--mode", choices=("read", "write"), required=True)
    run_parser.add_argument("--authorize-writes", action="store_true")
    run_parser.add_argument("--host", help="Explicit SSH host or configured alias.")
    run_parser.add_argument("--dir", required=True, help="Project directory on the target.")
    run_parser.add_argument("--model", help="Configured Reasonix model/provider reference.")
    run_parser.add_argument("--effort", help="Reasoning effort accepted by the target provider.")
    run_parser.add_argument("--max-steps", type=int)
    run_parser.add_argument("--timeout-seconds", type=int, default=1800)
    run_parser.set_defaults(handler=run_worker)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.timeout_seconds < 1 or args.timeout_seconds > 7200:
        emit({
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": "--timeout-seconds must be between 1 and 7200",
        })
        return 2
    if getattr(args, "max_steps", None) is not None and not 1 <= args.max_steps <= 200:
        emit({
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": "--max-steps must be between 1 and 200",
        })
        return 2
    try:
        return int(args.handler(args))
    except ValueError as exc:
        emit({
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": str(exc),
        })
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
