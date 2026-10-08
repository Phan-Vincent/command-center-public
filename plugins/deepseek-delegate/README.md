# deepseek-delegate

A Claude Code plugin that makes **Claude the planner and reviewer** and
**DeepSeek the implementer**. DeepSeek runs headlessly through the
[Reasonix](https://github.com/esengine/DeepSeek-Reasonix) CLI. Claude writes a
short spec, and DeepSeek edits files on disk. A wrapper script verifies the
change with your tests, commits it, logs tokens and cost, and returns
**one ~150-token JSON line**. That line is all Claude reads.

```
/delegate <request>
   │  Claude: plan → split into verifiable sub-tasks → route Flash/Pro → write spec
   ▼
scripts/delegate.py run   (one Bash call per sub-task)
   1. guardrails: clean tree; auto-branch off main/master → delegate/<slug>-xxxx
   2. reasonix run --model deepseek-flash --permission-mode workspace-write --output-format json  (spec on stdin)
   3. run YOUR --verify commands (the model's own "tests pass" is not trusted)
   4. pass → git commit "delegate(flash): <title>"         fail → retry with test output
   5. ladder: flash, flash, pro, pro → then stash + "needs_human"
   6. append every call to .delegate/log.jsonl
   7. print {status, files_changed, summary, test_result, open_questions, ...}
```

## Contents

| Path | What it is |
| --- | --- |
| `skills/deepseek-delegation/SKILL.md` | Claude's playbook: when to trigger, how to split and route, how to react to each status |
| `commands/delegate.md` | The `/delegate` slash command (`/delegate <task>`, `/delegate stats`, `/delegate doctor`) |
| `scripts/delegate.py` | The wrapper. Pure Python 3 stdlib (3.9+) |
| `tests/` | Unit tests, run against a fake `reasonix` binary (no network, no key) |

## Setup

**1. Install Reasonix 1.x (stable) and add your DeepSeek key.**

```bash
npm i -g reasonix                 # or: brew install esengine/reasonix/reasonix
reasonix --version                # tested against v1.39.2
export DEEPSEEK_API_KEY=sk-...    # put it in ~/.zshrc, or save it with `reasonix setup`
```

**2. Install the plugin.** Either use this repo as a marketplace:

```
/plugin marketplace add phan-vincent/command-center-public
/plugin install deepseek-delegate@command-center
```

or load it straight from a checkout for one session:

```bash
claude --plugin-dir ./plugins/deepseek-delegate
```

**3. Run the preflight check** in the repo you want to work on:

```
/delegate doctor
```

It checks that `reasonix` is on PATH, the key is present, you're in a git repo,
and the tree is clean.

**4. Optional: create a per-repo config.**

```bash
python3 plugins/deepseek-delegate/scripts/delegate.py init   # writes .delegate/config.json
```

## Usage

```
/delegate add unit tests for metrics.py and fix any lint errors in alerts.py
```

Claude splits that into (for example) two tasks, delegates each one, and reports
back with one commit per task and the cost. You can also call the script
directly:

```bash
python3 scripts/delegate.py run --title "Add metrics tests" --model flash \
  --files tests/test_metrics.py \
  --verify "python3 -m unittest tests.test_metrics -q" \
  --task "Add unit tests covering metrics.record() and metrics.query() edge cases."

python3 scripts/delegate.py run --read-only --task "Where is the SSH timeout set, and to what?"
```

| Flag | Meaning |
| --- | --- |
| `--model flash\|pro` | Starting model (default `flash`) |
| `--verify CMD` | Must exit 0 to count as a pass. Repeatable. Falls back to `default_verify` from config |
| `--files a,b` | Files in scope. Listed in the prompt; anything else changed is reported as `out_of_scope` |
| `--task TEXT` / `--task-file PATH\|-` | The spec. `-` reads stdin, which suits heredocs |
| `--read-only` | Search or summarize. Runs Reasonix with `--permission-mode read-only`; no commit |
| `--no-escalate` | Don't move from Flash to Pro |
| `--branch NAME` | Work on this branch (it's created if missing) |
| `--attempts N`, `--max-steps N`, `--timeout S` | Per-run overrides |

### Routing (as told to Claude in SKILL.md)

- **Flash** is the default: boilerplate, tests, refactors, docs, lint fixes,
  single-file changes, and search/summarize.
- **Pro** is used from the start for tasks spanning 3+ files with shared logic,
  or involving concurrency, security or math. It's also used automatically
  after Flash fails verification twice.
- There are at most 2 attempts per model. After Pro fails twice, the partial
  work is stashed and Claude **stops and asks you**. Claude only writes the code
  itself if you approve.

## Output contract

DeepSeek is told to end with only this JSON:

```json
{"status": "done|partial|failed", "files_changed": [...], "summary": "<=5 lines",
 "test_result": "...", "open_questions": [...]}
```

The wrapper returns this to Claude, with facts from git and your test commands
taking precedence over the model's own claims:

```json
{"status":"success","task_id":"20260928-101500-ab12","title":"Add metrics tests",
 "model":"flash","attempts":["flash:pass"],"branch":"delegate/add-metrics-tests-ab12",
 "base":"1f51e86c73a1","commit":"5005c3111ebd","files_changed":["tests/test_metrics.py"],
 "diff_stat":"1 file changed, 48 insertions(+)","summary":"Added 6 tests ...",
 "test_result":"python3 -m unittest tests.test_metrics -q: pass","open_questions":[],
 "security_sensitive":false,"security_reasons":[],"cost":{"USD":0.0041}}
```

| `status` | Meaning | Exit code |
| --- | --- | --- |
| `success` | Verified and committed (read-only: answered) | 0 |
| `needs_human` | Ladder exhausted. Work is in `stash@{0}` (`delegate-failed:<task_id>`); `failure_tail` has the last test output | 1 |
| `error` | Credential, model-name or config problem. The script doesn't retry | 1 |
| `blocked` | Precondition failed (dirty tree, detached HEAD, prompt too large) | 2 |

`security_sensitive` is set when a changed path matches patterns like
`auth|token|secret|crypt|sandbox|.env|…` or an added line matches
`shell=True|eval(|exec(|pickle.load|verify=False|hard-coded secret|…`. When it's
set, Claude reads the full diff. Both pattern lists can be configured.

## Logs and rollback

- `.delegate/log.jsonl` gets one `"kind":"call"` line per Reasonix call (model,
  tokens, cost, pass/fail, fail reason) and one `"kind":"task"` line per task.
- `.delegate/runs/<task_id>/attempt-N.json` holds the full prompt, Reasonix's
  result object, stderr, and verify output for debugging. Claude never needs to
  read these.
- `.delegate/` contains its own `.gitignore` (`*`), so nothing in it is ever committed.
- One commit per task. Undo one with `git revert <sha>`, or drop the whole branch.
  To inspect a failed task: `git stash list`, then `git stash show -p stash@{N}`.

## Cost check example

After a session:

```console
$ python3 scripts/delegate.py stats --since 24h
DeepSeek delegation — 6 calls, 4 tasks (since 24h)
model   calls  pass       input      cached     output  cost
flash       5     3     281,052     238,900     20,850  $0.0227
pro         1     1      83,310      70,200      6,020  $0.0311
total       6           364,362     309,100     26,870  $0.0538
tasks: 4/4 succeeded (3 first try, 1 escalated to pro), 0 needed you, 0 errors
```

To see what the same tokens would cost at another model's list price (per 1M
input,output tokens, same currency), pass `--compare`:

```console
$ python3 scripts/delegate.py stats --since 24h --compare 3,15
...
same tokens at 3.0/15.0 per 1M in/out would cost ≈ 1.4961
```

This is a rough comparison. Another model would use a different number of
tokens, and `--compare` bills cached input at the full input rate. It's still a
reasonable order-of-magnitude check. Use `stats --json` for scripts, or query
the log directly:

```bash
# cost per task, most expensive first
jq -s '[.[] | select(.kind=="task")] | sort_by(-(.cost|to_entries|map(.value)|add // 0))
       | .[] | {title, attempts, cost}' .delegate/log.jsonl
```

Costs come from Reasonix's own pricing table (`total_cost` + `currency`, which
may be `CNY`; set `[billing].display_currency` in Reasonix to change it). If
Reasonix reports no cost, the script estimates it from `prices` in
`.delegate/config.json`, when set.

