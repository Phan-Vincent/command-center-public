#!/usr/bin/env python3
"""
delegate.py — hand a coding task to DeepSeek (via the Reasonix CLI), verify it,
commit it, and print ONE compact JSON line for Claude to read.

Claude plans and reviews; DeepSeek writes the code. The only thing Claude has
to read per task is the JSON this script prints on stdout, so every token of
implementation work lands on DeepSeek's bill instead of Claude's.

Subcommands
  run     delegate one task (Flash x2 -> Pro x2 escalation ladder)
  stats   token / cost / pass-rate summary of .delegate/log.jsonl
  doctor  preflight checks (reasonix, API key, git state)
  init    create .delegate/ with a config.json template

Pure Python 3 standard library. See ../README.md.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

STATE_DIR = ".delegate"

DEFAULTS = {
    "reasonix_bin": "reasonix",
    # Reasonix model references (a configured provider name or "provider/model").
    # deepseek-flash / deepseek-pro are Reasonix's built-in DeepSeek presets.
    "models": {"flash": "deepseek-flash", "pro": "deepseek-pro"},
    "attempts_per_model": 2,
    "max_steps": 50,                 # Reasonix tool-call round budget; 0 = Reasonix auto
    "timeout_seconds": 1800,         # per Reasonix call
    "verify_timeout_seconds": 900,   # per verify command
    "permission_mode": "workspace-write",
    "protected_branches": ["main", "master"],
    "branch_prefix": "delegate/",
    "default_verify": [],            # used when a run passes no --verify
    "summary_max_lines": 5,
    "max_prompt_bytes": 512 * 1024,
    # Changed paths matching any of these => security_sensitive (Claude reads the diff).
    "security_path_patterns": [
        r"auth", r"login", r"passw", r"secret", r"token", r"crypt", r"cipher",
        r"jwt", r"oauth", r"session", r"permission", r"acl", r"rbac", r"sudo",
        r"ssh", r"cert", r"tls", r"ssl", r"sandbox", r"sanitiz", r"csrf",
        r"(^|/)\.env", r"allowlist", r"policy", r"security",
    ],
    # Added diff lines matching any of these => security_sensitive.
    "security_diff_patterns": [
        r"\beval\(", r"\bexec\(", r"shell\s*=\s*True", r"os\.system\(",
        r"subprocess\.", r"pickle\.loads?", r"yaml\.load\(", r"verify\s*=\s*False",
        r"chmod\s+777", r"\bsudo\b", r"innerHTML\s*=", r"dangerouslySetInnerHTML",
        r"(api[_-]?key|secret|password|token)\s*[:=]\s*['\"][^'\"]{6,}",
    ],
    # Fallback per-1M-token prices when Reasonix reports no cost, e.g.
    # {"flash": {"input": 0.0, "cache_hit": 0.0, "output": 0.0, "currency": "USD"}}
    "prices": {},
}

CONTRACT_KEYS = ("status", "files_changed", "summary", "test_result", "open_questions")

WORKER_RULES = """\
You are an implementation worker. A reviewer planned this task and will check
your work with automated tests and linters, so be precise and minimal.

RULES
- Make the changes directly on disk in this repository. Stay inside the task's scope.
- Do NOT run git commit, checkout, reset, stash, rebase, merge or push. The caller commits.
- Do not touch anything under .delegate/.
- If verification commands are listed, run them before finishing and fix failures you caused.
- If something is ambiguous, make the most conservative reasonable choice and list the
  question in open_questions instead of guessing big.
"""

READ_ONLY_RULES = """\
You are a research worker. Do NOT modify any files. Read and search the repository
to answer the task below as precisely as possible.
"""

FINAL_MESSAGE_CONTRACT = """\
FINAL MESSAGE
Your final message must be ONLY this JSON object: no prose, no code fences.
{{"status": "done" | "partial" | "failed",
 "files_changed": ["relative/path", ...],
 "summary": "at most {n} short lines, newline-separated",
 "test_result": "the command(s) you ran and pass/fail, or \\"not_run\\"",
 "open_questions": ["...", ...]}}
