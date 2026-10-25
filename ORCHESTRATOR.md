# Lightweight task orchestrator

This is a small, filesystem-backed supervisor for delegating bounded work while keeping the repository and local operator authoritative. It adds no service, database, queue, network listener, or Python dependency.

## Architecture

The CLI writes one JSON record per task, one normalized result per attempt, an inspectable context package per dispatch, and an append-only decision log under `data/orchestrator/`. That directory is already covered by this repository's `data/` runtime ignore rule. Set `ORCHESTRATOR_HOME` or pass `--store` to use another location.

Each worker assignment consumes one transition and exactly one worker attempt. There is no automatic retry, backoff, or Flash → Pro → Codex escalation inside a dispatch. A failed attempt is terminal until the supervisor explicitly delegates or retries it, and every continuation reuses the same task/objective ID. Work is refused while a prior Reasonix worker's stop state is unknown or still running. There is no autonomous planning loop.

Workers have deliberately different trust boundaries:

- `external_chatgpt`: a read-only external research worker in the user's external ChatGPT account. The CLI creates a complete prompt; the supervisor (Codex or Claude) submits it in the user's already logged-in ChatGPT browser tab, or the user does, and the response is imported.
- `gemini_app`: a read-only reviewer and second opinion running Gemini 4 Argon in the user's Google account. The Gemini CLIs don't serve Argon on the user's Pro plan, so it works like `external_chatgpt`: the CLI writes a prompt, the supervisor submits it in the user's logged-in gemini.google.com tab in Chrome, and the response is imported.
- `reasonix_flash`: default cheap reasoning and code proposals through the existing Reasonix stdio MCP server.
- `reasonix_pro`: bounded high-complexity reasoning or an explicit escalation. Pro requires a recorded escalation reason.
- `codex`: authoritative repository work, tests, debugging, Git, and final verification. The CLI produces a context handoff and waits; it never recursively launches Codex.
- `local`: manual/operator work with the same explicit handoff behavior.

## Setup

Run from this checkout:

```bash
./orchestrator create "Summarize the authentication design" --type research
./orchestrator run TASK_ID
./orchestrator status TASK_ID
./orchestrator result TASK_ID
./orchestrator context TASK_ID
./orchestrator stats
```

The Reasonix adapter searches for the existing Mac-first plugin at `~/plugins/reasonix-orchestrator/mcp/reasonix_mcp.py` and installed personal-plugin cache locations. Override discovery with:

```bash
export REASONIX_MCP_SERVER=/absolute/path/to/reasonix_mcp.py
```

It starts that stdio MCP server for a single bounded `delegate_read` call. It does not invoke Reasonix directly, open a port, move credentials, or grant repository writes.

## Routing and overrides

Routing is deterministic, requirements-first, and inspectable. New tasks store these boolean capability requirements with safe defaults:

```text
repo_read, repo_write, terminal, web_research, current_information,
human_account, external_files
```

Plain-language inference is intentionally conservative and centralized. Implementation/edit/fix language implies repository write access; test/build/execute language implies terminal access; latest/current/news/pricing language implies current information; documentation/API research implies web research; external/university account wording implies a human account. Supplying project files implies repository read access. Old v1.1 task files without `requirements` remain readable and are inferred when routed.

Explicit requirement flags override inference, including negative forms such as `--no-requires-repo-write`:

```bash
./orchestrator create "Research the latest API behavior" \
  --requires-web --requires-current

./orchestrator create "Fix the cache bug and run tests" \
  --requires-repo-write --requires-terminal
```

After explicit or created-worker overrides, requirements and wording are evaluated in this order:

- repository writes or terminal execution → `codex`
- a human account → `external_chatgpt`
- an affirmative Gemini request → `gemini_app` (`gemini_requested`), even when the task also needs web/current research
- web/current research → `external_chatgpt`
- asking for a second opinion, or `--type second_opinion` / `plan_review` → `gemini_app` (`gemini_second_opinion`)
- read-only research, summaries, document analysis, or brainstorming → `external_chatgpt`
- clearly complex non-repository reasoning → `reasonix_pro`
- routine reasoning and read-only code proposals → `reasonix_flash`

