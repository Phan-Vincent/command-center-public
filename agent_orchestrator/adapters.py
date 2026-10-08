"""Worker adapters. Delegated workers remain read-only proposal generators."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

from .context import reasonix_contract, external_prompt
from .models import FAILURE_CATEGORIES, normalize_result, utc_now, worker_error


def _summary(text: str, limit: int = 700) -> str:
    compact = " ".join(text.strip().split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def classify_reasonix_failure(message: str, structured: Any = None) -> dict[str, Any]:
    """Classify a Reasonix failure, preferring trusted structured fields."""
    data = structured if isinstance(structured, dict) else {}
    structured_error = data.get("error") if isinstance(data.get("error"), dict) else data
    category = structured_error.get("category") if isinstance(structured_error, dict) else None
    retryable = structured_error.get("retryable") if isinstance(structured_error, dict) else None
    code = structured_error.get("code") if isinstance(structured_error, dict) else None
    if category in FAILURE_CATEGORIES:
        if not isinstance(retryable, bool):
            retryable = category in {"transient", "timeout"}
        return worker_error(category, message, code=str(code) if code else None, retryable=retryable)

    text = " ".join(
        str(value) for value in (message, code, data.get("stderr"), data.get("parse_error")) if value
    ).lower()
    http_match = next((value for value in ("429", "500", "502", "503", "504") if re.search(rf"\b(?:http[ _-]?)?{value}\b", text)), None)
    if http_match:
        return worker_error("transient", message, code=f"HTTP_{http_match}", retryable=True)
    if any(phrase in text for phrase in (
        "connection reset", "connection timeout", "read timeout", "temporary network failure",
        "upstream unavailable", "temporarily unavailable",
    )):
        return worker_error("transient", message, code="NETWORK_TRANSIENT", retryable=True)
    if "timed out" in text or "timeout" in text:
        return worker_error("timeout", message, code="TIMEOUT", retryable=True)
    if any(phrase in text for phrase in ("cancelled", "canceled", "explicit cancellation")):
        return worker_error("cancelled", message, code="CANCELLED", retryable=False)
    if any(phrase in text for phrase in (
        "authentication failure", "invalid api key", "unauthorized", "permission denied",
        "invalid model", "forbidden",
    )):
        return worker_error("permanent", message, code="AUTH_OR_PERMISSION", retryable=False)
    if any(phrase in text for phrase in ("malformed task", "unsupported request", "invalid request")):
        return worker_error("invalid_input", message, code="INVALID_INPUT", retryable=False)
    return worker_error("unknown", message, code=str(code) if code else None, retryable=False)


class ReasonixAdapter:
    def __init__(self, mcp_server: str | Path | None = None, timeout_seconds: int = 300) -> None:
        self.mcp_server = Path(mcp_server).expanduser() if mcp_server else self._locate_server()
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _locate_server() -> Path | None:
        configured = os.environ.get("REASONIX_MCP_SERVER")
        if configured:
            return Path(configured).expanduser()
        home = Path.home()
        candidates = [home / "plugins" / "reasonix-orchestrator" / "mcp" / "reasonix_mcp.py"]
        candidates.extend(sorted(
            (home / ".codex" / "plugins" / "cache" / "personal" / "reasonix-orchestrator").glob("*/mcp/reasonix_mcp.py"),
            reverse=True,
        ))
        return next((path for path in candidates if path.is_file()), None)

    def run(
        self,
        task: dict[str, Any],
        package: dict[str, Any],
        worker: str,
        *,
        escalation_reason: str | None = None,
    ) -> dict[str, Any]:
        started_at = utc_now()
        started = time.monotonic()
        if self.mcp_server is None or not self.mcp_server.is_file():
            message = "Reasonix MCP server was not found."
            return normalize_result(
                task_id=task["task_id"], worker=worker, status="failed",
                summary=message, started_at=started_at,
                recommended_next_action="Set REASONIX_MCP_SERVER to the existing reasonix_mcp.py path.",
                uncertainties=["Reasonix availability was not probed."],
                error=worker_error("worker_unavailable", message, code="MCP_NOT_FOUND", retryable=False),
            )
        arguments: dict[str, Any] = {
            "dir": task["project"],
            "task_contract": reasonix_contract(package, worker),
            "worker_tier": "pro" if worker == "reasonix_pro" else "flash",
            "max_steps": 18,
            "timeout_seconds": self.timeout_seconds,
        }
        if worker == "reasonix_pro":
            arguments["escalation_reason"] = escalation_reason or "high_complexity"
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "command-center-orchestrator", "version": "1.2"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delegate_read", "arguments": arguments}},
        ]
        stdin = "\n".join(json.dumps(message, separators=(",", ":")) for message in messages) + "\n"
        try:
            completed = subprocess.run(
                [sys.executable, str(self.mcp_server)], input=stdin, text=True,
                capture_output=True, timeout=self.timeout_seconds + 30, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            message = "Reasonix delegation timed out."
            return normalize_result(
                task_id=task["task_id"], worker=worker, status="failed",
                summary=message, started_at=started_at,
                raw_output={"stdout": exc.stdout, "stderr": exc.stderr},
                recommended_next_action="Narrow the task before an explicit retry.",
                metadata={"runtime_seconds": round(time.monotonic() - started, 3)},
                error=worker_error("timeout", message, code="PROCESS_TIMEOUT", retryable=True),
            )
        except OSError as exc:
            message = f"Reasonix MCP could not start: {exc}"
            failure = classify_reasonix_failure(message)
            if failure["category"] == "unknown":
                failure = worker_error("worker_unavailable", message, code="PROCESS_START_FAILED", retryable=False)
            return normalize_result(
                task_id=task["task_id"], worker=worker, status="failed",
                summary=message, started_at=started_at,
                recommended_next_action="Verify the configured MCP server and Python runtime.",
                metadata={"runtime_seconds": round(time.monotonic() - started, 3)},
                error=failure,
            )

        response = None
        for line in completed.stdout.splitlines():
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("id") == 2:
                response = candidate
        raw = {"stdout": completed.stdout, "stderr": completed.stderr, "returncode": completed.returncode}
        if response is None or "error" in response:
            response_error = response.get("error") if isinstance(response, dict) else None
            message = "Reasonix MCP returned no valid tool response."
            if isinstance(response_error, dict):
                message = str(response_error.get("message") or message)
            elif response_error:
                message = str(response_error)
            failure = classify_reasonix_failure(message, response_error or {"stderr": completed.stderr})
            return normalize_result(
                task_id=task["task_id"], worker=worker, status="failed",
                summary=message, started_at=started_at,
                raw_output=raw, recommended_next_action="Inspect the bounded raw response before retrying.",
                metadata={"runtime_seconds": round(time.monotonic() - started, 3)},
                error=failure,
            )
        tool_result = response.get("result", {})
        payload = tool_result.get("structuredContent")
        if not isinstance(payload, dict):
            try:
                payload = json.loads(tool_result.get("content", [{}])[0].get("text", ""))
            except (IndexError, AttributeError, json.JSONDecodeError):
                payload = None
        if not isinstance(payload, dict) or payload.get("ok") is not True or tool_result.get("isError") is True:
            error = "malformed structured result"
            if isinstance(payload, dict):
                structured_error = payload.get("error")
                if isinstance(structured_error, dict):
                    error = str(
                        structured_error.get("message")
                        or structured_error.get("code")
                        or "Reasonix returned an unsuccessful structured result"
                    )
                else:
                    error = next(
                        (str(payload.get(key)).strip() for key in ("error", "stderr", "parse_error") if payload.get(key)),
                        "Reasonix returned an unsuccessful structured result",
                    )
            message = f"Reasonix delegation failed: {error}"
            return normalize_result(
                task_id=task["task_id"], worker=worker, status="failed",
                summary=message, started_at=started_at,
                raw_output=raw, uncertainties=["No successful worker result was verified."],
                recommended_next_action="Inspect the failure and make an explicit, narrower retry.",
                metadata={"runtime_seconds": round(time.monotonic() - started, 3), "routing": payload.get("routing") if isinstance(payload, dict) else None},
                error=classify_reasonix_failure(str(error), payload),
            )
        worker_text = str(payload.get("reasonix", {}).get("result", "")).strip()
        if not worker_text:
            message = "Reasonix reported success without a worker result."
            return normalize_result(
                task_id=task["task_id"], worker=worker, status="failed",
                summary=message, started_at=started_at,
                raw_output=payload, uncertainties=["The structured result was incomplete."],
                metadata={"runtime_seconds": round(time.monotonic() - started, 3)},
                error=worker_error("unknown", message, code="EMPTY_RESULT", retryable=False),
            )
        reasonix_details = payload.get("reasonix") if isinstance(payload.get("reasonix"), dict) else {}
        metadata = {
            "runtime_seconds": round(time.monotonic() - started, 3),
            "routing": payload.get("routing"),
            "cost": payload.get("cost_quote") or reasonix_details.get("cost_quote") or payload.get("total_cost_usd") or reasonix_details.get("total_cost_usd"),
            "usage": payload.get("usage") or reasonix_details.get("usage"),
        }
        return normalize_result(
            task_id=task["task_id"], worker=worker, status="completed",
            summary=_summary(worker_text), findings=[worker_text], started_at=started_at,
            recommended_next_action="Supervisor should inspect and independently verify the worker result.",
            confidence="unverified_worker_report", raw_output=payload, metadata=metadata,
        )


class ExternalChatGPTAdapter:
    def __init__(self, store: Any) -> None:
        self.store = store

    def run(self, task: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
        handoff_created_at = utc_now()
        prompt_path = self.store.save_text_artifact(
            task["task_id"], task["attempt_count"], "external_prompt.md", external_prompt(package),
        )
        return normalize_result(
            task_id=task["task_id"], worker="external_chatgpt", status="waiting_for_user",
            summary="External ChatGPT handoff is ready for the user.", started_at=handoff_created_at,
            artifacts=[{"type": "external_prompt", "path": str(prompt_path), "created_at": handoff_created_at}],
            recommended_next_action=f"Copy {prompt_path} into external ChatGPT, then import the answer with ./orchestrator complete {task['task_id']} --stdin.",
            uncertainties=["No external ChatGPT response has been imported yet."],
            metadata={"execution_mode": "manual_handoff", "handoff_created_at": handoff_created_at},
            execution_mode="manual_handoff",
        )


class ManualAdapter:
    def run(self, task: dict[str, Any], package: dict[str, Any], worker: str) -> dict[str, Any]:
        started_at = utc_now()
        action = "Codex must perform authoritative repository work" if worker == "codex" else "A local operator must complete this task"
        return normalize_result(
            task_id=task["task_id"], worker=worker, status="waiting_for_user",
            summary=f"{action}; automatic recursive execution is intentionally disabled.",
            started_at=started_at,
            recommended_next_action="Use the saved context package, complete the work, and record the result with the complete command.",
            artifacts=[{"context_package": "saved by supervisor"}],
        )


class AdapterRegistry:
    def __init__(self, store: Any, *, reasonix: ReasonixAdapter | None = None, external: ExternalChatGPTAdapter | None = None) -> None:
        self.reasonix = reasonix or ReasonixAdapter()
        self.external = external or ExternalChatGPTAdapter(store)
        self.manual = ManualAdapter()

    def dispatch(
        self,
        worker: str,
        task: dict[str, Any],
        package: dict[str, Any],
        *,
        escalation_reason: str | None = None,
    ) -> dict[str, Any]:
        if worker in {"reasonix_flash", "reasonix_pro"}:
            return self.reasonix.run(task, package, worker, escalation_reason=escalation_reason)
        if worker == "external_chatgpt":
            return self.external.run(task, package)
        return self.manual.run(task, package, worker)
