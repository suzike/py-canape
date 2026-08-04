"""供 Claude Code、Codex 等 AI Agent 使用的结构化工具和安全审批层。"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import IntEnum
from pathlib import Path
from typing import Any

from .engineering_context import EngineeringContextResolver
from .errors import SafetyViolationError


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError(f"对象不能转换为结构化结果：{type(value).__name__}")


def _numeric_values(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, (list, tuple)):
        return [number for item in value for number in _numeric_values(item)]
    return []


def _difference_summary(before: Any, target: Any) -> dict[str, Any]:
    before_values = _numeric_values(before)
    target_values = _numeric_values(target)
    if len(before_values) == len(target_values) == 1:
        delta = target_values[0] - before_values[0]
        return {
            "type": "scalar",
            "delta": delta,
            "percent": (
                delta / abs(before_values[0]) * 100.0
                if before_values[0] != 0
                else None
            ),
        }
    if before_values and len(before_values) == len(target_values):
        deltas = [
            target_value - before_value
            for before_value, target_value in zip(
                before_values,
                target_values,
                strict=True,
            )
        ]
        changed = [delta for delta in deltas if abs(delta) > 1e-12]
        return {
            "type": "array",
            "points": len(deltas),
            "changed_points": len(changed),
            "maximum_absolute_delta": max((abs(delta) for delta in deltas), default=0.0),
            "mean_absolute_delta": (
                sum(abs(delta) for delta in deltas) / len(deltas) if deltas else 0.0
            ),
        }
    return {
        "type": "structural",
        "before_points": len(before_values),
        "target_points": len(target_values),
    }


class ToolRisk(IntEnum):
    READ_ONLY = 0
    PROJECT_CONTROL = 10
    MEASUREMENT_CONTROL = 20
    CALIBRATION_WRITE = 30
    MEMORY_WRITE = 40
    DIAGNOSTIC = 50
    FLASH = 60


@dataclass(frozen=True, slots=True)
class AIToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: ToolRisk
    handler: Callable[..., Any] = field(repr=False, compare=False)
    previewer: Callable[..., dict[str, Any]] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk": self.risk.name,
            "approval_required": self.risk > ToolRisk.READ_ONLY,
        }


@dataclass(slots=True)
class AIActionPlan:
    id: str
    tool: str
    arguments: dict[str, Any]
    risk: str
    digest: str
    created_utc: str
    expires_utc: str
    status: str = "pending"
    approved_by: str = ""
    approved_utc: str = ""
    consumed_utc: str = ""
    preview: dict[str, Any] = field(default_factory=dict)
    precondition_digest: str = ""


class ApprovalStore:
    """可跨 CLI 与 MCP 进程共享的最小审批存储。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = (
            Path(path).expanduser().resolve()
            if path
            else Path.home() / ".agent2canape" / "approvals.json"
        )
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock = threading.RLock()

    @contextmanager
    def _process_lock(self, timeout: float = 5.0) -> Any:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        descriptor = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                    if age > 30.0:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待审批存储锁超时") from None
                time.sleep(0.02)
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(descriptor)
            self.lock_path.unlink(missing_ok=True)

    def _read(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("审批存储格式错误")
        return data

    def _write(self, data: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @staticmethod
    def digest(
        tool: str,
        arguments: Mapping[str, Any],
        precondition_digest: str = "",
    ) -> str:
        payload_data: dict[str, Any] = {
            "tool": tool,
            "arguments": dict(arguments),
        }
        if precondition_digest:
            payload_data["precondition_digest"] = precondition_digest
        payload = _canonical(payload_data)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def create(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        risk: ToolRisk,
        *,
        ttl_minutes: int = 30,
        preview: Mapping[str, Any] | None = None,
    ) -> AIActionPlan:
        if ttl_minutes <= 0:
            raise ValueError("ttl_minutes 必须大于 0")
        created = _now()
        preview_data = dict(preview or {})
        precondition_digest = str(preview_data.get("precondition_digest", ""))
        plan = AIActionPlan(
            id=str(uuid.uuid4()),
            tool=tool,
            arguments=dict(arguments),
            risk=risk.name,
            digest=self.digest(tool, arguments, precondition_digest),
            created_utc=created.isoformat(),
            expires_utc=(created + timedelta(minutes=ttl_minutes)).isoformat(),
            preview=preview_data,
            precondition_digest=precondition_digest,
        )
        with self._lock, self._process_lock():
            data = self._read()
            data[plan.id] = asdict(plan)
            self._write(data)
        return plan

    def get(self, plan_id: str) -> AIActionPlan:
        with self._lock, self._process_lock():
            raw = self._read().get(plan_id)
        if raw is None:
            raise KeyError(f"审批计划不存在：{plan_id}")
        return AIActionPlan(**raw)

    def approve(self, plan_id: str, approver: str) -> AIActionPlan:
        if not approver.strip():
            raise ValueError("审批人不能为空")
        with self._lock, self._process_lock():
            data = self._read()
            raw = data.get(plan_id)
            if raw is None:
                raise KeyError(f"审批计划不存在：{plan_id}")
            plan = AIActionPlan(**raw)
            if plan.status != "pending":
                raise SafetyViolationError(f"审批计划状态不可审批：{plan.status}")
            if _now() > datetime.fromisoformat(plan.expires_utc):
                plan.status = "expired"
                data[plan_id] = asdict(plan)
                self._write(data)
                raise SafetyViolationError("审批计划已过期")
            plan.status = "approved"
            plan.approved_by = approver.strip()
            plan.approved_utc = _now().isoformat()
            data[plan_id] = asdict(plan)
            self._write(data)
        return plan

    def claim(
        self,
        plan_id: str,
        tool: str,
        arguments: Mapping[str, Any],
        *,
        precondition_digest: str = "",
    ) -> AIActionPlan:
        with self._lock, self._process_lock():
            data = self._read()
            raw = data.get(plan_id)
            if raw is None:
                raise KeyError(f"审批计划不存在：{plan_id}")
            plan = AIActionPlan(**raw)
            if plan.status != "approved":
                raise SafetyViolationError(f"审批计划未批准：{plan.status}")
            if _now() > datetime.fromisoformat(plan.expires_utc):
                raise SafetyViolationError("审批计划已过期")
            digest = self.digest(tool, arguments, precondition_digest)
            if plan.tool != tool or plan.digest != digest:
                plan.status = "stale"
                data[plan_id] = asdict(plan)
                self._write(data)
                raise SafetyViolationError(
                    "工具、参数或执行前置状态已改变，必须重新生成审批计划"
                )
            plan.status = "executing"
            data[plan_id] = asdict(plan)
            self._write(data)
        return plan

    def complete(self, plan_id: str, *, success: bool) -> AIActionPlan:
        with self._lock, self._process_lock():
            data = self._read()
            plan = AIActionPlan(**data[plan_id])
            if plan.status != "executing":
                raise SafetyViolationError(f"审批计划不可完成：{plan.status}")
            plan.status = "consumed" if success else "failed"
            plan.consumed_utc = _now().isoformat()
            data[plan_id] = asdict(plan)
            self._write(data)
        return plan


class AIToolRegistry:
    def __init__(self, approvals: ApprovalStore | None = None) -> None:
        self.approvals = approvals or ApprovalStore()
        self._tools: dict[str, AIToolSpec] = {}

    def register(self, spec: AIToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"AI 工具重复：{spec.name}")
        self._tools[spec.name] = spec

    def manifest(self) -> list[dict[str, Any]]:
        return [self._tools[name].public() for name in sorted(self._tools)]

    def get(self, name: str) -> AIToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"未知 AI 工具：{name}") from exc

    @staticmethod
    def _validate(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
        required = set(schema.get("required", ()))
        missing = sorted(required - set(arguments))
        if missing:
            raise ValueError(f"缺少工具参数：{', '.join(missing)}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(arguments) - set(properties))
            if extra:
                raise ValueError(f"未知工具参数：{', '.join(extra)}")
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": (list, tuple),
            "object": Mapping,
        }
        for name, value in arguments.items():
            expected = properties.get(name, {}).get("type")
            if expected in {"number", "integer"} and isinstance(value, bool):
                raise TypeError(f"参数 {name} 必须是 {expected}")
            if expected in type_map and not isinstance(value, type_map[expected]):
                raise TypeError(f"参数 {name} 必须是 {expected}")

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        dry_run: bool = True,
        action_plan_id: str = "",
    ) -> dict[str, Any]:
        spec = self.get(name)
        values = dict(arguments or {})
        self._validate(spec.input_schema, values)
        if spec.risk > ToolRisk.READ_ONLY:
            if dry_run or not action_plan_id:
                preview = spec.previewer(**values) if spec.previewer else {}
                plan = self.approvals.create(
                    name,
                    values,
                    spec.risk,
                    preview=preview,
                )
                return {
                    "status": "planned",
                    "executed": False,
                    "tool": name,
                    "risk": spec.risk.name,
                    "action_plan": asdict(plan),
                    "execution_preview": preview,
                    "message": "请由用户或外部审批者批准后再次调用",
                }
            current_preview = spec.previewer(**values) if spec.previewer else {}
            approval = self.approvals.claim(
                action_plan_id,
                name,
                values,
                precondition_digest=str(
                    current_preview.get("precondition_digest", "")
                ),
            )
            try:
                result = spec.handler(**values)
            except Exception:
                self.approvals.complete(action_plan_id, success=False)
                raise
            self.approvals.complete(action_plan_id, success=True)
            return {
                "status": "executed",
                "executed": True,
                "tool": name,
                "risk": spec.risk.name,
                "approved_by": approval.approved_by,
                "result": result,
            }
        result = spec.handler(**values)
        return {
            "status": "executed",
            "executed": True,
            "tool": name,
            "risk": spec.risk.name,
            "result": result,
        }


class EngineeringCommandPlanner:
    """将常见中英文工程意图映射为候选工具；不执行任何操作。"""

    ROUTES = (
        (("列出标定", "标定量清单", "list calibration"), "calibration_list"),
        (("读取标定", "read calibration"), "calibration_read"),
        (("写入标定", "修改标定", "write calibration"), "calibration_write"),
        (("导出标定", "export calibration"), "calibration_export"),
        (("导入标定", "import calibration"), "calibration_import"),
        (
            ("标定评审", "变更集评审", "review calibration"),
            "calibration_change_set_status",
        ),
        (
            ("存储层状态", "ram rom 状态", "calibration memory status"),
            "calibration_memory_status",
        ),
        (
            ("持久化作业", "持久化状态", "persistence job"),
            "calibration_persistence_status",
        ),
        (
            ("doe 状态", "实验状态", "experiment status"),
            "calibration_experiment_status",
        ),
        (
            ("实验报告", "doe 报告", "experiment report"),
            "calibration_experiment_report",
        ),
        (
            ("推荐下一组标定", "安全优化候选", "safe calibration suggest"),
            "calibration_safe_suggest",
        ),
        (
            ("pareto", "帕累托", "多目标候选"),
            "calibration_pareto_analyze",
        ),
        (("启动测量", "start measurement"), "measurement_start"),
        (("停止测量", "stop measurement"), "measurement_stop"),
        (("测量状态", "measurement status"), "measurement_state"),
        (("设备上线", "go online"), "device_online"),
        (("设备下线", "go offline"), "device_offline"),
        (("读取内存", "read memory"), "memory_read"),
        (("写入内存", "write memory"), "memory_write"),
        (("诊断请求", "diagnostic request"), "diagnostic_raw"),
        (("诊断服务", "named diagnostic"), "diagnostic_named"),
        (("tester present", "诊断保活"), "tester_present"),
        (("刷写", "flash ecu"), "flash_start"),
        (("刷写状态", "flash status"), "flash_state"),
        (("停止刷写", "stop flash"), "flash_stop"),
        (("网络清单", "list network"), "network_list"),
        (("激活网络", "activate network"), "network_configure"),
        (("打开项目", "open project"), "project_open"),
        (("项目信息", "project info"), "project_info"),
    )

    def __init__(self, registry: AIToolRegistry) -> None:
        self.registry = registry

    def plan(
        self,
        text: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = text.casefold().strip()
        supplied = dict(context or {})
        matches = [
            (max(len(phrase) for phrase in phrases if phrase in normalized), tool)
            for phrases, tool in self.ROUTES
            if any(phrase in normalized for phrase in phrases)
        ]
        best = max((score for score, _ in matches), default=0)
        candidates = sorted({tool for score, tool in matches if score == best})
        if not candidates and supplied.get("objects"):
            if any(
                phrase in normalized
                for phrase in ("改为", "设为", "设置为", "调整为", "修改为", "写为")
            ):
                candidates = ["calibration_write"]
            elif any(phrase in normalized for phrase in ("读取", "查看", "read")):
                candidates = ["calibration_read"]
        if not candidates:
            return {
                "status": "unresolved",
                "text": text,
                "candidates": [],
                "message": "未识别工程意图，请明确测量、标定、诊断或刷写动作",
            }
        if len(candidates) > 1:
            return {
                "status": "ambiguous",
                "text": text,
                "candidates": candidates,
                "message": "检测到多个工程动作，必须拆分后执行",
            }
        spec = self.registry.get(candidates[0])
        properties = spec.input_schema.get("properties", {})
        arguments = {name: supplied[name] for name in properties if name in supplied}
        resolution: dict[str, Any] | None = None
        if spec.name in {"calibration_read", "calibration_write"} and supplied.get(
            "objects"
        ):
            resolution = EngineeringContextResolver.resolve(text, supplied)
            if resolution["status"] in {
                "ambiguous",
                "target_out_of_range",
                "unit_error",
            }:
                return {
                    "status": resolution["status"],
                    "text": text,
                    "tool": spec.name,
                    "risk": spec.risk.name,
                    "arguments": arguments,
                    "resolution": resolution,
                    "message": resolution.get("message", "工程对象解析失败"),
                }
            if resolution["status"] == "resolved":
                if resolution.get("device"):
                    arguments.setdefault("device", resolution["device"])
                arguments.setdefault("name", resolution["name"])
                if spec.name == "calibration_write" and "target_value" in resolution:
                    arguments.setdefault("value", resolution["target_value"])
        missing = sorted(set(spec.input_schema.get("required", ())) - set(arguments))
        result = {
            "status": "ready" if not missing else "needs_arguments",
            "text": text,
            "tool": spec.name,
            "risk": spec.risk.name,
            "arguments": arguments,
            "missing_arguments": missing,
            "approval_required": spec.risk > ToolRisk.READ_ONLY,
        }
        if resolution is not None:
            result["resolution"] = resolution
        return result


def _schema(
    properties: Mapping[str, str],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            name: {"type": value} for name, value in properties.items()
        },
        "required": list(required),
        "additionalProperties": False,
    }


class CANapeAIToolkit:
    """将 CANape 安全地暴露为细粒度 AI 工具。"""

    def __init__(
        self,
        canape: Any,
        approvals: ApprovalStore | None = None,
        *,
        default_project: str | Path | None = None,
    ) -> None:
        self.canape = canape
        self.default_project = (
            str(Path(default_project).expanduser().resolve())
            if default_project
            else None
        )
        self.registry = AIToolRegistry(approvals)
        self._register_tools()
        self.planner = EngineeringCommandPlanner(self.registry)

    def _connected(self) -> None:
        if not self.canape.connected:
            if self.default_project:
                self.canape.open(self.default_project)
            else:
                self.canape.connect()

    def _project_info(self) -> dict[str, Any]:
        self._connected()
        return self.canape.get_project_info()

    def _open_project(self, path: str) -> dict[str, Any]:
        self.canape.open(path)
        return self.canape.get_project_info()

    def _list_devices(self) -> list[dict[str, Any]]:
        self._connected()
        return [_object_dict(item) for item in self.canape.list_devices()]

    def _calibration_list(self, device: str, limit: int = 1000) -> list[str]:
        self._connected()
        return self.canape.list_calibration_objects(device, limit=limit)

    def _calibration_read(self, device: str, name: str) -> dict[str, Any]:
        self._connected()
        parameter = self.canape.read_calibration_parameter(device, name)
        result = asdict(parameter)
        result["kind"] = parameter.kind.value
        return result

    def _calibration_write_preview(
        self,
        device: str,
        name: str,
        value: Any,
        reason: str,
        x_axis: list[float] | None = None,
        y_axis: list[float] | None = None,
    ) -> dict[str, Any]:
        self._connected()
        before = self.canape.read_calibration_parameter(device, name)
        target = replace(
            before,
            value=value,
            x_axis=list(before.x_axis if x_axis is None else x_axis),
            y_axis=list(before.y_axis if y_axis is None else y_axis),
        )
        errors = target.validate()
        if errors:
            raise ValueError("; ".join(errors))
        before_data = asdict(before)
        before_data["kind"] = before.kind.value
        target_data = asdict(target)
        target_data["kind"] = target.kind.value
        precondition_payload = {
            "device": device,
            "name": name,
            "parameter": before_data,
        }
        precondition_digest = hashlib.sha256(
            _canonical(precondition_payload).encode("utf-8")
        ).hexdigest()
        return {
            "device": device,
            "name": name,
            "risk": ToolRisk.CALIBRATION_WRITE.name,
            "reason": reason,
            "current": before_data,
            "target": target_data,
            "difference": _difference_summary(before.value, target.value),
            "validation": {"passed": True, "errors": []},
            "recovery": {
                "method": "restore_calibration_parameter",
                "verify_readback": True,
                "snapshot": before_data,
            },
            "precondition_digest": precondition_digest,
        }

    def _calibration_write(
        self,
        device: str,
        name: str,
        value: Any,
        reason: str,
        x_axis: list[float] | None = None,
        y_axis: list[float] | None = None,
    ) -> dict[str, Any]:
        self._connected()
        before = self.canape.read_calibration_parameter(device, name)
        parameter = replace(
            before,
            value=value,
            x_axis=list(before.x_axis if x_axis is None else x_axis),
            y_axis=list(before.y_axis if y_axis is None else y_axis),
        )
        errors = parameter.validate()
        if errors:
            raise ValueError("; ".join(errors))
        written = self.canape.write_calibration_parameter(device, parameter, verify=True)
        return {
            "device": device,
            "name": name,
            "kind": parameter.kind.value,
            "before": asdict(before),
            "after": asdict(written),
            "reason": reason,
        }

    def _calibration_export(
        self, device: str, names: list[str], output_file: str
    ) -> dict[str, Any]:
        self._connected()
        dataset = self.canape.export_calibration_dataset(
            device, names, output_file
        )
        return {
            "output_file": str(Path(output_file).expanduser().resolve()),
            "parameter_count": len(dataset.parameters),
            "sha256": dataset.digest(),
        }

    def _calibration_import(self, device: str, input_file: str) -> dict[str, Any]:
        self._connected()
        return self.canape.import_calibration_dataset(device, input_file, verify=True)

    @staticmethod
    def _calibration_change_set_status(
        path: str, dataset_file: str = ""
    ) -> dict[str, Any]:
        from .calibration import CalibrationDataset
        from .calibration_operations import CalibrationChangeSet

        change_set = CalibrationChangeSet.load(path)
        dataset = CalibrationDataset.load(dataset_file) if dataset_file else None
        return change_set.summary(dataset)

    @staticmethod
    def _calibration_memory_status(path: str) -> dict[str, Any]:
        from .calibration_operations import CalibrationMemoryLedger

        return CalibrationMemoryLedger.load(path).status()

    @staticmethod
    def _calibration_experiment_status(path: str) -> dict[str, Any]:
        from .calibration_operations import CalibrationExperimentStore

        return CalibrationExperimentStore.load(path).summary()

    @staticmethod
    def _calibration_persistence_status(path: str) -> dict[str, Any]:
        from .calibration_targets import CalibrationPersistenceJob

        return CalibrationPersistenceJob.load(path).summary()

    @staticmethod
    def _calibration_experiment_report(path: str) -> dict[str, Any]:
        from .calibration_design import CalibrationExperimentReport
        from .calibration_operations import CalibrationExperimentStore

        return CalibrationExperimentReport.build(
            CalibrationExperimentStore.load(path)
        )

    @staticmethod
    def _calibration_safe_suggest(
        observations: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        bounds: dict[str, list[float]],
        objective: str,
        direction: str = "minimize",
        safety_limits: dict[str, list[float | None]] | None = None,
        length_scale: float = 0.35,
        observation_noise: float = 1e-6,
        exploration_weight: float = 1.0,
        safety_sigma: float = 2.0,
        max_extrapolation_distance: float = 0.75,
        limit: int = 10,
    ) -> dict[str, Any]:
        from .calibration_design import SafeBayesianCalibrationOptimizer

        return SafeBayesianCalibrationOptimizer.suggest_from_spec(
            {
                "observations": observations,
                "candidates": candidates,
                "bounds": bounds,
                "objective": objective,
                "direction": direction,
                "safety_limits": safety_limits or {},
                "length_scale": length_scale,
                "observation_noise": observation_noise,
                "exploration_weight": exploration_weight,
                "safety_sigma": safety_sigma,
                "max_extrapolation_distance": max_extrapolation_distance,
                "limit": limit,
            }
        )

    @staticmethod
    def _calibration_pareto_analyze(
        candidates: list[dict[str, Any]],
        objectives: list[dict[str, Any]],
        safety_limits: dict[str, list[float | None]] | None = None,
    ) -> dict[str, Any]:
        from .calibration_operations import (
            CalibrationCandidate,
            CalibrationObjective,
            ParetoCalibrationAnalysis,
        )

        parsed_candidates = [
            CalibrationCandidate(
                identifier=str(candidate["identifier"]),
                parameters={
                    name: float(value)
                    for name, value in candidate.get("parameters", {}).items()
                },
                metrics={
                    name: float(value)
                    for name, value in candidate["metrics"].items()
                },
                evidence=tuple(candidate.get("evidence", ())),
            )
            for candidate in candidates
        ]
        parsed_objectives = [
            CalibrationObjective(
                name=str(objective["name"]),
                direction=str(objective.get("direction", "minimize")),
            )
            for objective in objectives
        ]
        parsed_limits = {
            name: (
                values[0] if len(values) > 0 else None,
                values[1] if len(values) > 1 else None,
            )
            for name, values in (safety_limits or {}).items()
        }
        return ParetoCalibrationAnalysis.analyze(
            parsed_candidates,
            parsed_objectives,
            safety_limits=parsed_limits,
        )

    def _measurement_start(self) -> dict[str, Any]:
        self._connected()
        return {"running": self.canape.start_measurement()}

    def _measurement_stop(self) -> dict[str, Any]:
        self._connected()
        self.canape.stop_measurement()
        return {"running": self.canape.is_measurement_running()}

    def _measurement_state(self) -> dict[str, Any]:
        self._connected()
        state = self.canape.get_measurement_state()
        if isinstance(state, dict):
            return state
        if is_dataclass(state) or hasattr(state, "__dict__"):
            return _object_dict(state)
        return {
            "state": int(state),
            "running": bool(self.canape.is_measurement_running()),
        }

    def _device_online(self, device: str, download: bool = False) -> dict[str, Any]:
        self._connected()
        self.canape.set_device_online(device, download=download)
        return {"device": device, "online": self.canape.is_device_online(device)}

    def _device_offline(self, device: str) -> dict[str, Any]:
        self._connected()
        self.canape.set_device_offline(device)
        return {"device": device, "online": self.canape.is_device_online(device)}

    def _memory_read(
        self, device: str, address: int, size: int, address_extension: int = 0
    ) -> dict[str, Any]:
        self._connected()
        value = self.canape.read_memory(
            device,
            address,
            size,
            address_extension=address_extension,
        )
        return {"data": list(value), "size": len(value)}

    def _memory_write(
        self,
        device: str,
        address: int,
        data: list[int],
        address_extension: int = 0,
    ) -> dict[str, Any]:
        self._connected()
        value = self.canape.write_memory(
            device,
            address,
            data,
            address_extension=address_extension,
            verify=True,
        )
        return {"data": list(value), "size": len(value)}

    def _diagnostic_raw(
        self, device: str, payload: list[int], timeout: float = 5.0
    ) -> list[dict[str, Any]]:
        self._connected()
        return [
            asdict(item)
            for item in self.canape.send_raw_diagnostic_request(
                device, payload, timeout=timeout
            )
        ]

    def _diagnostic_named(
        self,
        device: str,
        service: str,
        parameters: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> list[dict[str, Any]]:
        self._connected()
        return [
            asdict(item)
            for item in self.canape.send_diagnostic_request(
                device,
                service,
                parameters=parameters,
                timeout=timeout,
            )
        ]

    def _tester_present(self, device: str, enabled: bool) -> dict[str, Any]:
        self._connected()
        return {
            "device": device,
            "enabled": enabled,
            "status": self.canape.set_tester_present(device, enabled=enabled),
        }

    def _flash_start(
        self,
        device: str,
        job: str,
        session: str,
        config_file: str = "",
    ) -> dict[str, Any]:
        self._connected()
        self.canape.start_flash(
            device, job, session, config_file=config_file or None
        )
        return {"device": device, "started": True}

    def _flash_stop(self, device: str) -> dict[str, Any]:
        self._connected()
        return _object_dict(
            self.canape.control_flash(device, action="stop")
        )

    def _flash_state(self, device: str) -> dict[str, Any]:
        self._connected()
        return _object_dict(self.canape.get_flash_state(device))

    def _network_list(self) -> list[dict[str, Any]]:
        self._connected()
        return self.canape.list_networks()

    def _network_configure(self, network: str, active: bool) -> dict[str, Any]:
        self._connected()
        return self.canape.configure_network(network, active=active)

    def _register_tools(self) -> None:
        definitions = (
            AIToolSpec(
                "project_info",
                "读取当前 CANape 工作目录、CNA 和应用信息。",
                _schema({}),
                ToolRisk.READ_ONLY,
                self._project_info,
            ),
            AIToolSpec(
                "project_open",
                "打开 CANape 项目目录或 CNA。只改变工具项目，不写 ECU。",
                _schema({"path": "string"}, required=("path",)),
                ToolRisk.PROJECT_CONTROL,
                self._open_project,
            ),
            AIToolSpec(
                "device_list",
                "列出 CANape 设备、驱动、通道、在线状态和数据库。",
                _schema({}),
                ToolRisk.READ_ONLY,
                self._list_devices,
            ),
            AIToolSpec(
                "device_online",
                "使设备上线，可选下载数据集。",
                _schema(
                    {"device": "string", "download": "boolean"},
                    required=("device",),
                ),
                ToolRisk.PROJECT_CONTROL,
                self._device_online,
            ),
            AIToolSpec(
                "device_offline",
                "使指定设备安全下线。",
                _schema({"device": "string"}, required=("device",)),
                ToolRisk.PROJECT_CONTROL,
                self._device_offline,
            ),
            AIToolSpec(
                "calibration_list",
                "列出指定 ECU 的标定对象。",
                _schema(
                    {"device": "string", "limit": "integer"},
                    required=("device",),
                ),
                ToolRisk.READ_ONLY,
                self._calibration_list,
            ),
            AIToolSpec(
                "calibration_read",
                "读取标定量完整信息，包括标量/曲线/MAP、轴、范围、单位和值。",
                _schema(
                    {"device": "string", "name": "string"},
                    required=("device", "name"),
                ),
                ToolRisk.READ_ONLY,
                self._calibration_read,
            ),
            AIToolSpec(
                "calibration_write",
                "写入一个标定量并强制回读；必须先生成并外部审批 Action Plan。",
                _schema(
                    {
                        "device": "string",
                        "name": "string",
                        "value": "any",
                        "reason": "string",
                        "x_axis": "array",
                        "y_axis": "array",
                    },
                    required=("device", "name", "value", "reason"),
                ),
                ToolRisk.CALIBRATION_WRITE,
                self._calibration_write,
                self._calibration_write_preview,
            ),
            AIToolSpec(
                "calibration_export",
                "将指定标定对象导出为带哈希的工程数据集。",
                _schema(
                    {
                        "device": "string",
                        "names": "array",
                        "output_file": "string",
                    },
                    required=("device", "names", "output_file"),
                ),
                ToolRisk.PROJECT_CONTROL,
                self._calibration_export,
            ),
            AIToolSpec(
                "calibration_import",
                "事务导入标定数据集并逐项回读；失败时回滚已写对象。",
                _schema(
                    {"device": "string", "input_file": "string"},
                    required=("device", "input_file"),
                ),
                ToolRisk.CALIBRATION_WRITE,
                self._calibration_import,
            ),
            AIToolSpec(
                "calibration_change_set_status",
                "读取标定变更集的功能组、页面、责任人、风险和审批摘要。",
                _schema(
                    {"path": "string", "dataset_file": "string"},
                    required=("path",),
                ),
                ToolRisk.READ_ONLY,
                self._calibration_change_set_status,
            ),
            AIToolSpec(
                "calibration_memory_status",
                "读取 Working/Reference/RAM/ROM 快照一致性和持久化状态。",
                _schema({"path": "string"}, required=("path",)),
                ToolRisk.READ_ONLY,
                self._calibration_memory_status,
            ),
            AIToolSpec(
                "calibration_experiment_status",
                "读取 DOE 检查点、失败 case 和证据完整性。",
                _schema({"path": "string"}, required=("path",)),
                ToolRisk.READ_ONLY,
                self._calibration_experiment_status,
            ),
            AIToolSpec(
                "calibration_persistence_status",
                "读取 Working→RAM→ROM 作业、掉线协调和补偿恢复状态。",
                _schema({"path": "string"}, required=("path",)),
                ToolRisk.READ_ONLY,
                self._calibration_persistence_status,
            ),
            AIToolSpec(
                "calibration_experiment_report",
                "汇总 DOE 稳态门禁、指标验收、异常剔除、证据和统计结果。",
                _schema({"path": "string"}, required=("path",)),
                ToolRisk.READ_ONLY,
                self._calibration_experiment_report,
            ),
            AIToolSpec(
                "calibration_safe_suggest",
                "用安全高斯过程代理模型和置信边界推荐下一组标定候选。",
                _schema(
                    {
                        "observations": "array",
                        "candidates": "array",
                        "bounds": "object",
                        "objective": "string",
                        "direction": "string",
                        "safety_limits": "object",
                        "length_scale": "number",
                        "observation_noise": "number",
                        "exploration_weight": "number",
                        "safety_sigma": "number",
                        "max_extrapolation_distance": "number",
                        "limit": "integer",
                    },
                    required=(
                        "observations",
                        "candidates",
                        "bounds",
                        "objective",
                    ),
                ),
                ToolRisk.READ_ONLY,
                self._calibration_safe_suggest,
            ),
            AIToolSpec(
                "calibration_pareto_analyze",
                "按安全边界筛选标定候选并计算多目标 Pareto 前沿。",
                _schema(
                    {
                        "candidates": "array",
                        "objectives": "array",
                        "safety_limits": "object",
                    },
                    required=("candidates", "objectives"),
                ),
                ToolRisk.READ_ONLY,
                self._calibration_pareto_analyze,
            ),
            AIToolSpec(
                "measurement_start",
                "启动 CANape 测量并等待运行态。",
                _schema({}),
                ToolRisk.MEASUREMENT_CONTROL,
                self._measurement_start,
            ),
            AIToolSpec(
                "measurement_stop",
                "停止 CANape 测量并等待停止态。",
                _schema({}),
                ToolRisk.MEASUREMENT_CONTROL,
                self._measurement_stop,
            ),
            AIToolSpec(
                "measurement_state",
                "读取 CANape 测量运行态和状态码。",
                _schema({}),
                ToolRisk.READ_ONLY,
                self._measurement_state,
            ),
            AIToolSpec(
                "memory_read",
                "读取 ECU 指定地址内存。",
                _schema(
                    {
                        "device": "string",
                        "address": "integer",
                        "size": "integer",
                        "address_extension": "integer",
                    },
                    required=("device", "address", "size"),
                ),
                ToolRisk.READ_ONLY,
                self._memory_read,
            ),
            AIToolSpec(
                "memory_write",
                "写 ECU 内存并回读校验；必须外部审批。",
                _schema(
                    {
                        "device": "string",
                        "address": "integer",
                        "data": "array",
                        "address_extension": "integer",
                    },
                    required=("device", "address", "data"),
                ),
                ToolRisk.MEMORY_WRITE,
                self._memory_write,
            ),
            AIToolSpec(
                "diagnostic_raw",
                "发送原始诊断请求并解析响应；必须外部审批。",
                _schema(
                    {
                        "device": "string",
                        "payload": "array",
                        "timeout": "number",
                    },
                    required=("device", "payload"),
                ),
                ToolRisk.DIAGNOSTIC,
                self._diagnostic_raw,
            ),
            AIToolSpec(
                "diagnostic_named",
                "发送数据库定义的命名诊断服务并解析所有响应。",
                _schema(
                    {
                        "device": "string",
                        "service": "string",
                        "parameters": "object",
                        "timeout": "number",
                    },
                    required=("device", "service"),
                ),
                ToolRisk.DIAGNOSTIC,
                self._diagnostic_named,
            ),
            AIToolSpec(
                "tester_present",
                "启停并查询指定 ECU 的 Tester Present。",
                _schema(
                    {"device": "string", "enabled": "boolean"},
                    required=("device", "enabled"),
                ),
                ToolRisk.DIAGNOSTIC,
                self._tester_present,
            ),
            AIToolSpec(
                "flash_start",
                "启动 ECU 刷写任务；最高风险，必须外部审批。",
                _schema(
                    {
                        "device": "string",
                        "job": "string",
                        "session": "string",
                        "config_file": "string",
                    },
                    required=("device", "job", "session"),
                ),
                ToolRisk.FLASH,
                self._flash_start,
            ),
            AIToolSpec(
                "flash_stop",
                "停止 ECU 刷写并返回刷写状态。",
                _schema({"device": "string"}, required=("device",)),
                ToolRisk.FLASH,
                self._flash_stop,
            ),
            AIToolSpec(
                "flash_state",
                "读取刷写进度、信息和返回值。",
                _schema({"device": "string"}, required=("device",)),
                ToolRisk.READ_ONLY,
                self._flash_state,
            ),
            AIToolSpec(
                "network_list",
                "读取 CANape 网络清单和激活状态。",
                _schema({}),
                ToolRisk.READ_ONLY,
                self._network_list,
            ),
            AIToolSpec(
                "network_configure",
                "激活或停用 CANape 网络。",
                _schema(
                    {"network": "string", "active": "boolean"},
                    required=("network", "active"),
                ),
                ToolRisk.PROJECT_CONTROL,
                self._network_configure,
            ),
        )
        for definition in definitions:
            self.registry.register(definition)
