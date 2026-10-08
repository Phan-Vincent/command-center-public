"""Schemas and validation for durable orchestration records."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
from uuid import uuid4


SCHEMA_VERSION = 1
WORKERS = ("external_chatgpt", "reasonix_flash", "reasonix_pro", "codex", "local")
STATUSES = ("pending", "running", "waiting_for_user", "completed", "failed", "cancelled")
PRIORITIES = ("low", "normal", "high", "urgent")
FAILURE_CATEGORIES = (
    "transient", "permanent", "invalid_input", "worker_unavailable",
    "timeout", "cancelled", "unknown",
)
REQUIREMENT_KEYS = (
    "repo_read", "repo_write", "terminal", "web_research",
    "current_information", "human_account", "external_files",
)
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{5,79}$")

REQUIREMENT_PATTERNS = {
    "repo_write": re.compile(
        r"\b(implement|modify|fix|refactor|patch|change the code|add (?:a )?feature|remove (?:a )?feature|"
        r"edit (?:the )?(?:code|repository)|update (?:the )?(?:code|repository))\b", re.I,
    ),
    "repo_read": re.compile(
        r"\b(repository|repo|codebase|source code|review (?:the )?code|analy[sz]e (?:the )?code|"
        r"implementation|scanner|module)\b", re.I,
    ),
    "terminal": re.compile(
        r"\b(run tests?|execute|build|compile|terminal|shell|debug runtime|check logs?|benchmark)\b", re.I,
    ),
    "web_research": re.compile(
        r"\b(latest|current|documentation|api docs?|pricing|news|recent|today|web research|online research)\b",
        re.I,
    ),
    "current_information": re.compile(r"\b(latest|current|pricing|news|recent|today)\b", re.I),
    "human_account": re.compile(r"\b(external chatgpt|work account|student account|university account|my school account)\b", re.I),
    "external_files": re.compile(r"\b(attached|attachment|uploaded|external files?|spreadsheet|pdf|csv)\b", re.I),
}


class OrchestratorError(RuntimeError):
    """An expected, user-actionable orchestration error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_task_id() -> str:
    return f"task-{uuid4().hex[:12]}"


def infer_requirements(
    instructions: str,
    *,
    task_type: str = "auto",
    files: list[str] | None = None,
) -> dict[str, bool]:
    """Infer conservative capability requirements with deterministic heuristics."""
    text = f"{task_type.replace('_', ' ').replace('-', ' ')} {instructions}"
    inferred = {name: bool(pattern.search(text)) for name, pattern in REQUIREMENT_PATTERNS.items()}
    inferred = {name: inferred.get(name, False) for name in REQUIREMENT_KEYS}
    normalized_type = task_type.lower().replace("-", "_")
    if normalized_type in {"implementation", "repo_edit"}:
        inferred["repo_write"] = True
    if normalized_type in {"terminal", "test", "git", "final_verification"}:
        inferred["terminal"] = True
    if normalized_type in {"research", "document_analysis"}:
        inferred["web_research"] = True
    if files:
        inferred["repo_read"] = True
    if inferred["repo_write"] or inferred["terminal"]:
        inferred["repo_read"] = True
    return inferred


def resolve_requirements(
    instructions: str,
    *,
    task_type: str = "auto",
    files: list[str] | None = None,
    overrides: dict[str, bool] | None = None,
) -> tuple[dict[str, bool], dict[str, str]]:
    inferred = infer_requirements(instructions, task_type=task_type, files=files)
    resolved = dict(inferred)
    sources = {name: "inferred" if value else "default" for name, value in inferred.items()}
    for name, value in (overrides or {}).items():
        if name not in REQUIREMENT_KEYS:
            raise OrchestratorError(f"unknown requirement: {name}")
        if not isinstance(value, bool):
            raise OrchestratorError(f"requirement override must be boolean: {name}")
        resolved[name] = value
        sources[name] = "explicit"
    if resolved["repo_write"]:
        resolved["repo_read"] = True
        if sources["repo_read"] == "default":
            sources["repo_read"] = "implied"
    return resolved, sources


def task_requirements(task: dict[str, Any]) -> dict[str, bool]:
    """Return stored requirements or infer them for a legacy v1.1 task."""
    existing = task.get("requirements")
    if isinstance(existing, dict):
        return {name: bool(existing.get(name, False)) for name in REQUIREMENT_KEYS}
    return infer_requirements(
        str(task.get("instructions", "")),
        task_type=str(task.get("task_type", "auto")),
        files=task.get("files") if isinstance(task.get("files"), list) else None,
    )