## Configuration

`.delegate/config.json` (all keys are optional; the defaults are shown):

```json
{
  "models": {"flash": "deepseek-flash", "pro": "deepseek-pro"},
  "attempts_per_model": 2,
  "max_steps": 50,
  "timeout_seconds": 1800,
  "verify_timeout_seconds": 900,
  "permission_mode": "workspace-write",
  "protected_branches": ["main", "master"],
  "branch_prefix": "delegate/",
  "default_verify": ["python3 -m unittest discover -s tests -q"],
  "summary_max_lines": 5,
  "prices": {"flash": {"input": 0.0, "cache_hit": 0.0, "output": 0.0, "currency": "USD"}},
  "security_path_patterns": ["..."],
  "security_diff_patterns": ["..."]
}
```

Environment overrides: `DELEGATE_REASONIX_BIN`, `DELEGATE_FLASH_MODEL`,
`DELEGATE_PRO_MODEL`.

**Model names.** `deepseek-flash` and `deepseek-pro` are Reasonix's built-in
DeepSeek presets, and `deepseek-v4-pro` also resolves out of the box. If your
`reasonix.toml` defines its own `[[providers]]`, the built-in presets go away,
so point `models` at your provider names. `reasonix -p --model X --output-format json hi`
tells you whether a name resolves.

