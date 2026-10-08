"""Deterministic, inspectable, requirements-first task routing."""

from __future__ import annotations

import re
from typing import Any

from .models import OrchestratorError, WORKERS, task_requirements


EXTERNAL_RE = re.compile(
    r"\b(research|summari[sz]e|article|document analysis|documentation research|explain|brainstorm|"
    r"architecture critique|test[- ]case ideation|first-pass analysis|non-repository review)\b",
    re.I,
)
PRO_RE = re.compile(
    r"\b(hard|difficult|complex|deep)\b.*\b(debug|reason|architect|analysis|design|plan)\b|"
    r"\b(root cause across|threat model|cross-system architecture)\b",
    re.I,
)


def _decision(
    worker: str,
    rule: str,
    requirements: dict[str, bool],
    reasons: list[str],
    *,
    explicit: bool = False,
) -> dict[str, Any]:
    escalation = {
        "reasonix_flash": ["reasonix_pro", "codex"],
        "reasonix_pro": ["codex"],
        "external_chatgpt": ["reasonix_flash", "codex"],
        "codex": [],
        "local": ["codex"],
    }[worker]
    return {
        "worker": worker,
        "reason": reasons[0],
        "reasons": reasons,
        "rule": rule,
        "requirements_snapshot": requirements,
        "potential_escalation": escalation,
        "explicit_override": explicit,
        "policy_version": "1.2",
    }


def route_task(
    task: dict[str, Any],
    explicit_worker: str | None = None,
    *,
    honor_created_worker: bool = True,
) -> dict[str, Any]:
    requirements = task_requirements(task)
    if explicit_worker is not None:
        if explicit_worker not in WORKERS:
            raise OrchestratorError(f"unknown worker override: {explicit_worker}")
        return _decision(
            explicit_worker, "explicit_worker_override", requirements,
            [f"explicit worker override selected {explicit_worker}"], explicit=True,
        )
    if honor_created_worker and task.get("worker") in WORKERS and task.get("attempt_count", 0) == 0:
        return _decision(
            task["worker"], "created_worker_override", requirements,
            ["worker selected when task was created"], explicit=True,
        )

    if requirements["repo_write"] or requirements["terminal"]:
        reasons = []
        if requirements["repo_write"]:
            reasons.append("repo_write=true; authoritative repository execution is required")
        if requirements["terminal"]:
            reasons.append("terminal=true; authoritative local execution is required")
        return _decision("codex", "authoritative_repo_or_runtime", requirements, reasons)
    if requirements["human_account"]:
        return _decision(
            "external_chatgpt", "human_account_required", requirements,
            ["human_account=true; a manual external ChatGPT handoff is required"],
        )
    if requirements["web_research"] or requirements["current_information"]:
        reasons = []
        if requirements["web_research"]:
            reasons.append("web_research=true; manual connected research is preferred")
        if requirements["current_information"]:
            reasons.append("current_information=true; current-source access is required")
        return _decision("external_chatgpt", "connected_research", requirements, reasons)

    task_type = str(task.get("task_type", "auto")).lower().replace("-", "_")
    text = f"{task_type} {task.get('instructions', '')}"
    if task_type in {"research", "summary", "document_analysis", "brainstorm"} or EXTERNAL_RE.search(text):
        return _decision(
            "external_chatgpt", "read_only_research", requirements,
            ["task classified as read-only research or analysis"],
        )
    if task_type in {"hard_debug", "architecture", "complex_analysis"} or PRO_RE.search(text):
        return _decision(
            "reasonix_pro", "high_complexity_reasoning", requirements,
            ["task classified as bounded high-complexity non-repository reasoning"],
        )
    reasons = ["routine reasoning workload", "Reasonix Flash is the preferred first-pass worker"]
    if requirements["repo_read"]:
        reasons.insert(0, "repo_read=true without repository modification or terminal execution")
    return _decision("reasonix_flash", "routine_reasoning", requirements, reasons)
