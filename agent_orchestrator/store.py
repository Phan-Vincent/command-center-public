"""Atomic filesystem storage for tasks, results, context, and event history."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator

from .models import OrchestratorError, TASK_ID_RE, make_task, utc_now, validate_task


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TaskStore:
    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.environ.get("ORCHESTRATOR_HOME")
        self.root = Path(configured).expanduser().resolve() if configured else PROJECT_ROOT / "data" / "orchestrator"
        self.tasks_dir = self.root / "tasks"
        self.results_dir = self.root / "results"
        self.context_dir = self.root / "context"
        self.routing_dir = self.root / "routing"
        self.logs_dir = self.root / "logs"
        for directory in (self.tasks_dir, self.results_dir, self.context_dir, self.routing_dir, self.logs_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _check_id(task_id: str) -> None:
        if not TASK_ID_RE.fullmatch(task_id):
            raise OrchestratorError("invalid task id")

    @staticmethod
    def _atomic_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def create(self, instructions: str, **kwargs: Any) -> dict[str, Any]:
        task = make_task(instructions, **kwargs)
        self._atomic_json(self.tasks_dir / f"{task['task_id']}.json", task)
        self.log_event(task["task_id"], "created", {"status": "pending"})
        self.log_event(task["task_id"], "requirements_inferred", {
            "requirements": task.get("requirements", {}),
            "sources": task.get("requirement_sources", {}),
        })
        return task

    def get(self, task_id: str) -> dict[str, Any]:
        self._check_id(task_id)
        path = self.tasks_dir / f"{task_id}.json"
        try:
            task = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise OrchestratorError(f"task not found: {task_id}") from exc
        except json.JSONDecodeError as exc:
            raise OrchestratorError(f"task file is malformed: {task_id}") from exc
        if not isinstance(task, dict):
            raise OrchestratorError(f"task file is not an object: {task_id}")
        validate_task(task)
        return task

    def save(self, task: dict[str, Any]) -> None:
        validate_task(task)
        task["updated_at"] = utc_now()
        self._atomic_json(self.tasks_dir / f"{task['task_id']}.json", task)

    def list(self, *, status: str | None = None, worker: str | None = None) -> list[dict[str, Any]]:
        tasks = []
        for path in self.tasks_dir.glob("*.json"):
            try:
                task = self.get(path.stem)
            except OrchestratorError:
                continue
            if status and task["status"] != status:
                continue
            if worker and task["worker"] != worker:
                continue
            tasks.append(task)
        return sorted(tasks, key=lambda item: item["created_at"])

    def save_result(self, result: dict[str, Any], attempt: int, *, phase: str | None = None) -> Path:
        task_id = result["task_id"]
        self._check_id(task_id)
        suffix = f"~{phase}" if phase else ""
        path = self.results_dir / task_id / f"{attempt:03d}-{result['worker']}{suffix}.json"
        self._atomic_json(path, result)
        return path

    def results(self, task_id: str) -> list[dict[str, Any]]:
        self._check_id(task_id)
        records = []
        for path in sorted((self.results_dir / task_id).glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                value["_path"] = str(path)
                records.append(value)
        return records

    def latest_result(self, task_id: str) -> dict[str, Any] | None:
        records = self.results(task_id)
        return records[-1] if records else None

    def save_context(self, task_id: str, attempt: int, package: dict[str, Any]) -> Path:
        self._check_id(task_id)
        path = self.context_dir / task_id / f"{attempt:03d}.json"
        self._atomic_json(path, package)
        return path

    def save_text_artifact(self, task_id: str, attempt: int, name: str, text: str) -> Path:
        self._check_id(task_id)
        if not name or Path(name).name != name or name in {".", ".."}:
            raise OrchestratorError("artifact name must be a plain filename")
        path = self.tasks_dir / task_id / "artifacts" / f"{attempt:03d}-{name}"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                if text and not text.endswith("\n"):
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        return path

    def external_prompt_path(self, task_id: str, attempt: int | None = None) -> Path | None:
        self._check_id(task_id)
        artifact_dir = self.tasks_dir / task_id / "artifacts"
        if attempt is not None:
            path = artifact_dir / f"{attempt:03d}-external_prompt.md"
            return path if path.is_file() else None
        paths = sorted(artifact_dir.glob("*-external_prompt.md"))
        return paths[-1] if paths else None

    def latest_context(self, task_id: str) -> dict[str, Any] | None:
        self._check_id(task_id)
        paths = sorted((self.context_dir / task_id).glob("*.json"))
        if not paths:
            return None
        try:
            value = json.loads(paths[-1].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrchestratorError(f"latest context is unreadable for {task_id}") from exc
        return value

    def save_routing(self, task_id: str, decision: dict[str, Any]) -> Path:
        self._check_id(task_id)
        directory = self.routing_dir / task_id
        sequence = len(list(directory.glob("*.json"))) + 1
        path = directory / f"{sequence:03d}.json"
        self._atomic_json(path, decision)
        return path

    def routing_history(self, task_id: str) -> list[dict[str, Any]]:
        self._check_id(task_id)
        records = []
        for path in sorted((self.routing_dir / task_id).glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                value["_path"] = str(path)
                records.append(value)
        return records

    def latest_routing(self, task_id: str) -> dict[str, Any] | None:
        records = self.routing_history(task_id)
        return records[-1] if records else None

    def events(self, task_id: str) -> list[dict[str, Any]]:
        self._check_id(task_id)
        path = self.logs_dir / "events.jsonl"
        if not path.is_file():
            return []
        records = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("task_id") == task_id:
                records.append(value)
        return records

    def log_event(self, task_id: str, event: str, details: dict[str, Any] | None = None) -> None:
        self._check_id(task_id)
        entry = {"timestamp": utc_now(), "task_id": task_id, "event": event, "details": details or {}}
        path = self.logs_dir / "events.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle, fcntl.LOCK_UN)

    @contextmanager
    def task_lock(self, task_id: str) -> Iterator[None]:
        self._check_id(task_id)
        path = self.logs_dir / f"{task_id}.lock"
        with path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
