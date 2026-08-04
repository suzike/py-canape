"""Agent2Canape 命令行入口。"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
from io import StringIO
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


def _a2l_context(
    path: str,
    *,
    output: str | None,
    device: str,
    include_measurements: bool,
    function: str,
    group: str,
    query: str,
) -> int:
    from .calibration_formats import A2LCatalog

    context = A2LCatalog.parse(path).to_engineering_context(
        device=device,
        include_measurements=include_measurements,
        function=function,
        group=group,
        query=query,
    )
    if output:
        target = Path(output).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(context, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = {
            "status": "passed",
            "output": str(target),
            **context["selection"],
        }
    else:
        result = context
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


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


def _calibration_experiment_report(path: str, output: str) -> int:
    from .calibration_design import CalibrationExperimentReport
    from .calibration_operations import CalibrationExperimentStore

    store = CalibrationExperimentStore.load(path)
    saved = CalibrationExperimentReport.save(store, output)
    result = CalibrationExperimentReport.build(store)
    print(
        json.dumps(
            {
                "status": "passed",
                "output": str(saved),
                "summary": result["summary"],
                "metrics": result["metrics"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _calibration_safe_suggest(path: str) -> int:
    from .calibration_design import SafeBayesianCalibrationOptimizer

    source = Path(path).expanduser().resolve()
    result = SafeBayesianCalibrationOptimizer.suggest_from_spec(
        json.loads(source.read_text(encoding="utf-8"))
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def _measurement_plan(path: str) -> int:
    from .measurement import MeasurementManifest

    result = MeasurementManifest.load(path).plan()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def _measurement_verify(
    path: str,
    *,
    minimum_bytes: int,
    expected_channels: list[str],
    minimum_duration_seconds: float,
    deep: bool,
) -> int:
    from .measurement import MeasurementArtifactVerifier

    result = MeasurementArtifactVerifier.verify(
        path,
        minimum_bytes=minimum_bytes,
        expected_channels=tuple(expected_channels),
        minimum_duration_seconds=minimum_duration_seconds,
        deep=deep,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def _measurement_stream_plan(path: str) -> int:
    from .streaming import MeasurementSubscriptionSpec

    result = MeasurementSubscriptionSpec.load(path).plan()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


def _measurement_stream_collect(
    project: str,
    subscription_file: str,
    *,
    sample_count: int,
    output_file: str | None,
    max_part_bytes: int,
    max_parts: int,
    flush_every: int,
) -> int:
    from .streaming import (
        MeasurementStreamSubscription,
        MeasurementSubscriptionSpec,
        RotatingMeasurementWriter,
    )

    spec = MeasurementSubscriptionSpec.load(subscription_file)
    writer = (
        RotatingMeasurementWriter(
            output_file,
            spec.channels,
            max_part_bytes=max_part_bytes,
            max_parts=max_parts,
            flush_every=flush_every,
        )
        if output_file
        else None
    )
    evidence = None
    try:
        with CANape() as canape:
            canape.open(project)
            subscription = MeasurementStreamSubscription(canape, spec, writer=writer)
            result = subscription.collect(sample_count)
            result["samples"] = [
                item.public() for item in subscription.buffer.recent(sample_count)
            ]
    finally:
        if writer is not None:
            evidence = writer.close()
    if evidence is not None:
        result["evidence"] = evidence
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
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
    context_file: str | None = None,
    reason: str | None = None,
) -> int:
    if context_file:
        context_data = json.loads(
            Path(context_file).expanduser().resolve().read_text(encoding="utf-8")
        )
    else:
        context_data = json.loads(context)
    if reason:
        context_data["reason"] = reason
    result = _ai_toolkit(approval_file).planner.plan(
        request,
        context=context_data,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ready" else 1


def _engineering_context_validate(path: str) -> int:
    from .engineering_context import EngineeringContextResolver

    source = Path(path).expanduser().resolve()
    context = json.loads(source.read_text(encoding="utf-8"))
    result = {
        "path": str(source),
        **EngineeringContextResolver.validate(context),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


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


def _mcp_doctor(
    project: str | None,
    *,
    live_canape: bool,
    skip_clients: bool,
) -> int:
    from .mcp_diagnostics import (
        MCPDiagnosticCheck,
        diagnostic_json,
        run_mcp_diagnostics,
    )

    runtime_stdout = StringIO()
    runtime_stderr = StringIO()
    with redirect_stdout(runtime_stdout), redirect_stderr(runtime_stderr):
        report = run_mcp_diagnostics(
            project=project,
            check_clients=not skip_clients,
            live_canape=live_canape,
        )
        gc.collect()
    suppressed_lines = sum(
        len(value.splitlines())
        for value in (runtime_stdout.getvalue(), runtime_stderr.getvalue())
    )
    if suppressed_lines:
        report = replace(
            report,
            checks=(
                *report.checks,
                MCPDiagnosticCheck(
                    name="runtime:out_of_band_output",
                    status="warning",
                    message="已隔离底层 COM 运行时的非 JSON 输出",
                    remediation=(
                        "若重复出现，请检查 pywin32 gen_py 缓存与 CANape COM 资源释放"
                    ),
                    evidence={"suppressed_line_count": suppressed_lines},
                ),
            ),
        )
    print(diagnostic_json(report))
    return 0 if report.passed else 1


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
    mcp_doctor = subparsers.add_parser(
        "mcp-doctor",
        help="分层检查 Codex、Claude Code、MCP 环境和 CANape 只读调用",
    )
    mcp_doctor.add_argument("--project", type=str)
    mcp_doctor.add_argument(
        "--live-canape",
        action="store_true",
        help="实际打开 CANape 工程并执行只读检查",
    )
    mcp_doctor.add_argument(
        "--skip-clients",
        action="store_true",
        help="跳过 Codex 和 Claude Code 客户端注册检查",
    )
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
    a2l_context = subparsers.add_parser(
        "a2l-context",
        help="从 A2L 功能组、枚举、范围和对象语义生成 AI 工程上下文",
    )
    a2l_context.add_argument("path", type=str)
    a2l_context.add_argument("--output", type=str)
    a2l_context.add_argument("--device", default="", type=str)
    a2l_context.add_argument("--include-measurements", action="store_true")
    a2l_context.add_argument("--function", default="", type=str)
    a2l_context.add_argument("--group", default="", type=str)
    a2l_context.add_argument("--query", default="", type=str)
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
    experiment_report = subparsers.add_parser(
        "calibration-experiment-report",
        help="生成含稳态、异常剔除和统计摘要的 JSON/Markdown 实验报告",
    )
    experiment_report.add_argument("path", type=str)
    experiment_report.add_argument("--output", required=True, type=str)
    safe_suggest = subparsers.add_parser(
        "calibration-safe-suggest",
        help="使用安全高斯过程代理模型推荐下一组标定候选",
    )
    safe_suggest.add_argument("spec", type=str)
    measurement_plan = subparsers.add_parser(
        "measurement-plan",
        help="校验测量清单、DAQ/FIFO 预算和触发录制约束",
    )
    measurement_plan.add_argument("manifest", type=str)
    measurement_verify = subparsers.add_parser(
        "measurement-verify",
        help="校验 MDF/MF4 录制产物的大小、哈希、信号和时长",
    )
    measurement_verify.add_argument("path", type=str)
    measurement_verify.add_argument("--minimum-bytes", type=int, default=1)
    measurement_verify.add_argument(
        "--expected-channel",
        action="append",
        default=[],
        dest="expected_channels",
    )
    measurement_verify.add_argument(
        "--minimum-duration-seconds",
        type=float,
        default=0.0,
    )
    measurement_verify.add_argument("--deep", action="store_true")
    stream_plan = subparsers.add_parser(
        "measurement-stream-plan",
        help="校验在线订阅、环形缓冲和长时间采集内存预算",
    )
    stream_plan.add_argument("subscription", type=str)
    stream_collect = subparsers.add_parser(
        "measurement-stream-collect",
        help="从 CANape 任务有界采样并增量写入 JSONL/CSV 证据",
    )
    stream_collect.add_argument("project", type=str)
    stream_collect.add_argument("subscription", type=str)
    stream_collect.add_argument("--samples", type=int, default=1)
    stream_collect.add_argument("--output", type=str)
    stream_collect.add_argument("--max-part-bytes", type=int, default=67108864)
    stream_collect.add_argument("--max-parts", type=int, default=100)
    stream_collect.add_argument("--flush-every", type=int, default=1)
    context_validate = subparsers.add_parser(
        "context-validate",
        help="验证 AI 工程对象、别名、单位和范围上下文",
    )
    context_validate.add_argument("path", type=str)
    ai_tools = subparsers.add_parser("ai-tools", help="列出 AI/MCP 工具和 JSON Schema")
    ai_tools.add_argument("--approval-file", type=str)
    ai_plan = subparsers.add_parser("ai-plan", help="把自然语言请求转换为工程工具计划")
    ai_plan.add_argument("request", type=str)
    ai_plan.add_argument("--context", default="{}", type=str)
    ai_plan.add_argument("--context-file", type=str)
    ai_plan.add_argument("--reason", type=str)
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
    if args.command == "mcp-doctor":
        return _mcp_doctor(
            args.project,
            live_canape=args.live_canape,
            skip_clients=args.skip_clients,
        )
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
    if args.command == "a2l-context":
        return _a2l_context(
            args.path,
            output=args.output,
            device=args.device,
            include_measurements=args.include_measurements,
            function=args.function,
            group=args.group,
            query=args.query,
        )
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
    if args.command == "calibration-experiment-report":
        return _calibration_experiment_report(args.path, args.output)
    if args.command == "calibration-safe-suggest":
        return _calibration_safe_suggest(args.spec)
    if args.command == "measurement-plan":
        return _measurement_plan(args.manifest)
    if args.command == "measurement-verify":
        return _measurement_verify(
            args.path,
            minimum_bytes=args.minimum_bytes,
            expected_channels=args.expected_channels,
            minimum_duration_seconds=args.minimum_duration_seconds,
            deep=args.deep,
        )
    if args.command == "measurement-stream-plan":
        return _measurement_stream_plan(args.subscription)
    if args.command == "measurement-stream-collect":
        return _measurement_stream_collect(
            args.project,
            args.subscription,
            sample_count=args.samples,
            output_file=args.output,
            max_part_bytes=args.max_part_bytes,
            max_parts=args.max_parts,
            flush_every=args.flush_every,
        )
    if args.command == "context-validate":
        return _engineering_context_validate(args.path)
    if args.command == "ai-tools":
        return _ai_tools(args.approval_file)
    if args.command == "ai-plan":
        return _ai_plan(
            args.request,
            args.context,
            args.approval_file,
            args.context_file,
            args.reason,
        )
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
