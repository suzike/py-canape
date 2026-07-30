"""Agent2Canape 命令行入口。"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .ai_tools import ApprovalStore, CANapeAIToolkit
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
    registry = CapabilityRegistry.default()
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
    allow_writes: bool,
    checkpoint: str | None,
    resume: bool,
) -> int:
    definition = WorkflowEngine.load(path)
    platform_api = EngineeringPlatform()
    platform_api.register_default_workflow_actions()
    result = platform_api.workflow.execute(
        definition,
        dry_run=dry_run,
        allow_writes=allow_writes,
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


def _a2l_summary(path: str) -> int:
    from .calibration_formats import A2LCatalog

    result = A2LCatalog.parse(path).summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def _calibration_convert(source: str, output: str) -> int:
    from .calibration import CalibrationDataset

    dataset = CalibrationDataset.load(source)
    saved = dataset.save(output)
    result = {
        "status": "passed",
        "source": str(Path(source).resolve()),
        "output": str(saved),
        "parameter_count": len(dataset.parameters),
        "identity": dataset.identity,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _calibration_verify(
    dataset_path: str,
    *,
    a2l: str | None,
    hex_file: str | None,
    expected: str,
) -> int:
    from .calibration import CalibrationDataset, CalibrationIdentity

    dataset = CalibrationDataset.load(dataset_path)
    result = CalibrationIdentity.verify(
        dataset,
        a2l=a2l,
        hex_file=hex_file,
        expected=json.loads(expected),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def _calibration_review(path: str, dataset_path: str | None) -> int:
    from .calibration import CalibrationDataset
    from .calibration_operations import CalibrationChangeSet

    change_set = CalibrationChangeSet.load(path)
    dataset = CalibrationDataset.load(dataset_path) if dataset_path else None
    result = change_set.summary(dataset)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result["errors"] else 1


def _calibration_memory_status(path: str) -> int:
    from .calibration_operations import CalibrationMemoryLedger

    result = CalibrationMemoryLedger.load(path).status()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["persistent"] else 1


def _calibration_experiment_status(path: str) -> int:
    from .calibration_operations import CalibrationExperimentStore

    result = CalibrationExperimentStore.load(path).summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def _calibration_persistence_status(path: str) -> int:
    from .calibration_targets import CalibrationPersistenceJob

    result = CalibrationPersistenceJob.load(path).summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


def _ai_toolkit(approval_file: str | None = None) -> CANapeAIToolkit:
    return CANapeAIToolkit(CANape(), approvals=ApprovalStore(approval_file))


def _ai_tools(approval_file: str | None) -> int:
    print(
        json.dumps(
            _ai_toolkit(approval_file).registry.manifest(),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _ai_plan(
    request: str,
    context: str,
    approval_file: str | None,
) -> int:
    result = _ai_toolkit(approval_file).planner.plan(
        request, context=json.loads(context)
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 1


def _ai_approve(plan_id: str, approver: str, approval_file: str | None) -> int:
    plan = ApprovalStore(approval_file).approve(plan_id, approver)
    print(json.dumps(asdict(plan), ensure_ascii=False, indent=2))
    return 0


def _ai_call(
    tool: str,
    arguments: str,
    *,
    execute: bool,
    action_plan_id: str,
    approval_file: str | None,
) -> int:
    result = _ai_toolkit(approval_file).registry.invoke(
        tool,
        json.loads(arguments),
        dry_run=not execute,
        action_plan_id=action_plan_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="Agent2Canape", description="Vector CANape COM 控制与诊断工具"
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
    run.add_argument(
        "--allow-writes",
        action="store_true",
        help="显式允许执行工作流中标记为写操作的步骤",
    )
    run.add_argument("--checkpoint", type=str)
    run.add_argument("--resume", action="store_true")
    manifest = subparsers.add_parser("asset-manifest", help="生成工程资产哈希清单")
    manifest.add_argument("paths", nargs="+", type=str)
    manifest.add_argument("--output", required=True, type=str)
    a2l_summary = subparsers.add_parser("a2l-summary", help="解析并校验 A2L 语义目录")
    a2l_summary.add_argument("path", type=str)
    calibration_convert = subparsers.add_parser(
        "calibration-convert", help="转换 JSON/CSV/CDFX/DCM/PAR 标定数据集"
    )
    calibration_convert.add_argument("source", type=str)
    calibration_convert.add_argument("output", type=str)
    calibration_verify = subparsers.add_parser(
        "calibration-verify", help="校验数据集与 A2L/HEX/软件标识的一致性"
    )
    calibration_verify.add_argument("dataset", type=str)
    calibration_verify.add_argument("--a2l", type=str)
    calibration_verify.add_argument("--hex", dest="hex_file", type=str)
    calibration_verify.add_argument("--expected", default="{}", type=str)
    calibration_review = subparsers.add_parser(
        "calibration-review", help="查看标定变更集的功能组、页面、责任人和审批状态"
    )
    calibration_review.add_argument("path", type=str)
    calibration_review.add_argument("--dataset", type=str)
    memory_status = subparsers.add_parser(
        "calibration-memory-status", help="检查 working/RAM/ROM 持久化状态"
    )
    memory_status.add_argument("path", type=str)
    experiment_status = subparsers.add_parser(
        "calibration-experiment-status", help="检查 DOE 断点、结果和证据完整性"
    )
    experiment_status.add_argument("path", type=str)
    persistence_status = subparsers.add_parser(
        "calibration-persistence-status",
        help="检查 Working→RAM→ROM 作业、掉线协调和补偿状态",
    )
    persistence_status.add_argument("path", type=str)
    ai_tools = subparsers.add_parser("ai-tools", help="列出 AI/MCP 工具和 JSON Schema")
    ai_tools.add_argument("--approval-file", type=str)
    ai_plan = subparsers.add_parser("ai-plan", help="把自然语言请求转换为工程工具计划")
    ai_plan.add_argument("request", type=str)
    ai_plan.add_argument("--context", default="{}", type=str)
    ai_plan.add_argument("--approval-file", type=str)
    ai_approve = subparsers.add_parser("ai-approve", help="由外部用户审批 AI Action Plan")
    ai_approve.add_argument("plan_id", type=str)
    ai_approve.add_argument("--approver", required=True, type=str)
    ai_approve.add_argument("--approval-file", type=str)
    ai_call = subparsers.add_parser("ai-call", help="Dry-run 或执行结构化 AI 工具")
    ai_call.add_argument("tool", type=str)
    ai_call.add_argument("--arguments", default="{}", type=str)
    ai_call.add_argument("--execute", action="store_true")
    ai_call.add_argument("--action-plan-id", default="", type=str)
    ai_call.add_argument("--approval-file", type=str)
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
            allow_writes=args.allow_writes,
            checkpoint=args.checkpoint,
            resume=args.resume,
        )
    if args.command == "asset-manifest":
        return _asset_manifest(args.paths, args.output)
    if args.command == "a2l-summary":
        return _a2l_summary(args.path)
    if args.command == "calibration-convert":
        return _calibration_convert(args.source, args.output)
    if args.command == "calibration-verify":
        return _calibration_verify(
            args.dataset,
            a2l=args.a2l,
            hex_file=args.hex_file,
            expected=args.expected,
        )
    if args.command == "calibration-review":
        return _calibration_review(args.path, args.dataset)
    if args.command == "calibration-memory-status":
        return _calibration_memory_status(args.path)
    if args.command == "calibration-experiment-status":
        return _calibration_experiment_status(args.path)
    if args.command == "calibration-persistence-status":
        return _calibration_persistence_status(args.path)
    if args.command == "ai-tools":
        return _ai_tools(args.approval_file)
    if args.command == "ai-plan":
        return _ai_plan(args.request, args.context, args.approval_file)
    if args.command == "ai-approve":
        return _ai_approve(args.plan_id, args.approver, args.approval_file)
    if args.command == "ai-call":
        return _ai_call(
            args.tool,
            args.arguments,
            execute=args.execute,
            action_plan_id=args.action_plan_id,
            approval_file=args.approval_file,
        )
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