"""


# ---------------------------------------------------------------- utilities

def now_iso():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def slugify(text, limit=40):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:limit].rstrip("-")) or "task"


def tail(text, n):
    lines = (text or "").rstrip().splitlines()
    return "\n".join(lines[-n:])


def emit(obj, code):
    print(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    sys.exit(code)


class DelegateError(Exception):
    """A problem that should end the run with a structured error."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def run_proc(cmd, cwd, timeout, shell=False, env=None, stdin_text=None):
    """Run a command in its own process group; kill the whole group on timeout.

    Returns (returncode, stdout, stderr, timed_out)."""
    proc = subprocess.Popen(
        cmd, cwd=cwd, shell=shell, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL if stdin_text is None else subprocess.PIPE,
        start_new_session=True)
    try:
        out, err = proc.communicate(input=stdin_text, timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()
        return -9, out, err, True


# ---------------------------------------------------------------- config

def load_config(root):
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    path = root / STATE_DIR / "config.json"
    if path.exists():
        try:
            user = json.loads(path.read_text())
        except ValueError as e:
            raise DelegateError("error", f"{path}: invalid JSON ({e})")
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    env = os.environ
    if env.get("DELEGATE_REASONIX_BIN"):
        cfg["reasonix_bin"] = env["DELEGATE_REASONIX_BIN"]
    if env.get("DELEGATE_FLASH_MODEL"):
        cfg["models"]["flash"] = env["DELEGATE_FLASH_MODEL"]
    if env.get("DELEGATE_PRO_MODEL"):
        cfg["models"]["pro"] = env["DELEGATE_PRO_MODEL"]
    return cfg


def ensure_state_dir(root):
    d = root / STATE_DIR
    (d / "runs").mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        # Keep logs, run transcripts and personal config out of every commit.
        gi.write_text("# delegate.py state: never committed (git add -f to override)\n*\n")
    return d


# ---------------------------------------------------------------- git

class Git:
    def __init__(self, root):
        self.root = root

    def __call__(self, *args, check=True):
        r = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise DelegateError(
                "error", f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
        return r.stdout.strip()

    def dirty_paths(self):
        r = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                           cwd=self.root, capture_output=True, text=True, check=True)
        entries = r.stdout.split("\0")
        paths, i = [], 0
        while i < len(entries):
            entry = entries[i]
            i += 1
            if len(entry) < 4:
                continue
            if entry[0] in "RC":  # rename/copy: the next entry is the source path
                i += 1
            p = entry[3:]
            if not p.startswith(STATE_DIR + "/"):
                paths.append(p)
        return paths

    def has_staged(self):
        return subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=self.root).returncode != 0

    def branch(self):
        return self("rev-parse", "--abbrev-ref", "HEAD")

    def head(self):
        return self("rev-parse", "HEAD")


def repo_root():
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return Path(r.stdout.strip())


# ---------------------------------------------------------------- reasonix I/O

def parse_envelope(stdout):
    """Find Reasonix's final {"type":"result",...} object."""
    text = (stdout or "").strip()
    candidates = [text] + list(reversed(text.splitlines()))
    for c in candidates:
        c = c.strip()
        if not c.startswith("{"):
            continue
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return None


