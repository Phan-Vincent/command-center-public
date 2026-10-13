# Operations compatibility reference

The maintained procedures are in [execution.md](execution.md). This redirect
preserves legacy links. The core CLI and MCP use the same engine and 90-second
worker default. Use the same objective ID and store across adapters; no automatic
retry or setup occurs.

Canonical failures use `failure.code`, `failure.stage`, and `failure.message`.
The connector may relabel the top-level error code. Worker success requires a
valid nonempty success envelope and zero exit. Signals/deadlines trigger bounded
owned-group cleanup; remote/escaped descendants remain uncertain. Guard version 2
identifies the lifecycle repair, not consolidation activation or confinement.

Read mode uses `dontAsk`, authorized writes use `auto`. Neither a flag nor a
contract is proof of filesystem enforcement. Missing CLI/provider setup is blocked;
never copy keys, run setup, or change permissions without applicable authorization.
