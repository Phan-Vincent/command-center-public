# Execution and Recovery

Define objective, authorized mode/target, exclusions, acceptance conditions,
required checks, and any worker-only constraint. Inspect workspace status before
writing and preserve existing edits. One invocation runs one attempt; the engine
never automatically retries, escalates, or changes Git state. Optional adapter
checks run under the same lock and active reservation. Genuine safely stopped
check failures may permit corrected Pro; crashes, timeouts, scope deviations,
and unknown verification cleanup block continuation.

MCP exposes `probe`, `delegate_read`, and `delegate_write`; write requires exact
`authorize_writes:true`. Optional `task_id` identifies the objective. CLI fallback:

```bash
python3 scripts/reasonix_delegate.py run --mode read --dir /absolute/project --task-id review-1 < task.txt
```

Here `scripts/` is relative to the core skill directory. Write uses `--mode write
--authorize-writes`. Default model is `deepseek-flash`; Pro uses `--model deepseek-pro
--escalation-reason explicit_request|high_complexity|flash_verification_failed`.
Probe uses `probe --dir /absolute/project`. The legacy `/delegate` adapter supports
`run --task-file - --read-only`, or authorized writes with `--authorize-writes`.
It can run explicit `--verify` checks separately and detect deviations via `--files`.
Neither adapter commits, stashes, resets, or creates branches.

The ledger is stored outside the target in `$XDG_STATE_HOME/reasonix-orchestrator/objectives`
(default `~/.local/state/`); `REASONIX_LEDGER_DIR` is an optional shared runtime setting.
Both adapters must use the same store and task ID. Never assign a new ID to bypass
an objective's budget. Missing IDs create a new objective and return its ID.

The original deadline includes worker timeout (default 90 seconds) plus two
seconds for cleanup. Continuation requests cannot extend it. Initial Pro consumes
the one substantive Pro allowance. A genuine completed Flash failure can permit
one corrected Pro contract. One same-tier infrastructure retry is allowed only
for a classified transient launch failure with unchanged contract and safe stop
evidence. Timeout, cancellation, malformed reports, missing config, and unknown
stop evidence do not permit retries. No reset for an unfinished/crashed attempt.
The ledger coordinates the same objective, not arbitrary tasks or escaped workers;
the supervisor must enforce isolation across overlapping objectives.

Owned local process groups receive bounded cleanup with reap/pipe evidence.
Process creation itself is not interruptibly bounded; launch time consumes budget
once creation returns. Signal/deadline cancellation is supported; synchronous
stdio cannot process cancellation notifications during execution. Local SSH stop
never proves remote termination. Unknown remote state blocks continuation.

Recover directly only after safe stop and within existing authorization unless
worker-only was required. Do not broaden permissions, install dependencies,
change credentials/setup, or stop unrelated sessions. For operational diagnosis,
see [operations.md](operations.md); detailed contracts remain in [task-contract.md](task-contract.md).