Affirmative Gemini wording includes `ask Gemini`, `use Gemini`, `have Argon`, `via Gemini`, `send this to Gemini`, `Gemini 4 Argon`, and `Gemini Argon`. A bare mention of Gemini or Argon is a topic, not a request: “Summarize the supplied notes about argon gas” stays with `external_chatgpt`. Second-opinion wording accepts `second opinion`, `second-opinion`, arbitrary whitespace between the words, and any case. A negation in the same punctuation-delimited clause — `don't`/`do not`, `never`, `not`, `without`, `avoid`, or `no` before Gemini/Argon — vetoes every automatic Gemini route, even when another clause asks for Gemini or the task type is `second_opinion`. Explicit worker or created-worker selection still wins. Inspect and record a deterministic explanation without running the task:

```bash
./orchestrator route TASK_ID
./orchestrator run TASK_ID --explain
```

Every dispatch writes an append-only routing record with the selected worker, timestamp, reasons, requirement snapshot, rule, and potential explicit follow-up workers under `data/orchestrator/routing/TASK_ID/`.

Use an explicit worker when needed:

```bash
./orchestrator run TASK_ID --worker reasonix_flash
./orchestrator delegate TASK_ID --worker reasonix_pro
./orchestrator retry TASK_ID --worker reasonix_pro
```

`delegate` is the explicit way to continue a completed task with another worker. `retry` is limited to failed or waiting tasks. Running and completed tasks are protected from accidental duplicate `run` calls. Any continuation is refused while the previous Reasonix worker's stop state is `unknown` or `running`; confirm the worker stopped before recovering.

When a prior route was actually wrong, record that empirical signal rather than treating every valid escalation as a misroute:

```bash
./orchestrator reroute TASK_ID \
  --worker codex \
  --reason "requires repository write"
```

The original worker, replacement worker, and reason are preserved and counted in `stats`.

## Reasonix failure handling and recovery

Reasonix failures include the original message plus structured `category`, `code`, and `retryable` fields. Categories are `transient`, `permanent`, `invalid_input`, `worker_unavailable`, `timeout`, `cancelled`, and `unknown`. Structured provider errors take precedence over text classification.

HTTP 429/500/502/503/504, connection resets, temporary network failures, and upstream unavailability are classified as transient. Authentication errors, invalid API keys/models, permission denial, malformed tasks, unsupported requests, cancellation, timeouts, missing results, and unknown outcomes are not retryable.

A dispatch performs exactly one MCP `delegate_read` attempt. It sends the task ID as the engine's objective ID and preserves the engine's `outcome` and `process` stop evidence in result metadata. The supervisor never retries, backs off, or escalates automatically, including for transient failures.

After a failure, inspect the result and recover explicitly with `retry`, `reroute`, or `delegate`. Recovery reuses the same objective ID. It requires positive stop evidence: a stopped or never-started local process, drained pipes, and no unknown outcome or unresolved process state. Missing evidence in legacy records also blocks recovery. All prior attempts are checked, so a manual completion record or different worker selection cannot conceal an unresolved worker. Evidence that the worker never started (for example a context-packaging failure) permits an explicit retry once the input or storage problem is fixed. The Reasonix engine still enforces its original deadline and continuation limits; passing the local stop check does not grant another engine attempt.

## One-command workflow

`ask` is sugar over the existing create, route, context-package, and dispatch paths:

```bash
./orchestrator ask \
  "Research the latest eBay Browse API pagination behavior" \
  --requires-web --requires-current

./orchestrator ask \
  "Fix the scanner cache invalidation bug and run tests" \
  --requires-repo-write --requires-terminal --explain

./orchestrator ask "Analyze this bounded design choice" --worker reasonix_pro
```

Automated workers print their final status and summary. External tasks print the handoff and import command. Clipboard use remains opt-in through `handoff --copy`.

## Context packaging

Add project-relative files at creation time:

```bash
./orchestrator create "Review pagination handling" \
  --file scanner.py --file tests/test_scanner.py \
  --constraint "Do not change public API behavior" \
  --accept "Identify the exact pagination termination rule"
```

The packager rejects absolute and parent-traversal paths, caps each excerpt and the total package size, records omitted files, includes only a short prior result summary, and saves the package for inspection. It never dumps the full repository implicitly.

## External ChatGPT handoff

