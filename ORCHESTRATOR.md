# Lightweight task orchestrator

This is a small, filesystem-backed supervisor for delegating bounded work while keeping the repository and local operator authoritative. It adds no service, database, queue, network listener, or Python dependency.

## Architecture

The CLI writes one JSON record per task, one normalized result per attempt, an inspectable context package per dispatch, and an append-only decision log under `data/orchestrator/`. That directory is already covered by this repository's `data/` runtime ignore rule. Set `ORCHESTRATOR_HOME` or pass `--store` to use another location.

Each worker assignment consumes one transition. Automatic retries against the same Reasonix worker are execution attempts inside that transition and do not consume depth. A retryable failure may trigger the bounded Flash → Pro → Codex escalation path while depth remains available. There is no autonomous planning loop.

Workers have deliberately different trust boundaries:

- `external_chatgpt`: a human-operated, read-only external research worker. The CLI creates a complete prompt; the user manually runs it in external ChatGPT and imports the response.
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

Plain-language inference is intentionally conservative and centralized. Implementation/edit/fix language implies repository write access; test/build/execute language implies terminal access; latest/current/news/pricing language implies current information; documentation/API research implies web research; personal/organization account wording implies a human account. Supplying project files implies repository read access. Old v1.1 task files without `requirements` remain readable and are inferred when routed.

Explicit requirement flags override inference, including negative forms such as `--no-requires-repo-write`:

```bash
./orchestrator create "Research the latest API behavior" \
  --requires-web --requires-current

./orchestrator create "Fix the cache bug and run tests" \
  --requires-repo-write --requires-terminal
```

Requirements are evaluated before task-type heuristics:

- repository writes or terminal execution → `codex`
- a human account → `external_chatgpt`
- web/current research → `external_chatgpt`
- clearly complex non-repository reasoning → `reasonix_pro`
- routine reasoning and read-only code proposals → `reasonix_flash`

Explicit worker selection still wins. Inspect and record a deterministic explanation without running the task:

```bash
./orchestrator route TASK_ID
./orchestrator run TASK_ID --explain
```

Every dispatch writes an append-only routing record with the selected worker, timestamp, reasons, requirement snapshot, rule, and potential escalation path under `data/orchestrator/routing/TASK_ID/`.

Use an explicit worker when needed:

```bash
./orchestrator run TASK_ID --worker reasonix_flash
./orchestrator delegate TASK_ID --worker reasonix_pro
./orchestrator retry TASK_ID --worker reasonix_pro
```

`delegate` is the explicit way to continue a completed task with another worker. `retry` is limited to failed or waiting tasks. Running and completed tasks are protected from accidental duplicate `run` calls.

When a prior route was actually wrong, record that empirical signal rather than treating every valid escalation as a misroute:

```bash
./orchestrator reroute TASK_ID \
  --worker codex \
  --reason "requires repository write"
```

The original worker, replacement worker, and reason are preserved and counted in `stats`.

## Transient Reasonix retries

Reasonix failures include the original message plus structured `category`, `code`, and `retryable` fields. Categories are `transient`, `permanent`, `invalid_input`, `worker_unavailable`, `timeout`, `cancelled`, and `unknown`. Structured provider errors take precedence over text classification.

HTTP 429/500/502/503/504, connection resets/timeouts, read timeouts, temporary network failures, and upstream unavailability can retry. Authentication errors, invalid API keys/models, permission denial, malformed tasks, unsupported requests, and cancellation do not retry.

One Reasonix invocation allows at most three execution attempts. Backoff defaults to about one second and two seconds with small jitter and a bounded total budget. Each attempt records its worker, number, start/end timestamps, latency, status, and classified error. The CLI shows a concise retry summary on standard error; the same sequence is preserved in result metadata and append-only events.

After three retryable Flash failures, non-repository work can escalate to Pro. If repository writes or terminal authority are required, it escalates directly to Codex. Exhausted Pro can escalate to Codex. Each escalation consumes normal transition depth and records its reason; same-worker retries do not.

## One-command workflow

`ask` is sugar over the existing create, route, context-package, and dispatch paths:

```bash
./orchestrator ask \
  "Research the latest third-party Browse API pagination behavior" \
  --requires-web --requires-current

./orchestrator ask \
  "Fix the scanner cache invalidation bug and run tests" \
  --requires-repo-write --requires-terminal --explain

./orchestrator ask "Analyze this bounded design choice" --worker reasonix_pro
```

Automated workers print their final status and summary. External tasks print the existing manual handoff and import command. Clipboard use remains opt-in through `handoff --copy`.

## Context packaging

Add project-relative files at creation time:

```bash
./orchestrator create "Review pagination handling" \
  --file scanner.py --file tests/test_scanner.py \
  --constraint "Do not change public API behavior" \
  --accept "Identify the exact pagination termination rule"
```

