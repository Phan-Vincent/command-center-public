---
description: Delegate bounded work through the shared Reasonix policy; preserve edits and verify independently.
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py:*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py":*)
---

Apply the plugin's `skills/reasonix-orchestrator/SKILL.md` and relevant references.
For stats or doctor, use the matching read-only adapter subcommand. Otherwise
prepare one cohesive contract and send it via stdin to `scripts/delegate.py run`.
Writes need current human authorization and `--authorize-writes`. Use the same
`--task-id` for continuations. Never invoke an automatic ladder or Git mutation.
Worker success remains partial until every changed file, diff, and required check
has been independently reviewed.

$ARGUMENTS
