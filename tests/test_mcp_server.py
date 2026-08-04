from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

from test_ai_tools import FakeAICANape
from test_calibration_formats import A2L_SAMPLE

from agent2canape.mcp_runtime import MCPRuntimeGovernor
from agent2canape.mcp_server import create_server


@unittest.skipUnless(importlib.util.find_spec("mcp"), "需要可选依赖 mcp")
class MCPServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.runtime = MCPRuntimeGovernor(
            session_id="mcp-server-test",
            lock_directory=root / "locks",
            audit_file=root / "audit.jsonl",
            rate_limit=1000,
            lock_timeout=0,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_tool_discovery_has_manifest_and_engineering_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(
                canape=FakeAICANape(),
                approval_file=Path(directory) / "approvals.json",
                runtime_governor=self.runtime,
            )
            tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertIn("agent2canape_tool_manifest", names)
        self.assertIn("agent2canape_runtime_status", names)
        self.assertIn("agent2canape_plan_natural_language", names)
        self.assertIn("agent2canape_a2l_context", names)
        self.assertIn("agent2canape_calibration_read", names)
        self.assertIn("agent2canape_calibration_write", names)
        self.assertIn("agent2canape_calibration_change_set_status", names)
        self.assertIn("agent2canape_calibration_persistence_status", names)
        self.assertIn("agent2canape_calibration_experiment_report", names)
        self.assertIn("agent2canape_calibration_safe_suggest", names)
        self.assertIn("agent2canape_calibration_pareto_analyze", names)
        self.assertIn("agent2canape_flash_start", names)
        self.assertGreaterEqual(len(names), 25)
        calibration_write = next(
            tool for tool in tools if tool.name == "agent2canape_calibration_write"
        )
        self.assertIn("device", calibration_write.inputSchema["properties"])
        self.assertIn("value", calibration_write.inputSchema["properties"])
        self.assertIn("device", calibration_write.inputSchema["required"])

    def test_mcp_builds_filtered_a2l_engineering_context(self):
        root = Path(self.temporary.name)
        a2l = root / "demo.a2l"
        a2l.write_text(A2L_SAMPLE, encoding="latin-1")
        server = create_server(
            canape=FakeAICANape(),
            runtime_governor=self.runtime,
        )

        _, structured = asyncio.run(
            server.call_tool(
                "agent2canape_a2l_context",
                {
                    "a2l_file": str(a2l),
                    "device": "HVAC",
                    "group": "ThermalCalibration",
                    "limit": 10,
                    "dry_run": True,
                    "action_plan_id": "",
                },
            )
        )

        context = structured["result"]
        self.assertEqual(context["default_device"], "HVAC")
        self.assertEqual(context["selection"]["returned_object_count"], 2)
        self.assertEqual(structured["_runtime"]["resource"], None)

    def test_in_process_read_only_tool_call(self):
        server = create_server(
            canape=FakeAICANape(),
            runtime_governor=self.runtime,
        )
        _, structured = asyncio.run(
            server.call_tool(
                "agent2canape_project_info",
                {
                    "dry_run": True,
                    "action_plan_id": "",
                },
            )
        )
        self.assertTrue(structured["executed"])
        self.assertEqual(structured["risk"], "READ_ONLY")
        self.assertEqual(
            structured["_runtime"]["session_id"],
            "mcp-server-test",
        )

    def test_default_project_is_opened_lazily_before_read_only_call(self):
        canape = FakeAICANape()
        server = create_server(
            canape=canape,
            default_project=Path("D:/CANape/Example"),
            runtime_governor=self.runtime,
        )
        self.assertFalse(canape.connected)

        _, structured = asyncio.run(
            server.call_tool(
                "agent2canape_project_info",
                {
                    "dry_run": True,
                    "action_plan_id": "",
                },
            )
        )

        self.assertTrue(structured["executed"])
        self.assertTrue(canape.connected)
        self.assertEqual(canape.path, str(Path("D:/CANape/Example").resolve()))

    def test_tool_allowlist_limits_discovery_manifest_and_planning(self):
        server = create_server(
            canape=FakeAICANape(),
            tool_allowlist="project_info",
            runtime_governor=self.runtime,
        )
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(
            names,
            {
                "agent2canape_tool_manifest",
                "agent2canape_runtime_status",
                "agent2canape_plan_natural_language",
                "agent2canape_project_info",
            },
        )

        _, manifest = asyncio.run(
            server.call_tool("agent2canape_tool_manifest", {})
        )
        self.assertEqual(
            [item["name"] for item in manifest["result"]],
            ["project_info"],
        )

        _, plan = asyncio.run(
            server.call_tool(
                "agent2canape_plan_natural_language",
                {"request": "flash ecu"},
            )
        )
        self.assertEqual(plan["status"], "not_exposed")
        self.assertEqual(plan["tool"], "flash_start")

    def test_mcp_natural_language_plan_uses_engineering_context(self):
        server = create_server(
            canape=FakeAICANape(),
            runtime_governor=self.runtime,
        )
        _, plan = asyncio.run(
            server.call_tool(
                "agent2canape_plan_natural_language",
                {
                    "request": "把目标增益修改为 2.5",
                    "context": {
                        "default_device": "ECU",
                        "reason": "response optimization",
                        "objects": [
                            {
                                "name": "Gain",
                                "aliases": ["目标增益"],
                                "unit": "",
                                "minimum": 0,
                                "maximum": 10,
                            }
                        ],
                    },
                },
            )
        )
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["arguments"]["name"], "Gain")
        self.assertEqual(plan["arguments"]["value"], 2.5)

    def test_mcp_calibration_dry_run_contains_live_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(
                canape=FakeAICANape(),
                approval_file=Path(directory) / "approvals.json",
                runtime_governor=self.runtime,
            )
            _, result = asyncio.run(
                server.call_tool(
                    "agent2canape_calibration_write",
                    {
                        "device": "ECU",
                        "name": "Gain",
                        "value": 2.5,
                        "reason": "response optimization",
                        "dry_run": True,
                    },
                )
            )
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["execution_preview"]["current"]["value"], 1.0)
        self.assertEqual(result["execution_preview"]["target"]["value"], 2.5)

    def test_runtime_status_reports_session_and_call_counts(self):
        server = create_server(
            canape=FakeAICANape(),
            runtime_governor=self.runtime,
        )
        asyncio.run(
            server.call_tool(
                "agent2canape_project_info",
                {"dry_run": True, "action_plan_id": ""},
            )
        )
        _, status = asyncio.run(
            server.call_tool("agent2canape_runtime_status", {})
        )
        self.assertEqual(status["session_id"], "mcp-server-test")
        self.assertEqual(status["completed_calls"], 1)


if __name__ == "__main__":
    unittest.main()
