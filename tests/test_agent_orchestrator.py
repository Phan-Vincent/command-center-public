#!/usr/bin/env python3
"""Network-free tests for the filesystem-backed task orchestrator."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from agent_orchestrator.adapters import ReasonixAdapter, ExternalChatGPTAdapter, classify_reasonix_failure  # noqa: E402
from agent_orchestrator.cli import main as cli_main  # noqa: E402
from agent_orchestrator.context import build_context_package, external_prompt  # noqa: E402
from agent_orchestrator.models import OrchestratorError, infer_requirements, normalize_result, utc_now, worker_error  # noqa: E402
from agent_orchestrator.router import route_task  # noqa: E402
from agent_orchestrator.store import TaskStore  # noqa: E402
from agent_orchestrator.supervisor import RetryPolicy, Supervisor  # noqa: E402


class TempProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "README.md").write_text("Example project\n", encoding="utf-8")
        self.store = TaskStore(self.root / "store")

    def tearDown(self):
        self.temp.cleanup()

    def create(self, instructions="Routine analysis", **kwargs):
        return self.store.create(instructions, project=str(self.project), **kwargs)


class StoreAndSchemaTests(TempProjectTest):
    def test_task_creation_and_round_trip(self):
        task = self.create(files=["README.md"], acceptance_criteria=["Be specific"])
        loaded = self.store.get(task["task_id"])
        self.assertEqual(loaded["status"], "pending")
        self.assertEqual(loaded["acceptance_criteria"], ["Be specific"])
        self.assertTrue((self.store.logs_dir / "events.jsonl").is_file())

    def test_schema_validation_rejects_unknown_worker(self):
        with self.assertRaises(OrchestratorError):
            self.create(worker="mystery")

    def test_result_serialization(self):
        task = self.create()
        result = normalize_result(
            task_id=task["task_id"], worker="reasonix_flash", status="completed",
            summary="done", findings=["finding"], started_at=utc_now(), raw_output={"x": 1},
        )
        path = self.store.save_result(result, 1)
        self.assertEqual(json.loads(path.read_text())["raw_output"], {"x": 1})
        self.assertEqual(self.store.latest_result(task["task_id"])["summary"], "done")

    def test_legacy_task_without_requirements_remains_readable(self):
        task = self.create()
        path = self.store.tasks_dir / f"{task['task_id']}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("requirements")
        payload.pop("requirement_sources")
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.store.get(task["task_id"])["task_id"], task["task_id"])
        self.assertEqual(len(self.store.list()), 1)
        supervisor = Supervisor(self.store, FakeRegistry())
        self.assertEqual(supervisor.dispatch(task["task_id"])["status"], "completed")
        self.assertEqual(supervisor.stats()["total_tasks"], 1)

    def test_requirement_inference_and_explicit_override(self):
        inferred = infer_requirements("Implement the cache fix and run tests")
        self.assertTrue(inferred["repo_write"])
        self.assertTrue(inferred["terminal"])
        self.assertTrue(inferred["repo_read"])
        self.assertTrue(infer_requirements("Research the latest API docs")["current_information"])
        self.assertTrue(infer_requirements("Use my university account")["human_account"])
        task = self.create("Implement a patch", requirements={"repo_write": False})
        self.assertFalse(task["requirements"]["repo_write"])
        self.assertEqual(task["requirement_sources"]["repo_write"], "explicit")
        self.assertEqual(route_task(task)["worker"], "reasonix_flash")


class RouterTests(TempProjectTest):
    def test_deterministic_routes(self):
        cases = [
            ("Research OAuth library behavior", "external_chatgpt"),
            ("Implement the repository fix and run tests", "codex"),
            ("Complex architecture analysis across services", "reasonix_pro"),
            ("Propose a small helper function", "reasonix_flash"),
        ]
        for instructions, expected in cases:
            with self.subTest(instructions=instructions):
                task = self.create(instructions)
                self.assertEqual(route_task(task)["worker"], expected)

    def test_explicit_override_wins(self):
        task = self.create("Research a document")
        decision = route_task(task, "reasonix_flash")
        self.assertEqual(decision["worker"], "reasonix_flash")
        self.assertIn("explicit", decision["reason"])

    def test_requirements_route_before_task_heuristics(self):
        cases = [
            ({"repo_write": True}, "codex"),
            ({"terminal": True}, "codex"),
            ({"human_account": True}, "external_chatgpt"),
        ]
        for requirements, expected in cases:
            with self.subTest(requirements=requirements):
                task = self.create("Routine reasoning", requirements=requirements)
                decision = route_task(task)
                self.assertEqual(decision["worker"], expected)
                self.assertEqual(decision["requirements_snapshot"], task["requirements"])

    def test_route_explanation_is_deterministic(self):
        task = self.create("Routine reasoning")
        first = route_task(task)
        second = route_task(task)
        self.assertEqual(first, second)
        self.assertEqual(first["worker"], "reasonix_flash")
        self.assertIn("Reasonix Flash", first["reasons"][-1])


class FailureClassificationTests(unittest.TestCase):
    def test_transient_http_failures(self):
        for code in (429, 504):
            with self.subTest(code=code):
                error = classify_reasonix_failure(f"HTTP {code} upstream failure")
                self.assertEqual(error["category"], "transient")
                self.assertTrue(error["retryable"])
                self.assertEqual(error["code"], f"HTTP_{code}")

    def test_invalid_api_key_is_permanent(self):
        error = classify_reasonix_failure("authentication failure: invalid API key")
        self.assertEqual(error["category"], "permanent")
        self.assertFalse(error["retryable"])

    def test_structured_error_wins_over_text(self):
        error = classify_reasonix_failure(
            "HTTP 504 text should not override structured data",
            {"error": {"category": "invalid_input", "code": "BAD_TASK", "retryable": False}},
        )
        self.assertEqual(error["category"], "invalid_input")
        self.assertFalse(error["retryable"])
        inferred_retry = classify_reasonix_failure(
            "provider returned a structured timeout",
            {"error": {"category": "timeout", "code": "PROVIDER_TIMEOUT"}},
        )
        self.assertEqual(inferred_retry["category"], "timeout")
        self.assertTrue(inferred_retry["retryable"])


class ContextTests(TempProjectTest):
    def test_context_includes_requested_file(self):
        (self.project / "code.py").write_text("print('ok')\n", encoding="utf-8")
        task = self.create(files=["code.py"])
        package = build_context_package(task)
        paths = [item["path"] for item in package["files"]]
        self.assertIn("code.py", paths)

    def test_context_rejects_traversal_and_absolute_paths(self):
        task = self.create(files=["../secret", "/etc/passwd"])
        package = build_context_package(task)
        reasons = [item["reason"] for item in package["omitted_files"]]
        self.assertEqual(len(reasons), 2)
        self.assertTrue(all("project-relative" in reason for reason in reasons))


class AdapterTests(TempProjectTest):
    def _package(self):
        return build_context_package(self.create())

    def test_external_adapter_creates_manual_handoff(self):
        task = self.create("Research docs", acceptance_criteria=["Name the source"])
        task["attempt_count"] = 1
        result = ExternalChatGPTAdapter(self.store).run(task, build_context_package(task))
        self.assertEqual(result["status"], "waiting_for_user")
        self.assertEqual(result["execution_mode"], "manual_handoff")
        self.assertEqual(result["metadata"]["execution_mode"], "manual_handoff")
        prompt_path = self.store.external_prompt_path(task["task_id"], 1)
        self.assertTrue(prompt_path.is_file())
        prompt = prompt_path.read_text(encoding="utf-8")
        self.assertIn("Name the source", prompt)
        self.assertIn("Response format", prompt)

    def test_external_prompt_includes_file_excerpt(self):
        (self.project / "notes.txt").write_text("important evidence", encoding="utf-8")
        task = self.create("Research docs", files=["notes.txt"])
        prompt = external_prompt(build_context_package(task))
        self.assertIn("notes.txt", prompt)
        self.assertIn("important evidence", prompt)

    def test_reasonix_mcp_success(self):
        server = self.root / "fake_mcp.py"
        server.write_text(textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                request = json.loads(line)
                if request["id"] == 1:
                    print(json.dumps({"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}), flush=True)
                else:
                    args = request["params"]["arguments"]
                    payload = {"ok": True, "reasonix": {"result": "bounded worker finding", "usage": {"input_tokens": 3}, "total_cost_usd": 0.001}, "routing": {"worker_tier": args["worker_tier"]}}
                    print(json.dumps({"jsonrpc":"2.0","id":2,"result":{"content":[],"structuredContent":payload,"isError":False}}), flush=True)
        """), encoding="utf-8")
        task = self.create()
        result = ReasonixAdapter(server, timeout_seconds=3).run(task, build_context_package(task), "reasonix_flash")
        self.assertEqual(result["status"], "completed")
        self.assertIn("bounded worker finding", result["summary"])
        self.assertEqual(result["metadata"]["usage"]["input_tokens"], 3)
        self.assertEqual(result["metadata"]["cost"], 0.001)

    def test_reasonix_malformed_response_fails_closed(self):
        server = self.root / "bad_mcp.py"
        server.write_text("import sys\nsys.stdin.read()\nprint('not json')\n", encoding="utf-8")
        task = self.create()
        result = ReasonixAdapter(server, timeout_seconds=3).run(task, build_context_package(task), "reasonix_flash")
        self.assertEqual(result["status"], "failed")
        self.assertIn("no valid", result["summary"].lower())

    def test_reasonix_error_uses_structured_stderr(self):
        server = self.root / "error_mcp.py"
        server.write_text(textwrap.dedent("""
            import json, sys
            for line in sys.stdin:
                request = json.loads(line)
                if request["id"] == 1:
                    print(json.dumps({"jsonrpc":"2.0","id":1,"result":{}}), flush=True)
                else:
                    payload = {"ok": False, "error": None, "stderr": "project session locked"}
                    print(json.dumps({"jsonrpc":"2.0","id":2,"result":{"content":[],"structuredContent":payload,"isError":True}}), flush=True)
        """), encoding="utf-8")
        task = self.create()
        result = ReasonixAdapter(server, timeout_seconds=3).run(task, build_context_package(task), "reasonix_flash")
        self.assertEqual(result["status"], "failed")
        self.assertIn("project session locked", result["summary"])


