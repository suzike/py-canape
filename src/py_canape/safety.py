"""危险操作的权限、白名单、范围、车辆前置条件和秘密提供协议。"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol

from .errors import SafetyViolationError


class PermissionLevel(IntEnum):
    READ_ONLY = 0
    CALIBRATION_WRITE = 10
    MEMORY_WRITE = 20
    DOWNLOAD = 30
    DIAGNOSTIC = 40
    FLASH = 50


class SecretProvider(Protocol):
    def get_secret(self, name: str) -> str: ...


class EnvironmentSecretProvider:
    def __init__(self, prefix: str = "PY_CANAPE_SECRET_") -> None:
        self.prefix = prefix

    def get_secret(self, name: str) -> str:
        key = f"{self.prefix}{name.upper()}"
        value = os.getenv(key)
        if value is None:
            raise KeyError(f"环境变量 {key} 未设置")
        return value


@dataclass(frozen=True, slots=True)
class ValueRule:
    minimum: float | None = None
    maximum: float | None = None
    allowed: frozenset[Any] | None = None

    def validate(self, value: Any) -> bool:
        if self.allowed is not None and value not in self.allowed:
            return False
        if self.minimum is not None and float(value) < self.minimum:
            return False
        return not (self.maximum is not None and float(value) > self.maximum)


@dataclass(slots=True)
class SafetyPolicy:
    maximum_permission: PermissionLevel = PermissionLevel.READ_ONLY
    object_rules: dict[str, ValueRule] = field(default_factory=dict)
    address_ranges: list[tuple[int, int]] = field(default_factory=list)
    preconditions: dict[str, ValueRule] = field(default_factory=dict)
    allowed_devices: set[str] = field(default_factory=set)
    require_confirmation: bool = True

    def authorize(
        self,
        permission: PermissionLevel,
        *,
        device: str | None = None,
        target: str | None = None,
        value: Any = None,
        address: int | None = None,
        size: int = 1,
        vehicle_state: Mapping[str, Any] | None = None,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        errors: list[str] = []
        if permission > self.maximum_permission:
            errors.append(
                f"权限不足：需要 {permission.name}，策略上限为 {self.maximum_permission.name}"
            )
        if (
            self.require_confirmation
            and permission > PermissionLevel.READ_ONLY
            and not confirmed
        ):
            errors.append("危险操作缺少显式 confirmed=True")
        if device and self.allowed_devices and device not in self.allowed_devices:
            errors.append(f"设备不在白名单：{device}")
        if target is not None:
            rule = self.object_rules.get(target)
            if rule is None and self.object_rules:
                errors.append(f"对象不在白名单：{target}")
            elif rule is not None and not rule.validate(value):
                errors.append(f"对象 {target} 的值 {value!r} 不符合范围")
        if address is not None:
            end = address + max(size, 1) - 1
            if not any(start <= address and end <= stop for start, stop in self.address_ranges):
                errors.append(f"地址范围未授权：0x{address:X}..0x{end:X}")
        state = vehicle_state or {}
        for signal, rule in self.preconditions.items():
            if signal not in state:
                errors.append(f"缺少车辆安全前置条件：{signal}")
            elif not rule.validate(state[signal]):
                errors.append(f"车辆安全条件不满足：{signal}={state[signal]!r}")
        if errors:
            raise SafetyViolationError("; ".join(errors))
        return {
            "authorized": True,
            "permission": permission.name,
            "device": device,
            "target": target,
            "address": address,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SafetyPolicy:
        return cls(
            maximum_permission=PermissionLevel[
                str(data.get("maximum_permission", "READ_ONLY")).upper()
            ],
            object_rules={
                name: ValueRule(
                    minimum=rule.get("minimum"),
                    maximum=rule.get("maximum"),
                    allowed=(
                        frozenset(rule["allowed"]) if "allowed" in rule else None
                    ),
                )
                for name, rule in data.get("object_rules", {}).items()
            },
            address_ranges=[
                (int(item[0], 0) if isinstance(item[0], str) else int(item[0]),
                 int(item[1], 0) if isinstance(item[1], str) else int(item[1]))
                for item in data.get("address_ranges", ())
            ],
            preconditions={
                name: ValueRule(
                    minimum=rule.get("minimum"),
                    maximum=rule.get("maximum"),
                    allowed=(
                        frozenset(rule["allowed"]) if "allowed" in rule else None
                    ),
                )
                for name, rule in data.get("preconditions", {}).items()
            },
            allowed_devices=set(data.get("allowed_devices", ())),
            require_confirmation=bool(data.get("require_confirmation", True)),
        )


class SafeCANape:
    """将 SafetyPolicy 应用于高风险 CANape 方法。"""

    def __init__(self, canape: Any, policy: SafetyPolicy) -> None:
        self.canape = canape
        self.policy = policy

    def write_calibration(
        self,
        device: str,
        name: str,
        value: Any,
        *,
        vehicle_state: Mapping[str, Any],
        confirmed: bool = False,
    ) -> Any:
        self.policy.authorize(
            PermissionLevel.CALIBRATION_WRITE,
            device=device,
            target=name,
            value=value,
            vehicle_state=vehicle_state,
            confirmed=confirmed,
        )
        return self.canape.write_calibration_value(device, name, value)

    def write_memory(
        self,
        device: str,
        address: int,
        data: Sequence[int],
        *,
        vehicle_state: Mapping[str, Any],
        confirmed: bool = False,
    ) -> tuple[int, ...]:
        self.policy.authorize(
            PermissionLevel.MEMORY_WRITE,
            device=device,
            address=address,
            size=len(data),
            vehicle_state=vehicle_state,
            confirmed=confirmed,
        )
        return self.canape.write_memory(device, address, data)

    def flash(
        self,
        device: str,
        job: Any,
        session: Any,
        *,
        vehicle_state: Mapping[str, Any],
        confirmed: bool = False,
        config_file: str | None = None,
    ) -> None:
        self.policy.authorize(
            PermissionLevel.FLASH,
            device=device,
            vehicle_state=vehicle_state,
            confirmed=confirmed,
        )
        self.canape.start_flash(device, job, session, config_file=config_file)
