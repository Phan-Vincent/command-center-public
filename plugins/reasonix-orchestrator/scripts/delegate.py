#!/usr/bin/env python3
"""Legacy /delegate adapter. Single attempt; no automatic Git mutations."""
import argparse
import json
import os
from pathlib import Path
import sys
import subprocess
import time
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mcp'))
from reasonix_engine import run_task, probe, failure_envelope, WORKER_MODELS
from reasonix_verification import expected_files, snapshot, verify
from reasonix_process import ProcessFailure


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    run.add_argument('--title')
    run.add_argument('--task')
    run.add_argument('--task-file')
    run.add_argument('--dir', default='.')
    run.add_argument('--read-only', action='store_true')
    run.add_argument('--authorize-writes', action='store_true')
    run.add_argument('--model', choices=('flash', 'pro'), default='flash')
    run.add_argument('--escalation-reason', choices=('explicit_request', 'high_complexity', 'flash_verification_failed'))
    run.add_argument('--task-id')
    run.add_argument('--effort')
    run.add_argument('--verify', action='append', default=[])
    run.add_argument('--files', help='Comma-separated exact paths; detects deviations, not confinement.')
    run.add_argument('--max-steps', type=int, default=24)
    run.add_argument('--timeout', type=int, default=90)
    run.add_argument('--no-escalate', action='store_true', help='Compatibility flag: all escalation is manual.')
    run.add_argument('--attempts', type=int, default=1)
    run.add_argument('--branch', help='Rejected: branch isolation belongs to supervisor.')
    doctor = commands.add_parser('doctor')
    doctor.add_argument('--dir', default='.')
    stats = commands.add_parser('stats')
    stats.add_argument('--log', default='.delegate/log.jsonl')
    stats.add_argument('--since')
    stats.add_argument('--json', action='store_true')
    commands.add_parser('init')
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            raise ValueError('No automatic configuration; engine initializes only its objective ledger when run.')
        if args.command == 'stats':
            path = Path(args.log)
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
            # Preserve access to historical data without inventing a comparable savings estimate.
            output, code = {'ok': True, 'historical_entries': len(rows), 'usage': 'unknown',
                            'note': 'Historical log only; --since is unsupported and cannot imply filtering.'}, 0
            if args.since:
                raise ValueError('--since filtering is unavailable; historical log remains unchanged')
        elif args.command == 'doctor':
            output, code = probe(SimpleNamespace(dir=args.dir, host=None, timeout_seconds=20))
        else:
            if args.branch or args.attempts != 1:
                raise ValueError('Automatic branches/repeat ladders are removed; use one attempt and the same task_id for continuation')
            if args.task is not None and args.task_file is not None:
                raise ValueError('Choose --task or --task-file, not both')
            prompt = args.task if args.task is not None else (
                sys.stdin.read() if args.task_file in (None, '-') else Path(args.task_file).read_text())
            # Trim before validating: a whitespace-only entry authorizes no path.
            args.files = ','.join(expected_files(args.files)) or None
            if args.read_only and (args.verify or args.files):
                raise ValueError('Read mode cannot run write verification commands or file-change checks')
            config = SimpleNamespace(mode='read' if args.read_only else 'write',
                authorize_writes=args.authorize_writes, host=None, dir=args.dir,
                model=WORKER_MODELS[args.model], effort=args.effort, max_steps=args.max_steps,
                timeout_seconds=args.timeout, task_id=args.task_id, escalation_reason=args.escalation_reason,
                reasonix_bin=os.environ.get('DELEGATE_REASONIX_BIN', 'reasonix'))
            root = str(Path(args.dir).resolve())
            # Snapshot only for authorized write verification; no Git precondition or
            # mutation. The initial snapshot runs inside the objective lock via
            # prepare, before any worker launch.
            before = {}

            def prepare():
                before['snapshot'] = snapshot(root)

            adjudicator = (lambda payload, remaining: verify(payload, args, before.get('snapshot'), remaining)) if args.verify or args.files else None
            output, code = run_task(config, prompt, adjudicator=adjudicator,
                                    prepare=prepare if args.files else None)
            if not output.get('ok') and code == 0:
                code = 1
            output['status'] = 'success' if output.get('ok') else output.get('outcome', 'failed')
            output['summary'] = str((output.get('reasonix') or {}).get('result', output.get('error', '')))[:4000]
            output['files_changed'] = output['scope']['changed_files']
        failure_envelope(output)
    except (ValueError, OSError, subprocess.SubprocessError, ProcessFailure) as exc:
        output, code = failure_envelope({'process': getattr(exc, 'process', {'local_status': 'not_started'}), 'ok': False, 'status': 'blocked', 'outcome': 'blocked',
                                        'error': str(exc), 'error_code': 'BLOCKED'}), 2
    print(json.dumps(output, ensure_ascii=False, separators=(',', ':')))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