The packager rejects absolute and parent-traversal paths, caps each excerpt and the total package size, records omitted files, includes only a short prior result summary, and saves the package for inspection. It never dumps the full repository implicitly.

## External ChatGPT manual handoff

External ChatGPT is intentionally human-operated. There is no browser runner, DOM scraping, cookie access, session extraction, or automated authentication.

Create and run a research task normally:

```bash
./orchestrator create \
  "Research whether a third-party REST API changed pagination behavior" \
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

Paste the prompt into the manually authenticated external ChatGPT account. Then import the entire response either interactively:

```bash
./orchestrator complete TASK_ID --stdin
```

Paste the response and finish input with the normal terminal EOF keystroke, or import a file:

```bash
./orchestrator complete TASK_ID --file response.txt
```

The raw response is preserved both in the normalized result and as a text artifact. Provenance remains `worker: external_chatgpt` with `execution_mode: manual_handoff`. Importing the answer completes the existing attempt; it does not consume another worker transition or delegation-depth slot. The completed task may then be explicitly delegated to Reasonix or Codex if repository work is needed.

## Recording Codex or manual completion

When a task routes to `codex` or `local`, inspect the saved context package, perform the authoritative work separately, then record it:

```bash
./orchestrator complete TASK_ID --summary "Implemented and verified with the focused test suite"
```

Use `--result-file PATH` to retain a longer local report.

## State, recovery, and metrics

Task states are `pending`, `running`, `waiting_for_user`, `completed`, `failed`, and `cancelled`. Writes use atomic replacement, events are append-only JSONL, and each attempt keeps its raw worker response. `list`, `status`, `result`, and `context` make state inspectable; `retry` preserves prior attempts.

`stats` retains the v1.1 totals and adds observed routing outcomes for initially selected Flash, Pro, external, and Codex tasks; direct completions; Flash → Pro; Flash → Codex; Pro → Codex; external → Reasonix/Codex; failures; routing overrides; structured failure categories; automatic retry counts; and completed-without-Codex percentages. It also reports average and median successful automated latency by worker and separately labeled median human external-handoff wait time. It does not fabricate token, monetary, or time savings.

If a process is interrupted while a task is `running`, inspect its event log and result directory before changing the task. This v1 intentionally does not guess whether an external side effect occurred.

## End-to-end example

Task: “Research the current third-party Browse API pagination behavior and determine whether our scanner implementation needs changing.”

1. Create the research task; the router selects `external_chatgpt`.
2. Run the task and use `handoff --copy` to copy the generated prompt.
3. Paste it into the external ChatGPT account, then import the complete answer with `complete --stdin`.
4. The supervisor preserves the answer and research provenance. If code analysis is needed, explicitly delegate to `reasonix_flash`, which proposes a patch without writing files.
5. Delegate to `codex`; the CLI records a waiting handoff with the accumulated context.
6. Codex inspects the live repository, implements the change, runs tests, and records completion with `complete --summary`.

The full decision and result history remains readable under `data/orchestrator/`.

## Adding another worker

Add the worker name to the schema, implement a small adapter returning the normalized result shape, register it in `AdapterRegistry`, and add deterministic routing plus tests. Keep credentials outside task records and logs, pass prompts through standard input or a structured API, and preserve the one-transition supervisor contract.

## Security assumptions and limitations

- The canonical repository remains under Codex/local control.
- Delegated Reasonix and external workers are read-only proposal generators.
- Existing Reasonix provider configuration remains owned by Reasonix; this project never reads or copies it.
- External ChatGPT authentication and operation remain entirely user-controlled.
- Runtime results can contain sensitive source context; protect `data/orchestrator/` like the repository itself.
- v1 has local file locking and duplicate-state checks, not distributed leases or crash recovery.
- No browser automation, scraping, cookie access, CAPTCHA handling, or credential storage is included.
- No dashboard or background scheduler is included; those are intentionally later milestones.

## Troubleshooting

- “Reasonix MCP server was not found”: set `REASONIX_MCP_SERVER` to the existing plugin server.
- Reasonix failure or timeout: inspect the structured error and per-attempt history. Retryable failures are attempted at most three times before a bounded escalation; permanent failures stop immediately. Do not bypass permissions.
- “Session is in use”: wait for the other bounded run to finish or close the duplicate Reasonix project window; do not stop the managed MCP tunnel.
- External worker waits: run `handoff TASK_ID`, use the prompt manually, then import the answer with `complete --stdin` or `complete --file`.
- Maximum depth reached: inspect the complete history; create a deliberate child task rather than silently extending an agent loop.
- Malformed state: do not overwrite it. Preserve the JSON and event log for diagnosis.
