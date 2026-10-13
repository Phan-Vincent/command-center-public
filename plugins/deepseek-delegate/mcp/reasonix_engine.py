"""Shared Reasonix execution: argv/stdin, gates, lifecycle, accounting, results.

Execution success is distinct from supervisor acceptance. This module performs
no Git mutation and does not claim filesystem sandbox enforcement.
"""
from __future__ import annotations
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import uuid
from typing import Any, Sequence
from reasonix_process import guarded_run, ProcessFailure, CLEANUP_SECONDS
from reasonix_ledger import Ledger
SCHEMA_VERSION = 1
MAX_PROMPT_BYTES = 512 * 1024
MAX_CAPTURE_CHARS = 2_000_000
HOST_PATTERN = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+$")
WORKER_MODELS = {"flash": "deepseek-flash", "pro": "deepseek-pro"}
ESCALATION_REASONS = {"explicit_request", "high_complexity", "flash_verification_failed"}


def bounded(text: str | bytes) -> str:
    if isinstance(text, bytes):
        text = text.decode('utf-8', errors='replace')
    if len(text) <= MAX_CAPTURE_CHARS:
        return text
    return text[:MAX_CAPTURE_CHARS] + '\n...[truncated by reasonix-orchestrator]'

def validate_host(host: str | None) -> None:
    if host is None:
        return
    if not isinstance(host, str) or not HOST_PATTERN.fullmatch(host) or host.startswith('-'):
        raise ValueError('unsafe SSH host or alias syntax')

def run_process(argv: Sequence[str], *, stdin_text: str | None, timeout_seconds: int, cwd: str | None=None) -> subprocess.CompletedProcess[str]:
    return guarded_run(list(argv), input=stdin_text, timeout=timeout_seconds, cwd=cwd, env=os.environ.copy())

def local_or_remote_argv(host: str | None, remote_argv: Sequence[str]) -> list[str]:
    if host is None:
        return list(remote_argv)
    login_shell_argv = ['sh', '-lc', 'exec "$@"', 'reasonix-remote', *remote_argv]
    remote_command = shlex.join(login_shell_argv)
    return ['ssh', '-o', 'BatchMode=yes', '--', host, remote_command]

def target_label(host: str | None) -> str:
    return host if host is not None else 'local'

def probe(args: argparse.Namespace, runner=run_process) -> tuple[dict, int]:
    validate_host(args.host)
    directory = args.dir
    if args.host is None:
        resolved = Path(directory).expanduser().resolve()
        if not resolved.is_dir():
            payload = {'schema_version': SCHEMA_VERSION, 'ok': False, 'operation': 'probe', 'target': 'local', 'error': 'project directory does not exist', 'dir': str(resolved)}
            return (payload, 1)
        directory = str(resolved)
        argv = ['reasonix', '--version']
        cwd = directory
    else:
        command = ['sh', '-lc', 'test -d "$1" && command -v reasonix >/dev/null && reasonix --version', 'reasonix-probe', directory]
        argv = local_or_remote_argv(args.host, command)
        cwd = None
    try:
        completed = runner(argv, stdin_text=None, timeout_seconds=args.timeout_seconds, cwd=cwd)
    except FileNotFoundError as exc:
        payload = {'schema_version': SCHEMA_VERSION, 'ok': False, 'operation': 'probe', 'target': target_label(args.host), 'error': f'executable not found: {exc.filename}'}
        return (payload, 127)
    except ProcessFailure as exc:
        payload = {'schema_version': SCHEMA_VERSION, 'ok': False, 'operation': 'probe', 'target': target_label(args.host), 'error': f'probe {exc.kind}', 'error_code': exc.kind.upper(), 'stage': 'probe', 'exit_code': exc.exit_code, 'process': process_evidence(exc.process, args.host), 'stdout': bounded(exc.stdout), 'stderr': bounded(exc.stderr)}
        return (payload, exc.exit_code)
    ok = completed.returncode == 0
    payload = {'schema_version': SCHEMA_VERSION, 'ok': ok, 'operation': 'probe', 'target': target_label(args.host), 'dir': directory, 'exit_code': completed.returncode, 'version': completed.stdout.strip() if ok else None, 'stderr': bounded(completed.stderr.strip()), 'process': process_evidence(completed.process, args.host)}
    return (payload, 0 if ok else 1)

