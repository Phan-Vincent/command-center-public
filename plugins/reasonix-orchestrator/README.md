# Reasonix Orchestrator

A Mac-first Codex plugin that keeps Codex as supervisor while Reasonix performs bounded repository work.

## Architecture

Codex starts `mcp/reasonix_mcp.py` as a local stdio MCP process when the plugin is active. The server exposes three deliberately separate tools:

- `probe`: read-only availability check for the current Mac or an explicitly named SSH host.
- `delegate_read`: fail-closed Reasonix work using `--permission-mode dontAsk`.
- `delegate_write`: write-capable Reasonix work using `--permission-mode auto`; the call is rejected unless `authorize_writes` is exactly `true`.

The MCP server invokes the bundled `reasonix_delegate.py` wrapper with a fixed executable path and argument vector. Worker contracts travel on stdin, never through shell interpolation. Local execution on the Mac is the default. SSH routing is available only when the caller supplies a host or configured alias.

Flash is the default worker and is selected explicitly through the configured `deepseek-flash` provider. Pro requires `worker_tier: pro` and one reason-coded gate: `explicit_request`, `high_complexity`, or `flash_verification_failed`. A failed Flash verification permits at most one narrowed Pro retry for the same objective.

There is no background daemon, listening socket, credential broker, or Reasonix configuration migration. Codex owns the MCP process lifetime, and each delegation remains a bounded Reasonix CLI invocation.

## Requirements

- macOS with `python3` on `PATH`.
- Reasonix 1.x installed and configured on every execution target.
- Batch-mode SSH already configured for any explicitly requested remote target.

The plugin never reads, copies, or configures Reasonix provider credentials.

## Secure MCP Tunnel

Hosted ChatGPT Work can reach this private stdio server through OpenAI Secure MCP Tunnel. `tunnel-client` runs on the Mac, opens only outbound HTTPS to OpenAI, and launches the same `mcp/reasonix_mcp.py` command locally. The Reasonix and tunnel credentials remain on the Mac; they are never placed in the plugin or sent as MCP arguments.

## Validate

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile mcp/reasonix_mcp.py skills/reasonix-orchestrator/scripts/reasonix_delegate.py
```
