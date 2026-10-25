---
name: reasonix-orchestrator
description: Handle explicit Reasonix, DeepSeek, delegation, and supervisor-usage-conservation requests (Codex or Claude) through bounded local workers, external ChatGPT, Gemini as a second-opinion worker, and independent verification.
---

# Reasonix Orchestrator

Use this as the shared policy. Answer trivial, context-complete requests directly
when handoff and review cost more than the work. Prefer the external account for
substantial self-contained drafting or analysis; use Reasonix when local access
is needed. Route by capability, complexity, sensitivity, expected savings, and
human authorization. Worker results are advisory.

The supervisor (Codex or Claude) may operate the external account directly in
the user's already logged-in ChatGPT browser tab: choose the model, paste or
upload material, submit, and read the response. Before each submission, visibly
verify GPT-6.1 Sol at High effort or above where offered (raise effort as the
task needs), GPT-6 Astra at Medium or High effort in ChatGPT's Work mode, or
a stronger verified model. The external account's Chat picker currently shows
only GPT-5.6 Sol, which does not qualify; GPT-6 Astra in Work mode is the
practical choice there. Record model and effort; never silently downgrade.
Never sign in, enter credentials, or solve CAPTCHAs; if the tab is logged out or
blocked, stop and ask the user. Assume no local files or plugins on that side.
Completion requires an observed response. This model floor does not apply to
Reasonix Flash/Pro.

For a Gemini second opinion, use the user's logged-in gemini.google.com tab
with 4 Argon at Thinking level Medium or High. Follow the [Gemini handoff](references/routing.md#gemini-handoff).

Human requests supply permission; `authorize_writes:true` is a mechanical gate.
Preserve existing edits. Reversible `codex/` isolation and bounded supervisor
recovery are permitted within authorized implementation unless worker-only was
requested. No automatic commits, stashes, resets, discards, branches, pushes,
deployment, installation, publication, or configuration changes. Exclude secrets,
credentials, unrelated context, and provider configuration from handoffs.

Prefer verified Mac-local MCP; use the bundled CLI fallback when needed. SSH
requires a named host and absolute target path. Execute one attempt per invocation,
Flash by default. Reuse the same task ID for continuations across adapters.
Allow at most one warranted Pro substantive attempt and one same-tier
infrastructure retry within the original deadline. Never escalate for slowness
or retry missing configuration/credentials. Running or unknown process state
blocks overlapping workers and takeover.

The engine's `ok` describes execution; `outcome: partial` still requires supervisor
acceptance. Inspect every changed file and relevant diff, run required project
checks, and report unresolved limits. Permission modes and scope declarations do
not prove filesystem confinement.

Read only the relevant procedures:

- [Routing](references/routing.md) for external and Gemini handoffs and worker selection.
- [Execution](references/execution.md) for CLI, contracts, accounting, and recovery.
- [Verification](references/verification.md) for scope review and result reporting.