def execute_once(args: argparse.Namespace, prompt, runner=run_process) -> tuple[dict, int]:
    validate_host(args.host)
    if args.mode == 'write' and args.authorize_writes is not True:
        raise ValueError('write mode requires --authorize-writes')
    if args.mode == 'read' and args.authorize_writes:
        raise ValueError('--authorize-writes is valid only with --mode write')
    prompt_size = len(prompt.encode('utf-8'))
    if not prompt.strip():
        raise ValueError('worker prompt on stdin is empty')
    if prompt_size > MAX_PROMPT_BYTES:
        raise ValueError(f'worker prompt exceeds {MAX_PROMPT_BYTES} bytes')
    directory = args.dir
    if args.host is None:
        resolved = Path(directory).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f'project directory does not exist: {resolved}')
        directory = str(resolved)
    permission_mode = 'dontAsk' if args.mode == 'read' else 'auto'
    max_steps = args.max_steps
    if max_steps is None:
        max_steps = 12 if args.mode == 'read' else 24
    reasonix_argv = [getattr(args, 'reasonix_bin', 'reasonix'), 'run', '--dir', directory, '--permission-mode', permission_mode, '--output-format', 'json', '--max-steps', str(max_steps)]
    if args.model:
        reasonix_argv.extend(['--model', args.model])
    if args.effort:
        reasonix_argv.extend(['--effort', args.effort])
    argv = local_or_remote_argv(args.host, reasonix_argv)
    try:
        completed = runner(argv, stdin_text=prompt, timeout_seconds=args.timeout_seconds, cwd=directory if args.host is None else None)
    except FileNotFoundError as exc:
        payload = {'schema_version': SCHEMA_VERSION, 'ok': False, 'operation': 'run', 'mode': args.mode, 'target': target_label(args.host), 'error': f'executable not found: {exc.filename}'}
        return (payload, 127)
    except ProcessFailure as exc:
        payload = {'schema_version': SCHEMA_VERSION, 'ok': False, 'operation': 'run', 'mode': args.mode, 'target': target_label(args.host), 'error': f'Reasonix run {exc.kind}', 'error_code': exc.kind.upper(), 'stage': 'worker', 'exit_code': exc.exit_code, 'process': process_evidence(exc.process, args.host), 'stdout': bounded(exc.stdout), 'stderr': bounded(exc.stderr)}
        return (payload, exc.exit_code)
    parsed: Any = None
    parse_error: str | None = None
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        parse_error = str(exc)
    reasonix_success = completed.returncode == 0 and isinstance(parsed, dict) and (parsed.get('type') == 'result') and (parsed.get('subtype') == 'success') and (parsed.get('is_error') is False) and isinstance(parsed.get('result'), str) and bool(parsed['result'].strip())
    payload: dict[str, Any] = {'schema_version': SCHEMA_VERSION, 'ok': reasonix_success, 'operation': 'run', 'mode': args.mode, 'target': target_label(args.host), 'dir': directory, 'exit_code': completed.returncode, 'reasonix': parsed, 'stderr': bounded(completed.stderr.strip()), 'process': process_evidence(completed.process, args.host)}
    if parse_error is not None:
        payload['parse_error'] = parse_error
        payload['stdout'] = bounded(completed.stdout)
    if not reasonix_success:
        detail = parsed.get('error') if isinstance(parsed, dict) else None
        message = detail.get('message') if isinstance(detail, dict) else detail
        code = parsed.get('error_code', 'INVALID_RESULT') if isinstance(parsed, dict) else 'INVALID_RESULT'
        payload['error_code'] = str(code)[:128]
        payload['error'] = str(message or 'Reasonix did not return a successful result envelope')[:4000]
        payload['stage'] = 'worker_result'
        subtype = parsed.get('subtype') if isinstance(parsed, dict) else None
        payload['worker_subtype'] = str(subtype)[:128] if subtype is not None else None
    return (payload, 0 if reasonix_success else 1)

def process_evidence(state: dict, host: str | None) -> dict:
    return {**state, 'remote_status': 'unknown' if host is not None else 'not_applicable'}


def failure_envelope(payload):
    if payload.get('ok') is False and not isinstance(payload.get('failure'), dict):
        payload['failure'] = {
            'code': str(payload.get('error_code', 'REASONIX_FAILURE'))[:128],
            'stage': str(payload.get('stage', payload.get('operation', 'validation')))[:128],
            'message': str(payload.get('error', 'Reasonix execution failed'))[:4000],
        }
    return payload


def routing(tier, reason):
    if type(tier) is not str or tier not in WORKER_MODELS:
        raise ValueError('worker_tier must be flash or pro')
    if tier == 'pro':
        if type(reason) is not str or reason not in ESCALATION_REASONS:
            raise ValueError('a Pro worker requires a valid escalation_reason')
    elif reason is not None:
        raise ValueError('escalation_reason is valid only with worker_tier=pro')
    return WORKER_MODELS[tier], reason


