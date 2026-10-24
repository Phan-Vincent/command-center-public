"""Transparent one-transition-at-a-time task supervision."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import random
import statistics
import time
from typing import Any, Callable

from .adapters import AdapterRegistry, worker_stop_confirmed
from .context import build_context_package
from .models import BROWSER_HANDOFFS, RESULT_STATUSES, WORKERS, OrchestratorError, normalize_result, utc_now, worker_error
from .router import route_task
from .store import TaskStore


@dataclass
class RetryPolicy:
    """Compatibility holder for the retired retry ladder.

    The installed policy is one attempt per dispatch. Backoff and automatic
    escalation no longer run; the fields remain so existing callers that
    construct a policy keep working.
    """

    max_attempts: int = 1
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

    @staticmethod
    def _stop_state(record: dict[str, Any] | None) -> str | None:
        """Return recorded local stop evidence for a prior worker attempt."""
        if not isinstance(record, dict):
            return None
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        process = metadata.get("process") if isinstance(metadata.get("process"), dict) else {}
        local = process.get("local_status")
        reported = process.get("status")
        # The engine may mark `status: unknown` even when only the outer wrapper
        # stopped; a separately grouped worker is then not proven stopped.
        for value in (local, reported):
            if value in {"unknown", "running"}:
                return str(value)
        state = local or reported or metadata.get("process_local_status")
        return str(state) if state else None

    def _prior_stop_blocks(self, record: dict[str, Any] | None) -> str | None:
        """Block overlapping work when a prior worker may still be running."""
        if not isinstance(record, dict) or record.get("worker") not in {"reasonix_flash", "reasonix_pro"}:
            return None
        if record.get("confidence") == "manually_recorded":
            # Manual records do not establish a worker stop; dispatch also
            # checks all preceding attempts so they cannot hide uncertainty.
            return None
        state = self._stop_state(record)
        if worker_stop_confirmed(record.get("metadata")):
            return None
        return (
            f"prior {record.get('worker')} worker stop state is not confirmed ({state or 'unknown'}); "
            "confirm that worker is stopped before continuing"
        )

    @staticmethod
    def _cancelled_result(
        task_id: str,
        worker: str,
        started_at: str,
        prior: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        prior = prior if isinstance(prior, dict) else {}
        metadata = dict(prior.get("metadata") or {})
        metadata["cancelled"] = True
        metadata.setdefault("outcome", "cancelled")
        if prior:
            metadata["worker_reported_status"] = prior.get("status")
        return normalize_result(
            task_id=task_id, worker=worker, status="cancelled",
            summary="Task was cancelled; worker output was not accepted.",
            findings=list(prior.get("findings") or []),
            recommended_next_action="No further work will run for a cancelled task.",
            uncertainties=(
                ["Worker output arrived after cancellation and was not accepted."]
                if prior else ["No worker was dispatched."]
            ),
            raw_output=prior.get("raw_output"),
            started_at=prior.get("started_at") or started_at,
            metadata=metadata,
        )

    def _finalize(
        self,
        task_id: str,
        result: dict[str, Any],
        attempt: int,
        *,
        selected: str,
        started_at: str,
    ) -> dict[str, Any]:
        with self.store.task_lock(task_id):
            task = self.store.get(task_id)
            cancelled = task["status"] == "cancelled"
            if cancelled and result.get("status") != "cancelled":
                result = self._cancelled_result(task_id, selected, started_at, result)
            # Retain safety evidence in the task even if its result file cannot
            # be written. An empty result history must never imply safe retry.
            task["last_dispatch"] = {
                "worker": selected, "status": result["status"],
                "metadata": {key: result.get("metadata", {}).get(key)
                             for key in ("outcome", "process")},
            }
            try:
                result_path = self.store.save_result(result, attempt)
            except (OSError, TypeError, ValueError) as exc:
                task["status"] = "cancelled" if cancelled else "failed"
                task["error"] = f"{result.get('summary', 'dispatch failed')} (result persistence failed: {exc})"
                result["status"] = task["status"]
                result["summary"] = task["error"]
                result["error"] = worker_error("unknown", str(exc), code="RESULT_PERSISTENCE_FAILED", retryable=False)
                self.store.save(task)
                self.store.log_event(task_id, "result_persistence_failed", {"error": str(exc)})
                result.setdefault("metadata", {})["result_persistence_error"] = str(exc)
                return result
            if not cancelled:
                task["status"] = result["status"]
            task["result_path"] = str(result_path)
            task["error"] = result.get("summary") if result.get("status") == "failed" else None
            self.store.save(task)
            self.store.log_event(task_id, "result", {
                "worker": selected, "status": result["status"], "path": str(result_path),
                "runtime_seconds": result.get("metadata", {}).get("runtime_seconds"),
                "automatic_retry_count": result.get("metadata", {}).get("automatic_retry_count", 0),
                "task_remained_cancelled": cancelled,
            })
        return result

    def _execute_once(
        self,
        selected: str,
        task: dict[str, Any],
        package: dict[str, Any],
        *,
        escalation_reason: str | None,
    ) -> dict[str, Any]:
        """Run exactly one worker attempt; there is no automatic retry ladder."""
        attempt_started_at = utc_now()
        attempt_started = time.monotonic()
        try:
            result = self.adapters.dispatch(
                selected, task, package, escalation_reason=escalation_reason,
            )
        except Exception as exc:  # adapter bugs are durable but never retried
            message = f"Worker adapter failed safely: {exc}"
            result = normalize_result(
                task_id=task["task_id"], worker=selected, status="failed",
                summary=message, started_at=attempt_started_at,
                recommended_next_action="Inspect the adapter failure and confirm any worker stop before an explicit retry.",
                metadata={
                    "process": {"local_status": "unknown", "scope": "unknown", "pipes_drained": None},
                    "outcome": "unknown",
                },
                error=worker_error("unknown", str(exc), code="ADAPTER_EXCEPTION", retryable=False),
            )
        latency = round(time.monotonic() - attempt_started, 3)
        history = [{
            "attempt_number": 1,
            "worker": selected,
            "started_at": attempt_started_at,
            "ended_at": utc_now(),
            "latency_seconds": latency,
            "status": result.get("status", "invalid") if isinstance(result, dict) else "invalid",
            "error": result.get("error") if isinstance(result, dict) else None,
        }]
        if not isinstance(result, dict) or result.get("status") not in RESULT_STATUSES:
            raw_result = result
            message = "Worker adapter returned an invalid normalized result."
            result = normalize_result(
                task_id=task["task_id"], worker=selected, status="failed",
                summary=message, started_at=utc_now(), raw_output=raw_result,
                recommended_next_action="Fix or inspect the adapter and confirm any worker stop before an explicit retry.",
                metadata={
                    "process": {"local_status": "unknown", "scope": "unknown", "pipes_drained": None},
                    "outcome": "unknown",
                },
                error=worker_error("unknown", message, code="INVALID_RESULT", retryable=False),
            )
        metadata = result.setdefault("metadata", {})
        metadata["execution_attempts"] = history
        metadata["automatic_retry_count"] = 0
        metadata["automatic_retries_disabled"] = True
        metadata["automated_latency_seconds"] = latency
        return result

    def dispatch(
        self,
        task_id: str,
        *,
        worker: str | None = None,
        retry: bool = False,
        allow_completed: bool = False,
        override_reason: str | None = None,
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
            for record in [task.get("last_dispatch"), *previous_results]:
                blocked = self._prior_stop_blocks(record)
                if blocked:
                    raise OrchestratorError(blocked)
            requested_worker = task.get("worker") if retry and worker is None else worker
            automatic_decision = route_task(task, honor_created_worker=False)
            decision = route_task(task, requested_worker)
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
        started_at = utc_now()
        try:
            package = build_context_package(task, prior)
            context_path = self.store.save_context(task_id, attempt, package)
        except Exception as exc:
            # A packaging/read/persistence failure means the worker never
            # started. Record a durable terminal state instead of leaving the
            # task stuck as running.
            message = f"Context packaging failed before worker dispatch: {exc}"
            self.store.log_event(task_id, "context_packaging_failed", {
                "error": str(exc), "attempt": attempt,
            })
            result = normalize_result(
                task_id=task_id, worker=selected, status="failed",
                summary=message, started_at=started_at,
                recommended_next_action="Fix the input or storage error, then retry explicitly.",
                uncertainties=["The worker was not started."],
                metadata={
                    "process": {"local_status": "not_started", "scope": "not_started", "pipes_drained": True},
                    "outcome": "not_started",
                    "packaging_error": str(exc),
                },
                error=worker_error("invalid_input", message, code="CONTEXT_PACKAGING_FAILED", retryable=True),
            )
            return self._finalize(task_id, result, attempt, selected=selected, started_at=started_at)
        self.store.log_event(task_id, "context_packaged", {"path": str(context_path)})

        # Cancellation is authoritative even when it arrives after packaging.
        if self.store.get(task_id)["status"] == "cancelled":
            self.store.log_event(task_id, "cancelled_before_dispatch", {"attempt": attempt})
            result = self._cancelled_result(task_id, selected, started_at)
            return self._finalize(task_id, result, attempt, selected=selected, started_at=started_at)
        result = self._execute_once(
            selected, task, package, escalation_reason=escalation_reason,
        )
        return self._finalize(task_id, result, attempt, selected=selected, started_at=started_at)

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

    def import_handoff_response(self, task_id: str, response: str, *, source: str) -> dict[str, Any]:
        """Import the answer pasted back from a browser-driven worker (external ChatGPT or Gemini)."""
        with self.store.task_lock(task_id):
            task = self.store.get(task_id)
            worker = task.get("worker")
            spec = BROWSER_HANDOFFS.get(worker)
            if spec is None:
                raise OrchestratorError(
                    f"response import is only valid for a {' or '.join(BROWSER_HANDOFFS)} handoff"
                )
            label, prefix = spec["label"], spec["artifact_prefix"]
            if not response or not response.strip():
                raise OrchestratorError(f"{label} response cannot be empty")
            if task["status"] != "waiting_for_user":
                raise OrchestratorError(f"{label} response cannot be imported from {task['status']}")
            attempt = task["attempt_count"]
            handoff = next(
                (
                    record for record in reversed(self.store.results(task_id))
                    if record.get("worker") == worker
                    and record.get("status") == "waiting_for_user"
                    and record.get("metadata", {}).get("execution_mode") == "manual_handoff"
                ),
                None,
            )
            if handoff is None:
                raise OrchestratorError(f"{label} handoff result is missing")
            prompt_path = self.store.handoff_prompt_path(task_id, prefix, attempt)
            if prompt_path is None:
                raise OrchestratorError(f"{label} prompt artifact is missing")

            imported_at = utc_now()
            response_path = self.store.save_text_artifact(
                task_id, attempt, f"{prefix}_response.txt", response,
            )
            compact = " ".join(response.strip().split())
            summary = compact[:700] + ("…" if len(compact) > 700 else "")
            result = normalize_result(
                task_id=task_id,
                worker=worker,
                status="completed",
                summary=summary,
                findings=[response],
                recommended_next_action="Supervisor should inspect the imported research and explicitly delegate any required repository work.",
                artifacts=[
                    {"type": f"{prefix}_prompt", "path": str(prompt_path), "created_at": handoff.get("metadata", {}).get("handoff_created_at")},
                    {"type": f"{prefix}_response", "path": str(response_path), "created_at": imported_at},
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
            self.store.log_event(task_id, f"{prefix}_response_imported", {
                "attempt": attempt,
                "path": str(path),
                "response_path": str(response_path),
                "source": source,
            })
            return result

    # The external ChatGPT name predates Gemini; keep it for existing callers.
    import_external_response = import_handoff_response

    def handoff(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        worker = task.get("worker")
        spec = BROWSER_HANDOFFS.get(worker)
        if spec is None:
            raise OrchestratorError(f"task is not assigned to {' or '.join(BROWSER_HANDOFFS)}")
        prompt_path = self.store.handoff_prompt_path(task_id, spec["artifact_prefix"])
        if prompt_path is None:
            raise OrchestratorError(f"{spec['label']} prompt artifact has not been created; run the task first")
        return {
            "task_id": task_id,
            "worker": worker,
            "objective": task["instructions"],
            "prompt_path": str(prompt_path),
            "status": task["status"],
            "where": spec["where"],
            "model_rule": spec["model_rule"],
            "next_command": f"./orchestrator complete {task_id} --stdin",
        }

    def stats(self) -> dict[str, Any]:
        tasks = self.store.list()
        worker_tasks = {worker: 0 for worker in WORKERS}
        worker_attempts = dict(worker_tasks)
        completed_without_codex = 0
        codex_escalations = 0
        flash_to_pro = 0
        external_failures_or_waits = 0
        handoff_stats = {
            worker: {"handoffs": 0, "completed": 0, "waiting_for_user": 0, "escalated_afterward": 0, "completed_without_codex": 0}
            for worker in BROWSER_HANDOFFS
        }
        runtimes: list[float] = []
        successful_latencies = {worker: [] for worker in worker_tasks}
        handoff_wait_seconds: dict[str, list[float]] = {worker: [] for worker in BROWSER_HANDOFFS}
        failure_counts: dict[str, int] = {}
        automatic_retries = 0
        routing_overrides = 0
        routing_outcomes = {
            "reasonix_flash": {"initial_tasks": 0, "completed_directly": 0, "escalated_to_pro": 0, "escalated_to_codex": 0, "failed": 0},
            "reasonix_pro": {"initial_tasks": 0, "completed": 0, "escalated_to_codex": 0, "failed": 0},
            "external_chatgpt": {"initial_tasks": 0, "completed": 0, "escalated_to_reasonix": 0, "escalated_to_codex": 0, "failed_or_waiting": 0},
            "gemini_app": {"initial_tasks": 0, "completed": 0, "escalated_to_reasonix": 0, "escalated_to_codex": 0, "failed_or_waiting": 0},
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
                    record.get("worker") in BROWSER_HANDOFFS
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
                elif first_worker in BROWSER_HANDOFFS:
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
            for handoff_worker, counts in handoff_stats.items():
                handoff_indexes = [
                    index for index, record in enumerate(results)
                    if record.get("worker") == handoff_worker
                    and record.get("status") == "waiting_for_user"
                    and record.get("metadata", {}).get("execution_mode") == "manual_handoff"
                ]
                import_indexes = [
                    index for index, record in enumerate(results)
                    if record.get("worker") == handoff_worker
                    and record.get("status") == "completed"
                    and record.get("metadata", {}).get("execution_mode") == "manual_handoff"
                ]
                counts["handoffs"] += len(handoff_indexes)
                if import_indexes:
                    counts["completed"] += 1
                    first_import = import_indexes[0]
                    if any(record.get("worker") != handoff_worker for record in results[first_import + 1:]):
                        counts["escalated_afterward"] += 1
                    if task["status"] == "completed" and "codex" not in workers:
                        counts["completed_without_codex"] += 1
                if task["status"] == "waiting_for_user" and task.get("worker") == handoff_worker:
                    counts["waiting_for_user"] += 1
            if task["status"] == "completed" and "codex" not in workers:
                completed_without_codex += 1
            if "codex" in workers:
                codex_escalations += 1
            if any(a == "reasonix_flash" and b == "reasonix_pro" for a, b in zip(workers, workers[1:])):
                flash_to_pro += 1
            for record in results:
                worker = record.get("worker")
                is_handoff_import = (
                    worker in BROWSER_HANDOFFS
                    and record.get("status") == "completed"
                    and record.get("metadata", {}).get("execution_mode") == "manual_handoff"
                )
                if worker in worker_attempts and not is_handoff_import:
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
                if is_handoff_import:
                    metadata = record.get("metadata", {})
                    start = metadata.get("handoff_created_at")
                    end = metadata.get("response_imported_at") or metadata.get("result_imported_at")
                    try:
                        if start and end:
                            handoff_wait_seconds[worker].append((datetime.fromisoformat(end.replace("Z", "+00:00")) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
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

        def human_wait(worker: str) -> dict[str, Any]:
            values = handoff_wait_seconds[worker]
            return {
                "completed_handoffs": len(values),
                "median_seconds": round(statistics.median(values), 3) if values else None,
            }

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
            "external_chatgpt": handoff_stats["external_chatgpt"],
            "gemini_app": handoff_stats["gemini_app"],
            "retries": retries,
            "automatic_reasonix_retries": automatic_retries,
            "failure_categories": failure_counts,
            "routing_overrides": routing_overrides,
            "routing_outcomes": {name: with_percentages(values) for name, values in routing_outcomes.items()},
            "completed_without_codex_percentage": completed_percentage,
            "tasks_requiring_codex": codex_escalations,
            "worker_latency": latency_metrics,
            "external_human_wait": human_wait("external_chatgpt"),
            "gemini_human_wait": human_wait("gemini_app"),
            "measured_runtime_seconds": round(sum(runtimes), 3),
            "measured_attempts": len(runtimes),
        }
