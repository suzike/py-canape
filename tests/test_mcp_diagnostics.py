from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent2canape.cli import main
from agent2canape.mcp_diagnostics import (
    MCPDiagnosticReport,
    run_mcp_diagnostics,
)


class FakeCANape:
    def __init__(self):
        self.connected = False
        self.opened = None
        self.quit_called = False

    def open(self, project):
        self.connected = True
        self.opened = str(project)

    def get_project_info(self):
        return {
            "working_directory": self.opened,
            "cna_filename": "test.cna",
            "application_name": "CANape",
        }

    def list_devices(self):
        return []

    def quit(self, *, non_modal=False):
        self.connected = False
        self.quit_called = True


class MCPDiagnosticsTests(unittest.TestCase):
    def test_reports_ready_clients_without_exposing_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            settings = root / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_BASE_URL": "https://api.example.test/",
                            "ANTHROPIC_AUTH_TOKEN": "must-not-leak",
                        }
                    }
                ),
                encoding="utf-8",
            )

            def runner(command, timeout):
                del timeout
                if "codex" in Path(command[0]).name.casefold():
                    return (
                        0,
                        json.dumps(
                            {
                                "enabled": True,
                                "startup_timeout_sec": 120,
                                "tool_timeout_sec": 120,
                                "transport": {
                                    "env_vars": ["WINDIR", "SYSTEMROOT"]
                                },
                            }
                        ),
                        "",
                    )
                return 0, "Status: Connected", ""

            report = run_mcp_diagnostics(
                project=project,
                environment={"WINDIR": "C:/Windows", "SYSTEMROOT": "C:/Windows"},
                command_runner=runner,
                claude_settings_file=settings,
                platform_name="nt",
            )

        data = report.to_dict()
        serialized = json.dumps(data)
        self.assertTrue(report.passed)
        self.assertNotIn("must-not-leak", serialized)
        provider = next(
            item for item in data["checks"] if item["name"] == "client:claude_provider"
        )
        self.assertEqual(provider["status"], "warning")
        self.assertEqual(provider["evidence"]["host"], "api.example.test")
        self.assertTrue(provider["evidence"]["has_authentication"])

    def test_codex_configuration_gaps_are_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)

            def runner(command, timeout):
                del timeout
                if "codex" in Path(command[0]).name.casefold():
                    return (
                        0,
                        json.dumps(
                            {
                                "enabled": True,
                                "transport": {"env_vars": []},
                            }
                        ),
                        "",
                    )
                return 0, "Connected", ""

            report = run_mcp_diagnostics(
                project=project,
                environment={"WINDIR": "x", "SYSTEMROOT": "x"},
                command_runner=runner,
                claude_settings_file=project / "missing.json",
                platform_name="nt",
            )

        codex = next(check for check in report.checks if check.name == "client:codex")
        self.assertEqual(codex.status, "warning")
        self.assertIn("WINDIR", codex.evidence["missing_environment"])
        self.assertEqual(codex.evidence["tool_timeout_sec"], 0)
        self.assertTrue(report.passed)

    def test_missing_windows_environment_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_mcp_diagnostics(
                project=directory,
                check_clients=False,
                environment={},
                platform_name="nt",
            )
        statuses = {check.name: check.status for check in report.checks}
        self.assertEqual(statuses["environment:WINDIR"], "failed")
        self.assertEqual(statuses["environment:SYSTEMROOT"], "failed")
        self.assertFalse(report.passed)

    def test_live_canape_read_only_check(self):
        instances = []

        def factory():
            instance = FakeCANape()
            instances.append(instance)
            return instance

        with tempfile.TemporaryDirectory() as directory:
            report = run_mcp_diagnostics(
                project=directory,
                check_clients=False,
                live_canape=True,
                environment={"WINDIR": "x", "SYSTEMROOT": "x"},
                canape_factory=factory,
                platform_name="nt",
            )

        check = next(
            item for item in report.checks if item.name == "canape:live_read_only"
        )
        self.assertEqual(check.status, "passed")
        self.assertEqual(check.evidence["device_count"], 0)
        self.assertTrue(instances[0].quit_called)

    def test_live_canape_retries_known_transient_startup_failure(self):
        calls = 0

        class TransientCANape(FakeCANape):
            def open(self, project):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("Invalid Asap3 Handle")
                super().open(project)

        with tempfile.TemporaryDirectory() as directory:
            report = run_mcp_diagnostics(
                project=directory,
                check_clients=False,
                live_canape=True,
                environment={"WINDIR": "x", "SYSTEMROOT": "x"},
                canape_factory=TransientCANape,
                platform_name="nt",
                live_retry_delay_seconds=0,
            )

        check = next(
            item for item in report.checks if item.name == "canape:live_read_only"
        )
        self.assertEqual(check.status, "passed")
        self.assertEqual(check.evidence["attempts"], 2)
        self.assertTrue(check.evidence["recovered_transient_startup"])

    def test_cli_mcp_doctor_without_client_or_live_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "WINDIR": "C:/Windows",
                "SYSTEMROOT": "C:/Windows",
            }
            output = StringIO()
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("agent2canape.mcp_diagnostics.os.name", "nt"),
                redirect_stdout(output),
            ):
                exit_code = main(
                    ["mcp-doctor", "--project", directory, "--skip-clients"]
                )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["passed"])
        self.assertEqual(result["summary"]["failed"], 0)

    def test_cli_mcp_doctor_keeps_json_clean_when_runtime_prints_noise(self):
        report = MCPDiagnosticReport(
            passed=True,
            package_version="test",
            platform="test",
            checks=(),
        )

        def noisy_diagnostics(**kwargs):
            del kwargs
            print("COM runtime noise")
            return report

        output = StringIO()
        with (
            patch(
                "agent2canape.mcp_diagnostics.run_mcp_diagnostics",
                side_effect=noisy_diagnostics,
            ),
            redirect_stdout(output),
        ):
            exit_code = main(["mcp-doctor", "--skip-clients"])

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertNotIn("COM runtime noise", output.getvalue())
        check = next(
            item
            for item in result["checks"]
            if item["name"] == "runtime:out_of_band_output"
        )
        self.assertEqual(check["evidence"]["suppressed_line_count"], 1)


if __name__ == "__main__":
    unittest.main()