def make_task(
    instructions: str,
    *,
    project: str,
    task_type: str = "auto",
    priority: str = "normal",
    worker: str | None = None,
    context: Any = None,
    files: list[str] | None = None,
    constraints: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    parent_task_id: str | None = None,
    max_depth: int = 3,
    requirements: dict[str, bool] | None = None,
) -> dict[str, Any]:
    now = utc_now()
    resolved_requirements, requirement_sources = resolve_requirements(
        instructions, task_type=task_type, files=files, overrides=requirements,
    )
    task = {
        "schema_version": SCHEMA_VERSION,
        "task_id": new_task_id(),
        "created_at": now,
        "updated_at": now,
        "project": project,
        "task_type": task_type,
        "priority": priority,
        "worker": worker,
        "status": "pending",
        "instructions": instructions,
        "context": {} if context is None else context,
        "files": list(files or []),
        "constraints": list(constraints or []),
        "acceptance_criteria": list(acceptance_criteria or []),
        "parent_task_id": parent_task_id,
        "result_path": None,
        "error": None,
        "attempt_count": 0,
        "transition_depth": 0,
        "max_depth": max_depth,
        "requirements": resolved_requirements,
        "requirement_sources": requirement_sources,
    }
    validate_task(task)
    return task


def validate_task(task: dict[str, Any]) -> None:
    required = {
        "schema_version", "task_id", "created_at", "updated_at", "project",
        "task_type", "priority", "worker", "status", "instructions", "context",
        "files", "constraints", "acceptance_criteria", "parent_task_id",
        "result_path", "error", "attempt_count", "transition_depth", "max_depth",
    }
    missing = sorted(required - set(task))
    if missing:
        raise OrchestratorError(f"task is missing fields: {', '.join(missing)}")
    if task["schema_version"] != SCHEMA_VERSION:
        raise OrchestratorError(f"unsupported task schema: {task['schema_version']}")
    if not isinstance(task["task_id"], str) or not TASK_ID_RE.fullmatch(task["task_id"]):
        raise OrchestratorError("invalid task_id")
    if not isinstance(task["instructions"], str) or not task["instructions"].strip():
        raise OrchestratorError("instructions must be non-empty")
    if not isinstance(task["project"], str) or not task["project"].strip():
        raise OrchestratorError("project must be non-empty")
    if task["worker"] is not None and task["worker"] not in WORKERS:
        raise OrchestratorError(f"unknown worker: {task['worker']}")
    if task["status"] not in STATUSES:
        raise OrchestratorError(f"unknown status: {task['status']}")
    if task["priority"] not in PRIORITIES:
        raise OrchestratorError(f"unknown priority: {task['priority']}")
    for name in ("files", "constraints", "acceptance_criteria"):
        if not isinstance(task[name], list) or not all(isinstance(item, str) for item in task[name]):
            raise OrchestratorError(f"{name} must be a list of strings")
    for name in ("attempt_count", "transition_depth"):
        if not isinstance(task[name], int) or isinstance(task[name], bool) or task[name] < 0:
            raise OrchestratorError(f"{name} must be a non-negative integer")
    if not isinstance(task["max_depth"], int) or isinstance(task["max_depth"], bool) or task["max_depth"] < 1:
        raise OrchestratorError("max_depth must be a positive integer")
    if "requirements" in task:
        requirements = task["requirements"]
        if not isinstance(requirements, dict):
            raise OrchestratorError("requirements must be an object")
        unknown = sorted(set(requirements) - set(REQUIREMENT_KEYS))
        if unknown:
            raise OrchestratorError(f"unknown requirements: {', '.join(unknown)}")
        if any(not isinstance(requirements.get(name, False), bool) for name in REQUIREMENT_KEYS):
            raise OrchestratorError("requirements values must be booleans")
    if "requirement_sources" in task:
        sources = task["requirement_sources"]
        if not isinstance(sources, dict) or any(name not in REQUIREMENT_KEYS for name in sources):
            raise OrchestratorError("requirement_sources must map known requirements")


def normalize_result(
    *,
    task_id: str,
    worker: str,
    status: str,
    summary: str,
    started_at: str,
    findings: list[str] | None = None,
    recommended_next_action: str | None = None,
    artifacts: list[Any] | None = None,
    proposed_patch: str | None = None,
    uncertainties: list[str] | None = None,
    confidence: str | None = None,
    raw_output: Any = None,
    metadata: dict[str, Any] | None = None,
    execution_mode: str | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if worker not in WORKERS:
        raise OrchestratorError(f"unknown result worker: {worker}")
    if status not in ("completed", "failed", "waiting_for_user"):
        raise OrchestratorError(f"invalid result status: {status}")
    result = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "worker": worker,
        "status": status,
        "summary": summary,
        "findings": list(findings or []),
        "recommended_next_action": recommended_next_action,
        "artifacts": list(artifacts or []),
        "proposed_patch": proposed_patch,
        "uncertainties": list(uncertainties or []),
        "confidence": confidence,
        "raw_output": raw_output,
        "started_at": started_at,
        "completed_at": utc_now(),
    }
    if metadata:
        result["metadata"] = metadata
    if execution_mode:
        result["execution_mode"] = execution_mode
    if error is not None:
        result["error"] = error
    return result


def worker_error(
    category: str,
    message: str,
    *,
    code: str | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    if category not in FAILURE_CATEGORIES:
        raise OrchestratorError(f"unknown failure category: {category}")
    return {
        "category": category,
        "code": code,
        "message": message,
        "retryable": retryable,
    }
