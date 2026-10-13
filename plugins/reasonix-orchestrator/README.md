# Reasonix Orchestrator

Version 0.2.0 consolidates the Reasonix and DeepSeek policies and execution
paths.

The core skill routes substantial self-contained drafting/analysis to the
external ChatGPT account when worthwhile, and bounded repository work to
Reasonix. The supervisor (Codex or Claude) may drive the user's already
logged-in ChatGPT tab directly; it never signs in or handles credentials.
External submissions must visibly use GPT-6.1 Sol at High effort or above, or a
stronger verified model.

MCP exposes `probe`, `delegate_read`, and `delegate_write`. Write requests require
exact `authorize_writes:true`; the human request supplies actual authorization.
The stdio server starts a thin CLI adapter with an independent outer deadline.
The shared stdlib engine owns Reasonix argv/stdin, gate validation, result parsing,
process lifecycle, and durable objective accounting. `dontAsk`/`auto` mode aliases
are preserved. No filesystem sandbox guarantee is inferred from those flags.

Use the same `task_id` and ledger directory for continuations across CLI/MCP.
Each invocation executes one attempt. Flash is default. The ledger permits one
warranted Pro substantive attempt and at most one same-tier transient launch
retry within the original deadline, after adequate stop evidence. A corrected
contract is required for Flash-to-Pro. Missing configuration/credentials,
timeouts, malformed results, unknown cleanup, and unfinished reservations block
continuation. Retries and escalation are manual, not a model cascade.

The default worker timeout is 90 seconds; two seconds of cleanup are reserved
in the objective deadline. Launch itself is not interruptibly bounded. Owned
local groups are cleaned up with bounded reap/pipe evidence. Escaped descendants
and remote termination remain unestablished. The synchronous server supports
signals and deadlines, not active JSON-RPC notification cancellation.

`ok` preserves execution compatibility; `outcome: partial` requires supervisor
scope/diff review and acceptance. Missing usage stays unknown. Use nested
`failure.code/stage/message`: the hosted connector may relabel top-level error
codes. The engine performs no Git mutation. The legacy `/delegate` adapter can
run explicit checks and detect tracked/nonignored file deviations separately;
it retains failures/extra files for review rather than resetting or stashing.
Ignored files and unrelated objectives remain supervisor isolation concerns.

The standalone `deepseek-delegate` package is generated from this core, including
its skill references and stdlib engine. It resolves without another installation:

```bash
python3 scripts/build-compat.py
python3 scripts/build-compat.py --check
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s ../deepseek-delegate/tests -v
```

Core CLI: `skills/reasonix-orchestrator/scripts/reasonix_delegate.py`.
Legacy CLI: `scripts/delegate.py` (`--branch` and repeated `--attempts` rejected;
authorized writes need `--authorize-writes`). Historical `stats` reads existing
logs only; filtering/config initialization and old `.delegate/config.json`
overrides are not supported. Provider configuration belongs to Reasonix.

Requirements: Python 3 on macOS/POSIX; Reasonix already configured on each named
target; noninteractive SSH for remote use. No daemon, listener, credential broker,
provider migration, installation, or automatic Git change is introduced.
Secure MCP Tunnel remains the existing outbound hosted transport.
