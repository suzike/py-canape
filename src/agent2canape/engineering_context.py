"""AI 工程请求中的对象消歧与受控单位换算。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


def _normalize(text: str) -> str:
    return re.sub(r"[\s_\-./\\]+", "", text).casefold()


@dataclass(frozen=True, slots=True)
class EngineeringObject:
    """可供 AI 检索的工程对象，不包含在线写能力。"""

    device: str
    name: str
    aliases: tuple[str, ...] = ()
    unit: str = ""
    kind: str = "scalar"
    minimum: float | None = None
    maximum: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> EngineeringObject:
        aliases = value.get("aliases", ())
        if isinstance(aliases, str):
            aliases = (aliases,)
        return cls(
            device=str(value.get("device", "")),
            name=str(value["name"]),
            aliases=tuple(str(item) for item in aliases),
            unit=str(value.get("unit", "")),
            kind=str(value.get("kind", "scalar")),
            minimum=(
                float(value["minimum"]) if value.get("minimum") is not None else None
            ),
            maximum=(
                float(value["maximum"]) if value.get("maximum") is not None else None
            ),
            metadata=dict(value.get("metadata", {})),
        )

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _UnitDefinition:
    dimension: str
    scale: float
    offset: float = 0.0


class EngineeringUnitConverter:
    """只转换显式注册且物理维度相同的单位。"""

    _UNITS: dict[str, _UnitDefinition] = {}

    @classmethod
    def _register(
        cls,
        aliases: tuple[str, ...],
        dimension: str,
        scale: float,
        offset: float = 0.0,
    ) -> None:
        definition = _UnitDefinition(dimension, scale, offset)
        for alias in aliases:
            cls._UNITS[_normalize(alias)] = definition

    @classmethod
    def supports(cls, unit: str) -> bool:
        return not unit or _normalize(unit) in cls._UNITS

    @classmethod
    def convert(cls, value: float, source: str, target: str) -> float:
        if not source and not target:
            return float(value)
        if source and not target:
            raise ValueError("工程对象未声明目标单位，不能安全换算")
        if not source or _normalize(source) == _normalize(target):
            return float(value)
        source_definition = cls._UNITS.get(_normalize(source))
        target_definition = cls._UNITS.get(_normalize(target))
        if source_definition is None:
            raise ValueError(f"不支持源单位：{source}")
        if target_definition is None:
            raise ValueError(f"不支持目标单位：{target}")
        if source_definition.dimension != target_definition.dimension:
            raise ValueError(f"单位维度不一致：{source} -> {target}")
        base = float(value) * source_definition.scale + source_definition.offset
        return (base - target_definition.offset) / target_definition.scale


EngineeringUnitConverter._register(("°C", "℃", "degC", "celsius"), "temperature", 1.0, 273.15)
EngineeringUnitConverter._register(("K", "kelvin"), "temperature", 1.0)
EngineeringUnitConverter._register(
    ("°F", "degF", "fahrenheit"),
    "temperature",
    5.0 / 9.0,
    255.3722222222,
)
EngineeringUnitConverter._register(("Pa",), "pressure", 1.0)
EngineeringUnitConverter._register(("kPa",), "pressure", 1_000.0)
EngineeringUnitConverter._register(("MPa",), "pressure", 1_000_000.0)
EngineeringUnitConverter._register(("bar",), "pressure", 100_000.0)
EngineeringUnitConverter._register(("ms",), "time", 0.001)
EngineeringUnitConverter._register(("s", "sec"), "time", 1.0)
EngineeringUnitConverter._register(("min", "minute"), "time", 60.0)
EngineeringUnitConverter._register(("W",), "power", 1.0)
EngineeringUnitConverter._register(("kW",), "power", 1_000.0)
EngineeringUnitConverter._register(("mV",), "voltage", 0.001)
EngineeringUnitConverter._register(("V",), "voltage", 1.0)
EngineeringUnitConverter._register(("mA",), "current", 0.001)
EngineeringUnitConverter._register(("A",), "current", 1.0)
EngineeringUnitConverter._register(("g/s",), "mass_flow", 0.001)
EngineeringUnitConverter._register(("kg/s",), "mass_flow", 1.0)
EngineeringUnitConverter._register(("kg/h",), "mass_flow", 1.0 / 3600.0)
EngineeringUnitConverter._register(("%", "percent"), "ratio", 0.01)
EngineeringUnitConverter._register(("ratio", "1"), "ratio", 1.0)
EngineeringUnitConverter._register(("Nm", "N*m", "N·m"), "torque", 1.0)
EngineeringUnitConverter._register(("rpm", "r/min"), "rotational_speed", 1.0)


class EngineeringContextResolver:
    """从 JSON 可序列化上下文中解析 ECU、对象和目标值。"""

    _TARGET_PATTERN = re.compile(
        r"(?:改为|设为|设置为|调整为|修改为|写为|to|=)\s*"
        r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
        r"(?P<unit>[%°℃a-zA-Z][%°℃a-zA-Z0-9*/·^-]*)?",
        re.IGNORECASE,
    )

    @classmethod
    def validate(cls, context: dict[str, Any]) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        objects: list[EngineeringObject] = []
        for index, value in enumerate(context.get("objects", ())):
            try:
                item = (
                    value
                    if isinstance(value, EngineeringObject)
                    else EngineeringObject.from_mapping(value)
                )
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"objects[{index}] 无效：{exc}")
                continue
            if not item.name.strip():
                errors.append(f"objects[{index}] name 不能为空")
            if (
                item.minimum is not None
                and item.maximum is not None
                and item.minimum > item.maximum
            ):
                errors.append(f"{item.device}/{item.name} 下限大于上限")
            if not EngineeringUnitConverter.supports(item.unit):
                errors.append(f"{item.device}/{item.name} 使用不支持的单位：{item.unit}")
            objects.append(item)

        identities: set[tuple[str, str]] = set()
        aliases: dict[str, list[str]] = {}
        for item in objects:
            identity = (item.device.casefold(), item.name.casefold())
            if identity in identities:
                errors.append(f"工程对象重复：{item.device}/{item.name}")
            identities.add(identity)
            for alias in (item.name, *item.aliases):
                key = _normalize(alias)
                if key:
                    aliases.setdefault(key, []).append(f"{item.device}/{item.name}")
        for alias, targets in aliases.items():
            unique_targets = sorted(set(targets))
            if len(unique_targets) > 1:
                warnings.append(
                    f"别名 {alias} 对应多个对象，调用时必须提供 ECU："
                    + ", ".join(unique_targets)
                )
        return {
            "passed": not errors,
            "object_count": len(objects),
            "errors": errors,
            "warnings": warnings,
        }

    @classmethod
    def resolve(
        cls,
        text: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        definitions = context.get("objects", ())
        objects = [
            item if isinstance(item, EngineeringObject) else EngineeringObject.from_mapping(item)
            for item in definitions
        ]
        normalized_text = _normalize(text)
        scored: list[tuple[int, EngineeringObject, str]] = []
        for item in objects:
            for index, alias in enumerate((item.name, *item.aliases)):
                normalized_alias = _normalize(alias)
                if normalized_alias and normalized_alias in normalized_text:
                    score = len(normalized_alias) * 10 + (1 if index == 0 else 0)
                    scored.append((score, item, alias))
        if not scored:
            return {"status": "unresolved", "candidates": []}
        best_score = max(score for score, _, _ in scored)
        best: dict[tuple[str, str], tuple[EngineeringObject, str]] = {}
        for score, item, alias in scored:
            if score == best_score:
                best[(item.device.casefold(), item.name.casefold())] = (item, alias)
        if len(best) > 1:
            explicit_device = str(context.get("device", ""))
            matching_devices = {
                item.device.casefold()
                for item, _ in best.values()
                if item.device and _normalize(item.device) in normalized_text
            }
            if explicit_device:
                matching_devices.add(explicit_device.casefold())
            filtered = {
                key: value
                for key, value in best.items()
                if value[0].device.casefold() in matching_devices
            }
            if len(filtered) == 1:
                best = filtered
        if len(best) > 1:
            return {
                "status": "ambiguous",
                "candidates": [item.public() for item, _ in best.values()],
                "message": "对象别名匹配到多个工程对象，必须明确 ECU 或对象名",
            }
        item, matched_alias = next(iter(best.values()))
        device = item.device or str(context.get("default_device", ""))
        result: dict[str, Any] = {
            "status": "resolved",
            "device": device,
            "name": item.name,
            "matched_alias": matched_alias,
            "object": item.public(),
        }
        target = cls._TARGET_PATTERN.search(text)
        if target:
            raw_value = float(target.group("value"))
            source_unit = target.group("unit") or item.unit
            try:
                converted = EngineeringUnitConverter.convert(
                    raw_value,
                    source_unit,
                    item.unit,
                )
            except ValueError as exc:
                result.update(
                    {
                        "status": "unit_error",
                        "source_value": raw_value,
                        "source_unit": source_unit,
                        "message": str(exc),
                    }
                )
                return result
            result.update(
                {
                    "target_value": converted,
                    "source_value": raw_value,
                    "source_unit": source_unit,
                    "target_unit": item.unit,
                    "unit_converted": _normalize(source_unit) != _normalize(item.unit),
                }
            )
            if item.minimum is not None and converted < item.minimum:
                result.update(
                    {
                        "status": "target_out_of_range",
                        "message": (
                            f"目标值 {converted} {item.unit} 小于下限 "
                            f"{item.minimum} {item.unit}"
                        ),
                    }
                )
            if item.maximum is not None and converted > item.maximum:
                result.update(
                    {
                        "status": "target_out_of_range",
                        "message": (
                            f"目标值 {converted} {item.unit} 大于上限 "
                            f"{item.maximum} {item.unit}"
                        ),
                    }
                )
        return result
