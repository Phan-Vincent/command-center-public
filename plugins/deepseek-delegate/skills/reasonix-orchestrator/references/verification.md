# Verification and Results

For reads and external responses, check relevance, evidence, scope, omissions,
and unsupported claims. Worker results remain advisory.

For writes, compare pre/post state, inspect every changed file, review the diff,
and distinguish worker changes from user edits. Worker security labels never
waive review. Run required project checks, including broad checks, and focused
acceptance checks. Avoid redundant repeats only when evidence covers the final
state. Passing checks do not prove untested properties.

## Results and acceptance

Return JSON or equivalent structured data:

- task_id: stable string identifying the objective.
- outcome: success | partial | failed | blocked | cancelled | unknown.
- scope: operation, target, host or null, inspected files, changed files, deviations.
- changes: concise descriptions; empty list when none.
- verification: status passed | failed | partial | not_run | unknown; checks with
  name, executor (worker or supervisor), result, and evidence.
- process: status stopped | running | unknown | not_applicable; evidence and any
  remote or descendant uncertainty.
- unresolved: remaining issues.
- risks: material limitations.
- usage: model, effort, attempts, tokens, cost, measurement source; unavailable
  values are "unknown".

Engine results report execution only. A worker `ok` means the run completed and
returned; it is not acceptance. Until the supervisor verifies, the final outcome
stays partial. Record visible model and effort even when token or cost telemetry
is unknown; never infer completion or savings from silence. Malformed results
cannot prove success; reconstruct facts independently within authorization or
retain unknown. Acceptance requires evidence, adequate verification, and safely
stopped or not_applicable task process state.

Normalized outcomes must preserve original tool failures and verification limits
rather than turning a useful partial summary into successful execution.
