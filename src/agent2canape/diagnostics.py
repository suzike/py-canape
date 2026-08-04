"""UDS 诊断清单、响应语义、状态机与 DTC 证据。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import SafetyViolationError


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


NRC_DEFINITIONS: dict[int, tuple[str, str, bool, str]] = {
    0x10: ("generalReject", "request", False, "检查请求格式和 ECU 前置状态"),
    0x11: ("serviceNotSupported", "capability", False, "核对会话和 ECU 服务清单"),
    0x12: ("subFunctionNotSupported", "capability", False, "核对子功能与当前会话"),
    0x13: ("incorrectMessageLengthOrInvalidFormat", "request", False, "修正长度或编码"),
    0x14: ("responseTooLong", "transport", False, "检查传输层和响应缓冲配置"),
    0x21: ("busyRepeatRequest", "temporary", True, "按项目退避策略有限重试"),
    0x22: ("conditionsNotCorrect", "precondition", False, "采集并满足车辆与 ECU 前置条件"),
    0x24: ("requestSequenceError", "state", False, "恢复正确的诊断步骤顺序"),
    0x25: ("noResponseFromSubnetComponent", "network", True, "检查子网 ECU 与网关状态"),
    0x26: ("failurePreventsExecution", "ecu-fault", False, "读取 DTC 并排除 ECU 内部故障"),
    0x31: ("requestOutOfRange", "request", False, "核对 DID、Routine、地址和参数范围"),
    0x33: ("securityAccessDenied", "security", False, "进入正确安全等级并核对授权提供器"),
    0x35: ("invalidKey", "security", False, "停止重试并检查外部 Seed/Key 提供器"),
    0x36: ("exceedNumberOfAttempts", "security", False, "停止访问并等待 ECU 解锁策略"),
    0x37: ("requiredTimeDelayNotExpired", "security", True, "等待 ECU 指定延迟后再申请 Seed"),
    0x70: ("uploadDownloadNotAccepted", "transfer", False, "核对刷写会话、地址和文件身份"),
    0x71: ("transferDataSuspended", "transfer", True, "按受控恢复策略续传或安全退出"),
    0x72: ("generalProgrammingFailure", "programming", False, "停止刷写并进入救援流程"),
    0x73: ("wrongBlockSequenceCounter", "transfer", True, "从 ECU 确认的块序号恢复"),
    0x78: ("responsePending", "timing", True, "继续等待 P2*；不要重复发送非幂等请求"),
    0x7E: ("subFunctionNotSupportedInActiveSession", "session", False, "切换到允许的会话"),
    0x7F: ("serviceNotSupportedInActiveSession", "session", False, "切换到允许的会话"),
    0x81: ("rpmTooHigh", "vehicle-state", False, "降低发动机转速"),
    0x82: ("rpmTooLow", "vehicle-state", False, "满足发动机转速条件"),
    0x83: ("engineIsRunning", "vehicle-state", False, "按项目要求停止发动机"),
    0x84: ("engineIsNotRunning", "vehicle-state", False, "按项目要求启动发动机"),
    0x85: ("engineRunTimeTooLow", "vehicle-state", True, "等待最小运行时间"),
    0x86: ("temperatureTooHigh", "vehicle-state", False, "恢复允许温度窗口"),
    0x87: ("temperatureTooLow", "vehicle-state", False, "恢复允许温度窗口"),
    0x88: ("vehicleSpeedTooHigh", "vehicle-state", False, "停车并确认车速信号可信"),
    0x89: ("vehicleSpeedTooLow", "vehicle-state", False, "满足项目车速条件"),
    0x8A: ("throttlePedalTooHigh", "vehicle-state", False, "释放加速踏板"),
    0x8F: ("brakeSwitchNotClosed", "vehicle-state", False, "满足制动开关条件"),
    0x90: ("shifterLeverNotInPark", "vehicle-state", False, "切换到 P 挡并确认档位"),
    0x92: ("voltageTooHigh", "power", False, "恢复允许供电上限"),
    0x93: ("voltageTooLow", "power", False, "恢复允许供电下限"),
}


@dataclass(frozen=True, slots=True)
class NRCInfo:
    code: int
    name: str
    category: str
    retryable: bool
    remediation: str

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "hex": f"0x{self.code:02X}"}


def interpret_nrc(code: int) -> NRCInfo:
    number = int(code)
    name, category, retryable, remediation = NRC_DEFINITIONS.get(
        number,
        ("unknownNegativeResponseCode", "unknown", False, "查阅 ECU 诊断规范并保留原始响应"),
    )
    return NRCInfo(number, name, category, retryable, remediation)


@dataclass(frozen=True, slots=True)
class UDSResponse:
    positive: bool
    service_id: int | None
    request_service_id: int | None
    payload: tuple[int, ...]
    sender: str = ""
    nrc: NRCInfo | None = None
    transport_response_code: int = 0

    @classmethod
    def parse(
        cls,
        stream: Any,
        *,
        positive: bool | None = None,
        sender: str = "",
        response_code: int = 0,
    ) -> UDSResponse:
        data = tuple(int(item) for item in stream)
        if any(item < 0 or item > 255 for item in data):
            raise ValueError("诊断响应必须是 0~255 字节序列")
        if len(data) >= 3 and data[0] == 0x7F:
            return cls(
                positive=False,
                service_id=0x7F,
                request_service_id=data[1],
                payload=data,
                sender=sender,
                nrc=interpret_nrc(data[2]),
                transport_response_code=int(response_code),
            )
        response_sid = data[0] if data else None
        request_sid = (
            response_sid - 0x40
            if response_sid is not None and 0x40 <= response_sid <= 0x7E
            else None
        )
        return cls(
            positive=bool(positive) if positive is not None else bool(data),
            service_id=response_sid,
            request_service_id=request_sid,
            payload=data,
            sender=sender,
            transport_response_code=int(response_code),
        )

    @classmethod
    def from_canape(cls, response: Any) -> UDSResponse:
        return cls.parse(
            response.stream,
            positive=bool(response.positive),
            sender=str(response.sender),
            response_code=int(response.response_code),
        )

    def public(self) -> dict[str, Any]:
        return {
            "positive": self.positive,
            "service_id": self.service_id,
            "request_service_id": self.request_service_id,
            "payload": list(self.payload),
            "sender": self.sender,
            "nrc": self.nrc.public() if self.nrc else None,
            "transport_response_code": self.transport_response_code,
        }


VALID_SESSIONS = {"default", "programming", "extended", "safety_system"}


@dataclass(frozen=True, slots=True)
class DiagnosticStepSpec:
    id: str
    device: str = ""
    service: str = ""
    payload: tuple[int, ...] = ()
    parameters: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float | None = None
    p2_star_timeout_seconds: float | None = None
    expected_positive: bool = True
    allowed_nrc: tuple[int, ...] = ()
    required_session: str = ""
    required_security_level: int | None = None
    transition_session: str = ""
    transition_security_level: int | None = None
    tester_present: bool = False

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> DiagnosticStepSpec:
        payload = tuple(
            int(item, 0) if isinstance(item, str) else int(item)
            for item in value.get("payload", ())
        )
        allowed = tuple(
            int(item, 0) if isinstance(item, str) else int(item)
            for item in value.get("allowed_nrc", ())
        )
        return cls(
            id=str(value["id"]),
            device=str(value.get("device", "")),
            service=str(value.get("service", "")),
            payload=payload,
            parameters=dict(value.get("parameters", {})),
            timeout_seconds=float(value["timeout_seconds"])
            if value.get("timeout_seconds") is not None
            else None,
            p2_star_timeout_seconds=float(value["p2_star_timeout_seconds"])
            if value.get("p2_star_timeout_seconds") is not None
            else None,
            expected_positive=bool(value.get("expected_positive", True)),
            allowed_nrc=allowed,
            required_session=str(value.get("required_session", "")).casefold(),
            required_security_level=int(value["required_security_level"])
            if value.get("required_security_level") is not None
            else None,
            transition_session=str(value.get("transition_session", "")).casefold(),
            transition_security_level=int(value["transition_security_level"])
            if value.get("transition_security_level") is not None
            else None,
            tester_present=bool(value.get("tester_present", False)),
        )


@dataclass(frozen=True, slots=True)
class DiagnosticManifest:
    name: str
    default_device: str
    steps: tuple[DiagnosticStepSpec, ...]
    p2_timeout_seconds: float = 0.05
    p2_star_timeout_seconds: float = 5.0
    tester_present: bool = False
    stop_on_failure: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> DiagnosticManifest:
        if not isinstance(value, dict):
            raise ValueError("诊断清单根节点必须是对象")
        return cls(
            name=str(value.get("name", "diagnostic-sequence")),
            default_device=str(value.get("default_device", "")),
            steps=tuple(DiagnosticStepSpec.from_mapping(item) for item in value.get("steps", ())),
            p2_timeout_seconds=float(value.get("p2_timeout_seconds", 0.05)),
            p2_star_timeout_seconds=float(value.get("p2_star_timeout_seconds", 5.0)),
            tester_present=bool(value.get("tester_present", False)),
            stop_on_failure=bool(value.get("stop_on_failure", True)),
            metadata=dict(value.get("metadata", {})),
        )

    @classmethod
    def load(cls, path: str | Path) -> DiagnosticManifest:
        source = Path(path)
        text = source.read_text(encoding="utf-8")
        value = json.loads(text) if source.suffix.casefold() == ".json" else yaml.safe_load(text)
        return cls.from_mapping(value)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "default_device": self.default_device,
            "steps": [
                {
                    **asdict(step),
                    "payload": list(step.payload),
                    "allowed_nrc": list(step.allowed_nrc),
                }
                for step in self.steps
            ],
            "p2_timeout_seconds": self.p2_timeout_seconds,
            "p2_star_timeout_seconds": self.p2_star_timeout_seconds,
            "tester_present": self.tester_present,
            "stop_on_failure": self.stop_on_failure,
            "metadata": self.metadata,
        }

    def digest(self) -> str:
        return _digest(self.public())

    def validate(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not self.name.strip():
            errors.append("诊断清单 name 不能为空")
        if not self.default_device and any(not step.device for step in self.steps):
            errors.append("缺少 default_device，且存在未指定 device 的步骤")
        if not self.steps:
            errors.append("诊断清单至少需要一个步骤")
        if self.p2_timeout_seconds <= 0 or self.p2_star_timeout_seconds <= 0:
            errors.append("P2/P2* 超时必须大于 0")
        if self.p2_star_timeout_seconds < self.p2_timeout_seconds:
            errors.append("P2* 超时不能小于 P2 超时")
        ids: set[str] = set()
        for step in self.steps:
            if not step.id or step.id in ids:
                errors.append(f"步骤 ID 为空或重复：{step.id!r}")
            ids.add(step.id)
            if bool(step.service) == bool(step.payload):
                errors.append(f"{step.id} 必须且只能定义 service 或 payload")
            if step.payload and (any(item < 0 or item > 255 for item in step.payload)):
                errors.append(f"{step.id} payload 包含非字节值")
            if step.timeout_seconds is not None and step.timeout_seconds <= 0:
                errors.append(f"{step.id} timeout_seconds 必须大于 0")
            if step.p2_star_timeout_seconds is not None and step.p2_star_timeout_seconds <= 0:
                errors.append(f"{step.id} p2_star_timeout_seconds 必须大于 0")
            for session in (step.required_session, step.transition_session):
                if session and session not in VALID_SESSIONS:
                    errors.append(f"{step.id} 未知诊断会话：{session}")
            for level in (
                step.required_security_level,
                step.transition_security_level,
            ):
                if level is not None and not 0 <= level <= 255:
                    errors.append(f"{step.id} 安全等级必须在 0~255 之间")
            for code in step.allowed_nrc:
                if code < 0 or code > 255:
                    errors.append(f"{step.id} allowed_nrc 包含非字节值")
                elif code == 0x78:
                    warnings.append(
                        f"{step.id} 允许 NRC 0x78 仅表示记录 ResponsePending，不会重复发送请求"
                    )
        return {"passed": not errors, "errors": errors, "warnings": warnings}

    def require_valid(self) -> None:
        result = self.validate()
        if not result["passed"]:
            raise SafetyViolationError("；".join(result["errors"]))

    def plan(self) -> dict[str, Any]:
        validation = self.validate()
        return {
            **validation,
            "name": self.name,
            "digest": self.digest(),
            "step_count": len(self.steps),
            "devices": sorted({step.device or self.default_device for step in self.steps}),
            "p2_timeout_seconds": self.p2_timeout_seconds,
            "p2_star_timeout_seconds": self.p2_star_timeout_seconds,
            "tester_present": self.tester_present,
            "steps": [
                {
                    "id": step.id,
                    "device": step.device or self.default_device,
                    "request": step.service or [f"0x{item:02X}" for item in step.payload],
                    "timeout_seconds": step.timeout_seconds
                    or step.p2_star_timeout_seconds
                    or self.p2_star_timeout_seconds,
                    "p2_star_timeout_seconds": step.p2_star_timeout_seconds
                    or self.p2_star_timeout_seconds,
                    "required_session": step.required_session or "any",
                    "required_security_level": step.required_security_level,
                    "allowed_nrc": [interpret_nrc(code).public() for code in step.allowed_nrc],
                }
                for step in self.steps
            ],
        }


@dataclass(slots=True)
class DiagnosticDeviceState:
    session: str = "default"
    security_level: int = 0
    tester_present: bool = False

    def public(self) -> dict[str, Any]:
        return asdict(self)


class DiagnosticSequenceRunner:
    """按清单执行诊断请求；审批仍由上层 AI/CLI 安全层负责。"""

    def __init__(self, canape: Any) -> None:
        self.canape = canape

    def execute(self, manifest: DiagnosticManifest) -> dict[str, Any]:
        manifest.require_valid()
        started = datetime.now(UTC)
        states: dict[str, DiagnosticDeviceState] = {}
        tester_started: set[str] = set()
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            for step in manifest.steps:
                device = step.device or manifest.default_device
                state = states.setdefault(device, DiagnosticDeviceState())
                before = state.public()
                step_errors: list[str] = []
                if step.required_session and state.session != step.required_session:
                    step_errors.append(f"要求会话 {step.required_session}，当前为 {state.session}")
                if (
                    step.required_security_level is not None
                    and state.security_level != step.required_security_level
                ):
                    step_errors.append(
                        f"要求安全等级 {step.required_security_level}，"
                        f"当前为 {state.security_level}"
                    )
                if step_errors:
                    result = {
                        "id": step.id,
                        "device": device,
                        "passed": False,
                        "executed": False,
                        "errors": step_errors,
                        "state_before": before,
                        "state_after": state.public(),
                        "responses": [],
                    }
                    results.append(result)
                    errors.extend(f"{step.id}: {item}" for item in step_errors)
                    if manifest.stop_on_failure:
                        break
                    continue
                if (
                    manifest.tester_present or step.tester_present
                ) and device not in tester_started:
                    try:
                        self.canape.set_tester_present(device, enabled=True)
                    except Exception as exc:
                        message = (
                            "启动 Tester Present 失败："
                            f"{type(exc).__name__}: {exc}"
                        )
                        results.append(
                            {
                                "id": step.id,
                                "device": device,
                                "passed": False,
                                "executed": False,
                                "errors": [message],
                                "state_before": before,
                                "state_after": state.public(),
                                "responses": [],
                            }
                        )
                        errors.append(f"{step.id}: {message}")
                        if manifest.stop_on_failure:
                            break
                        continue
                    tester_started.add(device)
                    state.tester_present = True
                timeout = (
                    step.timeout_seconds
                    or step.p2_star_timeout_seconds
                    or manifest.p2_star_timeout_seconds
                )
                tick = time.monotonic()
                try:
                    if step.payload:
                        raw = self.canape.send_raw_diagnostic_request(
                            device, step.payload, timeout=timeout
                        )
                        request_sid = step.payload[0]
                    else:
                        raw = self.canape.send_diagnostic_request(
                            device,
                            step.service,
                            parameters=step.parameters,
                            timeout=timeout,
                        )
                        request_sid = None
                except Exception as exc:
                    message = f"诊断请求异常：{type(exc).__name__}: {exc}"
                    results.append(
                        {
                            "id": step.id,
                            "device": device,
                            "passed": False,
                            "executed": True,
                            "duration_seconds": time.monotonic() - tick,
                            "errors": [message],
                            "state_before": before,
                            "state_after": state.public(),
                            "responses": [],
                        }
                    )
                    errors.append(f"{step.id}: {message}")
                    if manifest.stop_on_failure:
                        break
                    continue
                responses = [UDSResponse.from_canape(item) for item in raw]
                negatives = [item for item in responses if not item.positive]
                positives = [item for item in responses if item.positive]
                disallowed = [
                    item
                    for item in negatives
                    if item.nrc is None or item.nrc.code not in step.allowed_nrc
                ]
                service_mismatch = [
                    item
                    for item in positives
                    if request_sid is not None
                    and item.request_service_id != request_sid
                ]
                passed = bool(positives) if step.expected_positive else bool(negatives)
                passed = passed and not disallowed and not service_mismatch
                if not responses:
                    step_errors.append("未返回诊断响应")
                if disallowed:
                    step_errors.extend(
                        f"不允许的 NRC 0x{item.nrc.code:02X} {item.nrc.name}"
                        if item.nrc
                        else "无法解释的负响应"
                        for item in disallowed
                    )
                if service_mismatch:
                    step_errors.append("正响应服务 ID 与请求不匹配")
                if passed:
                    if step.transition_session:
                        state.session = step.transition_session
                    if step.transition_security_level is not None:
                        state.security_level = step.transition_security_level
                result = {
                    "id": step.id,
                    "device": device,
                    "passed": passed,
                    "executed": True,
                    "duration_seconds": time.monotonic() - tick,
                    "errors": step_errors,
                    "state_before": before,
                    "state_after": state.public(),
                    "responses": [item.public() for item in responses],
                }
                results.append(result)
                if not passed:
                    errors.extend(
                        f"{step.id}: {item}" for item in step_errors or ["响应不满足期望"]
                    )
                    if manifest.stop_on_failure:
                        break
        finally:
            for device in sorted(tester_started):
                try:
                    self.canape.set_tester_present(device, enabled=False)
                    states[device].tester_present = False
                except Exception as exc:
                    errors.append(f"{device}: 停止 Tester Present 失败：{exc}")
        completed = datetime.now(UTC)
        report = {
            "name": manifest.name,
            "manifest_digest": manifest.digest(),
            "started_utc": started.isoformat(),
            "completed_utc": completed.isoformat(),
            "passed": len(results) == len(manifest.steps)
            and all(item["passed"] for item in results)
            and not errors,
            "errors": errors,
            "steps": results,
            "final_states": {device: state.public() for device, state in sorted(states.items())},
        }
        report["evidence_digest"] = _digest(
            {
                key: value
                for key, value in report.items()
                if key not in {"started_utc", "completed_utc", "evidence_digest"}
            }
        )
        return report


@dataclass(frozen=True, slots=True)
class DTCRecord:
    code: str
    status: int
    raw_code: int

    def public(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "status_hex": f"0x{self.status:02X}",
            "raw_code": self.raw_code,
        }


@dataclass(frozen=True, slots=True)
class DTCSnapshot:
    records: tuple[DTCRecord, ...]
    status_availability_mask: int = 0
    subfunction: int = 0
    captured_utc: str = ""
    source: str = ""

    @classmethod
    def parse_uds(cls, stream: Any, *, source: str = "") -> DTCSnapshot:
        data = tuple(int(item) for item in stream)
        if len(data) < 3 or data[0] != 0x59:
            raise ValueError("DTC 响应必须以 UDS 正响应服务 0x59 开始")
        payload = data[3:]
        if len(payload) % 4:
            raise ValueError("DTC 记录区必须由 3 字节故障码和 1 字节状态组成")
        records = []
        for offset in range(0, len(payload), 4):
            raw_code = (payload[offset] << 16) | (payload[offset + 1] << 8) | payload[offset + 2]
            records.append(DTCRecord(f"{raw_code:06X}", payload[offset + 3], raw_code))
        return cls(tuple(records), data[2], data[1], datetime.now(UTC).isoformat(), source)

    def public(self) -> dict[str, Any]:
        return {
            "records": [item.public() for item in self.records],
            "count": len(self.records),
            "status_availability_mask": self.status_availability_mask,
            "status_availability_mask_hex": f"0x{self.status_availability_mask:02X}",
            "subfunction": self.subfunction,
            "captured_utc": self.captured_utc,
            "source": self.source,
            "digest": self.digest(),
        }

    def digest(self) -> str:
        return _digest(
            {
                "records": [item.public() for item in self.records],
                "mask": self.status_availability_mask,
                "subfunction": self.subfunction,
                "source": self.source,
            }
        )

    def diff(self, other: DTCSnapshot) -> dict[str, Any]:
        before = {item.code: item for item in self.records}
        after = {item.code: item for item in other.records}
        return {
            "added": [after[code].public() for code in sorted(after.keys() - before.keys())],
            "removed": [before[code].public() for code in sorted(before.keys() - after.keys())],
            "status_changed": [
                {"code": code, "before": before[code].status, "after": after[code].status}
                for code in sorted(before.keys() & after.keys())
                if before[code].status != after[code].status
            ],
        }
