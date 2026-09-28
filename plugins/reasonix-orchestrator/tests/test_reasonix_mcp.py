from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "mcp" / "reasonix_mcp.py"
SPEC = importlib.util.spec_from_file_location("reasonix_mcp", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
reasonix_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reasonix_mcp)


class ToolContractTests(unittest.TestCase):
    def test_tools_separate_read_and_write_permissions(self) -> None:
        tools = {tool["name"]: tool for tool in reasonix_mcp.TOOLS}
        self.assertEqual(set(tools), {"probe", "delegate_read", "delegate_write"})
        self.assertTrue(tools["probe"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["delegate_read"]["annotations"]["readOnlyHint"])
        self.assertTrue(tools["delegate_write"]["annotations"]["destructiveHint"])
        write_schema = tools["delegate_write"]["inputSchema"]
        self.assertIn("authorize_writes", write_schema["required"])
        self.assertIs(write_schema["properties"]["authorize_writes"]["const"], True)

    def test_write_command_requires_explicit_authorization(self) -> None:
        with self.assertRaisesRegex(reasonix_mcp.McpError, "authorize_writes=true"):
            reasonix_mcp.wrapper_command(
                "delegate_write",
                {"dir": "/tmp/project", "task_contract": "bounded task"},
            )

    def test_write_command_uses_bundled_wrapper_and_stdin(self) -> None:
        argv, stdin_text = reasonix_mcp.wrapper_command(
            "delegate_write",
            {
                "dir": "/tmp/project with spaces",
                "task_contract": "inspect $(whoami); do not execute this as shell",
                "authorize_writes": True,
                "host": "build@mac-mini",
                "max_steps": 9,
            },
        )
        self.assertEqual(argv[0], reasonix_mcp.sys.executable)
        self.assertEqual(Path(argv[1]), reasonix_mcp.WRAPPER_PATH)
        self.assertIn("--authorize-writes", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "deepseek-flash")
        self.assertNotIn(stdin_text, argv)
        self.assertEqual(stdin_text, "inspect $(whoami); do not execute this as shell")

    def test_flash_is_the_default_worker(self) -> None:
        argv, _ = reasonix_mcp.wrapper_command(
            "delegate_read",
            {"dir": "/tmp/project", "task_contract": "bounded task"},
        )
        self.assertEqual(argv[argv.index("--model") + 1], "deepseek-flash")

    def test_pro_requires_a_bounded_escalation_reason(self) -> None:
        with self.assertRaisesRegex(reasonix_mcp.McpError, "requires a valid escalation_reason"):
            reasonix_mcp.wrapper_command(
                "delegate_read",
                {
                    "dir": "/tmp/project",
                    "task_contract": "bounded task",
                    "worker_tier": "pro",
                },
            )

        argv, _ = reasonix_mcp.wrapper_command(
            "delegate_read",
            {
                "dir": "/tmp/project",
                "task_contract": "bounded task",
                "worker_tier": "pro",
                "escalation_reason": "high_complexity",
            },
        )
        self.assertEqual(argv[argv.index("--model") + 1], "deepseek-pro")

    def test_flash_rejects_escalation_reason(self) -> None:
        with self.assertRaisesRegex(reasonix_mcp.McpError, "valid only"):
            reasonix_mcp.wrapper_command(
                "delegate_read",
                {
                    "dir": "/tmp/project",
                    "task_contract": "bounded task",
                    "worker_tier": "flash",
                    "escalation_reason": "high_complexity",
                },
            )

    def test_unknown_arguments_fail_closed(self) -> None:
        with self.assertRaisesRegex(reasonix_mcp.McpError, "unexpected argument"):
            reasonix_mcp.call_tool("probe", {"dir": "/tmp", "command": "rm -rf /"})

    def test_numeric_arguments_enforce_schema_bounds(self) -> None:
        with self.assertRaisesRegex(reasonix_mcp.McpError, "timeout_seconds must be between"):
            reasonix_mcp.wrapper_command("probe", {"dir": "/tmp", "timeout_seconds": 7201})
        with self.assertRaisesRegex(reasonix_mcp.McpError, "max_steps must be between"):
            reasonix_mcp.wrapper_command(
                "delegate_read",
                {"dir": "/tmp", "task_contract": "bounded task", "max_steps": 0},
            )

    @mock.patch.object(reasonix_mcp.subprocess, "run")
    def test_wrapper_json_becomes_structured_tool_result(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok":true,"version":"reasonix v1"}\n', stderr=""
        )
        result = reasonix_mcp.call_tool("probe", {"dir": "/tmp"})
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["version"], "reasonix v1")
        self.assertIsNone(run.call_args.kwargs["input"])

    @mock.patch.object(reasonix_mcp.subprocess, "run")
    def test_result_records_worker_routing(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"ok":true}\n', stderr=""
        )
        result = reasonix_mcp.call_tool(
            "delegate_read",
            {"dir": "/tmp", "task_contract": "bounded task"},
        )
        self.assertEqual(
            result["structuredContent"]["routing"],
            {
                "worker_tier": "flash",
                "model": "deepseek-flash",
                "escalation_reason": None,
            },
        )

    @mock.patch.object(reasonix_mcp.subprocess, "run")
    def test_malformed_wrapper_output_preserves_bounded_stderr(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="not-json", stderr="diagnostic detail"
        )
        result = reasonix_mcp.call_tool("probe", {"dir": "/tmp"})
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["exit_code"], 1)
        self.assertEqual(result["structuredContent"]["stderr"], "diagnostic detail")

    @mock.patch.object(reasonix_mcp.subprocess, "run")
    def test_nonzero_wrapper_exit_cannot_report_success(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout='{"ok":true}\n', stderr="wrapper failed"
        )
        result = reasonix_mcp.call_tool("probe", {"dir": "/tmp"})
        self.assertTrue(result["isError"])
        self.assertFalse(result["structuredContent"]["ok"])
        self.assertEqual(result["structuredContent"]["exit_code"], 1)


class ProtocolTests(unittest.TestCase):
    def test_initialize_counteroffers_supported_version(self) -> None:
        stdout = io.StringIO()
        reasonix_mcp.serve(
            io.StringIO(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2099-01-01"},
                    }
                )
                + "\n"
            ),
            stdout,
        )
        response = json.loads(stdout.getvalue())
        self.assertEqual(response["result"]["protocolVersion"], reasonix_mcp.PROTOCOL_VERSION)

    def test_initialize_and_tools_list(self) -> None:
        stdin = io.StringIO(
            '\n'.join(
                [
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"},
                        }
                    ),
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                ]
            )
            + "\n"
        )
        stdout = io.StringIO()
        self.assertEqual(reasonix_mcp.serve(stdin, stdout), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2])
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "reasonix-orchestrator")
        self.assertEqual(len(responses[1]["result"]["tools"]), 3)

    def test_invalid_json_returns_parse_error(self) -> None:
        stdout = io.StringIO()
        reasonix_mcp.serve(io.StringIO("not-json\n"), stdout)
        response = json.loads(stdout.getvalue())
        self.assertEqual(response["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
