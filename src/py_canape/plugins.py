"""领域包和企业扩展的稳定插件协议。"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


def _mean_absolute_error(target: Any, actual: Any) -> float:
    pairs = list(zip(target, actual, strict=False))
    if not pairs:
        return 0.0
    return sum(abs(float(left) - float(right)) for left, right in pairs) / len(pairs)


def _power_balance(inputs: Any, outputs: Any) -> dict[str, float | None]:
    energy_in = sum(float(value) for value in inputs)
    energy_out = sum(float(value) for value in outputs)
    return {
        "input": energy_in,
        "output": energy_out,
        "loss": energy_in - energy_out,
        "efficiency": energy_out / energy_in if energy_in else None,
    }


def _comfort_error(target: Any, cabin: Any) -> dict[str, float]:
    errors = [
        float(actual) - float(expected)
        for expected, actual in zip(target, cabin, strict=False)
    ]
    return {
        "mae": sum(abs(value) for value in errors) / len(errors) if errors else 0.0,
        "maximum_deviation": max((abs(value) for value in errors), default=0.0),
    }


def _latency_budget(latencies: Any, budget_ms: float) -> dict[str, Any]:
    values = [float(value) for value in latencies]
    violations = [index for index, value in enumerate(values) if value > budget_ms]
    return {
        "passed": not violations,
        "maximum_ms": max(values, default=0.0),
        "violations": violations,
    }


def _bus_health(frames: int, errors: int, duration: float) -> dict[str, float]:
    safe_duration = max(float(duration), 1e-12)
    safe_frames = max(int(frames), 1)
    return {
        "frame_rate": int(frames) / safe_duration,
        "error_rate": int(errors) / safe_frames,
    }


class DomainAdapter(Protocol):
    name: str
    version: str

    def signal_aliases(self) -> Mapping[str, str]: ...

    def rules(self) -> Mapping[str, Mapping[str, Any]]: ...

    def metrics(self) -> Mapping[str, Callable[..., Any]]: ...


@dataclass(slots=True)
class BasicDomainAdapter:
    name: str
    version: str = "1.0"
    aliases: dict[str, str] = field(default_factory=dict)
    quality_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    metric_functions: dict[str, Callable[..., Any]] = field(default_factory=dict)
    required_capabilities: tuple[int, ...] = ()

    def signal_aliases(self) -> Mapping[str, str]:
        return self.aliases

    def rules(self) -> Mapping[str, Mapping[str, Any]]:
        return self.quality_rules

    def metrics(self) -> Mapping[str, Callable[..., Any]]:
        return self.metric_functions

    def validate(self) -> dict[str, Any]:
        issues = []
        if not self.aliases:
            issues.append("缺少领域信号别名")
        if not self.quality_rules:
            issues.append("缺少领域质量规则")
        if not self.metric_functions:
            issues.append("缺少领域指标")
        if any(not callable(metric) for metric in self.metric_functions.values()):
            issues.append("领域指标不可调用")
        invalid_capabilities = [
            number for number in self.required_capabilities if not 1 <= number <= 140
        ]
        if invalid_capabilities:
            issues.append(f"非法能力编号：{invalid_capabilities}")
        return {"passed": not issues, "issues": issues}


class PluginRegistry:
    ENTRY_POINT_GROUP = "py_canape.domain_adapters"

    def __init__(self) -> None:
        self.adapters: dict[str, DomainAdapter] = {}

    def register(self, adapter: DomainAdapter) -> None:
        key = adapter.name.casefold()
        if key in self.adapters:
            raise ValueError(f"领域适配器重复：{adapter.name}")
        validator = getattr(adapter, "validate", None)
        if callable(validator):
            result = validator()
            if not result.get("passed", False):
                raise ValueError(f"领域适配器无效：{adapter.name}: {result['issues']}")
        self.adapters[key] = adapter

    def discover(self) -> list[str]:
        loaded = []
        entry_points = importlib.metadata.entry_points()
        for entry in entry_points.select(group=self.ENTRY_POINT_GROUP):
            adapter = entry.load()()
            self.register(adapter)
            loaded.append(adapter.name)
        return loaded

    def get(self, name: str) -> DomainAdapter:
        return self.adapters[name.casefold()]

    def compatibility(self, name: str, minimum_version: str) -> bool:
        from packaging.version import Version

        return Version(self.get(name).version) >= Version(minimum_version)


def built_in_domains() -> PluginRegistry:
    registry = PluginRegistry()
    adapters = (
        BasicDomainAdapter(
            name="powertrain",
            aliases={"driver_torque_request": "DrvTrqReq", "actual_torque": "ActTrq"},
            quality_rules={"ActTrq": {"minimum": -1000, "maximum": 2000, "max_rate": 10000}},
            metric_functions={"torque_tracking_mae": _mean_absolute_error},
            required_capabilities=(83, 86, 89, 123),
        ),
        BasicDomainAdapter(
            name="chassis",
            aliases={"steering_request": "SteerReq", "yaw_rate": "YawRate"},
            quality_rules={"YawRate": {"minimum": -180, "maximum": 180, "max_rate": 720}},
            metric_functions={"tracking_mae": _mean_absolute_error},
            required_capabilities=(83, 87, 88, 124),
        ),
        BasicDomainAdapter(
            name="body",
            aliases={"body_mode": "BodyMode", "door_state": "DoorState"},
            quality_rules={"BodyMode": {"invalid_values": [255], "freeze_seconds": 300}},
            metric_functions={"actuation_mae": _mean_absolute_error},
            required_capabilities=(84, 85, 120, 122),
        ),
        BasicDomainAdapter(
            name="electric_powertrain",
            aliases={"battery_power": "BattPwr", "motor_power": "MotPwr"},
            quality_rules={"BattPwr": {"minimum": -500, "maximum": 500, "max_rate": 5000}},
            metric_functions={"power_balance": _power_balance},
            required_capabilities=(86, 125, 126),
        ),
        BasicDomainAdapter(
            name="thermal_hvac",
            aliases={"cabin_temperature": "CabinTemp", "cabin_target": "CabinTempTar"},
            quality_rules={"CabinTemp": {"minimum": -40, "maximum": 85, "max_rate": 5}},
            metric_functions={"comfort_error": _comfort_error, "energy_balance": _power_balance},
            required_capabilities=(85, 86, 122, 125),
        ),
        BasicDomainAdapter(
            name="adas",
            aliases={"adas_request": "ADASReq", "degradation_state": "ADASDegrade"},
            quality_rules={"ADASDegrade": {"invalid_values": [255], "freeze_seconds": 60}},
            metric_functions={"latency_budget": _latency_budget},
            required_capabilities=(83, 87, 121, 127),
        ),
        BasicDomainAdapter(
            name="network_diagnostics",
            aliases={"diagnostic_session": "DiagSession", "bus_off": "BusOff"},
            quality_rules={"BusOff": {"minimum": 0, "maximum": 0}},
            metric_functions={"bus_health": _bus_health},
            required_capabilities=(61, 64, 71, 72, 74),
        ),
    )
    for adapter in adapters:
        registry.register(adapter)
    return registry
