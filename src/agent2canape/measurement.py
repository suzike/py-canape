"""测量清单、DAQ 预算、事务配置、重连恢复与录制文件验收。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import OptionalDependencyError, SafetyViolationError, WorkflowError


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _task_key(device: str, task: str) -> str:
    return f"{device}::{task}"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class MeasurementChannelSpec:
    device: str
    task: str
    name: str
    bytes_per_sample: int = 8
    sample_rate_hz: float | None = None
    overhead_bytes: int = 4
    required: bool = True
    priority: str = "normal"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MeasurementChannelSpec:
        return cls(
            device=str(value["device"]),
            task=str(value["task"]),
            name=str(value["name"]),
            bytes_per_sample=int(value.get("bytes_per_sample", 8)),
            sample_rate_hz=(
                float(value["sample_rate_hz"])
                if value.get("sample_rate_hz") is not None
                else None
            ),
            overhead_bytes=int(value.get("overhead_bytes", 4)),
            required=bool(value.get("required", True)),
            priority=str(value.get("priority", "normal")),
        )


@dataclass(frozen=True, slots=True)
class MeasurementTaskLimit:
    device: str
    task: str
    sampling_time_seconds: float
    max_channels: int
    max_bytes_per_second: float
    max_utilization: float = 0.8
    minimum_fifo_samples: int = 0
    event: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MeasurementTaskLimit:
        return cls(
            device=str(value["device"]),
            task=str(value["task"]),
            sampling_time_seconds=float(value["sampling_time_seconds"]),
            max_channels=int(value["max_channels"]),
            max_bytes_per_second=float(value["max_bytes_per_second"]),
            max_utilization=float(value.get("max_utilization", 0.8)),
            minimum_fifo_samples=int(value.get("minimum_fifo_samples", 0)),
            event=int(value["event"]) if value.get("event") is not None else None,
        )


@dataclass(frozen=True, slots=True)
class MeasurementTriggerSpec:
    name: str
    recorder: str
    expression: str
    pre_trigger_seconds: float = 0.0
    post_trigger_seconds: float = 0.0
    holdoff_seconds: float = 0.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MeasurementTriggerSpec:
        return cls(
            name=str(value["name"]),
            recorder=str(value["recorder"]),
            expression=str(value["expression"]),
            pre_trigger_seconds=float(value.get("pre_trigger_seconds", 0.0)),
            post_trigger_seconds=float(value.get("post_trigger_seconds", 0.0)),
            holdoff_seconds=float(value.get("holdoff_seconds", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class MeasurementRecorderSpec:
    name: str
    output_file: str = ""
    data_reduction: int = 1

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MeasurementRecorderSpec:
        return cls(
            name=str(value["name"]),
            output_file=str(value.get("output_file", "")),
            data_reduction=int(value.get("data_reduction", 1)),
        )


@dataclass(frozen=True, slots=True)
class MeasurementManifest:
    name: str
    channels: tuple[MeasurementChannelSpec, ...]
    task_limits: tuple[MeasurementTaskLimit, ...]
    recorders: tuple[MeasurementRecorderSpec, ...] = ()
    triggers: tuple[MeasurementTriggerSpec, ...] = ()
    sample_size: int | None = None
    fifo_size: int | None = None
    sync_mode: bool | None = None
    resume_mode: bool | None = None
    use_nan: bool | None = None
    measurement_output_file: str = ""
    require_task_limits: bool = True
    start_after_apply: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MeasurementManifest:
        return cls(
            name=str(value.get("name", "measurement-manifest")),
            channels=tuple(
                MeasurementChannelSpec.from_mapping(item)
                for item in value.get("channels", ())
            ),
            task_limits=tuple(
                MeasurementTaskLimit.from_mapping(item)
                for item in value.get("task_limits", ())
            ),
            recorders=tuple(
                MeasurementRecorderSpec.from_mapping(item)
                for item in value.get("recorders", ())
            ),
            triggers=tuple(
                MeasurementTriggerSpec.from_mapping(item)
                for item in value.get("triggers", ())
            ),
            sample_size=(
                int(value["sample_size"])
                if value.get("sample_size") is not None
                else None
            ),
            fifo_size=(
                int(value["fifo_size"])
                if value.get("fifo_size") is not None
                else None
            ),
            sync_mode=(
                bool(value["sync_mode"]) if value.get("sync_mode") is not None else None
            ),
            resume_mode=(
                bool(value["resume_mode"])
                if value.get("resume_mode") is not None
                else None
            ),
            use_nan=bool(value["use_nan"]) if value.get("use_nan") is not None else None,
            measurement_output_file=str(value.get("measurement_output_file", "")),
            require_task_limits=bool(value.get("require_task_limits", True)),
            start_after_apply=bool(value.get("start_after_apply", False)),
            metadata=dict(value.get("metadata", {})),
        )

    @classmethod
    def load(cls, path: str | Path) -> MeasurementManifest:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        text = source.read_text(encoding="utf-8")
        data = (
            yaml.safe_load(text)
            if source.suffix.casefold() in {".yaml", ".yml"}
            else json.loads(text)
        )
        if not isinstance(data, Mapping):
            raise ValueError("测量清单根节点必须是对象")
        return cls.from_mapping(data)

    def public(self) -> dict[str, Any]:
        return asdict(self)

    def digest(self) -> str:
        return _digest(self.public())

    def plan(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not self.channels:
            errors.append("测量清单不能为空")
        if self.sample_size is not None and self.sample_size <= 0:
            errors.append("sample_size 必须大于0")
        if self.fifo_size is not None and self.fifo_size <= 0:
            errors.append("fifo_size 必须大于0")

        limit_map: dict[tuple[str, str], MeasurementTaskLimit] = {}
        for limit in self.task_limits:
            identity = (limit.device.casefold(), limit.task.casefold())
            if identity in limit_map:
                errors.append(f"任务预算重复：{limit.device}/{limit.task}")
            limit_map[identity] = limit
            if limit.sampling_time_seconds <= 0:
                errors.append(f"{limit.device}/{limit.task} sampling_time_seconds 必须大于0")
            if limit.max_channels <= 0 or limit.max_bytes_per_second <= 0:
                errors.append(f"{limit.device}/{limit.task} DAQ 容量必须大于0")
            if not 0 < limit.max_utilization <= 1:
                errors.append(f"{limit.device}/{limit.task} max_utilization 必须在(0,1]内")
            if limit.minimum_fifo_samples < 0:
                errors.append(f"{limit.device}/{limit.task} minimum_fifo_samples 不能为负")

        channel_ids: set[tuple[str, str, str]] = set()
        task_channels: dict[tuple[str, str], list[MeasurementChannelSpec]] = {}
        for channel in self.channels:
            identity = (
                channel.device.casefold(),
                channel.task.casefold(),
                channel.name.casefold(),
            )
            if not all((channel.device.strip(), channel.task.strip(), channel.name.strip())):
                errors.append("测量信号 device/task/name 不能为空")
            if identity in channel_ids:
                errors.append(
                    f"测量信号重复：{channel.device}/{channel.task}/{channel.name}"
                )
            channel_ids.add(identity)
            if channel.bytes_per_sample <= 0 or channel.overhead_bytes < 0:
                errors.append(f"{channel.name} 字节预算无效")
            if channel.sample_rate_hz is not None and channel.sample_rate_hz <= 0:
                errors.append(f"{channel.name} sample_rate_hz 必须大于0")
            if channel.priority not in {"critical", "high", "normal", "low"}:
                errors.append(f"{channel.name} priority 无效：{channel.priority}")
            task_channels.setdefault(identity[:2], []).append(channel)

        task_reports: list[dict[str, Any]] = []
        for identity, channels in sorted(task_channels.items()):
            limit = limit_map.get(identity)
            label = f"{channels[0].device}/{channels[0].task}"
            if limit is None:
                message = f"{label} 未声明 DAQ 任务预算"
                (errors if self.require_task_limits else warnings).append(message)
                task_reports.append(
                    {"device": channels[0].device, "task": channels[0].task, "budgeted": False}
                )
                continue
            nominal_rate = 1.0 / limit.sampling_time_seconds
            load = 0.0
            channel_loads: dict[str, float] = {}
            for channel in channels:
                rate = channel.sample_rate_hz or nominal_rate
                if rate > nominal_rate * (1.0 + 1e-9):
                    errors.append(
                        f"{label}/{channel.name} 请求 {rate:g}Hz，超过任务 {nominal_rate:g}Hz"
                    )
                channel_load = (channel.bytes_per_sample + channel.overhead_bytes) * rate
                channel_loads[channel.name] = channel_load
                load += channel_load
            utilization = load / limit.max_bytes_per_second
            degradation_candidates: list[str] = []
            if len(channels) > limit.max_channels:
                errors.append(
                    f"{label} 信号数 {len(channels)} 超过上限 {limit.max_channels}"
                )
            if utilization > limit.max_utilization:
                errors.append(
                    f"{label} DAQ 利用率 {utilization:.1%} 超过门限 {limit.max_utilization:.1%}"
                )
                remaining_load = load
                priority_order = {"low": 0, "normal": 1, "high": 2, "critical": 3}
                removable = sorted(
                    (channel for channel in channels if not channel.required),
                    key=lambda channel: (
                        priority_order.get(channel.priority, len(priority_order)),
                        -channel_loads[channel.name],
                        channel.name.casefold(),
                    ),
                )
                for channel in removable:
                    degradation_candidates.append(channel.name)
                    remaining_load -= channel_loads[channel.name]
                    if remaining_load <= (
                        limit.max_bytes_per_second * limit.max_utilization
                    ):
                        break
            elif utilization > limit.max_utilization * 0.9:
                warnings.append(f"{label} DAQ 利用率接近门限：{utilization:.1%}")
            if self.fifo_size is not None and self.fifo_size < limit.minimum_fifo_samples:
                errors.append(
                    f"{label} FIFO {self.fifo_size} 小于要求 {limit.minimum_fifo_samples}"
                )
            task_reports.append(
                {
                    "device": channels[0].device,
                    "task": channels[0].task,
                    "budgeted": True,
                    "event": limit.event,
                    "nominal_rate_hz": nominal_rate,
                    "channel_count": len(channels),
                    "max_channels": limit.max_channels,
                    "estimated_bytes_per_second": load,
                    "max_bytes_per_second": limit.max_bytes_per_second,
                    "utilization": utilization,
                    "utilization_limit": limit.max_utilization,
                    "degradation_candidates": degradation_candidates,
                }
            )

        unused_limits = sorted(set(limit_map) - set(task_channels))
        for identity in unused_limits:
            limit = limit_map[identity]
            warnings.append(f"{limit.device}/{limit.task} 的 DAQ 预算未被任何信号使用")

        recorder_names: set[str] = set()
        for recorder in self.recorders:
            folded = recorder.name.casefold()
            if not recorder.name.strip() or folded in recorder_names:
                errors.append(f"记录器名称为空或重复：{recorder.name}")
            recorder_names.add(folded)
            if recorder.data_reduction <= 0:
                errors.append(f"{recorder.name} data_reduction 必须大于0")
            if recorder.output_file and Path(recorder.output_file).suffix.casefold() not in {
                ".mdf",
                ".mf4",
            }:
                errors.append(f"{recorder.name} 输出文件必须是 MDF/MF4")

        trigger_names: set[str] = set()
        for trigger in self.triggers:
            if not trigger.name.strip() or trigger.name.casefold() in trigger_names:
                errors.append(f"触发器名称为空或重复：{trigger.name}")
            trigger_names.add(trigger.name.casefold())
            if trigger.recorder.casefold() not in recorder_names:
                errors.append(f"{trigger.name} 引用未定义记录器：{trigger.recorder}")
            if not trigger.expression.strip():
                errors.append(f"{trigger.name} expression 不能为空")
            if min(
                trigger.pre_trigger_seconds,
                trigger.post_trigger_seconds,
                trigger.holdoff_seconds,
            ) < 0:
                errors.append(f"{trigger.name} 触发时间不能为负")
            if self.fifo_size is not None:
                for report in task_reports:
                    if not report.get("budgeted"):
                        continue
                    required = math.ceil(
                        float(report["nominal_rate_hz"])
                        * (trigger.pre_trigger_seconds + trigger.post_trigger_seconds)
                    )
                    if required > self.fifo_size:
                        errors.append(
                            f"{trigger.name} 对 {report['device']}/{report['task']} 需要约 "
                            f"{required} 个 FIFO 样本，超过 {self.fifo_size}"
                        )

        if self.measurement_output_file and Path(
            self.measurement_output_file
        ).suffix.casefold() not in {".mdf", ".mf4"}:
            errors.append("measurement_output_file 必须是 MDF/MF4")
        return {
            "status": "passed" if not errors else "failed",
            "passed": not errors,
            "name": self.name,
            "manifest_digest": self.digest(),
            "channel_count": len(self.channels),
            "task_count": len(task_channels),
            "recorder_count": len(self.recorders),
            "trigger_count": len(self.triggers),
            "tasks": task_reports,
            "errors": errors,
            "warnings": warnings,
            "trigger_application": (
                "CANape 1.9 COM 不暴露通用触发配置；需由 CNA 或受控项目脚本预配置"
                if self.triggers
                else "not_requested"
            ),
        }

    def require_valid(self) -> dict[str, Any]:
        plan = self.plan()
        if not plan["passed"]:
            raise SafetyViolationError("; ".join(plan["errors"]))
        return plan


@dataclass(frozen=True, slots=True)
class MeasurementSessionSnapshot:
    captured_utc: str
    running: bool
    configuration: dict[str, Any]
    measurement_output_file: str
    task_channels: dict[str, tuple[str, ...]]
    recorders: dict[str, dict[str, Any]]

    def public(self) -> dict[str, Any]:
        return asdict(self)

    def digest(self) -> str:
        value = self.public()
        value.pop("captured_utc", None)
        return _digest(value)


class MeasurementSessionManager:
    """以快照、回滚和前置摘要保护 CANape 测量配置变更。"""

    def __init__(self, canape: Any) -> None:
        self.canape = canape

    def capture(self, manifest: MeasurementManifest) -> MeasurementSessionSnapshot:
        tasks = sorted({(item.device, item.task) for item in manifest.channels})
        task_channels = {
            _task_key(device, task): tuple(
                self.canape.list_measurement_channels(device, task)
            )
            for device, task in tasks
        }
        recorders = {
            item.name: dict(self.canape.get_recorder_configuration(item.name))
            for item in manifest.recorders
        }
        return MeasurementSessionSnapshot(
            captured_utc=datetime.now(UTC).isoformat(),
            running=bool(self.canape.is_measurement_running()),
            configuration=dict(self.canape.get_measurement_configuration()),
            measurement_output_file=str(self.canape.get_measurement_output_file()),
            task_channels=task_channels,
            recorders=recorders,
        )

    def preview(self, manifest: MeasurementManifest) -> dict[str, Any]:
        plan = manifest.require_valid()
        snapshot = self.capture(manifest)
        desired_channels: dict[str, list[str]] = {}
        for channel in manifest.channels:
            desired_channels.setdefault(_task_key(channel.device, channel.task), []).append(
                channel.name
            )
        return {
            "risk": "MEASUREMENT_CONTROL",
            "manifest": plan,
            "current": snapshot.public(),
            "desired": {
                "configuration": {
                    "sample_size": manifest.sample_size,
                    "fifo_size": manifest.fifo_size,
                    "sync_mode": manifest.sync_mode,
                    "resume_mode": manifest.resume_mode,
                    "use_nan": manifest.use_nan,
                },
                "measurement_output_file": manifest.measurement_output_file,
                "task_channels": desired_channels,
                "recorders": [asdict(item) for item in manifest.recorders],
                "start_after_apply": manifest.start_after_apply,
            },
            "recovery": {"method": "restore_measurement_snapshot", "snapshot": snapshot.public()},
            "precondition_digest": snapshot.digest(),
        }

    def _restore(self, snapshot: MeasurementSessionSnapshot) -> None:
        if self.canape.is_measurement_running():
            self.canape.stop_measurement()
        self.canape.configure_measurement(**snapshot.configuration)
        if snapshot.measurement_output_file:
            self.canape.set_measurement_output_file(snapshot.measurement_output_file)
        for key, channels in snapshot.task_channels.items():
            device, task = key.split("::", 1)
            self.canape.configure_measurement_channels(device, task, channels, clear=True)
        for name, recorder in snapshot.recorders.items():
            if recorder.get("mdf_filename"):
                self.canape.set_recorder_output_file(name, recorder["mdf_filename"])
            self.canape.set_recorder_data_reduction(name, int(recorder["data_reduction"]))
        if snapshot.running:
            self.canape.start_measurement()

    def _apply_with_snapshot(
        self,
        manifest: MeasurementManifest,
        snapshot: MeasurementSessionSnapshot,
    ) -> dict[str, Any]:
        plan = manifest.require_valid()
        try:
            if snapshot.running:
                self.canape.stop_measurement()
            self.canape.configure_measurement(
                sample_size=manifest.sample_size,
                fifo_size=manifest.fifo_size,
                sync_mode=manifest.sync_mode,
                resume_mode=manifest.resume_mode,
                use_nan=manifest.use_nan,
            )
            if manifest.measurement_output_file:
                self.canape.set_measurement_output_file(manifest.measurement_output_file)
            desired_channels: dict[tuple[str, str], list[str]] = {}
            for channel in manifest.channels:
                desired_channels.setdefault((channel.device, channel.task), []).append(
                    channel.name
                )
            for (device, task), channels in desired_channels.items():
                self.canape.configure_measurement_channels(device, task, channels, clear=True)
            for recorder in manifest.recorders:
                if recorder.output_file:
                    self.canape.set_recorder_output_file(recorder.name, recorder.output_file)
                self.canape.set_recorder_data_reduction(
                    recorder.name, recorder.data_reduction
                )
            if manifest.start_after_apply:
                self.canape.start_measurement()
        except Exception as exc:
            try:
                self._restore(snapshot)
            except Exception as rollback_exc:
                raise WorkflowError(
                    f"应用测量清单失败且快照恢复失败：{exc}; rollback={rollback_exc}"
                ) from exc
            raise WorkflowError(f"应用测量清单失败，已恢复快照：{exc}") from exc
        return {
            "status": "applied",
            "manifest_digest": manifest.digest(),
            "precondition_digest": snapshot.digest(),
            "channel_count": len(manifest.channels),
            "task_count": plan["task_count"],
            "running": bool(self.canape.is_measurement_running()),
            "trigger_application": plan["trigger_application"],
        }

    def apply(self, manifest: MeasurementManifest) -> dict[str, Any]:
        return self._apply_with_snapshot(manifest, self.capture(manifest))

    def reconnect_and_restore(
        self,
        device: str,
        manifest: MeasurementManifest,
        *,
        download: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.capture(manifest)
        was_running = snapshot.running
        if was_running:
            self.canape.stop_measurement()
        self.canape.reconnect_device(
            device,
            download=download,
            restore_measurement=False,
        )
        result = self._apply_with_snapshot(manifest, snapshot)
        if was_running and not self.canape.is_measurement_running():
            self.canape.start_measurement()
        return {
            **result,
            "device": device,
            "reconnected": True,
            "restored_running_state": was_running,
            "running": bool(self.canape.is_measurement_running()),
        }


class MeasurementArtifactVerifier:
    """校验 MDF/MF4 录制产物存在性、哈希及可选信号/时长。"""

    @staticmethod
    def verify(
        path: str | Path,
        *,
        minimum_bytes: int = 1,
        expected_channels: tuple[str, ...] = (),
        minimum_duration_seconds: float = 0.0,
        deep: bool = False,
    ) -> dict[str, Any]:
        source = Path(path).expanduser().resolve()
        errors: list[str] = []
        warnings: list[str] = []
        if minimum_bytes < 0 or minimum_duration_seconds < 0:
            raise ValueError("minimum_bytes 和 minimum_duration_seconds 不能为负")
        if source.suffix.casefold() not in {".mdf", ".mf4"}:
            errors.append("录制文件扩展名必须是 MDF/MF4")
        if not source.is_file():
            errors.append("录制文件不存在")
            size = 0
            sha256 = ""
        else:
            size = source.stat().st_size
            sha256 = _file_digest(source)
            if size < minimum_bytes:
                errors.append(f"录制文件大小 {size} 小于要求 {minimum_bytes}")

        channels: list[str] = []
        duration = 0.0
        if deep and source.is_file():
            try:
                from asammdf import MDF
            except ImportError as exc:
                raise OptionalDependencyError(
                    "深度 MDF 验证需要安装 Agent2Canape[vector-files]"
                ) from exc
            with MDF(source) as mdf:
                channels = sorted(str(name) for name in mdf.channels_db)
                for name in expected_channels:
                    if name not in mdf.channels_db:
                        errors.append(f"录制文件缺少信号：{name}")
                duration_channels = expected_channels or tuple(channels)
                for name in duration_channels:
                    if name not in mdf.channels_db:
                        continue
                    try:
                        signal = mdf.get(name)
                    except Exception as exc:
                        warnings.append(f"无法读取信号 {name} 的时长：{type(exc).__name__}")
                        continue
                    if len(signal.timestamps):
                        duration = max(
                            duration,
                            float(signal.timestamps[-1] - signal.timestamps[0]),
                        )
            if duration < minimum_duration_seconds:
                errors.append(
                    f"录制时长 {duration:g}s 小于要求 {minimum_duration_seconds:g}s"
                )
        elif expected_channels or minimum_duration_seconds:
            warnings.append("未启用 deep，未校验信号清单和录制时长")

        return {
            "status": "passed" if not errors else "failed",
            "passed": not errors,
            "path": str(source),
            "size": size,
            "sha256": sha256,
            "deep": deep,
            "channel_count": len(channels),
            "duration_seconds": duration,
            "errors": errors,
            "warnings": warnings,
        }
