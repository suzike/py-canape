"""py-canape 命令行入口。"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .assets import AssetManager
from .canape import CANape
from .capabilities import CapabilityRegistry
from .platform import EngineeringPlatform
from .workflow import WorkflowEngine


def _check() -> int:
    result: dict[str, object] = {
        "package_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "com_progid": CANape.PROG_ID,
    }
    try:
        with CANape() as canape:
            try:
                canape.connect()
                result["com_available"] = True
                result["com_api_version"] = str(canape.get_com_api_version_info())
                result["application_name"] = canape.application.Name
            finally:
                if canape.connected:
                    canape.quit()
                    # CANape 17 的进程退出和共享内存释放晚于 Quit 返回。
                    # 若下一条命令立即 Open2，会偶发 memory mapped file 错误。
                    time.sleep(2.0)
    except Exception as exc:
        result["com_available"] = False
        result["error"] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["com_available"] else 1


def _project_info(project: str) -> int:
    with CANape() as canape:
        try:
            canape.open(project)
            data = {
                **canape.get_project_info(),
                "canape_version": str(canape.get_canape_version_info()),
                "com_api_version": str(canape.get_com_api_version_info()),
                "devices": [asdict(device) for device in canape.list_devices()],
            }
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(
                json.dumps(
                    {"project": project, "status": "failed", "error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        finally:
            if canape.connected:
                canape.quit()


def _capabilities() -> int:
    matrix = Path(__file__).resolve().parents[2] / "CAPABILITIES.md"
    registry = CapabilityRegistry.from_markdown(matrix)
    validation = registry.validate()
    print(
        json.dumps(
            {
                **validation,
                "capabilities": [asdict(item) for item in registry.list()],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if validation["passed"] else 1


def _preflight(paths: list[str], output: str | None, free_bytes: int) -> int:
    result = AssetManager().preflight(
        required_paths=paths,
        output_directory=output,
        minimum_free_bytes=free_bytes,
    )
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2, default=str))
    return 0 if result.passed else 1


def _workflow_validate(path: str) -> int:
    definition = WorkflowEngine.load(path)
    data = {
        "status": "valid",
        "steps": len(definition["steps"]),
        "actions": [step.get("action") for step in definition["steps"]],
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def _workflow_run(
    path: str,
    *,
    dry_run: bool,
    checkpoint: str | None,
    resume: bool,
) -> int:
    definition = WorkflowEngine.load(path)
    platform_api = EngineeringPlatform()
    platform_api.register_default_workflow_actions()
    result = platform_api.workflow.execute(
        definition,
        dry_run=dry_run,
        checkpoint=checkpoint,
        resume=resume,
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "steps": [asdict(item) for item in result.steps],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    return 0 if result.status == "passed" else 1


def _asset_manifest(paths: list[str], output: str) -> int:
    manifest = AssetManager().create_manifest(paths, output)
    print(
        json.dumps(
            {
                "status": "passed",
                "output": str(Path(output).resolve()),
                "asset_count": len(manifest["assets"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="py-canape", description="Vector CANape COM 控制与诊断工具"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="检查 CANape COM 注册与版本")
    subparsers.add_parser("capabilities", help="验证并列出 140 项能力注册表")
    info = subparsers.add_parser("project-info", help="只读打开项目并列出设备")
    info.add_argument("project", type=str)
    preflight = subparsers.add_parser("preflight", help="检查工程路径和输出目录")
    preflight.add_argument("paths", nargs="*", type=str)
    preflight.add_argument("--output", type=str)
    preflight.add_argument("--minimum-free-bytes", type=int, default=0)
    workflow = subparsers.add_parser("workflow-validate", help="验证 YAML/JSON 工作流")
    workflow.add_argument("path", type=str)
    run = subparsers.add_parser("workflow-run", help="执行通用工程工作流")
    run.add_argument("path", type=str)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--checkpoint", type=str)
    run.add_argument("--resume", action="store_true")
    manifest = subparsers.add_parser("asset-manifest", help="生成工程资产哈希清单")
    manifest.add_argument("paths", nargs="+", type=str)
    manifest.add_argument("--output", required=True, type=str)
    args = parser.parse_args(argv)

    if args.command == "check":
        return _check()
    if args.command == "project-info":
        return _project_info(args.project)
    if args.command == "capabilities":
        return _capabilities()
    if args.command == "preflight":
        return _preflight(args.paths, args.output, args.minimum_free_bytes)
    if args.command == "workflow-validate":
        return _workflow_validate(args.path)
    if args.command == "workflow-run":
        return _workflow_run(
            args.path,
            dry_run=args.dry_run,
            checkpoint=args.checkpoint,
            resume=args.resume,
        )
    if args.command == "asset-manifest":
        return _asset_manifest(args.paths, args.output)
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