class FakeRegistry:
    def __init__(self, statuses=None, raises=False):
        self.statuses = list(statuses or ["completed"])
        self.raises = raises
        self.calls = []

    def dispatch(self, worker, task, package, *, escalation_reason=None):
        self.calls.append({"worker": worker, "escalation_reason": escalation_reason, "package": package})
        if self.raises:
            raise RuntimeError("adapter boom")
        status = self.statuses.pop(0) if self.statuses else "completed"
        return normalize_result(
            task_id=task["task_id"], worker=worker, status=status,
            summary=f"{worker} {status}", started_at=utc_now(),
            metadata={"runtime_seconds": 1.25},
        )


class SequencedRegistry:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def dispatch(self, worker, task, package, *, escalation_reason=None):
        self.calls.append({"worker": worker, "escalation_reason": escalation_reason})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, dict) and "category" in outcome:
            return normalize_result(
                task_id=task["task_id"], worker=worker, status="failed",
                summary=outcome["message"], started_at=utc_now(), error=outcome,
                metadata={"runtime_seconds": 0.01},
            )
        return normalize_result(
            task_id=task["task_id"], worker=worker, status=str(outcome),
            summary=f"{worker} {outcome}", started_at=utc_now(),
            metadata={"runtime_seconds": 0.01},
        )


