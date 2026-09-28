---
description: Delegate coding work to DeepSeek via Reasonix (Claude plans and reviews, DeepSeek writes the code)
argument-hint: <task description> | stats [--since 7d] | doctor
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py:*), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py":*), Bash(git status:*), Bash(git log:*), Bash(git diff:*), Bash(git show:*), Bash(git stash list:*)
---

Request: $ARGUMENTS

- If the request is `stats ...`, run
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py" stats <rest of the arguments>`
  and show the output as is.
- If the request is `doctor`, run
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/delegate.py" doctor` and explain any problems
  it finds, using the plugin README's setup steps.
- If the request is empty, ask what should be delegated.
- Otherwise, handle the request with the **deepseek-delegation** skill. Run the
  preflight, split the work into verifiable sub-tasks, route each one to Flash or
  Pro, run them one at a time with `delegate.py run`, and then do the final
  review. Do not write implementation code yourself.