def validate(args, prompt):
    if args.mode not in ('read', 'write'):
        raise ValueError('mode must be read or write')
    if type(args.authorize_writes) is not bool or args.authorize_writes != (args.mode == 'write'):
        raise ValueError('write mode requires exact authorize_writes=true; read mode forbids it')
    validate_host(args.host)
    if type(args.timeout_seconds) is not int or not 1 <= args.timeout_seconds <= 7200:
        raise ValueError('timeout_seconds must be between 1 and 7200')
    if args.max_steps is not None and (type(args.max_steps) is not int or not 1 <= args.max_steps <= 200):
        raise ValueError('max_steps must be between 1 and 200')
    if type(prompt) is not str or not prompt.strip() or len(prompt.encode()) > MAX_PROMPT_BYTES:
        raise ValueError('empty or oversized worker prompt')
    if not isinstance(args.dir, str) or not args.dir.strip() or '\0' in args.dir:
        raise ValueError('dir must be a non-empty path')
    if args.host is None:
        args.dir = str(Path(args.dir).expanduser().resolve())
        if not Path(args.dir).is_dir():
            raise ValueError('project directory does not exist')
    elif not args.dir.startswith('/'):
        raise ValueError('SSH requires an explicitly named absolute target path')
    model = args.model or WORKER_MODELS['flash']
    tiers = {v: k for k, v in WORKER_MODELS.items()}
    if model not in tiers:
        raise ValueError('model must be deepseek-flash or deepseek-pro')
    args.model = model
    tier = tiers[model]
    reason = getattr(args, 'escalation_reason', None)
    routing(tier, reason)
    if args.effort is not None and (type(args.effort) is not str or not args.effort.strip() or '\0' in args.effort):
        raise ValueError('effort must be a non-empty string')
    return tier, reason


def ledger_directory():
    override = os.environ.get('REASONIX_LEDGER_DIR')
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state')))
    return base / 'reasonix-orchestrator' / 'objectives'


def classification(payload):
    if payload.get('ok') is True:
        return 'success'
    code = re.sub(r'(?<!^)(?=[A-Z])', '_', str(payload.get('error_code', ''))) if not str(payload.get('error_code', '')).isupper() else str(payload.get('error_code', ''))
    code = code.lower().replace('-', '_')
    worker = payload.get('reasonix')
    detail = worker.get('result', '') if isinstance(worker, dict) else ''
    message = (str(payload.get('error', '')) + str(detail)[:8000]).lower()
    if any(marker in message for marker in ('missing env', 'unknown model', 'not configured', 'missing credential', 'api key', 'api_key', 'unauthorized', 'authentication')):
        return 'blocked'
    if code in {'missing_credential', 'missing_credentials', 'missing_config', 'configuration_error',
                'unknown_model', 'authentication_error', 'unauthorized', 'api_key_missing', 'executable_missing'}:
        return 'blocked'
    if code == 'timeout':
        return 'timeout'
    if code == 'cancelled':
        return 'cancelled'
    worker = payload.get('reasonix')
    if isinstance(worker, dict) and worker.get('type') == 'result' and worker.get('is_error') is True:
        return 'substantive'
    return 'unknown'


def decorate(payload, args, task_id, ledger=None):
    failure_envelope(payload)
    payload['task_id'] = task_id
    evidence = payload.setdefault('process', {'local_status': 'not_started', 'scope': 'not_started',
                                             'remote_status': 'not_started' if args.host else 'not_applicable',
                                             'pipes_drained': True})
    remote = evidence.get('remote_status', 'unknown')
    status = evidence.get('local_status', 'unknown')
    evidence['status'] = ('unknown' if args.host and remote != 'stopped' else
                         'stopped' if status == 'not_started' else status)
    # Execution ok is retained for compatibility, never supervisor acceptance.
    payload['outcome'] = 'partial' if payload.get('ok') is True else {
        'blocked': 'blocked', 'cancelled': 'cancelled', 'unknown': 'unknown',
    }.get(payload.get('failure_class'), 'failed')
    payload.setdefault('scope', {'operation': args.mode, 'target': args.dir, 'host': args.host,
                        'inspected_files': 'unknown', 'changed_files': 'unknown', 'deviations': 'unknown'})
    payload.setdefault('changes', [])
    payload.setdefault('verification', {'status': 'not_run', 'checks': []})
    payload['unresolved'] = ['Supervisor scope review and acceptance remain required.']
    payload['risks'] = ['Permission modes and directory flags do not prove filesystem confinement.',
                        'Stop evidence covers the owned local process group; escaped descendants are not established.']
    if args.host:
        payload['risks'].append('Remote worker termination is unknown; no automatic continuation.')
    worker = payload.get('reasonix')
    worker = worker if isinstance(worker, dict) else {}
    payload['usage'] = {'model': args.model or 'unknown', 'effort': args.effort or 'unknown',
                       'attempts': ledger['attempts'] if ledger else 'unknown',
                       'tokens': worker.get('usage', 'unknown'),
                       'cost': worker.get('cost_quote', worker.get('total_cost_usd', 'unknown')),
                       'measurement_source': 'worker_report' if 'usage' in worker or 'total_cost_usd' in worker else 'unknown'}
    if ledger is not None:
        payload['ledger'] = ledger
    return payload


