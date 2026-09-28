---
name: reasonix-orchestrator
description: Orchestrate bounded coding, research, review, and verification work through the plugin's Mac-local Reasonix MCP server, with a guarded CLI fallback, on the current machine or an explicitly named SSH host. Use when the user asks Codex to use, delegate to, or orchestrate Reasonix or DeepSeek workers through Reasonix, including to conserve Codex usage. Do not use for ordinary Codex subagents or generic coding requests that do not involve Reasonix.
---

# Reasonix Orchestrator

Keep Codex as the supervisor and Reasonix as a bounded worker. Preserve the user's requested scope and authorization. A Reasonix answer is a claim to inspect, not evidence that a change is correct.

The architecture is Mac-first: Codex launches the plugin's local stdio MCP server and that server starts bounded Reasonix CLI processes. Default to the current Mac and current workspace. Use SSH only when the user explicitly names the target and remote project path. Do not start a daemon, open a listening port, or move provider credentials into Codex.

## Route the request

1. Use the current workspace unless the user explicitly names an SSH target or remote path. Never guess a host, copy credentials between machines, or broaden the workspace.
2. Select `read` for exploration, diagnosis, review, planning, or ambiguity. Select `write` only when the current user request authorizes implementation or file changes.
3. For a small cohesive task, use one worker. Use two to four workers only for genuinely independent research or verification partitions. Run concurrent writers only in isolated worktrees with non-overlapping ownership; otherwise use one writer at a time.
4. Inspect `git status` before writer delegation. Preserve existing changes. If the delegated scope overlaps dirty user work and cannot be isolated safely, stop and ask for direction.

Read [task-contract.md](references/task-contract.md) when decomposing work or constructing a worker prompt. Read [operations.md](references/operations.md) when Reasonix is missing, the target is remote, flags need customization, or a run fails.

## Use the MCP tools

Prefer the `reasonix` MCP server tools supplied by this plugin:

- `probe` checks the local or explicitly named target without changing it.
- `delegate_read` runs the worker in fail-closed read mode.
- `delegate_write` runs a write-capable worker and requires `authorize_writes: true` in addition to authorization from the user's request.

Pass the complete worker contract in the `task_contract` argument. It is forwarded as process stdin, not shell text. Keep `host`, `worker_tier`, `escalation_reason`, and `effort` absent unless the routing rules or user request require them; an omitted `worker_tier` is Flash.

## Select the worker tier

Use `worker_tier: flash` by default. The MCP server maps it to the configured `deepseek-flash` Reasonix provider even if the machine-wide Reasonix default later changes.

Use `worker_tier: pro` only with exactly one `escalation_reason`:

- `explicit_request`: the user explicitly asks for DeepSeek Pro or the highest Reasonix tier.
- `high_complexity`: the bounded contract spans multiple dependent systems or repositories, requires security or credential threat modeling, or needs broad evidence synthesis across several plausible failure causes.
- `flash_verification_failed`: a completed Flash attempt fails, is partial or malformed, or its result does not satisfy independent Codex verification.

For `flash_verification_failed`, narrow or correct the contract before the Pro retry. Allow at most one Pro retry for the same objective. Do not escalate solely because Flash is slower than expected, and never cascade into another model automatically.

## CLI fallback

Use the bundled wrapper only when the MCP tools are unavailable or when validating the plugin itself.

Locate this skill directory and invoke the bundled wrapper with the same Python environment Codex can execute:

```bash
python3 scripts/reasonix_delegate.py probe --dir "$PWD"
```

For an explicitly named SSH host:

```bash
python3 scripts/reasonix_delegate.py probe --host HOST --dir REMOTE_PROJECT
```

The probe is read-only. If Reasonix is missing or unconfigured, report the exact blocker and the appropriate setup command from `operations.md`. Do not install, upgrade, run an interactive setup wizard, or expose/copy an API key without the user's separate instruction.

### Delegate through the wrapper

Send the complete worker contract on standard input. Do not interpolate task text into a shell command.

Read-only example:

```bash
python3 scripts/reasonix_delegate.py run \
  --mode read --dir "$PWD" --max-steps 12 < task.txt
```

Authorized writer example:

```bash
python3 scripts/reasonix_delegate.py run \
  --mode write --authorize-writes --dir "$PWD" --max-steps 24 < task.txt
```

Add `--host HOST` for a configured SSH target. For direct CLI fallback, pass `--model deepseek-flash` by default or `--model deepseek-pro` only under the bounded escalation policy. Add `--effort` only when the user requested a value or the target's known configuration requires it.

The wrapper and MCP server use Reasonix structured JSON output. Read mode uses fail-closed `dontAsk`; write mode uses `auto` and requires an explicit authorization flag. Never use `bypassPermissions`, YOLO, dynamic Bash opt-ins, or a broader directory merely to make a task succeed.

## Adjudicate the result

1. Check the wrapper exit status and structured `ok` field. Treat timeouts, malformed JSON, denied operations, and partial reports as failures or unresolved work.
2. Inspect every changed file and the repository diff. Confirm that no unrelated user changes were overwritten.
3. Run the relevant focused checks yourself. Expand to broader tests only when useful and safe.
4. Compare results against the acceptance criteria. If evidence is missing, perform a narrow follow-up or report the gap; do not silently assert success.
5. Summarize what Reasonix did, what Codex independently verified, remaining risks, and any cost/usage data present in the structured result.

Do not leave unattended Reasonix processes running after the requested work is complete. Do not expose transcripts, trajectories, prompts, or provider configuration unless the user asks for them.
