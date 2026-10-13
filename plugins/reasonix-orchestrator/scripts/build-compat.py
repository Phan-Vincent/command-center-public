#!/usr/bin/env python3
"""Generate the standalone compatibility bundle; --check is read-only."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

CORE_FILES = ['mcp/reasonix_engine.py', 'mcp/reasonix_process.py', 'mcp/reasonix_ledger.py',
              'mcp/reasonix_verification.py', 'scripts/delegate.py']

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--check', action='store_true')
    args = p.parse_args()
    source = Path(__file__).resolve().parents[1]
    target = source.parent / 'deepseek-delegate'
    files = CORE_FILES + [str(f.relative_to(source)) for f in (source/'skills').rglob('*')
                         if f.is_file() and '__pycache__' not in f.parts]
    manifest = {}
    stale = []
    for name in files:
        src = source / name
        dst = target / name
        data = src.read_bytes()
        manifest[str(dst.relative_to(target))] = hashlib.sha256(data).hexdigest()
        if args.check:
            if not dst.is_file() or dst.read_bytes() != data:
                stale.append(name)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
    path = target/'bundle-manifest.json'
    if args.check:
        if not path.is_file() or json.loads(path.read_text()) != manifest:
            stale.append('bundle-manifest.json')
        if stale:
            raise SystemExit('Compatibility bundle differs: ' + ', '.join(stale))
    else:
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True)+'\n')
    print('Compatibility bundle matches canonical source' if args.check else 'Built compatibility bundle')

if __name__ == '__main__':
    main()
