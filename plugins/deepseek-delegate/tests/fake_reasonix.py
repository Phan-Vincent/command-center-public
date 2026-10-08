#!/usr/bin/env python3
"""
Stand-in for the `reasonix` CLI used by test_delegate.py.

Plays back one scripted step per invocation from $FAKE_REASONIX_PLAN (a JSON
list), writes the step's files under --dir, records each call to
$FAKE_REASONIX_CALLS, and prints a Reasonix-shaped `--output-format json`
result object. Step keys:

  write       {path: content} to write before answering
  report      dict -> JSON final message (the worker contract)
  raw         str  -> final message verbatim (overrides report)
  is_error    bool; error_code str
  usage       dict (defaults to small token counts)
  cost        float; currency str (omit cost to simulate "no cost reported")
"""

import json
import os
import sys
from pathlib import Path


def main():
    args = sys.argv[1:]
    if args[:1] in (["--version"], ["-v"]):
        print("reasonix v0.0.0-fake")
        return 0
    opts, prompt, i = {}, None, 0
    while i < len(args):
        a = args[i]
        if a in ("-p", "run"):
            i += 1
        elif a.startswith("--"):
            opts[a[2:]] = args[i + 1]
            i += 2
        else:
            prompt = a
            i += 1
    if prompt is None:
        prompt = sys.stdin.read()

    plan_path = Path(os.environ["FAKE_REASONIX_PLAN"])
    counter = plan_path.with_suffix(".count")
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    plan = json.loads(plan_path.read_text())
    step = plan[min(n, len(plan) - 1)]

    with open(os.environ["FAKE_REASONIX_CALLS"], "a") as f:
        f.write(json.dumps({"n": n, "opts": opts, "prompt": prompt}) + "\n")

    root = Path(opts.get("dir", "."))
    for rel, content in (step.get("write") or {}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    result = step.get("raw")
    if result is None:
        result = json.dumps(step.get("report") or {
            "status": "done", "files_changed": list((step.get("write") or {}).keys()),
            "summary": "did the thing", "test_result": "not_run", "open_questions": []})
    env = {
        "type": "result",
        "subtype": "error_during_execution" if step.get("is_error") else "success",
        "is_error": bool(step.get("is_error")),
        "duration_ms": 42, "num_turns": 1, "result": result, "session_id": f"s{n}",
        "usage": step.get("usage") or {"input_tokens": 1000, "output_tokens": 200,
                                       "cache_read_input_tokens": 600,
                                       "cache_creation_input_tokens": 0},
    }
    if "cost" in step:
        env.update(total_cost=step["cost"], currency=step.get("currency", "USD"),
                   total_cost_usd=step["cost"])
    if step.get("error_code"):
        env["error_code"] = step["error_code"]
    print(json.dumps(env))
    return 1 if step.get("is_error") else 0


if __name__ == "__main__":
    sys.exit(main())