## How it relates to Reasonix

- **Headless:** `reasonix run … --output-format json` reads the task from stdin,
  runs it and exits with a `{"type":"result", usage, total_cost, …}` object.
  That's the only integration this plugin uses. Specs can be up to 512 KiB, and
  the task text never touches argv or a shell.
- **Sandbox:** `workspace-write` (Reasonix's default) lets DeepSeek write inside
  the repo and run builds and tests in its OS sandbox without approval prompts.
  `read-only` fails closed on any write.
- **MCP:** Reasonix itself is only an MCP *client* (`reasonix acp` speaks ACP,
  not MCP). The sibling plugin [`reasonix-orchestrator`](../reasonix-orchestrator/)
  adds a small stdio MCP server (`probe`, `delegate_read`, `delegate_write`,
  with optional SSH hosts). The skill uses those tools only for read-only work
  on remote hosts. Code changes always go through `delegate.py`, because it adds
  verification, per-task commits, cost logging and escalation, which the MCP
  tools don't.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `error: … missing env DEEPSEEK_API_KEY` | Export the key, or run `reasonix setup` |
| `error: … unknown model "…"` | Your `reasonix.toml` replaced the presets; set `models` in `.delegate/config.json` |
| `blocked: working tree has uncommitted changes` | Commit or stash your own work first, so a delegated commit never mixes in your edits |
| Build artifacts showing up in commits | Add them to `.gitignore`. The wrapper commits everything that isn't ignored |
| A run takes too long | Lower `max_steps` / `timeout_seconds`, or split the task |

## Tests

```bash
python3 -m unittest discover -s plugins/deepseek-delegate/tests -v
```

The tests cover the success path, the Flash→Pro escalation, stash on
exhaustion, fatal credential and unknown-model errors, the dirty-tree block,
security flagging, fenced-JSON parsing, read-only mode, scope checks, the
price fallback, and stats.