class CancelDuringDispatchRegistry:
    def __init__(self, store):
        self.store = store

    def dispatch(self, worker, task, package, *, escalation_reason=None):
        current = self.store.get(task["task_id"])
        current["status"] = "cancelled"
        self.store.save(current)
        return normalize_result(
            task_id=task["task_id"], worker=worker, status="completed",
            summary="finished after cancellation", started_at=utc_now(),
        )


class SupervisorTests(TempProjectTest):
    def external_handoff(self, *, max_depth=3):
        supervisor = Supervisor(self.store)
        task = self.create(
            "Research whether feature Y exists",
            acceptance_criteria=["State whether Y is supported"],
            max_depth=max_depth,
        )
        result = supervisor.dispatch(task["task_id"], worker="external_chatgpt")
        return supervisor, task, result

    def test_dispatch_persists_context_result_and_state(self):
        fake = FakeRegistry()
        supervisor = Supervisor(self.store, fake)
        task = self.create()
        result = supervisor.dispatch(task["task_id"])
        loaded = self.store.get(task["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(loaded["status"], "completed")
        self.assertTrue(Path(loaded["result_path"]).is_file())
        self.assertIsNotNone(self.store.latest_context(task["task_id"]))

    def test_adapter_exception_becomes_failed_state(self):
        supervisor = Supervisor(self.store, FakeRegistry(raises=True))
        task = self.create()
        result = supervisor.dispatch(task["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.get(task["task_id"])["status"], "failed")

    def test_transient_reasonix_failure_retries_then_succeeds_without_depth_cost(self):
        transient = worker_error("transient", "upstream timeout", code="HTTP_504", retryable=True)
        registry = SequencedRegistry([transient, transient, "completed"])
        sleeps = []
        policy = RetryPolicy(
            base_delay_seconds=1.0, jitter_ratio=0.0, max_total_seconds=30.0,
            sleeper=sleeps.append,
        )
        supervisor = Supervisor(self.store, registry, policy)
        task = self.create("Routine reasoning")
        result = supervisor.dispatch(task["task_id"])
        state = self.store.get(task["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(registry.calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(state["attempt_count"], 1)
        self.assertEqual(state["transition_depth"], 1)
        self.assertEqual(len(result["metadata"]["execution_attempts"]), 3)
        self.assertEqual(len(self.store.results(task["task_id"])), 1)
        self.assertEqual(
            [event["event"] for event in self.store.events(task["task_id"])].count("worker_retry_scheduled"),
            2,
        )

    def test_permanent_reasonix_failure_does_not_retry(self):
        permanent = worker_error("permanent", "invalid API key", code="AUTH", retryable=False)
        registry = SequencedRegistry([permanent])
        supervisor = Supervisor(self.store, registry, RetryPolicy(sleeper=lambda _: None))
        task = self.create("Routine reasoning")
        result = supervisor.dispatch(task["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(registry.calls), 1)
        self.assertEqual(result["error"]["message"], "invalid API key")

    def test_transient_retry_limit_is_three_and_history_is_preserved(self):
        transient = worker_error("transient", "rate limited", code="HTTP_429", retryable=True)
        registry = SequencedRegistry([transient, transient, transient])
        supervisor = Supervisor(
            self.store, registry,
            RetryPolicy(base_delay_seconds=0, jitter_ratio=0, sleeper=lambda _: None),
        )
        task = self.create("Routine reasoning", max_depth=1)
        result = supervisor.dispatch(task["task_id"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["metadata"]["retry_exhausted"])
        self.assertEqual(len(registry.calls), 3)
        self.assertEqual(len(result["metadata"]["execution_attempts"]), 3)
        self.assertEqual(self.store.get(task["task_id"])["transition_depth"], 1)
        self.assertEqual(supervisor.stats()["routing_outcomes"]["reasonix_flash"]["failed"], 1)

    def test_flash_retry_exhaustion_escalates_to_pro(self):
        transient = worker_error("timeout", "read timeout", code="TIMEOUT", retryable=True)
        registry = SequencedRegistry([transient, transient, transient, "completed"])
        supervisor = Supervisor(
            self.store, registry,
            RetryPolicy(base_delay_seconds=0, jitter_ratio=0, sleeper=lambda _: None),
        )
        task = self.create("Routine reasoning")
        result = supervisor.dispatch(task["task_id"])
        self.assertEqual(result["worker"], "reasonix_pro")
        self.assertEqual([call["worker"] for call in registry.calls], [
            "reasonix_flash", "reasonix_flash", "reasonix_flash", "reasonix_pro",
        ])
        self.assertEqual(self.store.get(task["task_id"])["transition_depth"], 2)
        self.assertEqual(len(self.store.results(task["task_id"])), 2)
        self.assertEqual(registry.calls[-1]["escalation_reason"], "flash_verification_failed")
        self.assertEqual(result["metadata"]["escalation_history"][0]["worker"], "reasonix_flash")
        persisted = self.store.latest_result(task["task_id"])
        self.assertEqual(len(persisted["metadata"]["escalation_history"][0]["execution_attempts"]), 3)

    def test_duplicate_completed_run_is_rejected(self):
        supervisor = Supervisor(self.store, FakeRegistry())
        task = self.create()
        supervisor.dispatch(task["task_id"])
        with self.assertRaises(OrchestratorError):
            supervisor.dispatch(task["task_id"])

    def test_maximum_transition_depth(self):
        supervisor = Supervisor(self.store, FakeRegistry(["completed", "completed"]))
        task = self.create(max_depth=1)
        supervisor.dispatch(task["task_id"])
        with self.assertRaises(OrchestratorError):
            supervisor.dispatch(task["task_id"], worker="reasonix_pro", allow_completed=True)

    def test_manual_reasonix_pro_override_records_reason(self):
        fake = FakeRegistry()
        supervisor = Supervisor(self.store, fake)
        task = self.create("Routine proposal")
        supervisor.dispatch(task["task_id"], worker="reasonix_pro")
        self.assertEqual(fake.calls[0]["escalation_reason"], "explicit_request")

    def test_reroute_records_override_and_metric(self):
        supervisor = Supervisor(self.store, FakeRegistry(["completed"]))
        task = self.create("Routine reasoning")
        result = supervisor.reroute(task["task_id"], "codex", "requires repository write")
        self.assertEqual(result["worker"], "codex")
        record = self.store.latest_routing(task["task_id"])
        self.assertEqual(record["routing_override"]["original_worker"], "reasonix_flash")
        self.assertEqual(record["routing_override"]["new_worker"], "codex")
        self.assertEqual(supervisor.stats()["routing_overrides"], 1)

    def test_cancel_pending_task(self):
        supervisor = Supervisor(self.store, FakeRegistry())
        task = self.create()
        supervisor.cancel(task["task_id"])
        self.assertEqual(self.store.get(task["task_id"])["status"], "cancelled")

    def test_cancelled_state_wins_race_with_worker_result(self):
        supervisor = Supervisor(self.store, CancelDuringDispatchRegistry(self.store))
        task = self.create()
        result = supervisor.dispatch(task["task_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.store.get(task["task_id"])["status"], "cancelled")

    def test_retry_waiting_task(self):
        supervisor, task, _ = self.external_handoff()
        result = supervisor.dispatch(task["task_id"], retry=True)
        self.assertEqual(result["status"], "waiting_for_user")
        self.assertEqual(self.store.get(task["task_id"])["attempt_count"], 2)
        self.assertTrue(self.store.external_prompt_path(task["task_id"], 2).is_file())

    def test_external_response_import_preserves_attempt_and_provenance(self):
        supervisor, task, handoff = self.external_handoff()
        before = self.store.get(task["task_id"])
        result = supervisor.import_external_response(task["task_id"], "Feature Y requires API v3.", source="stdin")
        after = self.store.get(task["task_id"])
        self.assertEqual(result["worker"], "external_chatgpt")
        self.assertEqual(result["execution_mode"], "manual_handoff")
        self.assertEqual(result["metadata"]["execution_mode"], "manual_handoff")
        self.assertEqual(result["metadata"]["import_source"], "stdin")
        self.assertEqual(result["raw_output"], "Feature Y requires API v3.")
        self.assertEqual(after["status"], "completed")
        self.assertEqual(after["attempt_count"], before["attempt_count"])
        self.assertEqual(after["transition_depth"], before["transition_depth"])
        self.assertEqual(len(self.store.results(task["task_id"])), 2)
        self.assertIn("handoff_created_at", handoff["metadata"])
        self.assertIn("result_imported_at", result["metadata"])

    def test_external_empty_response_is_rejected_without_state_change(self):
        supervisor, task, _ = self.external_handoff()
        with self.assertRaises(OrchestratorError):
            supervisor.import_external_response(task["task_id"], "  \n", source="stdin")
        self.assertEqual(self.store.get(task["task_id"])["status"], "waiting_for_user")

    def test_external_cancel_while_waiting_blocks_import(self):
        supervisor, task, _ = self.external_handoff()
        supervisor.cancel(task["task_id"])
        with self.assertRaises(OrchestratorError):
            supervisor.import_external_response(task["task_id"], "late answer", source="stdin")

    def test_external_completion_can_continue_to_another_worker(self):
        supervisor, task, _ = self.external_handoff()
        supervisor.import_external_response(task["task_id"], "Feature Y requires API v3.", source="file")
        supervisor.adapters = FakeRegistry(["completed"])
        follow_up = supervisor.dispatch(task["task_id"], worker="reasonix_flash", allow_completed=True)
        state = self.store.get(task["task_id"])
        self.assertEqual(follow_up["worker"], "reasonix_flash")
        self.assertEqual(state["transition_depth"], 2)

    def test_external_handoff_metrics(self):
        supervisor, task, _ = self.external_handoff()
        supervisor.import_external_response(task["task_id"], "Feature Y requires API v3.", source="stdin")
        supervisor.adapters = FakeRegistry(["completed"])
        supervisor.dispatch(task["task_id"], worker="reasonix_flash", allow_completed=True)
        waiting_supervisor, _, _ = self.external_handoff()
        stats = waiting_supervisor.stats()
        self.assertEqual(stats["external_chatgpt"]["handoffs"], 2)
        self.assertEqual(stats["external_chatgpt"]["completed"], 1)
        self.assertEqual(stats["external_chatgpt"]["waiting_for_user"], 1)
        self.assertEqual(stats["external_chatgpt"]["escalated_afterward"], 1)
        self.assertEqual(stats["external_chatgpt"]["completed_without_codex"], 1)
        self.assertEqual(stats["worker_attempts"]["external_chatgpt"], 2)
        self.assertEqual(stats["external_failures_or_waits"], 1)
        self.assertEqual(stats["routing_outcomes"]["external_chatgpt"]["initial_tasks"], 2)
        self.assertEqual(stats["routing_outcomes"]["external_chatgpt"]["escalated_to_reasonix"], 1)

    def test_stats_capture_flash_to_pro_and_retries(self):
        supervisor = Supervisor(self.store, FakeRegistry(["completed", "completed"]))
        task = self.create()
        supervisor.dispatch(task["task_id"], worker="reasonix_flash")
        supervisor.dispatch(task["task_id"], worker="reasonix_pro", allow_completed=True)
        stats = supervisor.stats()
        self.assertEqual(stats["total_tasks"], 1)
        self.assertEqual(stats["flash_to_pro_escalations"], 1)
        self.assertEqual(stats["retries"], 1)
        self.assertEqual(stats["measured_attempts"], 2)
        self.assertEqual(stats["routing_outcomes"]["reasonix_flash"]["initial_tasks"], 1)
        self.assertEqual(stats["routing_outcomes"]["reasonix_flash"]["escalated_to_pro"], 1)

    def test_direct_flash_completion_and_conservation_percentage(self):
        supervisor = Supervisor(self.store, FakeRegistry(["completed", "completed"]))
        flash = self.create("Routine reasoning")
        codex = self.create("Implement the repository fix")
        supervisor.dispatch(flash["task_id"])
        supervisor.dispatch(codex["task_id"])
        stats = supervisor.stats()
        self.assertEqual(stats["routing_outcomes"]["reasonix_flash"]["completed_directly"], 1)
        self.assertEqual(stats["routing_outcomes"]["codex"]["initial_tasks"], 1)
        self.assertEqual(stats["completed_without_codex_percentage"], 50.0)
        self.assertIn("reasonix_flash", stats["worker_latency"])

    def test_flash_to_codex_routing_outcome_is_counted(self):
        supervisor = Supervisor(self.store, FakeRegistry(["completed", "completed"]))
        task = self.create("Routine reasoning")
        supervisor.dispatch(task["task_id"])
        supervisor.dispatch(task["task_id"], worker="codex", allow_completed=True)
        stats = supervisor.stats()
        self.assertEqual(stats["routing_outcomes"]["reasonix_flash"]["escalated_to_codex"], 1)
        self.assertEqual(stats["tasks_requiring_codex"], 1)

    def test_manual_completion_for_codex_handoff(self):
        supervisor = Supervisor(self.store, FakeRegistry(["waiting_for_user"]))
        task = self.create("Implement repository feature")
        supervisor.dispatch(task["task_id"])
        result = supervisor.complete(task["task_id"], "verified locally")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.store.get(task["task_id"])["status"], "completed")
        self.assertEqual(len(self.store.results(task["task_id"])), 2)


class CliTests(TempProjectTest):
    def create_cli_external_task(self, cli_store):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main([
                "--store", str(cli_store), "create", "Research docs",
                "--project", str(self.project), "--accept", "Cite a source",
            ])
        self.assertEqual(rc, 0)
        return output.getvalue().strip()

    def test_create_and_list_cli(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main(["--store", str(self.root / "cli-store"), "create", "Research docs", "--project", str(self.project)])
        self.assertEqual(rc, 0)
        task_id = output.getvalue().strip()
        self.assertTrue(task_id.startswith("task-"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main(["--store", str(self.root / "cli-store"), "list", "--status", "pending"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())[0]["task_id"], task_id)

    def test_create_requirement_flags_override_inference(self):
        cli_store = self.root / "cli-requirements"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main([
                "--store", str(cli_store), "create", "Implement a patch",
                "--project", str(self.project), "--no-requires-repo-write", "--requires-current",
            ])
        self.assertEqual(rc, 0)
        task = TaskStore(cli_store).get(output.getvalue().strip())
        self.assertFalse(task["requirements"]["repo_write"])
        self.assertTrue(task["requirements"]["current_information"])

    def test_route_command_explains_and_persists_decision(self):
        cli_store = self.root / "cli-route"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main([
                "--store", str(cli_store), "create", "Routine reasoning", "--project", str(self.project),
            ]), 0)
        task_id = output.getvalue().strip()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main(["--store", str(cli_store), "route", task_id]), 0)
        self.assertIn("Selected worker: reasonix_flash", output.getvalue())
        self.assertIn("Routing factors:", output.getvalue())
        self.assertEqual(TaskStore(cli_store).latest_routing(task_id)["kind"], "preview")

    def test_ask_runs_automated_reasonix_and_prints_summary(self):
        cli_store = self.root / "cli-ask-reasonix"

        def fake_run(_adapter, task, _package, worker, *, escalation_reason=None):
            return normalize_result(
                task_id=task["task_id"], worker=worker, status="completed",
                summary="bounded answer", started_at=utc_now(),
                metadata={"runtime_seconds": 0.01},
            )

        output = io.StringIO()
        with mock.patch.object(ReasonixAdapter, "run", autospec=True, side_effect=fake_run), contextlib.redirect_stdout(output):
            rc = cli_main([
                "--store", str(cli_store), "ask", "Routine reasoning",
                "--project", str(self.project), "--explain",
            ])
        self.assertEqual(rc, 0)
        self.assertIn("Created: task-", output.getvalue())
        self.assertIn("Worker: reasonix_flash", output.getvalue())
        self.assertIn("Completed.", output.getvalue())
        self.assertIn("bounded answer", output.getvalue())
        task = TaskStore(cli_store).list()[0]
        self.assertEqual(task["transition_depth"], 1)

    def test_ask_emits_external_handoff_and_forwards_requirement(self):
        cli_store = self.root / "cli-ask-external"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main([
                "--store", str(cli_store), "ask", "Use my university account",
                "--project", str(self.project), "--requires-human-account",
            ])
        self.assertEqual(rc, 0)
        self.assertIn("Worker: external_chatgpt", output.getvalue())
        self.assertIn("Status: waiting_for_user", output.getvalue())
        task = TaskStore(cli_store).list()[0]
        self.assertTrue(task["requirements"]["human_account"])

    def test_ask_repo_write_routes_to_codex(self):
        cli_store = self.root / "cli-ask-codex"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main([
                "--store", str(cli_store), "ask", "Fix the cache bug",
                "--project", str(self.project), "--requires-repo-write", "--requires-terminal",
            ])
        self.assertEqual(rc, 0)
        self.assertIn("Worker: codex", output.getvalue())
        self.assertIn("Waiting For User.", output.getvalue())

    def test_reroute_cli_records_reason(self):
        cli_store = self.root / "cli-reroute"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(cli_main([
                "--store", str(cli_store), "create", "Routine reasoning", "--project", str(self.project),
            ]), 0)
        task_id = output.getvalue().strip()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main([
                "--store", str(cli_store), "reroute", task_id,
                "--worker", "codex", "--reason", "requires repository write",
            ]), 0)
        record = TaskStore(cli_store).latest_routing(task_id)
        self.assertEqual(record["routing_override"]["reason"], "requires repository write")

    def test_external_run_prints_handoff_and_stdin_imports_response(self):
        cli_store = self.root / "cli-external"
        task_id = self.create_cli_external_task(cli_store)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main(["--store", str(cli_store), "run", task_id])
        self.assertEqual(rc, 0)
        self.assertIn("Delegated to: external_chatgpt", output.getvalue())
        self.assertIn(f"./orchestrator complete {task_id} --stdin", output.getvalue())
        store = TaskStore(cli_store)
        self.assertTrue(store.external_prompt_path(task_id).is_file())

        output = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("External answer from stdin")), contextlib.redirect_stdout(output):
            rc = cli_main(["--store", str(cli_store), "complete", task_id, "--stdin"])
        self.assertEqual(rc, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["raw_output"], "External answer from stdin")
        self.assertEqual(result["worker"], "external_chatgpt")

    def test_external_file_import_and_handoff_command(self):
        cli_store = self.root / "cli-file"
        task_id = self.create_cli_external_task(cli_store)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["--store", str(cli_store), "run", task_id]), 0)
        handoff_output = io.StringIO()
        with contextlib.redirect_stdout(handoff_output):
            self.assertEqual(cli_main(["--store", str(cli_store), "handoff", task_id]), 0)
        self.assertIn("Prompt:", handoff_output.getvalue())

        response = self.root / "response.txt"
        response.write_text("External answer from file", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = cli_main(["--store", str(cli_store), "complete", task_id, "--file", str(response)])
        self.assertEqual(rc, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["metadata"]["import_source"], "file")
        self.assertEqual(result["raw_output"], "External answer from file")

    def test_external_empty_stdin_returns_error(self):
        cli_store = self.root / "cli-empty"
        task_id = self.create_cli_external_task(cli_store)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["--store", str(cli_store), "run", task_id]), 0)
        stderr = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("")), contextlib.redirect_stderr(stderr):
            rc = cli_main(["--store", str(cli_store), "complete", task_id, "--stdin"])
        self.assertEqual(rc, 2)
        self.assertIn("cannot be empty", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
