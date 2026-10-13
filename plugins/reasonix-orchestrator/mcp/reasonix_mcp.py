#!/usr/bin/env python3
"""Local stdio MCP server for bounded Reasonix delegation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import uuid
from typing import Any, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reasonix_process import guarded_run, ProcessFailure, CLEANUP_SECONDS
from reasonix_engine import routing, WORKER_MODELS, ESCALATION_REASONS

SERVER_NAME = "reasonix-orchestrator"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2025-06-18"
MAX_ERROR_CHARS = 20_000
EXECUTION_GUARD_VERSION = 2
DEFAULT_WORKER_TIMEOUT_SECONDS = 90
WRAPPER_OVERHEAD_SECONDS = 1
OUTER_CLEANUP_SECONDS = CLEANUP_SECONDS + 2
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = (
    PLUGIN_ROOT
    / "skills"
    / "reasonix-orchestrator"
    / "scripts"
    / "reasonix_delegate.py"
)

COMMON_PROPERTIES: dict[str, Any] = {
    "dir": {
        "type": "string",
        "minLength": 1,
        "description": "Absolute or target-local project directory.",
    },
    "host": {
        "type": "string",
        "minLength": 1,
        "description": "Explicit SSH host or configured alias. Omit for this Mac.",
    },
    "timeout_seconds": {
        "type": "integer",
        "minimum": 1,
        "maximum": 7200,
    },
}

RUN_PROPERTIES: dict[str, Any] = {
    **COMMON_PROPERTIES,
    "task_id": {"type": "string", "minLength": 1, "maxLength": 4096,
                "description": "Reuse the same objective ID across CLI/MCP continuations."},
    "task_contract": {
        "type": "string",
        "minLength": 1,
        "maxLength": 524288,
        "description": "Complete bounded worker contract passed to Reasonix on stdin.",
    },
    "worker_tier": {
        "type": "string",
        "enum": ["flash", "pro"],
        "default": "flash",
        "description": "Use Flash by default; Pro requires a bounded escalation reason.",
    },
    "escalation_reason": {
        "type": "string",
        "enum": [
            "explicit_request",
            "high_complexity",
            "flash_verification_failed"
        ],
        "description": "Required only for a Pro worker.",
    },
    "effort": {
        "type": "string",
        "minLength": 1,
        "description": "Optional effort value accepted by the configured provider.",
    },
    "max_steps": {
        "type": "integer",
        "minimum": 1,
        "maximum": 200,
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "probe",
        "title": "Probe Reasonix",
        "description": (
            "Check that a project directory exists and Reasonix is available on this Mac "
            "or an explicitly named SSH host."
        ),
        "inputSchema": {
            "type": "object",
            "properties": COMMON_PROPERTIES,
            "required": ["dir"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    },
    {
        "name": "delegate_read",
        "title": "Delegate Read-Only Work",
        "description": (
            "Run a bounded Reasonix worker in fail-closed read mode on this Mac or an "
            "explicitly named SSH host."
        ),
        "inputSchema": {
            "type": "object",
            "properties": RUN_PROPERTIES,
            "required": ["dir", "task_contract"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
    {
        "name": "delegate_write",
        "title": "Delegate Authorized Writes",
        "description": (
            "Run a bounded Reasonix worker with file-write permission. The caller must "
            "explicitly set authorize_writes to true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **RUN_PROPERTIES,
                "authorize_writes": {
                    "type": "boolean",
                    "const": True,
                    "description": "Explicit confirmation that this task may modify files.",
                },
            },
            "required": ["dir", "task_contract", "authorize_writes"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
]


class McpError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpError(-32602, f"{label} must be an object")
    return value


def require_string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise McpError(-32602, f"{name} must be a non-empty string")
    return value


def bounded_error(text: str) -> str:
    if len(text) <= MAX_ERROR_CHARS:
        return text
    return text[:MAX_ERROR_CHARS] + "\n...[truncated by reasonix-orchestrator MCP]"


def append_optional(argv: list[str], arguments: dict[str, Any], name: str) -> None:
    if name not in arguments:
        return
    value = arguments[name]
    if name in {"timeout_seconds", "max_steps"}:
        if not isinstance(value, int) or isinstance(value, bool):
            raise McpError(-32602, f"{name} must be an integer")
        minimum, maximum = (1, 7200) if name == "timeout_seconds" else (1, 200)
        if not minimum <= value <= maximum:
            raise McpError(-32602, f"{name} must be between {minimum} and {maximum}")
    elif not isinstance(value, str) or not value.strip():
        raise McpError(-32602, f"{name} must be a non-empty string")
    argv.extend([f"--{name.replace('_', '-')}", str(value)])


def worker_routing(arguments: dict[str, Any]) -> tuple[str, str | None]:
    try:
        return routing(arguments.get("worker_tier", "flash"), arguments.get("escalation_reason"))
    except ValueError as exc:
        raise McpError(-32602, str(exc)) from exc


def wrapper_command(tool_name: str, arguments: dict[str, Any]) -> tuple[list[str], str | None]:
    directory = require_string(arguments, "dir")
    argv = [sys.executable, str(WRAPPER_PATH)]
    stdin_text: str | None = None

    if tool_name == "probe":
        argv.extend(["probe", "--dir", directory])
        append_optional(argv, arguments, "host")
        append_optional(argv, arguments, "timeout_seconds")
        return argv, stdin_text

    if tool_name not in {"delegate_read", "delegate_write"}:
        raise McpError(-32602, f"unknown tool: {tool_name}")

    stdin_text = require_string(arguments, "task_contract")
    worker_model, _ = worker_routing(arguments)
    mode = "read" if tool_name == "delegate_read" else "write"
    argv.extend(["run", "--mode", mode, "--dir", directory, "--model", worker_model])
    if tool_name == "delegate_write":
        if arguments.get("authorize_writes") is not True:
            raise McpError(-32602, "delegate_write requires authorize_writes=true")
        argv.append("--authorize-writes")
    for name in ("host", "effort", "max_steps", "timeout_seconds", "task_id", "escalation_reason"):
        append_optional(argv, arguments, name)
    if "timeout_seconds" not in arguments:
        argv.extend(["--timeout-seconds", str(DEFAULT_WORKER_TIMEOUT_SECONDS)])
    return argv, stdin_text


def call_tool(name: str, arguments: Any) -> dict[str, Any]:
    values = dict(require_object(arguments, "arguments"))
    allowed = set(COMMON_PROPERTIES)
    if name != "probe":
        allowed.update(RUN_PROPERTIES)
    if name == "delegate_write":
        allowed.add("authorize_writes")
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise McpError(-32602, f"unexpected argument(s): {', '.join(unknown)}")

    if name != "probe" and "task_id" not in values:
        values["task_id"] = str(uuid.uuid4())
    argv, stdin_text = wrapper_command(name, values)
    routing: dict[str, Any] | None = None
    if name != "probe":
        worker_model, escalation_reason = worker_routing(values)
        routing = {
            "worker_tier": values.get("worker_tier", "flash"),
            "model": worker_model,
            "escalation_reason": escalation_reason,
        }
    try:
        timeout_seconds = values.get("timeout_seconds", 20 if name == "probe" else DEFAULT_WORKER_TIMEOUT_SECONDS)
        completed = guarded_run(
            argv,
            input=stdin_text,
            timeout=timeout_seconds + CLEANUP_SECONDS + WRAPPER_OVERHEAD_SECONDS,
            cleanup_seconds=OUTER_CLEANUP_SECONDS,
            term_grace=CLEANUP_SECONDS + .5,
        )
    except ProcessFailure as exc:
        payload = {
            "ok": False, "operation": name, "stage": "wrapper",
            "error": f"Reasonix wrapper {exc.kind}", "error_code": exc.kind.upper(),
            "exit_code": exc.exit_code,
            "stdout": bounded_error(exc.stdout), "stderr": bounded_error(exc.stderr),
            "process": {**exc.process, "remote_status": "unknown" if values.get("host") else "not_applicable"},
        }
    except OSError as exc:
        payload = {"ok": False, "error": f"failed to launch Reasonix wrapper: {exc}"}
    else:
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = {
                "ok": False,
                "error": "Reasonix wrapper returned malformed JSON",
                "exit_code": completed.returncode,
                "stderr": bounded_error(completed.stderr.strip()),
            }
        if not isinstance(payload, dict):
            payload = {
                "ok": False,
                "error": "Reasonix wrapper returned a non-object result",
                "exit_code": completed.returncode,
                "stderr": bounded_error(completed.stderr.strip()),
            }
        elif completed.returncode != 0 and payload.get("ok") is True:
            payload["ok"] = False
            payload["error"] = "Reasonix wrapper exited nonzero despite reporting success"
            payload["exit_code"] = completed.returncode
            payload.setdefault("stderr", bounded_error(completed.stderr.strip()))

    if routing is not None:
        payload["routing"] = routing
    if payload.get("ok") is not True and not isinstance(payload.get("failure"), dict):
        payload["failure"] = {
            "code": str(payload.get("error_code", "WRAPPER_FAILURE"))[:128],
            "stage": str(payload.get("stage", "wrapper_result"))[:128],
            "message": str(payload.get("error", "Reasonix wrapper failed"))[:4000],
        }
    if name != "probe" and "task_id" not in payload:
        # An outer wrapper stop is not evidence that its separately grouped
        # worker stopped. Preserve correlation and report the uncertainty.
        payload["task_id"] = values["task_id"]
        payload["outcome"] = "partial" if payload.get("ok") is True else "unknown"
        payload["scope"] = {"operation": "read" if name == "delegate_read" else "write",
                            "target": values["dir"], "host": values.get("host"),
                            "inspected_files": "unknown", "changed_files": "unknown", "deviations": "unknown"}
        payload["changes"] = []
        payload["verification"] = {"status": "not_run", "checks": []}
        payload.setdefault("process", {})["status"] = "unknown"
        payload["unresolved"] = ["Worker termination and supervisor acceptance are not established."]
        payload["risks"] = ["Outer wrapper cleanup does not prove separately grouped worker termination."]
        payload["usage"] = {"model": routing["model"], "effort": values.get("effort", "unknown"),
                            "attempts": "unknown", "tokens": "unknown", "cost": "unknown",
                            "measurement_source": "unknown"}
    payload["execution_guard_version"] = EXECUTION_GUARD_VERSION
    payload["consolidation_version"] = 1

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": encoded}],
        "structuredContent": payload,
        "isError": payload.get("ok") is not True,
    }


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:
        return None

    if method == "initialize":
        params = require_object(message.get("params", {}), "params")
        requested_version = params.get("protocolVersion")
        protocol_version = (
            requested_version
            if requested_version == PROTOCOL_VERSION
            else PROTOCOL_VERSION
        )
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = require_object(message.get("params"), "params")
        name = params.get("name")
        if not isinstance(name, str):
            raise McpError(-32602, "tool name must be a string")
        result = call_tool(name, params.get("arguments", {}))
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    raise McpError(-32601, f"method not found: {method}")


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    for line in stdin:
        if not line.strip():
            continue
        request_id: Any = None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise McpError(-32600, "request must be an object")
            request_id = message.get("id")
            response = handle_request(message)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "parse error"},
            }
        except McpError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": exc.code, "message": str(exc)},
            }
        except Exception as exc:
            print(f"reasonix-orchestrator MCP error: {exc}", file=sys.stderr)
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "internal error"},
            }
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()
            result = response.get("result", {})
            if isinstance(result, dict) and result.get("structuredContent", {}).get("error_code") == "CANCELLED":
                return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
