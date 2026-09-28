# Operations and recovery

## Target requirements

Each execution target needs:

- a current Reasonix 1.x CLI available as `reasonix`;
- its own Reasonix provider configuration and API key;
- access to the requested project directory;
- Python 3 on the Codex side to run the bundled wrapper;
- non-interactive SSH authentication for remote targets.

Do not read, print, transfer, or duplicate the target's Reasonix API key. The same plugin can supervise multiple hosts, but Reasonix setup is per host.

## User-approved setup

When Reasonix is absent and the user asks to install it, use the current official installation path for that target:

```bash
npm i -g reasonix
reasonix setup
reasonix --version
```

`reasonix setup` is interactive and may store provider credentials. Let the user complete credential entry. Do not automate it through shell history, command arguments, logs, or chat.

## Wrapper reference

```text
reasonix_delegate.py probe [--host HOST] [--dir PATH]

reasonix_delegate.py run --mode read|write --dir PATH
  [--host HOST] [--authorize-writes]
  [--model NAME] [--effort LEVEL]
  [--max-steps N] [--timeout-seconds N]
```

`run` accepts the worker prompt only on standard input. It emits one JSON object and uses these exit codes:

- `0`: Reasonix returned a successful structured result.
- `1`: Reasonix ran but failed, returned malformed output, or the probe failed.
- `2`: invalid wrapper arguments or unsafe target input.
- `124`: timeout.
- `127`: local executable or SSH transport missing.

Read mode maps to Reasonix `--permission-mode dontAsk`; unapproved writers fail closed. Write mode maps to `--permission-mode auto` and is rejected unless `--authorize-writes` is present. The wrapper never offers bypass permissions.

## Mac-first MCP lifecycle

The plugin declares `reasonix` in `.mcp.json`. Codex starts `mcp/reasonix_mcp.py` as a local stdio child process and owns its lifetime. The server does not bind a network port, install a launch agent, or persist a daemon.

The server implements `initialize`, `ping`, `tools/list`, and `tools/call` over newline-delimited JSON-RPC. It exposes `probe`, `delegate_read`, and `delegate_write`, then invokes the bundled wrapper through a fixed Python argument vector. `task_contract` is sent on stdin. `delegate_write` is marked destructive and is rejected unless `authorize_writes` is exactly `true`.

The MCP server selects `deepseek-flash` for every ordinary delegation. `deepseek-pro` requires one of the enumerated escalation reasons in `SKILL.md`; the server rejects an unreasoned Pro request and records the selected tier, provider, and escalation reason in the structured result.

Use the CLI wrapper as a fallback when the MCP server is unavailable and as the direct validation surface during plugin development.

For hosted ChatGPT Work testing, use OpenAI Secure MCP Tunnel in stdio mode. Keep the tunnel profile free of secrets, provide the runtime API key only through the Mac process environment, and point `--mcp-command` at the installed plugin's absolute `python3 mcp/reasonix_mcp.py` command. Do not open an inbound port or put Reasonix credentials in tunnel configuration.

## Remote use

Pass only a host or alias already named by the user and available in SSH configuration. The wrapper accepts conservative host syntax (`host`, `user@host`, dots, underscores, and hyphens) and runs SSH in batch mode so it cannot hang on a password prompt.

Remote project paths are shell-quoted by the wrapper. Prompt content travels through standard input rather than the remote shell command. If the remote project, CLI, or authentication is missing, stop and report that target-specific blocker.

## Failure recovery

- Missing CLI: ask whether to install Reasonix on that target.
- Missing provider setup: ask the user to run `reasonix setup` on that target.
- Permission denial: narrow the task or request the needed authorization; never switch to bypass mode.
- Timeout: inspect partial stderr/result, reduce scope, then retry once only if the retry remains within authorization.
- Malformed JSON: preserve the exit code, report a short bounded excerpt, and use `reasonix --version` to check compatibility.
- Writer conflict or dirty overlap: stop the writer, preserve the tree, and use an isolated worktree or ask the user.

Reasonix CLI source of truth: https://reasonix.io/docs/ and https://github.com/esengine/DeepSeek-Reasonix/blob/main-v2/docs/CLI.md