def parse_report(text):
    """Extract the worker's contract JSON from its final message."""
    if not text:
        return None
    t = text.strip()
    candidates = []
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.S):
        candidates.append(m.group(1))
    candidates.append(t)
    i, j = t.find("{"), t.rfind("}")
    if i != -1 and j > i:
        candidates.append(t[i:j + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
        except ValueError:
            continue
        if isinstance(obj, dict) and ("status" in obj or "summary" in obj):
            return obj
    return None


def is_fatal_reasonix_error(env):
    """Config/credential problems: retrying or escalating cannot fix them."""
    code = str(env.get("error_code") or env.get("authentication_status") or "")
    if any(k in code for k in ("credential", "auth", "select_model", "unknown_model")):
        return True
    text = str(env.get("result") or "").lower()
    return any(k in text for k in ("unknown model", "missing env", "invalid api key",
                                   "insufficient balance", "unauthorized", "payment required"))


def extract_cost(env, alias, usage, cfg):
    cost = env.get("total_cost")
    currency = env.get("currency")
    if cost is None and env.get("total_cost_usd") is not None:
        cost = env["total_cost_usd"]
    if cost is not None:
        return float(cost), currency or "USD", "reasonix"
    price = cfg.get("prices", {}).get(alias)
    if not price:
        return None, None, None
    cache = usage.get("cache_read_input_tokens", 0)
    fresh = max(usage.get("input_tokens", 0) - cache, 0)
    est = (fresh * price.get("input", 0) + cache * price.get("cache_hit", price.get("input", 0))
           + usage.get("output_tokens", 0) * price.get("output", 0)) / 1_000_000
    return round(est, 6), price.get("currency", "USD"), "config_prices"


def build_prompt(task, title, files, verify, read_only, feedback, cfg):
    parts = [READ_ONLY_RULES if read_only else WORKER_RULES, f"TASK: {title}", task.strip()]
    if files:
        parts.append("FILES IN SCOPE (touch others only if unavoidable):\n"
                     + "\n".join(f"- {f}" for f in files))
    if verify and not read_only:
        parts.append("VERIFICATION (the reviewer will run exactly these; they must pass):\n"
                     + "\n".join(f"$ {v}" for v in verify))
    if feedback:
        parts.append(feedback)
    parts.append(FINAL_MESSAGE_CONTRACT.format(n=cfg["summary_max_lines"]))
    prompt = "\n\n".join(parts)
    size, cap = len(prompt.encode()), int(cfg["max_prompt_bytes"])
    if size > cap:
        raise DelegateError("blocked", f"prompt is {size} bytes (> {cap}); split the task")
    return prompt


def call_reasonix(cfg, root, alias, prompt, read_only):
    # `reasonix run` reads the task from stdin, so task text never touches argv or a shell.
    cmd = [cfg["reasonix_bin"], "run", "--model", cfg["models"][alias],
           "--permission-mode", "read-only" if read_only else cfg["permission_mode"],
           "--output-format", "json", "--dir", str(root)]
    if int(cfg.get("max_steps") or 0) > 0:
        cmd += ["--max-steps", str(cfg["max_steps"])]
    t0 = time.monotonic()
    try:
        rc, out, err, timed_out = run_proc(cmd, root, cfg["timeout_seconds"], stdin_text=prompt)
    except OSError as e:
        raise DelegateError(
            "error", f"cannot run reasonix ({cfg['reasonix_bin']}): {e}; npm i -g reasonix")
    return {"rc": rc, "stdout": out, "stderr": err, "timed_out": timed_out,
            "duration_ms": int((time.monotonic() - t0) * 1000), "cmd": cmd}


def run_verify(commands, root, timeout):
    """Run each verify command; stop at the first failure."""
    results, ok = [], True
    for c in commands:
        rc, out, err, timed_out = run_proc(c, root, timeout, shell=True)
        combined = (out or "") + (("\n" + err) if err else "")
        results.append({"cmd": c, "rc": rc, "timed_out": timed_out, "output": combined})
        if rc != 0:
            ok = False
            break
    return ok, results


def verify_summary(ok, results):
    if not results:
        return "not_run (no verify command)"
    parts = []
    for r in results:
        state = "timeout" if r["timed_out"] else ("pass" if r["rc"] == 0 else f"FAIL rc={r['rc']}")
        parts.append(f"{r['cmd']}: {state}")
    return "; ".join(parts)


# ---------------------------------------------------------------- security scan

def security_scan(git, base, cfg, paths):
    reasons = []
    path_res = [re.compile(p, re.I) for p in cfg["security_path_patterns"]]
    for p in paths:
        if any(r.search(p) for r in path_res):
            reasons.append(f"path:{p}")
    diff = git("diff", "--cached", "-U0", base, check=False)
    diff_res = [re.compile(p, re.I) for p in cfg["security_diff_patterns"]]
    current = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not line.startswith("+++"):
            for r in diff_res:
                if r.search(line):
                    reasons.append(f"diff:{current}:{r.pattern}")
                    break
    # de-dupe, keep order, cap
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out[:10]


# ---------------------------------------------------------------- logging

def log_line(state_dir, record):
    with open(state_dir / "log.jsonl", "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def save_attempt(state_dir, task_id, n, payload):
    d = state_dir / "runs" / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"attempt-{n}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- run

def ladder(start, attempts_per_model, escalate):
    order = ["flash", "pro"] if start == "flash" else ["pro"]
    if not escalate:
        order = [start]
    return [m for m in order for _ in range(attempts_per_model)]


def read_task(args):
    if args.task_file:
        if args.task_file == "-":
            return sys.stdin.read()
        return Path(args.task_file).read_text()
    if args.task:
        return args.task
    raise DelegateError("blocked", "no task given (use --task or --task-file)")


def cmd_run(args):
    root = repo_root()
    if root is None:
        raise DelegateError("blocked", "not inside a git repository")
    cfg = load_config(root)
    if args.attempts:
        cfg["attempts_per_model"] = args.attempts
    if args.max_steps is not None:
        cfg["max_steps"] = args.max_steps
    if args.timeout:
        cfg["timeout_seconds"] = args.timeout
    git = Git(root)
    state_dir = ensure_state_dir(root)

    task = read_task(args)
    if not task.strip():
        raise DelegateError("blocked", "task text is empty")
    title = (args.title or task.strip().splitlines()[0])[:72]
    files = [f.strip() for f in (args.files or "").split(",") if f.strip()]
    verify = args.verify or list(cfg.get("default_verify") or [])
    task_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    # --- guardrail: clean tree, feature branch
    if git("rev-parse", "--verify", "HEAD", check=False) == "":
        raise DelegateError("blocked", "repository has no commits yet; make an initial commit")
    dirty = git.dirty_paths()
    if dirty and not args.read_only:
        raise DelegateError(
            "blocked",
            "working tree has uncommitted changes (commit or stash them first): "
            + ", ".join(dirty[:8]) + (" ..." if len(dirty) > 8 else ""))
    branch = git.branch()
    if branch == "HEAD":
        raise DelegateError("blocked", "detached HEAD; check out a branch first")
    if args.branch and args.branch != branch:
        exists = git("rev-parse", "--verify", "--quiet", "refs/heads/" + args.branch, check=False)
        if exists:
            git("checkout", args.branch)
        else:
            git("checkout", "-b", args.branch)
        branch = args.branch
    elif branch in cfg["protected_branches"] and not args.read_only:
        branch = f"{cfg['branch_prefix']}{slugify(title)}-{task_id[-4:]}"
        git("checkout", "-b", branch)
    base = git.head()

    models = ladder(args.model, int(cfg["attempts_per_model"]), not args.no_escalate)
    feedback = None
    totals = {}   # currency -> cost
    attempts_log = []
    last = {}

    for n, alias in enumerate(models, start=1):
        prompt = build_prompt(task, title, files, verify, args.read_only, feedback, cfg)
        call = call_reasonix(cfg, root, alias, prompt, args.read_only)
        env = parse_envelope(call["stdout"]) or {}
        usage = env.get("usage") or {}
        cost, currency, cost_source = extract_cost(env, alias, usage, cfg)
        if cost is not None:
            totals[currency] = round(totals.get(currency, 0.0) + cost, 6)
        report = parse_report(env.get("result"))

        fail_reason, fatal = None, False
        verify_ok, verify_results = None, []
        if call["timed_out"]:
            fail_reason = f"reasonix timed out after {cfg['timeout_seconds']}s"
        elif not env:
            fail_reason = "reasonix produced no result object: " + tail(
                call["stderr"] or call["stdout"], 5)
        elif env.get("is_error"):
            fail_reason = "reasonix error: " + str(env.get("result", ""))[:300]
            fatal = is_fatal_reasonix_error(env)
        elif report and str(report.get("status", "")).lower() == "failed":
            fail_reason = "worker reported status=failed"
        if fail_reason is None and not args.read_only:
            verify_ok, verify_results = run_verify(verify, root, cfg["verify_timeout_seconds"])
            if not verify_ok:
                fail_reason = "verification failed"

        passed = fail_reason is None
        record = {
            "kind": "call", "ts": now_iso(), "task_id": task_id, "title": title,
            "attempt": n, "model": alias, "model_id": cfg["models"][alias],
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cost": cost, "currency": currency, "cost_source": cost_source,
            "duration_ms": env.get("duration_ms", call["duration_ms"]),
            "pass": passed, "fail_reason": fail_reason, "read_only": bool(args.read_only),
        }
        log_line(state_dir, record)
        save_attempt(state_dir, task_id, n, {
            "record": record, "cmd": call["cmd"], "prompt": prompt, "envelope": env,
            "stderr_tail": tail(call["stderr"], 60),
            "verify": [{**r, "output": tail(r["output"], 200)} for r in verify_results],
        })
        attempts_log.append(f"{alias}:{'pass' if passed else 'fail'}")
        last = {"alias": alias, "env": env, "report": report, "verify_ok": verify_ok,
                "verify_results": verify_results, "fail_reason": fail_reason}

        if passed:
            break
        if fatal:
            break
        # Feed the failure back so the next attempt fixes rather than restarts.
        failed_out = verify_results[-1]["output"] if verify_results else ""
        feedback = (
            f"PREVIOUS ATTEMPT #{n} ({alias}) FAILED: {fail_reason}.\n"
            "Any changes it made are still on disk; fix them rather than starting over.")
        if report and report.get("summary"):
            feedback += "\nPrevious attempt's own summary:\n" + tail(str(report["summary"]), 5)
        if failed_out:
            feedback += (f"\nOutput of `{verify_results[-1]['cmd']}` (tail):\n"
                         + tail(failed_out, 40))

    return finish(args, cfg, git, state_dir, task_id, title, branch, base, files,
                  attempts_log, totals, last)


def finish(args, cfg, git, state_dir, task_id, title, branch, base, files,
           attempts_log, totals, last):
    report = last.get("report") or {}
    passed = last.get("fail_reason") is None
    summary_lines = str(report.get("summary") or "").strip().splitlines()
    if not report:
        summary_lines = tail(str(last.get("env", {}).get("result", "")), 5).splitlines()
    summary = "\n".join(summary_lines[: int(cfg["summary_max_lines"])])

    out = {
        "status": None, "task_id": task_id, "title": title,
        "model": last.get("alias"), "attempts": attempts_log,
        "branch": branch, "base": base[:12], "commit": None,
        "files_changed": [], "diff_stat": "",
        "summary": summary,
        "test_result": verify_summary(last.get("verify_ok"), last.get("verify_results") or []),
        "open_questions": list(report.get("open_questions") or []),
        "security_sensitive": False, "security_reasons": [],
        "cost": totals,
    }
    if not report and last.get("env"):
        out["report_parse_error"] = True

    if args.read_only:
        out["status"] = "success" if passed else "needs_human"
        if not passed:
            out["error"] = last.get("fail_reason")
        stray = git.dirty_paths()
        if stray:
            out["warning"] = "read-only task left changes on disk: " + ", ".join(stray[:8])
    elif passed:
        git("add", "-A")
        changed = [p for p in git("diff", "--cached", "--name-only", base).splitlines() if p]
        out["files_changed"] = changed
        out["security_reasons"] = security_scan(git, base, cfg, changed)
        out["security_sensitive"] = bool(out["security_reasons"])
        if files:
            scope = set(files)
            extra = [p for p in changed if p not in scope]
            if extra:
                out["out_of_scope"] = extra
        if git.has_staged():
            msg = (f"delegate({last['alias']}): {title}\n\n{summary}\n\n"
                   f"Delegate-Task: {task_id}\n"
                   f"Delegate-Model: {cfg['models'][last['alias']]}\n"
                   f"Delegate-Attempts: {', '.join(attempts_log)}\n")
            git("commit", "-q", "-m", msg)
        head = git.head()
        if head != base:
            out["commit"] = head[:12]
            stat = git("diff", "--stat", base, head).splitlines()
            out["diff_stat"] = stat[-1].strip() if stat else ""
        else:
            out["warning"] = "worker reported success but changed no files"
        out["status"] = "success"
    else:
        out["error"] = last.get("fail_reason")
        vr = last.get("verify_results") or []
        if vr and vr[-1]["rc"] != 0:
            out["failure_tail"] = tail(vr[-1]["output"], 15)
        if git.head() != base:
            out["warning"] = (f"worker made its own commits ({base[:12]}..HEAD); "
                              "they are left on the branch for review")
        leftovers = git.dirty_paths()
        out["files_changed"] = leftovers
        if leftovers:
            git("stash", "push", "-u", "-q", "-m", f"delegate-failed:{task_id} {title}")
            out["stash"] = "stash@{0}"
            out["stash_message"] = f"delegate-failed:{task_id}"
        env = last.get("env") or {}
        out["status"] = "error" if (env.get("is_error") and is_fatal_reasonix_error(env)) \
            else "needs_human"

    log_line(state_dir, {
        "kind": "task", "ts": now_iso(), "task_id": task_id, "title": title,
        "status": out["status"], "attempts": attempts_log, "final_model": last.get("alias"),
        "commit": out["commit"], "files_changed": len(out["files_changed"]),
        "cost": totals, "security_sensitive": out["security_sensitive"],
    })
    return out


# ---------------------------------------------------------------- stats

def parse_since(s):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([hd])", s)
    if m:
        delta = dt.timedelta(hours=int(m.group(1))) if m.group(2) == "h" \
            else dt.timedelta(days=int(m.group(1)))
        return dt.datetime.now(dt.timezone.utc) - delta
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.astimezone()


def fmt_cost(by_cur):
    if not by_cur:
        return "n/a"
    sym = {"USD": "$", "CNY": "¥"}
    return " + ".join(f"{sym.get(c, (c or '') + ' ')}{v:.4f}" for c, v in sorted(by_cur.items()))


def cmd_stats(args):
    root = repo_root() or Path.cwd()
    log = Path(args.log) if args.log else root / STATE_DIR / "log.jsonl"
    if not log.exists():
        raise DelegateError("blocked", f"no log at {log}")
    since = parse_since(args.since)
    calls, tasks = [], []
    for line in log.read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if since and dt.datetime.fromisoformat(r["ts"]) < since:
            continue
        (tasks if r.get("kind") == "task" else calls).append(r)

    by_model = {}
    for r in calls:
        m = by_model.setdefault(r["model"], {"calls": 0, "pass": 0, "input_tokens": 0,
                                             "cache_read_tokens": 0, "output_tokens": 0,
                                             "cost": {}})
        m["calls"] += 1
        m["pass"] += bool(r.get("pass"))
        for k in ("input_tokens", "cache_read_tokens", "output_tokens"):
            m[k] += int(r.get(k) or 0)
        if r.get("cost") is not None:
            cur = r.get("currency") or "USD"
            m["cost"][cur] = round(m["cost"].get(cur, 0) + r["cost"], 6)

    total = {"calls": len(calls), "input_tokens": 0, "cache_read_tokens": 0,
             "output_tokens": 0, "cost": {}}
    for m in by_model.values():
        for k in ("input_tokens", "cache_read_tokens", "output_tokens"):
            total[k] += m[k]
        for c, v in m["cost"].items():
            total["cost"][c] = round(total["cost"].get(c, 0) + v, 6)

    t_stats = {"tasks": len(tasks),
               "success": sum(t["status"] == "success" for t in tasks),
               "first_try": sum(t["status"] == "success" and len(t["attempts"]) == 1
                                for t in tasks),
               "escalated_to_pro": sum(any(a.startswith("pro") for a in t["attempts"])
                                       and t["attempts"][0].startswith("flash") for t in tasks),
               "needs_human": sum(t["status"] == "needs_human" for t in tasks),
               "errors": sum(t["status"] == "error" for t in tasks)}

    compare = None
    if args.compare:
        rin, rout = (float(x) for x in args.compare.split(","))
        compare = {"rate_in_per_mtok": rin, "rate_out_per_mtok": rout,
                   "cost": round((total["input_tokens"] * rin
                                  + total["output_tokens"] * rout) / 1_000_000, 4)}

    result = {"since": args.since, "by_model": by_model, "total": total,
              "tasks": t_stats, "compare": compare}
    if args.json:
        print(json.dumps(result, indent=2))
        return
    hdr = f"{'model':<7}{'calls':>6}{'pass':>6}{'input':>12}{'cached':>12}{'output':>11}  cost"
    print(f"DeepSeek delegation — {len(calls)} calls, {len(tasks)} tasks"
          + (f" (since {args.since})" if args.since else ""))
    print(hdr)
    for name, m in sorted(by_model.items()):
        print(f"{name:<7}{m['calls']:>6}{m['pass']:>6}{m['input_tokens']:>12,}"
              f"{m['cache_read_tokens']:>12,}{m['output_tokens']:>11,}  {fmt_cost(m['cost'])}")
    print(f"{'total':<7}{total['calls']:>6}{'':>6}{total['input_tokens']:>12,}"
          f"{total['cache_read_tokens']:>12,}{total['output_tokens']:>11,}  "
          f"{fmt_cost(total['cost'])}")
    print(f"tasks: {t_stats['success']}/{t_stats['tasks']} succeeded "
          f"({t_stats['first_try']} first try, {t_stats['escalated_to_pro']} escalated to pro), "
          f"{t_stats['needs_human']} needed you, {t_stats['errors']} errors")
    if compare:
        print(f"same tokens at {rin}/{rout} per 1M in/out would cost ≈ {compare['cost']:.4f}")


# ---------------------------------------------------------------- doctor / init

def cmd_doctor(args):
    checks = {}
    root = repo_root()
    cfg = load_config(root) if root else json.loads(json.dumps(DEFAULTS))
    binp = shutil.which(cfg["reasonix_bin"]) or (
        cfg["reasonix_bin"] if Path(cfg["reasonix_bin"]).is_file() else None)
    checks["reasonix"] = binp or "MISSING — npm i -g reasonix"
    if binp:
        rc, out, err, _ = run_proc([binp, "--version"], None, 30)
        checks["reasonix_version"] = (out or err).strip()
    checks["deepseek_api_key"] = "set" if os.environ.get("DEEPSEEK_API_KEY") else \
        "not in env (ok only if saved via `reasonix setup`)"
    checks["models"] = cfg["models"]
    if root:
        g = Git(root)
        checks["repo"] = str(root)
        checks["branch"] = g.branch()
        dirty = g.dirty_paths()
        checks["clean_tree"] = not dirty
        checks["on_protected_branch"] = checks["branch"] in cfg["protected_branches"]
    else:
        checks["repo"] = "NOT a git repository"
    ok = bool(binp) and bool(root)
    emit({"ok": ok, **checks}, 0 if ok else 1)


def cmd_init(args):
    root = repo_root()
    if root is None:
        raise DelegateError("blocked", "not inside a git repository")
    d = ensure_state_dir(root)
    cfgp = d / "config.json"
    if not cfgp.exists():
        template = {
            "_comment": "Overrides for delegate.py; see plugins/deepseek-delegate/README.md",
            "models": DEFAULTS["models"],
            "attempts_per_model": DEFAULTS["attempts_per_model"],
            "max_steps": DEFAULTS["max_steps"],
            "default_verify": [],
            "protected_branches": DEFAULTS["protected_branches"],
        }
        cfgp.write_text(json.dumps(template, indent=2) + "\n")
    emit({"ok": True, "state_dir": str(d), "config": str(cfgp)}, 0)


# ---------------------------------------------------------------- main

def main(argv=None):
    p = argparse.ArgumentParser(prog="delegate.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="delegate one task")
    r.add_argument("--title", help="short commit title (defaults to the task's first line)")
    r.add_argument("--task", help="task text")
    r.add_argument("--task-file", help="file with the task text, or - for stdin")
    r.add_argument("--model", choices=["flash", "pro"], default="flash")
    r.add_argument("--verify", action="append",
                   help="command that must exit 0 (repeatable); run by this script")
    r.add_argument("--files", help="comma-separated files in scope")
    r.add_argument("--read-only", action="store_true",
                   help="search/summarize task: no writes, no commit, no verify")
    r.add_argument("--no-escalate", action="store_true", help="never move from flash to pro")
    r.add_argument("--branch", help="work on this branch (created if missing)")
    r.add_argument("--attempts", type=int, help="attempts per model (default 2)")
    r.add_argument("--max-steps", type=int, help="Reasonix tool-round budget")
    r.add_argument("--timeout", type=int, help="seconds per Reasonix call")

    s = sub.add_parser("stats", help="summarize .delegate/log.jsonl")
    s.add_argument("--since", help="ISO date/time, or 24h / 7d")
    s.add_argument("--log", help="path to a log.jsonl (default: this repo's)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--compare", metavar="IN,OUT",
                   help="price per 1M input,output tokens to compare against")

    sub.add_parser("doctor", help="preflight checks")
    sub.add_parser("init", help="create .delegate/config.json")

    args = p.parse_args(argv)
    try:
        if args.cmd == "run":
            out = cmd_run(args)
            emit(out, 0 if out["status"] == "success" else 1)
        elif args.cmd == "stats":
            cmd_stats(args)
        elif args.cmd == "doctor":
            cmd_doctor(args)
        elif args.cmd == "init":
            cmd_init(args)
    except DelegateError as e:
        emit({"status": e.status, "error": str(e)}, 2 if e.status == "blocked" else 1)


if __name__ == "__main__":
    main()
