---
name: deepseek-delegation
description: Delegate implementation work to DeepSeek (Flash/Pro) through the Reasonix CLI so Claude only plans, splits and reviews, which cuts Claude usage. Use when the user runs /delegate, asks to "delegate", "offload", "hand off to DeepSeek/Reasonix", "use the cheap model", or "save Claude tokens/usage", or when the repository has a .delegate/ directory and the request is implementation-shaped (boilerplate, tests, refactors, docs, lint fixes, single-file changes, search/summarize). Do not use for pure questions answerable from context, or when the user asks Claude to write the code itself.
---

# DeepSeek delegation

You are the **planner, task splitter and final reviewer**. DeepSeek writes the
code. You do not write implementation code yourself unless the full escalation
ladder has failed AND the user says so.

Everything goes through one script, which runs Reasonix headlessly, verifies the
result, commits it, logs tokens and cost, and prints **one compact JSON line**.
That line is all you read.

## 0. Preflight (once per session)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py" doctor
```

If `ok` is false or `reasonix` is MISSING, stop and show the user the README setup
steps. A dirty working tree blocks runs, so ask the user to commit or stash
first. Never stash or discard their work yourself.

## 1. Plan and split (spend tokens here, not on code)

- Read only what you need to write precise specs: file names, signatures, the
  failing test. Don't read whole modules "for context" when DeepSeek can.
- Split the request into **sub-tasks that are each independently verifiable**.
  Aim for one concern per task, ideally 1-2 files.
- Give every task a **verify command** that exits non-zero on failure (a focused
  test, a linter, a type-check, `python3 -m py_compile x.py`, and so on). Without
  one, success only means "the worker said so".
- Run tasks **sequentially**. They share one working tree.

## 2. Route: Flash by default, Pro only when warranted

| Use `--model flash` (default) | Start at `--model pro` |
| --- | --- |
| boilerplate, tests, docs, lint/format fixes | task spans **3+ files with shared logic** |
| single-file changes, renames, small refactors | concurrency, locking, async ordering |
| search / summarize (`--read-only`) | security-relevant code (auth, crypto, sandbox, input validation) |
| | non-trivial math / numerics |

Escalation on failure is automatic: the script tries **Flash ×2, then Pro ×2**.
Each retry sees the previous test output and fixes the changes on disk instead of
starting over. Pass `--no-escalate` to stay on the starting model.

## 3. Write the spec and run

Pass the spec on stdin with a heredoc (one tool call):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py" run \
  --title "Add retry to fetch_status" \
  --model flash \
  --files collector.py,tests/test_collector.py \
  --verify "python3 -m unittest tests.test_collector -q" \
  --task-file - <<'TASK'
Goal: fetch_status() should retry up to 3 times on URLError with 0.5s backoff.
Context: collector.py:120 fetch_status(host) currently raises on first failure.
Constraints: stdlib only; keep the signature; no new modules.
Acceptance: new test test_fetch_status_retries passes; existing tests pass.
TASK
```

A good spec has the **goal**, **where** (file:line and the symbols involved),
**constraints**, and **acceptance** criteria. It should not contain the
implementation.

For search or summarize work, use `--read-only`. It makes no writes, no commit
and no verify, and the answer comes back in `summary`.

## 4. Read the result (and nothing more, unless told to)

```json
{"status":"success","task_id":"…","model":"flash","attempts":["flash:pass"],
 "branch":"delegate/add-retry-…","commit":"a1b2c3d4e5f6","files_changed":["collector.py","tests/test_collector.py"],
 "diff_stat":"2 files changed, 31 insertions(+), 3 deletions(-)","summary":"…≤5 lines…",
 "test_result":"python3 -m unittest …: pass","open_questions":[],
 "security_sensitive":false,"security_reasons":[],"cost":{"USD":0.0011}}
```

| status | What you do |
| --- | --- |
| `success` | The script already ran the verify command and committed. **Do not** re-run it or read the diff. Check `files_changed` and `out_of_scope` for sanity. If `security_sensitive` is true, read the diff with `git show <commit>` and review it properly. If `warning` says no files changed, check that the task really was a no-op. |
| `needs_human` | Flash ×2 and Pro ×2 all failed. The partial work is in `stash` (`git stash list`), and `failure_tail` shows the last test output. **Stop and ask the user**: (a) re-scope and re-delegate, (b) let Claude implement it (`git stash pop` first to build on the partial work), or (c) drop it. |
| `error` | A credential, model-name or config problem. Retrying won't help. Show `error` to the user. |
| `blocked` | A precondition failed (dirty tree, detached HEAD, prompt too large). Fix it or ask. |

Answer `open_questions` yourself when you can, by re-delegating with the answer
in the spec. Otherwise pass them to the user.

## 5. Final review (once, after all sub-tasks)

1. Run the project's full test and lint command once.
2. Get the overview: `git log --oneline <base>..HEAD` and `git diff --stat <base>..HEAD`.
   `base` is in the first result.
3. Read full diffs **only** for commits that failed checks or were security-sensitive.
4. If something is wrong, delegate a focused fix-up task rather than editing it yourself.
5. Report to the user: the commits (one per task, so each can be rolled back with
   `git revert <sha>`), anything escalated or stashed, and the cost from
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py" stats --since 24h`.

## Reasonix MCP tools (optional)

If the `reasonix-orchestrator` plugin is installed, its `reasonix` MCP server
offers `probe`, `delegate_read` and `delegate_write`. Use them like this:

- Use `probe` / `delegate_read` for read-only work **on an SSH host the user
  named**. Its reply contains Reasonix's full answer, so keep the contract
  tightly scoped.
- For local read-only work, prefer `delegate.py run --read-only`. It returns a
  capped summary and logs cost.
- Don't use `delegate_write` for code changes. It skips verification, commits,
  cost logging and the Flash→Pro ladder. Use `delegate.py run` instead.

## Rules

- Never write implementation code yourself unless DeepSeek failed the full ladder
  and the user approved. Small spec fixes and re-delegation come first.
- Never pass secrets, API keys or `.env` contents in a spec.
- Never commit, push, or rewrite history on the user's main branch. The script
  creates `delegate/<slug>` branches automatically when started on `main`/`master`.
- Keep each spec under ~2k words. If it needs more, the task is too big, so split it.