def run_task(args, prompt, runner=run_process, ledger_root=None, adjudicator=None):
    task_id = getattr(args, 'task_id', None) or str(uuid.uuid4())
    session = None
    try:
        tier, reason = validate(args, prompt)
        store = Ledger(ledger_root or ledger_directory(), cleanup_reserve=CLEANUP_SECONDS)
        identity = {'mode': args.mode, 'directory': args.dir, 'host': args.host}
        # timeout_seconds is worker budget; reserve finite cleanup within the
        # persisted objective deadline. Follow-up timeout values never extend it.
        with store.locked(task_id, identity, args.timeout_seconds + CLEANUP_SECONDS) as session:
            session.before_launch(tier, reason, hashlib.sha256(prompt.encode()).hexdigest())
            budget = session.remaining() - CLEANUP_SECONDS
            if budget <= 0:
                raise ValueError('execution deadline expired before launch')
            original_timeout = args.timeout_seconds
            args.timeout_seconds = budget
            try:
                payload, code = execute_once(args, prompt, runner=runner)
                kind = classification(payload)
            except OSError as exc:
                kind = 'infrastructure' if exc.errno in {errno.EAGAIN, errno.EINTR, errno.ETIMEDOUT} else 'blocked'
                payload = {'ok': False, 'error': f'worker launch failed: {exc}',
                    'error_code': 'EXECUTABLE_MISSING' if isinstance(exc, FileNotFoundError) else 'LAUNCH_FAILURE',
                    'stage': 'launch', 'exit_code': 127 if isinstance(exc, FileNotFoundError) else 1,
                    'process': {'local_status': 'not_started', 'scope': 'not_started',
                                'remote_status': 'not_started', 'pipes_drained': True}}
                code = payload['exit_code']
            finally:
                args.timeout_seconds = original_timeout
            # FileNotFoundError is also normalized inside the original execution
            # helper; no process existed, so this is a blocked precondition.
            if code == 127:
                kind = 'blocked'
                payload.setdefault('error_code', 'EXECUTABLE_MISSING')
                payload.setdefault('process', {'local_status': 'not_started', 'scope': 'not_started',
                                               'remote_status': 'not_started', 'pipes_drained': True})
            payload['failure_class'] = kind
            # Keep the durable active reservation and lock through optional
            # trusted local verification. A crash here cannot persist success.
            if adjudicator is not None:
                payload = decorate(payload, args, task_id, session.snapshot())
                adjudicator(payload, session.remaining)
                kind = payload['failure_class']
                if payload.get('ok') is not True and code == 0:
                    code = 1
            outcome = {'success': 'success', 'blocked': 'blocked', 'cancelled': 'cancelled',
                       'unknown': 'unknown'}.get(kind, 'failed')
            raw = payload.get('process', {})
            process = {k: raw[k] for k in ('local_status', 'remote_status', 'scope', 'pipes_drained') if k in raw}
            session.finish({'outcome': outcome, 'process': process}, kind)
            saved = session.snapshot()
        return decorate(payload, args, task_id, saved), code
    except (ValueError, OSError) as exc:
        # A launch or completed-attempt persistence failure may leave active
        # reservation on disk. Never clear it or infer safe cleanup from a lock.
        payload = {'schema_version': SCHEMA_VERSION, 'ok': False, 'error': str(exc),
                   'error_code': 'BLOCKED', 'stage': 'accounting' if session else 'validation',
                   'failure_class': 'blocked', 'exit_code': 2}
        if session is not None and session.launched:
            payload['process'] = {'local_status': 'unknown', 'scope': 'unknown', 'remote_status': 'unknown'}
        return decorate(payload, args, task_id), 2