The CLI itself has no browser runner. The supervisor agent (Codex or Claude) is authorized to operate the user's already logged-in external ChatGPT tab through its browser tools: select the model, submit the prompt, wait for the full response, and import it. It never signs in, enters or reads credentials, extracts cookies or session data, or solves CAPTCHAs; a logged-out, rate-limited, or blocked tab stops the handoff and goes back to the user. Every submission must visibly use GPT-6.1 Sol at High effort or above where offered (higher when the task needs it), GPT-6 Astra at Medium or High effort in ChatGPT's Work mode, or a stronger verified model; record the model and effort. The external account's Chat picker currently shows only GPT-5.6 Sol, which does not qualify. GPT-6 Astra in Work mode is the practical choice there.

Create and run a research task normally:

```bash
./orchestrator create \
  "Research whether the eBay Browse API changed pagination behavior" \
  --accept "Identify the current termination rule and cite the source"

./orchestrator run TASK_ID
```

When the router selects `external_chatgpt`, the task moves to `waiting_for_user` and a complete Markdown prompt is saved beneath:

```text
data/orchestrator/tasks/TASK_ID/artifacts/001-external_prompt.md
```

Inspect the handoff or copy the prompt on macOS:

```bash
./orchestrator handoff TASK_ID
./orchestrator handoff TASK_ID --copy
```

Submit the prompt in the logged-in external ChatGPT tab after confirming the model and effort. Then import the entire response either interactively:

```bash
./orchestrator complete TASK_ID --stdin
```

Paste the response and finish input with the normal terminal EOF keystroke, or import a file:

```bash
./orchestrator complete TASK_ID --file response.txt
```

The raw response is preserved both in the normalized result and as a text artifact. Provenance remains `worker: external_chatgpt` with `execution_mode: manual_handoff` (the stored name for a prompt-file handoff, whoever submits it). Importing the answer completes the existing attempt; it does not consume another worker transition or delegation-depth slot. The completed task may then be explicitly delegated to Reasonix or Codex if repository work is needed.

## Gemini Argon handoff

Automatic Gemini routing requires affirmative delegation wording such as `ask Gemini`, `use Gemini`, `have Argon`, `via Gemini`, `send this to Gemini`, `Gemini 4 Argon`, or `Gemini Argon`; bare topic mentions do not select it. Repository/terminal and human-account requirements come first. An affirmative request outranks web/current research, while a `second opinion`, `second-opinion`, or whitespace-separated variant (or `second_opinion` / `plan_review` task type) comes after web/current research. A same-clause negated Gemini/Argon mention vetoes both automatic Gemini rules; explicit worker overrides still win.

`gemini_app` uses the same handoff, `handoff`, and `complete --stdin`/`--file` commands. The user's Gemini 4 Argon access exists only in the Gemini apps: the `gemini` CLI no longer serves individual accounts, and Antigravity (`agy`) doesn't offer Argon on the Pro plan. The supervisor drives gemini.google.com in the user's logged-in Chrome (Claude in Chrome) rather than the native Gemini Mac app, because the page can be read directly instead of through screenshots.

```bash
./orchestrator ask "Second opinion on this migration plan" --file docs/plan.md
./orchestrator create "Review the rollout plan" --worker gemini_app
```

The prompt is saved as `artifacts/NNN-gemini_prompt.md` and the imported answer as `NNN-gemini_response.txt`. Before each submit, the supervisor opens the mode picker, selects **4 Argon**, sets **Thinking level** to **Medium** or **High** (the page defaults to Low), and confirms both are visible. It uses High for hard reviews, starts a new chat for each task, and records the model and level. The Pro plan is generous, so Gemini can take a second opinion on any substantial plan or analysis, not only rare ones. It is advisory and read-only: its answer never authorizes repository writes, sends, or other actions.

`complete --stdin` and `complete --file` snapshot the worker and attempt count before reading the response and bind the import to them. If the handoff was rerouted or retried while the answer was being pasted, the import is rejected with a `handoff changed … re-run handoff TASK_ID` error before anything is written; no task state or artifact changes, so run `handoff TASK_ID` again and resubmit. `import_external_response` is a thin wrapper that requires `external_chatgpt`.

## Recording Codex or manual completion

When a task routes to `codex` or `local`, inspect the saved context package, perform the authoritative work separately, then record it:

```bash
./orchestrator complete TASK_ID --summary "Implemented and verified with the focused test suite"
```

