# DeepSeek Delegation compatibility

Version 0.2.0 is a generated compatibility distribution of Reasonix Orchestrator.
It bundles the same core policy, references, engine, ledger, and local verification
helpers; it needs no name-only dependency on another installed plugin.

`/delegate` and `scripts/delegate.py` remain entry points. Runs execute one bounded
attempt and retain changes for independent review. Writes now require
`--authorize-writes`. No automatic commit, stash, reset, branch, push, retry ladder,
provider configuration, or setup occurs. `--branch` and repeated `--attempts` are
rejected. Existing `.delegate/config.json` overrides are not applied. Historical
`stats` is read-only; filtering and initialization are unavailable.

```bash
python3 scripts/delegate.py run --task-file - --read-only --task-id review-1
python3 scripts/delegate.py run --task-file - --authorize-writes --files src/example.py --verify 'python3 -m unittest' --task-id implementation-1
```

Keep the same task ID and ledger directory for a continuation. Worker `ok` is
execution-only; `outcome: partial` still requires supervisor acceptance. File
checks detect tracked/nonignored deviations; they do not establish confinement.
See [the bundled policy](skills/reasonix-orchestrator/SKILL.md).

Rebuild this package from `../reasonix-orchestrator/scripts/build-compat.py`.
Do not edit generated files directly. `bundle-manifest.json` records parity.
Source validation does not install, activate, or refresh cached plugins.
