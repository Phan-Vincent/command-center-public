"""Transparent one-transition-at-a-time task supervision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import random
import statistics
import time
from typing import Any, Callable

from .adapters import AdapterRegistry
from .context import build_context_package
from .models import OrchestratorError, normalize_result, task_requirements, utc_now, worker_error
from .router import route_task
from .store import TaskStore


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 2.5
    jitter_ratio: float = 0.15
    max_total_seconds: float = 15.0
    sleeper: Callable[[float], None] = time.sleep
    random_fn: Callable[[], float] = random.random

    def delay(self, retry_number: int) -> float:
        base = min(self.max_delay_seconds, self.base_delay_seconds * (self.multiplier ** (retry_number - 1)))
        jitter = base * self.jitter_ratio * ((self.random_fn() * 2.0) - 1.0)
        return max(0.0, base + jitter)


class Supervisor:
    def __init__(
        self,
        store: TaskStore | None = None,
        adapters: AdapterRegistry | Any | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.store = store or TaskStore()
        self.adapters = adapters or AdapterRegistry(self.store)
        self.retry_policy = retry_policy or RetryPolicy()

    def create(self, instructions: str, **kwargs: Any) -> dict[str, Any]:
        return self.store.create(instructions, **kwargs)

    def explain_route(
        self,
        task_id: str,
        *,
        worker: str | None = None,
        persist: bool = False,
    ) -> dict[str, Any]:
        task = self.store.get(task_id)
        decision = route_task(task, worker)
        record = {
            **decision,
            "task_id": task_id,
            "timestamp": utc_now(),
            "kind": "preview",
        }
        if persist:
            path = self.store.save_routing(task_id, record)
            self.store.log_event(task_id, "route_explained", {"worker": decision["worker"], "path": str(path)})
        return record

    def _execute_with_retries(
        self,
        selected: str,
        task: dict[str, Any],
        package: dict[str, Any],
        *,
        escalation_reason: str | None,
    ) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        overall_started = time.monotonic()
        result: Any = None
        max_attempts = self.retry_policy.max_attempts if selected in {"reasonix_flash", "reasonix_pro"} else 1
        for execution_attempt in range(1, max_attempts + 1):
            attempt_started_at = utc_now()
            attempt_started = time.monotonic()
            try:
                result = self.adapters.dispatch(
                    selected, task, package, escalation_reason=escalation_reason,
                )
            except Exception as exc:  # adapter bugs are durable but never automatically retried
                message = f"Worker adapter failed safely: {exc}"
                result = normalize_result(
                    task_id=task["task_id"], worker=selected, status="failed",
                    summary=message, started_at=attempt_started_at,
                    recommended_next_action="Inspect the adapter failure before retrying.",
                    error=worker_error("unknown", str(exc), code="ADAPTER_EXCEPTION", retryable=False),
                )
            latency = round(time.monotonic() - attempt_started, 3)
            error = result.get("error") if isinstance(result, dict) else None
            history.append({
                "attempt_number": execution_attempt,
                "worker": selected,
                "started_at": attempt_started_at,
                "ended_at": utc_now(),
                "latency_seconds": latency,
                "status": result.get("status", "invalid") if isinstance(result, dict) else "invalid",
                "error": error,
            })
            if not isinstance(result, dict) or result.get("status") != "failed":
                break
            retryable = isinstance(error, dict) and error.get("retryable") is True
            if not retryable:
                break
            if execution_attempt >= max_attempts:
                result.setdefault("metadata", {})["retry_exhausted"] = True
                break
            elapsed = time.monotonic() - overall_started
            delay = self.retry_policy.delay(execution_attempt)
            if elapsed + delay > self.retry_policy.max_total_seconds:
                result.setdefault("metadata", {})["retry_exhausted"] = True
                result["metadata"]["retry_deadline_reached"] = True
                break
            current = self.store.get(task["task_id"])
            if current["status"] == "cancelled":
                break
            self.store.log_event(task["task_id"], "worker_retry_scheduled", {
                "worker": selected,
                "completed_attempt": execution_attempt,
                "next_attempt": execution_attempt + 1,
                "delay_seconds": round(delay, 3),
                "error": error,
            })
            self.retry_policy.sleeper(delay)

        if not isinstance(result, dict) or result.get("status") not in {"completed", "failed", "waiting_for_user"}:
            raw_result = result
            message = "Worker adapter returned an invalid normalized result."
            result = normalize_result(
                task_id=task["task_id"], worker=selected, status="failed",
                summary=message, started_at=utc_now(), raw_output=raw_result,
                recommended_next_action="Fix or inspect the adapter before retrying.",
                error=worker_error("unknown", message, code="INVALID_RESULT", retryable=False),
            )
        metadata = result.setdefault("metadata", {})
        metadata["execution_attempts"] = history
        metadata["automatic_retry_count"] = max(0, len(history) - 1)
        metadata["automated_latency_seconds"] = round(time.monotonic() - overall_started, 3)
        return result

    def dispatch(
        self,
        task_id: str,
        *,
        worker: str | None = None,
        retry: bool = False,
        allow_completed: bool = False,
        override_reason: str | None = None,
        _escalation_reason: str | None = None,
    ) -> dict[str, Any]:
        with self.store.task_lock(task_id):
            task = self.store.get(task_id)
            if task["status"] == "cancelled":
                raise OrchestratorError("cancelled tasks cannot run")
            if task["status"] == "running":
                raise OrchestratorError("task is already running")
            if task["status"] == "completed" and not allow_completed:
                raise OrchestratorError("task is completed; use delegate for an explicit follow-up transition")
            if task["status"] == "failed" and not retry and worker is None:
                raise OrchestratorError("task failed; use retry or delegate explicitly")
            if task["transition_depth"] >= task["max_depth"]:
                raise OrchestratorError(f"maximum worker-transition depth reached ({task['max_depth']})")

            previous_results = self.store.results(task_id)
            requested_worker = task.get("worker") if retry and worker is None else worker
            automatic_decision = route_task(task, honor_created_worker=False)
            decision = route_task(task, requested_worker)
            if _escalation_reason:
                decision = {
                    **decision,
                    "reason": _escalation_reason,
                    "reasons": [_escalation_reason],
                    "rule": "retry_exhaustion_escalation",
                    "explicit_override": False,
                }
            selected = decision["worker"]
            escalation_reason = None
            if selected == "reasonix_pro":
                if worker is not None:
                    if previous_results and previous_results[-1].get("worker") == "reasonix_flash" and previous_results[-1].get("status") == "failed":
                        escalation_reason = "flash_verification_failed"
                    else:
                        escalation_reason = "explicit_request"
                else:
                    escalation_reason = "high_complexity"
            task["worker"] = selected
            task["status"] = "running"
            task["error"] = None
            task["attempt_count"] += 1
            task["transition_depth"] += 1
            attempt = task["attempt_count"]
            self.store.save(task)
            routing_record = {
                **decision,
                "task_id": task_id,
                "timestamp": utc_now(),
                "kind": "dispatch",
                "attempt": attempt,
                "transition_depth": task["transition_depth"],
            }
            if override_reason:
                routing_record["routing_override"] = {
                    "original_worker": automatic_decision["worker"],
                    "new_worker": selected,
                    "reason": override_reason,
                }
            routing_path = self.store.save_routing(task_id, routing_record)
            self.store.log_event(task_id, "dispatch", {
                "worker": selected, "reason": decision["reason"], "attempt": attempt,
                "transition_depth": task["transition_depth"], "escalation_reason": escalation_reason,
                "routing_path": str(routing_path),
            })

        prior = previous_results[-1] if previous_results else None
        package = build_context_package(task, prior)
        context_path = self.store.save_context(task_id, attempt, package)
        self.store.log_event(task_id, "context_packaged", {"path": str(context_path)})
        result = self._execute_with_retries(
            selected, task, package, escalation_reason=escalation_reason,
        )
        result_path = self.store.save_result(result, attempt)
        with self.store.task_lock(task_id):
            task = self.store.get(task_id)
            was_cancelled = task["status"] == "cancelled"
            if not was_cancelled:
                task["status"] = result["status"]
            task["result_path"] = str(result_path)
            task["error"] = result["summary"] if result["status"] == "failed" else None
            self.store.save(task)
            self.store.log_event(task_id, "result", {
                "worker": selected, "status": result["status"], "path": str(result_path),
                "runtime_seconds": result.get("metadata", {}).get("runtime_seconds"),
                "automatic_retry_count": result.get("metadata", {}).get("automatic_retry_count", 0),
                "task_remained_cancelled": was_cancelled,
            })
        if (
            result["status"] == "failed"
            and result.get("metadata", {}).get("retry_exhausted") is True
            and selected in {"reasonix_flash", "reasonix_pro"}
        ):
            current = self.store.get(task_id)
            if current["status"] != "cancelled" and current["transition_depth"] < current["max_depth"]:
                requirements = task_requirements(current)
                if selected == "reasonix_flash":
                    next_worker = "codex" if requirements["repo_write"] or requirements["terminal"] else "reasonix_pro"
                else:
                    next_worker = "codex"
                reason = (
                    f"{selected} failed after {len(result['metadata']['execution_attempts'])} transient attempts; "
                    + ("authoritative repository execution is required" if next_worker == "codex" else "escalating to Reasonix Pro")
                )
                self.store.log_event(task_id, "retry_exhaustion_escalation", {
                    "from_worker": selected, "to_worker": next_worker, "reason": reason,
                })
                escalated_result = self.dispatch(
                    task_id,
                    worker=next_worker,
                    retry=True,
                    _escalation_reason=reason,
                )
                escalated_result.setdefault("metadata", {}).setdefault("escalation_history", []).insert(0, {
                    "worker": selected,
                    "status": result["status"],
                    "summary": result["summary"],
                    "error": result.get("error"),
                    "execution_attempts": result.get("metadata", {}).get("execution_attempts", []),
                    "escalation_reason": reason,
                })
                final_state = self.store.get(task_id)
                self.store.save_result(escalated_result, final_state["attempt_count"])
                return escalated_result
        return result

    def reroute(self, task_id: str, worker: str, reason: str) -> dict[str, Any]:
        if not reason or not reason.strip():
            raise OrchestratorError("reroute reason must be non-empty")
        task = self.store.get(task_id)
        return self.dispatch(
            task_id,
            worker=worker,
            retry=task["status"] in {"failed", "waiting_for_user"},
            allow_completed=task["status"] == "completed",
            override_reason=reason.strip(),
        )

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self.store.task_lock(task_id):
            task = self.store.get(task_id)
            if task["status"] == "completed":
                raise OrchestratorError("completed tasks cannot be cancelled")
            task["status"] = "cancelled"
            self.store.save(task)
            self.store.log_event(task_id, "cancelled")
            return task

    def complete(self, task_id: str, summary: str, result_file: str | None = None) -> dict[str, Any]:
        with self.store.task_lock(task_id):
            task = self.store.get(task_id)
            if task["status"] not in {"waiting_for_user", "failed", "pending"}:
                raise OrchestratorError(f"task cannot be manually completed from {task['status']}")
            raw = None
            if result_file:
                raw = Path(result_file).read_text(encoding="utf-8", errors="replace")
            worker = task.get("worker") or "local"
            result = normalize_result(
                task_id=task_id, worker=worker, status="completed", summary=summary,
                findings=[raw] if raw else [], raw_output=raw, started_at=utc_now(),
                confidence="manually_recorded",
            )
            attempt = task["attempt_count"] + 1
            path = self.store.save_result(result, attempt)
            task["attempt_count"] = attempt
            task["status"] = "completed"
            task["result_path"] = str(path)
            task["error"] = None
            self.store.save(task)
            self.store.log_event(task_id, "manually_completed", {"path": str(path)})
            return result

    def import_external_response(self, task_id: str, response: str, *, source: str) -> dict[str, Any]:
        if not response or not response.strip():
            raise OrchestratorError("external ChatGPT response cannot be empty")
        with self.store.task_lock(task_id):
            task = self.store.get(task_id)
            if task.get("worker") != "external_chatgpt":
                raise OrchestratorError("response import is only valid for a external_chatgpt handoff")
            if task["status"] != "waiting_for_user":
                raise OrchestratorError(f"external response cannot be imported from {task['status']}")
            attempt = task["attempt_count"]
            handoff = next(
                (
                    record for record in reversed(self.store.results(task_id))
                    if record.get("worker") == "external_chatgpt"
                    and record.get("status") == "waiting_for_user"
                    and record.get("metadata", {}).get("execution_mode") == "manual_handoff"
                ),
                None,
            )
            if handoff is None:
                raise OrchestratorError("external handoff result is missing")
            prompt_path = self.store.external_prompt_path(task_id, attempt)
            if prompt_path is None:
                raise OrchestratorError("external prompt artifact is missing")

            imported_at = utc_now()
            response_path = self.store.save_text_artifact(
                task_id, attempt, "external_response.txt", response,
            )
            compact = " ".join(response.strip().split())
            summary = compact[:700] + ("…" if len(compact) > 700 else "")
            result = normalize_result(
                task_id=task_id,
                worker="external_chatgpt",
                status="completed",
                summary=summary,
                findings=[response],
                recommended_next_action="Supervisor should inspect the imported research and explicitly delegate any required repository work.",
                artifacts=[
                    {"type": "external_prompt", "path": str(prompt_path), "created_at": handoff.get("metadata", {}).get("handoff_created_at")},
                    {"type": "external_response", "path": str(response_path), "created_at": imported_at},
                ],
                confidence="manually_imported_unverified",
                raw_output=response,
                started_at=handoff.get("metadata", {}).get("handoff_created_at") or handoff["started_at"],
                metadata={
                    "execution_mode": "manual_handoff",
                    "handoff_created_at": handoff.get("metadata", {}).get("handoff_created_at") or handoff["started_at"],
                    "result_imported_at": imported_at,
                    "response_imported_at": imported_at,
                    "import_source": source,
                },
                execution_mode="manual_handoff",
            )
            result["completed_at"] = imported_at
            path = self.store.save_result(result, attempt, phase="imported")
            task["status"] = "completed"
            task["result_path"] = str(path)
            task["error"] = None
            self.store.save(task)
            self.store.log_event(task_id, "external_response_imported", {
                "attempt": attempt,
                "path": str(path),
                "response_path": str(response_path),
                "source": source,
            })
            return result

    def handoff(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        if task.get("worker") != "external_chatgpt":
            raise OrchestratorError("task is not assigned to external_chatgpt")
        prompt_path = self.store.external_prompt_path(task_id)
        if prompt_path is None:
            raise OrchestratorError("external prompt artifact has not been created; run the task first")
        return {
            "task_id": task_id,
            "worker": "external_chatgpt",
            "objective": task["instructions"],
            "prompt_path": str(prompt_path),
            "status": task["status"],
            "next_command": f"./orchestrator complete {task_id} --stdin",
        }

    def stats(self) -> dict[str, Any]:
        tasks = self.store.list()
        worker_tasks = {worker: 0 for worker in ("external_chatgpt", "reasonix_flash", "reasonix_pro", "codex", "local")}
        worker_attempts = dict(worker_tasks)
        completed_without_codex = 0
        codex_escalations = 0
        flash_to_pro = 0
        external_failures_or_waits = 0
        external_handoffs = 0
        external_completed = 0
        external_waiting = 0
        external_escalated_afterward = 0
        external_completed_without_codex = 0
        runtimes: list[float] = []
        successful_latencies = {worker: [] for worker in worker_tasks}
        external_wait_seconds: list[float] = []
        failure_counts: dict[str, int] = {}
        automatic_retries = 0
        routing_overrides = 0
        routing_outcomes = {
            "reasonix_flash": {"initial_tasks": 0, "completed_directly": 0, "escalated_to_pro": 0, "escalated_to_codex": 0, "failed": 0},
            "reasonix_pro": {"initial_tasks": 0, "completed": 0, "escalated_to_codex": 0, "failed": 0},
            "external_chatgpt": {"initial_tasks": 0, "completed": 0, "escalated_to_reasonix": 0, "escalated_to_codex": 0, "failed_or_waiting": 0},
            "codex": {"initial_tasks": 0},
        }
        retries = 0
        for task in tasks:
            if task.get("worker") in worker_tasks:
                worker_tasks[task["worker"]] += 1
            results = self.store.results(task["task_id"])
            retries += max(0, task.get("attempt_count", 0) - 1)
            if task.get("worker") == "external_chatgpt" and task["status"] in {"failed", "waiting_for_user"}:
                external_failures_or_waits += 1
            transition_results = [
                record for record in results
                if not (
                    record.get("worker") == "external_chatgpt"
                    and record.get("status") == "completed"
                    and record.get("metadata", {}).get("execution_mode") == "manual_handoff"
                )
            ]
            workers = [record.get("worker") for record in transition_results]
            if workers:
                first_worker = workers[0]
                if first_worker in routing_outcomes:
                    routing_outcomes[first_worker]["initial_tasks"] += 1
                if first_worker == "reasonix_flash":
                    if "reasonix_pro" in workers[1:]:
                        routing_outcomes[first_worker]["escalated_to_pro"] += 1
                    if "codex" in workers[1:]:
                        routing_outcomes[first_worker]["escalated_to_codex"] += 1
                    if task["status"] == "completed" and all(worker == "reasonix_flash" for worker in workers):
                        routing_outcomes[first_worker]["completed_directly"] += 1
                    if task["status"] == "failed":
                        routing_outcomes[first_worker]["failed"] += 1
                elif first_worker == "reasonix_pro":
                    if "codex" in workers[1:]:
                        routing_outcomes[first_worker]["escalated_to_codex"] += 1
                    if task["status"] == "completed" and "codex" not in workers[1:]:
                        routing_outcomes[first_worker]["completed"] += 1
                    if task["status"] == "failed":
                        routing_outcomes[first_worker]["failed"] += 1
                elif first_worker == "external_chatgpt":
                    if task["status"] == "completed":
                        routing_outcomes[first_worker]["completed"] += 1
                    if any(worker in {"reasonix_flash", "reasonix_pro"} for worker in workers[1:]):
                        routing_outcomes[first_worker]["escalated_to_reasonix"] += 1
                    if "codex" in workers[1:]:
                        routing_outcomes[first_worker]["escalated_to_codex"] += 1
                    if task["status"] in {"failed", "waiting_for_user"}:
                        routing_outcomes[first_worker]["failed_or_waiting"] += 1
            routing_overrides += sum(
                1 for record in self.store.routing_history(task["task_id"])
                if isinstance(record.get("routing_override"), dict)
            )
            external_handoff_indexes = [
                index for index, record in enumerate(results)
                if record.get("worker") == "external_chatgpt"
                and record.get("status") == "waiting_for_user"
                and record.get("metadata", {}).get("execution_mode") == "manual_handoff"
            ]
            external_import_indexes = [
                index for index, record in enumerate(results)
                if record.get("worker") == "external_chatgpt"
                and record.get("status") == "completed"
                and record.get("metadata", {}).get("execution_mode") == "manual_handoff"
            ]
            external_handoffs += len(external_handoff_indexes)
            if external_import_indexes:
                external_completed += 1
                first_import = external_import_indexes[0]
                if any(record.get("worker") != "external_chatgpt" for record in results[first_import + 1:]):
                    external_escalated_afterward += 1
                if task["status"] == "completed" and "codex" not in workers:
                    external_completed_without_codex += 1
            if task["status"] == "waiting_for_user" and task.get("worker") == "external_chatgpt":
                external_waiting += 1
            if task["status"] == "completed" and "codex" not in workers:
                completed_without_codex += 1
            if "codex" in workers:
                codex_escalations += 1
            if any(a == "reasonix_flash" and b == "reasonix_pro" for a, b in zip(workers, workers[1:])):
                flash_to_pro += 1
            for record in results:
                worker = record.get("worker")
                is_external_import = (
                    worker == "external_chatgpt"
                    and record.get("status") == "completed"
                    and record.get("metadata", {}).get("execution_mode") == "manual_handoff"
                )
                if worker in worker_attempts and not is_external_import:
                    worker_attempts[worker] += 1
                error = record.get("error")
                if record.get("status") == "failed" and isinstance(error, dict):
                    category = str(error.get("category") or "unknown")
                    failure_counts[category] = failure_counts.get(category, 0) + 1
                execution_attempts = record.get("metadata", {}).get("execution_attempts", [])
                if isinstance(execution_attempts, list):
                    automatic_retries += max(0, len(execution_attempts) - 1)
                    for execution in execution_attempts:
                        latency = execution.get("latency_seconds") if isinstance(execution, dict) else None
                        if (
                            worker in successful_latencies
                            and isinstance(latency, (int, float))
                            and not isinstance(latency, bool)
                            and execution.get("status") == "completed"
                        ):
                            successful_latencies[worker].append(float(latency))
                runtime = record.get("metadata", {}).get("runtime_seconds")
                if isinstance(runtime, (int, float)) and not isinstance(runtime, bool):
                    runtimes.append(float(runtime))
                    if record.get("status") == "completed" and not execution_attempts and worker in successful_latencies:
                        successful_latencies[worker].append(float(runtime))
                if is_external_import:
                    metadata = record.get("metadata", {})
                    start = metadata.get("handoff_created_at")
                    end = metadata.get("response_imported_at") or metadata.get("result_imported_at")
                    try:
                        if start and end:
                            external_wait_seconds.append((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
                    except (TypeError, ValueError):
                        pass

        def with_percentages(values: dict[str, int]) -> dict[str, Any]:
            denominator = values.get("initial_tasks", 0)
            enriched: dict[str, Any] = dict(values)
            if denominator:
                enriched["percentages"] = {
                    key: round((value / denominator) * 100.0, 1)
                    for key, value in values.items() if key != "initial_tasks"
                }
            return enriched

        latency_metrics = {}
        for worker, values in successful_latencies.items():
            if values:
                latency_metrics[worker] = {
                    "successful_attempts": len(values),
                    "average_successful_seconds": round(sum(values) / len(values), 3),
                    "median_successful_seconds": round(statistics.median(values), 3),
                }
        completed_percentage = round((completed_without_codex / len(tasks)) * 100.0, 1) if tasks else None
        return {
            "total_tasks": len(tasks),
            "completed_without_codex": completed_without_codex,
            "codex_escalations": codex_escalations,
            "worker_tasks": worker_tasks,
            "worker_attempts": worker_attempts,
            "flash_to_pro_escalations": flash_to_pro,
            "external_failures_or_waits": external_failures_or_waits,
            "external_chatgpt": {
                "handoffs": external_handoffs,
                "completed": external_completed,
                "waiting_for_user": external_waiting,
                "escalated_afterward": external_escalated_afterward,
                "completed_without_codex": external_completed_without_codex,
            },
            "retries": retries,
            "automatic_reasonix_retries": automatic_retries,
            "failure_categories": failure_counts,
            "routing_overrides": routing_overrides,
            "routing_outcomes": {name: with_percentages(values) for name, values in routing_outcomes.items()},
            "completed_without_codex_percentage": completed_percentage,
            "tasks_requiring_codex": codex_escalations,
            "worker_latency": latency_metrics,
            "external_human_wait": {
                "completed_handoffs": len(external_wait_seconds),
                "median_seconds": round(statistics.median(external_wait_seconds), 3) if external_wait_seconds else None,
            },
            "measured_runtime_seconds": round(sum(runtimes), 3),
            "measured_attempts": len(runtimes),
        }
