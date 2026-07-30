from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

from test_ai_tools import FakeAICANape

from agent2canape.mcp_server import create_server


@unittest.skipUnless(importlib.util.find_spec("mcp"), "需要可选依赖 mcp")
class MCPServerTests(unittest.TestCase):
    def test_tool_discovery_has_manifest_and_engineering_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(
                canape=FakeAICANape(),
                approval_file=Path(directory) / "approvals.json",
            )
            tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertIn("agent2canape_tool_manifest", names)
        self.assertIn("agent2canape_plan_natural_language", names)
        self.assertIn("agent2canape_calibration_read", names)
        self.assertIn("agent2canape_calibration_write", names)
        self.assertIn("agent2canape_calibration_change_set_status", names)
        self.assertIn("agent2canape_calibration_pareto_analyze", names)
        self.assertIn("agent2canape_flash_start", names)
        self.assertGreaterEqual(len(names), 25)
        calibration_write = next(
            tool for tool in tools if tool.name == "agent2canape_calibration_write"
        )
        self.assertIn("device", calibration_write.inputSchema["properties"])
        self.assertIn("value", calibration_write.inputSchema["properties"])
        self.assertIn("device", calibration_write.inputSchema["required"])

    def test_in_process_read_only_tool_call(self):
        server = create_server(canape=FakeAICANape())
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


if __name__ == "__main__":
    unittest.main()
