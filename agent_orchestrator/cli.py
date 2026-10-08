"""Command-line interface for the lightweight orchestrator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

from .models import OrchestratorError, PRIORITIES, STATUSES, WORKERS
from .store import TaskStore
from .supervisor import Supervisor


REQUIREMENT_FLAGS = {
    "repo_read": "requires_repo_read",
    "repo_write": "requires_repo_write",
    "terminal": "requires_terminal",
    "web_research": "requires_web",
    "current_information": "requires_current",
    "human_account": "requires_human_account",
    "external_files": "requires_external_files",
}


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _display_path(path: str) -> str:
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(resolved)


def _print_handoff(info: dict[str, Any], *, copied: bool = False) -> None:
    print(f"Task: {info['task_id']}")
    print(f"Delegated to: {info['worker']}")
    print(f"Status: {info['status']}")
    print(f"Objective: {info['objective']}")
    print(f"Prompt: {_display_path(info['prompt_path'])}")
    if copied:
        print("Clipboard: prompt copied with pbcopy")
    print("\nNext:")
    print("Copy the prompt into your external ChatGPT account.")
    print("Then run:")
    print(f"\n{info['next_command']}")


def _copy_prompt(path: str) -> None:
    pbcopy = shutil.which("pbcopy")
    if pbcopy is None:
        raise OrchestratorError("pbcopy is unavailable; copy the prompt file manually")
    prompt = Path(path).read_text(encoding="utf-8")
    completed = subprocess.run([pbcopy], input=prompt, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        raise OrchestratorError(f"pbcopy failed: {completed.stderr.strip() or 'unknown error'}")


def _add_task_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("instructions")
    parser.add_argument("--project", default=str(Path.cwd()))
    parser.add_argument("--type", default="auto", dest="task_type")
    parser.add_argument("--priority", choices=PRIORITIES, default="normal")
    parser.add_argument("--worker", choices=WORKERS)
    parser.add_argument("--context", default="")
    parser.add_argument("--file", action="append", default=[], dest="files")
    parser.add_argument("--constraint", action="append", default=[], dest="constraints")
    parser.add_argument("--accept", action="append", default=[], dest="acceptance_criteria")
    parser.add_argument("--parent", dest="parent_task_id")
    parser.add_argument("--max-depth", type=int, default=3)
    for flag in REQUIREMENT_FLAGS.values():
        parser.add_argument(
            "--" + flag.replace("_", "-"),
            action=argparse.BooleanOptionalAction,
            default=None,
        )


def _requirement_overrides(args: argparse.Namespace) -> dict[str, bool]:
    return {
        requirement: getattr(args, destination)
        for requirement, destination in REQUIREMENT_FLAGS.items()
        if getattr(args, destination, None) is not None
    }


def _create_task(supervisor: Supervisor, args: argparse.Namespace) -> dict[str, Any]:
    return supervisor.create(
        args.instructions,
        project=str(Path(args.project).expanduser().resolve()),
        task_type=args.task_type,
        priority=args.priority,
        worker=args.worker,
        context=args.context,
        files=args.files,
        constraints=args.constraints,
        acceptance_criteria=args.acceptance_criteria,
        parent_task_id=args.parent_task_id,
        max_depth=args.max_depth,
        requirements=_requirement_overrides(args),
    )


def _print_route_explanation(decision: dict[str, Any]) -> None:
    print(f"Selected worker: {decision['worker']}")
    print("\nTask requirements:")
    for name, required in decision.get("requirements_snapshot", {}).items():
        print(f"  {name}: {str(required).lower()}")
    print("\nRouting factors:")
    for reason in decision.get("reasons", [decision.get("reason", "unspecified")]):
        print(f"  - {reason}")
    escalation = decision.get("potential_escalation", [])
    if escalation:
        print("\nPotential escalation:")
        for worker in escalation:
            print(f"  {worker}")


def _print_retry_summary(result: dict[str, Any]) -> None:
    histories = list(result.get("metadata", {}).get("escalation_history", []))
    histories.append({
        "worker": result.get("worker", "worker"),
        "execution_attempts": result.get("metadata", {}).get("execution_attempts", []),
    })
    for history in histories:
        attempts = history.get("execution_attempts", []) if isinstance(history, dict) else []
        if not isinstance(attempts, list) or len(attempts) <= 1:
            continue
        total = len(attempts)
        for attempt in attempts:
            error = attempt.get("error") if isinstance(attempt, dict) else None
            detail = attempt.get("status", "unknown")
            if isinstance(error, dict):
                detail = error.get("code") or error.get("category") or error.get("message") or detail
            print(
                f"{history.get('worker', 'worker')} attempt {attempt.get('attempt_number')}/{total}: {detail}",
                file=sys.stderr,
            )
        if history.get("escalation_reason"):
            print(f"Escalating: {history['escalation_reason']}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestrator", description="Filesystem-backed multi-agent task supervisor")
    parser.add_argument("--store", help="override task store (default: data/orchestrator or ORCHESTRATOR_HOME)")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="create a task")
    _add_task_arguments(create)

    ask = sub.add_parser("ask", help="create, route, and run a task")
    _add_task_arguments(ask)
    ask.add_argument("--explain", action="store_true", help="show the deterministic routing explanation")

    run = sub.add_parser("run", help="route and dispatch a pending task once")
    run.add_argument("task_id")
    run.add_argument("--worker", choices=WORKERS)
    run.add_argument("--explain", action="store_true", help="show the deterministic routing explanation")

    route = sub.add_parser("route", help="explain and record a routing decision without running it")
    route.add_argument("task_id")
    route.add_argument("--worker", choices=WORKERS)

    delegate = sub.add_parser("delegate", help="explicitly add one worker transition")
    delegate.add_argument("task_id")
    delegate.add_argument("--worker", choices=WORKERS, required=True)

    reroute = sub.add_parser("reroute", help="mark a routing override and run the selected worker")
    reroute.add_argument("task_id")
    reroute.add_argument("--worker", choices=WORKERS, required=True)
    reroute.add_argument("--reason", required=True)

    status = sub.add_parser("status", help="show a task")
    status.add_argument("task_id")
    result = sub.add_parser("result", help="show the latest normalized result")
    result.add_argument("task_id")
    context = sub.add_parser("context", help="show the latest inspectable context package")
    context.add_argument("task_id")
    handoff = sub.add_parser("handoff", help="show a external ChatGPT manual handoff")
    handoff.add_argument("task_id")
    handoff.add_argument("--copy", action="store_true", help="copy the prompt with macOS pbcopy")

    retry = sub.add_parser("retry", help="explicitly retry a failed or waiting task")
    retry.add_argument("task_id")
    retry.add_argument("--worker", choices=WORKERS)
    cancel = sub.add_parser("cancel", help="cancel a task")
    cancel.add_argument("task_id")

    listing = sub.add_parser("list", help="list tasks")
    listing.add_argument("--status", choices=STATUSES)
    listing.add_argument("--worker", choices=WORKERS)
    sub.add_parser("stats", help="show conservation and reliability metrics")

    complete = sub.add_parser("complete", help="record authoritative/manual completion")
    complete.add_argument("task_id")
    complete.add_argument("--summary")
    response = complete.add_mutually_exclusive_group()
    response.add_argument("--stdin", action="store_true", help="read a external ChatGPT response until EOF")
    response.add_argument("--file", "--result-file", dest="response_file", help="read the complete response from a file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = TaskStore(args.store)
    supervisor = Supervisor(store)
    try:
        if args.command == "create":
            task = _create_task(supervisor, args)
            print(task["task_id"])
        elif args.command == "ask":
            task = _create_task(supervisor, args)
            decision = supervisor.explain_route(task["task_id"], worker=args.worker)
            print(f"Created: {task['task_id']}")
            print(f"Worker: {decision['worker']}")
            if args.explain:
                print()
                _print_route_explanation(decision)
            result = supervisor.dispatch(task["task_id"], worker=args.worker)
            _print_retry_summary(result)
            if result.get("worker") == "external_chatgpt" and result.get("status") == "waiting_for_user":
                print()
                _print_handoff(supervisor.handoff(task["task_id"]))
            else:
                print(f"\n{result['status'].replace('_', ' ').title()}.")
                print(f"\nSummary:\n{result['summary']}")
        elif args.command == "run":
            if args.explain:
                _print_route_explanation(supervisor.explain_route(args.task_id, worker=args.worker))
                print()
            result = supervisor.dispatch(args.task_id, worker=args.worker)
            _print_retry_summary(result)
            if result.get("worker") == "external_chatgpt" and result.get("status") == "waiting_for_user":
                _print_handoff(supervisor.handoff(args.task_id))
            else:
                _json(result)
        elif args.command == "route":
            _print_route_explanation(supervisor.explain_route(args.task_id, worker=args.worker, persist=True))
        elif args.command == "delegate":
            _json(supervisor.dispatch(args.task_id, worker=args.worker, allow_completed=True))
        elif args.command == "reroute":
            result = supervisor.reroute(args.task_id, args.worker, args.reason)
            _print_retry_summary(result)
            if result.get("worker") == "external_chatgpt" and result.get("status") == "waiting_for_user":
                _print_handoff(supervisor.handoff(args.task_id))
            else:
                _json(result)
        elif args.command == "status":
            _json(store.get(args.task_id))
        elif args.command == "result":
            value = store.latest_result(args.task_id)
            if value is None:
                raise OrchestratorError("task has no result")
            _json(value)
        elif args.command == "context":
            value = store.latest_context(args.task_id)
            if value is None:
                raise OrchestratorError("task has no context package")
            _json(value)
        elif args.command == "handoff":
            info = supervisor.handoff(args.task_id)
            if args.copy:
                _copy_prompt(info["prompt_path"])
            _print_handoff(info, copied=args.copy)
        elif args.command == "retry":
            task = store.get(args.task_id)
            if task["status"] not in {"failed", "waiting_for_user"}:
                raise OrchestratorError("only failed or waiting tasks can be retried")
            _json(supervisor.dispatch(args.task_id, worker=args.worker, retry=True))
        elif args.command == "cancel":
            _json(supervisor.cancel(args.task_id))
        elif args.command == "list":
            _json(store.list(status=args.status, worker=args.worker))
        elif args.command == "stats":
            _json(supervisor.stats())
        elif args.command == "complete":
            task = store.get(args.task_id)
            if task.get("worker") == "external_chatgpt":
                if args.stdin:
                    response_text = sys.stdin.read()
                    source = "stdin"
                elif args.response_file:
                    response_text = Path(args.response_file).read_text(encoding="utf-8", errors="replace")
                    source = "file"
                else:
                    raise OrchestratorError("external_chatgpt completion requires --stdin or --file")
                _json(supervisor.import_external_response(args.task_id, response_text, source=source))
            else:
                if not args.summary:
                    raise OrchestratorError("manual completion requires --summary")
                _json(supervisor.complete(args.task_id, args.summary, args.response_file))
        return 0
    except (OrchestratorError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
