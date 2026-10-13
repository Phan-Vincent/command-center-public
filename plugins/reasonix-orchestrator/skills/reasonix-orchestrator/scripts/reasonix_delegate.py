#!/usr/bin/env python3
"""CLI adapter to the shared Reasonix engine; contracts arrive on stdin."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mcp"))
from reasonix_engine import (SCHEMA_VERSION, ESCALATION_REASONS, run_process,
                            run_task, probe as engine_probe, failure_envelope)

def emit(payload):
    failure_envelope(payload)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

def probe(args):
    payload, code = engine_probe(args, runner=run_process)
    emit(payload)
    return code

def run_worker(args):
    payload, code = run_task(args, sys.stdin.read(), runner=run_process)
    emit(payload)
    return code

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
    run_parser.add_argument("--timeout-seconds", type=int, default=90)
    run_parser.add_argument("--task-id", help="Reuse for the same objective across adapters.")
    run_parser.add_argument("--escalation-reason", choices=sorted(ESCALATION_REASONS))
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
