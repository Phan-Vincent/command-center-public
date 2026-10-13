"""Optional trusted local checks, separate from worker execution and Git mutation."""
import hashlib
from pathlib import Path
from reasonix_process import guarded_run, ProcessFailure, CLEANUP_SECONDS


def snapshot(root, timeout=20):
    """Hash tracked/nonignored files. This detects deviations, not confinement."""
    completed = guarded_run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            cwd=root, timeout=timeout)
    if completed.returncode:
        raise ValueError('File snapshot failed: ' + completed.stderr[-1000:])
    result = {}
    for name in completed.stdout.split('\0'):
        if not name:
            continue
        path = Path(root) / name
        if path.is_symlink():
            result[name] = 'symlink:' + str(path.readlink())
        elif path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            result[name] = 'missing'
    return result


def changed(before, after):
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def check(command, root, timeout):
    """Only explicit caller-supplied commands; no generated checks."""
    try:
        completed = guarded_run(['/bin/sh', '-c', command], cwd=root, timeout=timeout)
        classification = ('success' if completed.returncode == 0 else
                          'blocked' if completed.returncode in (126, 127) else 'substantive')
        return {'name': command, 'executor': 'adapter', 'result': 'passed' if completed.returncode == 0 else 'failed',
                'classification': classification,
                'evidence': {'exit_code': completed.returncode, 'process': completed.process,
                             'stdout': completed.stdout[-4000:], 'stderr': completed.stderr[-4000:]}}
    except ProcessFailure as exc:
        return {'name': command, 'executor': 'adapter', 'result': 'failed',
                'classification': exc.kind if exc.kind in ('timeout', 'cancelled') else 'unknown',
                'evidence': {'exit_code': exc.exit_code, 'process': exc.process}}
    except OSError as exc:
        return {'name': command, 'executor': 'adapter', 'result': 'failed', 'classification': 'blocked',
                'evidence': {'error': str(exc), 'process': {'local_status': 'not_started',
                             'scope': 'not_started', 'pipes_drained': True}}}


def mark_failure(payload, kind, code, message):
    payload.update(ok=False, failure_class=kind, error_code=code, error=message)
    payload.pop('failure', None)


def verify(payload, args, before, remaining):
    """Called internally while the objective lock and active reservation remain held."""
    root = str(Path(args.dir).resolve())
    if payload['process'].get('local_status') not in ('stopped', 'not_started'):
        return  # Do not inspect files or launch overlapping checks while uncertain.
    checks = []
    if payload.get('ok') and args.verify:
        for command in args.verify:
            budget = remaining() - CLEANUP_SECONDS
            if budget <= .05:
                checks.append({'name': command, 'executor': 'adapter', 'result': 'not_run',
                               'classification': 'timeout', 'evidence': {'error': 'verification deadline exhausted'}})
                mark_failure(payload, 'timeout', 'VERIFICATION_TIMEOUT', 'Required check did not fit the original deadline')
                break
            item = check(command, root, budget)
            checks.append(item)
            state = item['evidence'].get('process', {})
            if state.get('local_status') not in ('stopped', 'not_started'):
                payload['process'] = {**state, 'remote_status': 'not_applicable'}
                mark_failure(payload, 'unknown', 'VERIFICATION_CLEANUP_UNKNOWN', 'Verification cleanup is unknown')
                break
            if item['result'] != 'passed':
                # Genuine nonzero checks can permit corrected Pro; timeout,
                # cancellation, prerequisites and unknown state cannot.
                mark_failure(payload, item['classification'], 'VERIFICATION_FAILED', 'Required verification failed')
                if item['classification'] != 'substantive':
                    break
        passed = len(checks) == len(args.verify) and all(c['result'] == 'passed' for c in checks)
        payload['verification'] = {'status': 'passed' if passed else 'failed', 'checks': checks}
    if before is not None and payload['process'].get('local_status') in ('stopped', 'not_started'):
        budget = remaining() - CLEANUP_SECONDS
        if budget <= .05:
            mark_failure(payload, 'timeout', 'SCOPE_CHECK_TIMEOUT', 'No original budget remains for the final scope snapshot')
            return
        try:
            paths = changed(before, snapshot(root, timeout=budget))
        except ProcessFailure as exc:
            payload['process'] = {**exc.process, 'remote_status': 'not_applicable'}
            mark_failure(payload, exc.kind if exc.kind in ('timeout', 'cancelled') else 'unknown',
                         'SCOPE_CHECK_FAILED', 'Final scope snapshot failed')
            return
        deviations = sorted(set(paths) - set(args.files.split(',')))
        payload['scope'].update(changed_files=paths, deviations=deviations)
        payload['changes'] = paths
        if deviations:
            mark_failure(payload, 'scope', 'SCOPE_DEVIATION', 'Out-of-scope changes retained for supervisor review')