Use `--result-file PATH` to retain a longer local report.

## State, recovery, and metrics

Task states are `pending`, `running`, `waiting_for_user`, `completed`, `failed`, and `cancelled`. Writes use atomic replacement, events are append-only JSONL, and each attempt keeps its raw worker response. A context read, packaging, or persistence failure before dispatch is recorded as a failed result with `process.local_status: not_started` and diagnostic metadata; the worker is not launched and the task does not remain `running`. A cancellation always wins: a task cancelled during packaging or while a worker is running keeps the `cancelled` task state and stores a `cancelled` result rather than reporting the worker's late output as completed. `list`, `status`, `result`, and `context` make state inspectable; `retry` preserves prior attempts.

`stats` retains the v1.1 totals and adds observed routing outcomes for initially selected Flash, Pro, external, Gemini, and Codex tasks; direct completions; Flash → Pro; Flash → Codex; Pro → Codex; external or Gemini → Reasonix/Codex; failures; routing overrides; structured failure categories; automatic retry counts (always zero under the one-attempt policy); and completed-without-Codex percentages. It also reports average and median successful automated latency by worker and separately labeled median human handoff wait times for external ChatGPT and Gemini (`external_human_wait`, `gemini_human_wait`). It does not fabricate token, monetary, or time savings.

If a process is interrupted while a task is `running`, inspect its event log and result directory before changing the task. This v1 intentionally does not guess whether an external side effect occurred.

## End-to-end example

Task: “Research the current eBay Browse API pagination behavior and determine whether our scanner implementation needs changing.”

1. Create the research task; the router selects `external_chatgpt`.
2. Run the task and use `handoff --copy` to copy the generated prompt.
3. The supervisor submits it in the logged-in external ChatGPT tab (GPT-6 Astra Medium or High in Work mode, or GPT-6.1 Sol High or above where offered), then imports the complete answer with `complete --stdin`.
4. The supervisor preserves the answer and research provenance. If code analysis is needed, explicitly delegate to `reasonix_flash`, which proposes a patch without writing files.
5. Delegate to `codex`; the CLI records a waiting handoff with the accumulated context.
6. Codex inspects the live repository, implements the change, runs tests, and records completion with `complete --summary`.

The full decision and result history remains readable under `data/orchestrator/`.

## Adding another worker

Add the worker name to the schema, implement a small adapter returning the normalized result shape, register it in `AdapterRegistry`, and add deterministic routing plus tests. Keep credentials outside task records and logs, pass prompts through standard input or a structured API, and preserve the one-transition supervisor contract.

## Security assumptions and limitations

- The canonical repository remains under Codex/local control.
- Delegated Reasonix, external, and Gemini workers are read-only proposal generators.
- Existing Reasonix provider configuration remains owned by Reasonix; this project never reads or copies it.
- External ChatGPT and Google/Gemini authentication stays with the user. The supervisor may operate an already logged-in tab but never signs in or touches credentials.
- Runtime results can contain sensitive source context; protect `data/orchestrator/` like the repository itself.
- v1 has local file locking and duplicate-state checks, not distributed leases or crash recovery.
- The CLI includes no browser automation, scraping, cookie access, CAPTCHA handling, or credential storage; browser use belongs to the supervisor agent.
- No dashboard or background scheduler is included; those are intentionally later milestones.

## Troubleshooting

- “Reasonix MCP server was not found”: set `REASONIX_MCP_SERVER` to the existing plugin server.
- Reasonix failure or timeout: inspect the structured error, `outcome`, and `process` stop evidence. Each dispatch makes one attempt; there is no automatic retry or escalation. If the stop state is `unknown` or `running`, confirm the worker stopped before an explicit retry. Permanent failures stop immediately. Do not bypass permissions.
- “Session is in use”: wait for the other bounded run to finish or close the duplicate Reasonix project window; do not stop the managed MCP tunnel.
- External or Gemini worker waits: run `handoff TASK_ID`, submit the prompt in the tab it names (ChatGPT, or gemini.google.com with 4 Argon at Medium/High), then import the answer with `complete --stdin` or `complete --file`.
- Maximum depth reached: inspect the complete history; create a deliberate child task rather than silently extending an agent loop.
- Malformed state: do not overwrite it. Preserve the JSON and event log for diagnosis.
