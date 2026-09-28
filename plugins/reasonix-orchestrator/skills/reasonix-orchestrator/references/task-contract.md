# Reasonix task contract

Use one contract per worker. Include only task-relevant context; never include API keys, unrelated file contents, private transcripts, or broad personal context.

## Required fields

```text
ROLE
You are a bounded coding worker. Stay inside the stated workspace and scope.

OBJECTIVE
One concrete outcome.

MODE
Read-only analysis, or authorized implementation.

SCOPE
Files, directories, components, or questions owned by this worker.

OUT OF SCOPE
Explicit boundaries, protected files, and unrelated user changes.

CONSTRAINTS
Project instructions, compatibility requirements, and commands that must not run.

ACCEPTANCE CRITERIA
Observable conditions that must be satisfied.

VERIFICATION
Specific checks the worker should run or evidence it should collect.

RETURN
Concise summary; files changed; commands run and outcomes; unresolved items; risks.
```

## Decomposition rules

- Partition by independent question, component, or verification surface—not by arbitrary file counts.
- Give each worker enough context to finish without rediscovering the whole project.
- Avoid duplicate broad repository scans. Assign one orientation worker, then use its findings to scope follow-ups.
- Prefer parallel read-only workers for architecture mapping, security review, test-gap analysis, and alternative hypotheses.
- A writer owns explicit files or an isolated worktree. Never give two concurrent writers overlapping paths.
- Keep Reasonix subagents inside a worker disabled only when the task specifically requires a single-agent baseline; otherwise let the target's configured default stand.

## Acceptance quality

Acceptance criteria should be falsifiable. Prefer “`pytest tests/test_auth.py` passes and no route outside `src/auth/` changes” over “the auth bug is fixed.” Worker-reported success does not replace Codex inspection or verification.
