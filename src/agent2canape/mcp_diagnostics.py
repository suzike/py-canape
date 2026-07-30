"""Codex、Claude Code、MCP 与 CANape 的分层诊断。"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from . import __version__
from .canape import CANape

CheckStatus = Literal["passed", "warning", "failed", "skipped"]


@dataclass(frozen=True)
class MCPDiagnosticCheck:
    """单项诊断结果，不包含凭据值。"""

    name: str
    status: CheckStatus
    message: str
    remediation: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPDiagnosticReport:
    """机器可读的 MCP 诊断报告。"""

    passed: bool
    package_version: str
    platform: str
    checks: tuple[MCPDiagnosticCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "package_version": self.package_version,
            "platform": self.platform,
            "summary": {
                status: sum(check.status == status for check in self.checks)
                for status in ("passed", "warning", "failed", "skipped")
            },
            "checks": [asdict(check) for check in self.checks],
        }


CommandRunner = Callable[[Sequence[str], float], tuple[int, str, str]]


def _run_command(command: Sequence[str], timeout: float) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            list(command),
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", str(exc)


def _dependency_check(module: str, label: str) -> MCPDiagnosticCheck:
    available = importlib.util.find_spec(module) is not None
    return MCPDiagnosticCheck(
        name=f"dependency:{module}",
        status="passed" if available else "failed",
        message=f"{label}{'已安装' if available else '缺失'}",
        remediation=(
            ""
            if available
            else '运行 python -m pip install -e ".[all]" 后重试'
        ),
        evidence={"available": available},
    )


def _windows_environment_checks(
    environment: Mapping[str, str],
) -> list[MCPDiagnosticCheck]:
    checks = []
    for name in ("WINDIR", "SYSTEMROOT"):
        present = bool(environment.get(name))
        checks.append(
            MCPDiagnosticCheck(
                name=f"environment:{name}",
                status="passed" if present else "failed",
                message=f"{name}{'已传入 MCP 子进程' if present else '缺失'}",
                remediation=(
                    ""
                    if present
                    else f"在 MCP 客户端配置中继承或设置 {name}"
                ),
                evidence={"present": present},
            )
        )
    return checks


def _project_check(project: str | Path | None) -> MCPDiagnosticCheck:
    if not project:
        return MCPDiagnosticCheck(
            name="canape:default_project",
            status="warning",
            message="未配置默认 CANape 工程",
            remediation=(
                "设置 AGENT2CANAPE_DEFAULT_PROJECT，或先审批并调用 project_open"
            ),
        )
    path = Path(project).expanduser().resolve()
    exists = path.exists()
    valid = exists and (path.is_dir() or path.suffix.casefold() == ".cna")
    return MCPDiagnosticCheck(
        name="canape:default_project",
        status="passed" if valid else "failed",
        message="默认 CANape 工程有效" if valid else "默认 CANape 工程不存在或格式错误",
        remediation="" if valid else "检查工程目录或 .cna 文件路径",
        evidence={"path": str(path), "exists": exists},
    )


def _codex_check(
    runner: CommandRunner,
    *,
    discover_executable: bool,
) -> MCPDiagnosticCheck:
    executable = (
        shutil.which("codex.cmd") or shutil.which("codex")
        if discover_executable
        else "codex"
    )
    if not executable:
        return MCPDiagnosticCheck(
            name="client:codex",
            status="skipped",
            message="未找到 Codex CLI",
            remediation="安装 Codex CLI 后执行 mcp-doctor",
        )
    code, stdout, _stderr = runner(
        (executable, "mcp", "get", "Agent2Canape", "--json"),
        20.0,
    )
    if code != 0:
        return MCPDiagnosticCheck(
            name="client:codex",
            status="failed",
            message="Codex 未注册或无法读取 Agent2Canape",
            remediation="使用 codex mcp add 注册 Agent2Canape",
            evidence={"exit_code": code},
        )
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return MCPDiagnosticCheck(
            name="client:codex",
            status="failed",
            message="Codex MCP 配置输出不是有效 JSON",
            remediation="运行 codex mcp get Agent2Canape --json 检查配置",
        )
    transport = data.get("transport", {})
    inherited = {str(item).casefold() for item in transport.get("env_vars", [])}
    missing_environment = sorted(
        name for name in ("WINDIR", "SYSTEMROOT") if name.casefold() not in inherited
    )
    startup_timeout = float(data.get("startup_timeout_sec") or 0)
    tool_timeout = float(data.get("tool_timeout_sec") or 0)
    issues = []
    if not data.get("enabled"):
        issues.append("server disabled")
    if missing_environment:
        issues.append("missing " + ", ".join(missing_environment))
    if startup_timeout < 60:
        issues.append("startup timeout below 60s")
    if tool_timeout < 60:
        issues.append("tool timeout below 60s")
    return MCPDiagnosticCheck(
        name="client:codex",
        status="passed" if not issues else "warning",
        message="Codex MCP 配置就绪" if not issues else "Codex MCP 配置需要增强",
        remediation=(
            ""
            if not issues
            else "继承 WINDIR/SYSTEMROOT，并把启动与工具超时设置为至少60秒"
        ),
        evidence={
            "enabled": bool(data.get("enabled")),
            "missing_environment": missing_environment,
            "startup_timeout_sec": startup_timeout,
            "tool_timeout_sec": tool_timeout,
        },
    )


def _claude_check(
    runner: CommandRunner,
    *,
    discover_executable: bool,
) -> MCPDiagnosticCheck:
    executable = (
        shutil.which("claude.exe") or shutil.which("claude.cmd")
        if discover_executable
        else "claude"
    )
    if not executable:
        return MCPDiagnosticCheck(
            name="client:claude",
            status="skipped",
            message="未找到 Claude Code CLI",
            remediation="安装 Claude Code 后执行 mcp-doctor",
        )
    code, stdout, _stderr = runner(
        (executable, "mcp", "get", "Agent2Canape"),
        30.0,
    )
    connected = code == 0 and "connected" in stdout.casefold()
    return MCPDiagnosticCheck(
        name="client:claude",
        status="passed" if connected else "failed",
        message="Claude Code MCP 已连接" if connected else "Claude Code MCP 未连接",
        remediation=(
            ""
            if connected
            else "使用 claude mcp add 注册 Agent2Canape，并检查 stdio 命令"
        ),
        evidence={"connected": connected, "exit_code": code},
    )


def _claude_provider_check(settings_file: str | Path | None) -> MCPDiagnosticCheck:
    path = (
        Path(settings_file).expanduser()
        if settings_file
        else Path.home() / ".claude" / "settings.json"
    )
    if not path.is_file():
        return MCPDiagnosticCheck(
            name="client:claude_provider",
            status="skipped",
            message="未找到 Claude Code settings.json",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return MCPDiagnosticCheck(
            name="client:claude_provider",
            status="warning",
            message="无法解析 Claude Code settings.json",
            remediation="检查 settings.json 语法",
        )
    environment = data.get("env", {})
    base_url = str(environment.get("ANTHROPIC_BASE_URL") or "")
    host = urlparse(base_url).hostname or ""
    if not host:
        return MCPDiagnosticCheck(
            name="client:claude_provider",
            status="passed",
            message="Claude Code 使用默认模型端点",
            evidence={"third_party": False},
        )
    official = host.casefold() in {"api.anthropic.com", "claude.ai"}
    return MCPDiagnosticCheck(
        name="client:claude_provider",
        status="passed" if official else "warning",
        message=(
            "Claude Code 使用官方模型端点"
            if official
            else "Claude Code 使用第三方模型端点，需单独验证工具协议"
        ),
        remediation=(
            ""
            if official
            else "确认端点支持 tool_use；延迟工具发现还需支持 tool_reference"
        ),
        evidence={
            "host": host,
            "third_party": not official,
            "has_authentication": bool(environment.get("ANTHROPIC_AUTH_TOKEN")),
        },
    )


def _live_canape_check(
    project: str | Path | None,
    canape_factory: Callable[[], CANape],
    retry_delay_seconds: float,
) -> MCPDiagnosticCheck:
    if not project:
        return MCPDiagnosticCheck(
            name="canape:live_read_only",
            status="failed",
            message="现场 CANape 测试需要默认工程",
            remediation="通过 --project 或 AGENT2CANAPE_DEFAULT_PROJECT 指定工程",
        )
    last_error = ""
    transient_markers = (
        "invalid asap3 handle",
        "memory mapped file",
    )
    for attempt in range(1, 3):
        canape = canape_factory()
        try:
            canape.open(project)
            project_info = canape.get_project_info()
            return MCPDiagnosticCheck(
                name="canape:live_read_only",
                status="passed",
                message="CANape 只读工程调用成功",
                evidence={
                    "project_info": project_info,
                    "device_count": len(canape.list_devices()),
                    "attempts": attempt,
                    "recovered_transient_startup": attempt > 1,
                },
            )
        except Exception as exc:
            last_error = str(exc)
            transient = any(
                marker in last_error.casefold() for marker in transient_markers
            )
            if attempt == 1 and transient:
                time.sleep(max(0.0, retry_delay_seconds))
                continue
            break
        finally:
            if canape.connected:
                canape.quit(non_modal=True)
    return MCPDiagnosticCheck(
        name="canape:live_read_only",
        status="failed",
        message="CANape 只读工程调用失败",
        remediation="检查授权、工程路径、COM 环境和客户端超时",
        evidence={"error": last_error, "attempts": attempt},
    )


def run_mcp_diagnostics(
    *,
    project: str | Path | None = None,
    check_clients: bool = True,
    live_canape: bool = False,
    environment: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
    canape_factory: Callable[[], CANape] = CANape,
    claude_settings_file: str | Path | None = None,
    platform_name: str | None = None,
    live_retry_delay_seconds: float = 2.0,
) -> MCPDiagnosticReport:
    """执行分层检查；默认不启动 CANape，不输出任何凭据。"""

    current_environment = dict(os.environ if environment is None else environment)
    configured_project = project or current_environment.get(
        "AGENT2CANAPE_DEFAULT_PROJECT"
    )
    checks: list[MCPDiagnosticCheck] = [
        _dependency_check("mcp", "MCP SDK"),
        _dependency_check("pythoncom", "pywin32 COM"),
        _project_check(configured_project),
    ]
    current_platform_name = platform_name or os.name
    if current_platform_name == "nt":
        checks.extend(_windows_environment_checks(current_environment))
    else:
        checks.append(
            MCPDiagnosticCheck(
                name="platform:windows",
                status="failed",
                message="CANape COM 仅支持 Windows",
                evidence={"os_name": current_platform_name},
            )
        )

    if check_clients:
        runner = command_runner or _run_command
        discover_executable = command_runner is None
        checks.extend(
            (
                _codex_check(runner, discover_executable=discover_executable),
                _claude_check(runner, discover_executable=discover_executable),
                _claude_provider_check(claude_settings_file),
            )
        )
    if live_canape:
        checks.append(
            _live_canape_check(
                configured_project,
                canape_factory,
                live_retry_delay_seconds,
            )
        )

    return MCPDiagnosticReport(
        passed=not any(check.status == "failed" for check in checks),
        package_version=__version__,
        platform=platform.platform(),
        checks=tuple(checks),
    )


def diagnostic_json(report: MCPDiagnosticReport) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
