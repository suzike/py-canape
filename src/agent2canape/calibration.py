"""ECU 标定数据集、变更计划、会话、试验设计和优化内核。"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import os
import random
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .errors import SafetyViolationError


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_value(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _flatten_numeric(value: Any) -> list[float]:
    value = _json_value(value)
    if isinstance(value, list):
        result: list[float] = []
        for item in value:
            result.extend(_flatten_numeric(item))
        return result
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"标定值不是数值：{value!r}")
    return [float(value)]


def _same_value(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    left_value = _json_value(left)
    right_value = _json_value(right)
    if isinstance(left_value, list) and isinstance(right_value, list):
        return len(left_value) == len(right_value) and all(
            _same_value(a, b, tolerance) for a, b in zip(left_value, right_value, strict=True)
        )
    if isinstance(left_value, (int, float)) and isinstance(right_value, (int, float)):
        return math.isclose(float(left_value), float(right_value), abs_tol=tolerance)
    return left_value == right_value


def _parameter_payload(parameter: CalibrationParameter | None) -> dict[str, Any] | None:
    if parameter is None:
        return None
    value = asdict(parameter)
    value["kind"] = parameter.kind.value
    return _json_value(value)


def _same_parameter(
    left: CalibrationParameter | None,
    right: CalibrationParameter | None,
    tolerance: float = 1e-9,
) -> bool:
    if left is None or right is None:
        return left is right
    left_data = _parameter_payload(left) or {}
    right_data = _parameter_payload(right) or {}
    return set(left_data) == set(right_data) and all(
        _same_value(left_data[name], right_data[name], tolerance)
        for name in left_data
    )


_UNSET = object()


class CalibrationKind(str, Enum):
    SCALAR = "scalar"
    CURVE = "curve"
    MAP = "map"
    AXIS = "axis"
    BLOCK = "block"
    ASCII = "ascii"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class CalibrationParameter:
    name: str
    value: Any
    kind: CalibrationKind = CalibrationKind.SCALAR
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    x_axis: list[float] = field(default_factory=list)
    y_axis: list[float] = field(default_factory=list)
    address: int | None = None
    conversion: str = ""
    comment: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        errors: list[str] = []
        try:
            values = _flatten_numeric(self.value)
        except TypeError as exc:
            if self.kind is not CalibrationKind.ASCII:
                errors.append(str(exc))
            values = []
        for value in values:
            if self.minimum is not None and value < self.minimum:
                errors.append(f"{self.name}={value} 小于下限 {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                errors.append(f"{self.name}={value} 大于上限 {self.maximum}")
        if self.kind is CalibrationKind.SCALAR and isinstance(_json_value(self.value), list):
            errors.append(f"{self.name} 声明为 scalar，但值是数组")
        if self.kind in {CalibrationKind.CURVE, CalibrationKind.AXIS}:
            vector = _json_value(self.value)
            if not isinstance(vector, list) or any(isinstance(item, list) for item in vector):
                errors.append(f"{self.name} 必须是一维数组")
            elif (
                self.kind is CalibrationKind.CURVE
                and self.x_axis
                and len(vector) != len(self.x_axis)
            ):
                errors.append(f"{self.name} 曲线长度与 X 轴长度不一致")
        if self.kind is CalibrationKind.MAP:
            matrix = _json_value(self.value)
            if (
                not isinstance(matrix, list)
                or not matrix
                or not all(isinstance(row, list) for row in matrix)
            ):
                errors.append(f"{self.name} MAP 必须是二维数组")
            else:
                widths = {len(row) for row in matrix}
                if len(widths) != 1:
                    errors.append(f"{self.name} MAP 行长度不一致")
                width = next(iter(widths), 0)
                if self.x_axis and width != len(self.x_axis):
                    errors.append(f"{self.name} MAP 列数与 X 轴长度不一致")
                if self.y_axis and len(matrix) != len(self.y_axis):
                    errors.append(f"{self.name} MAP 行数与 Y 轴长度不一致")
        for axis_name, axis in (("X", self.x_axis), ("Y", self.y_axis)):
            if axis and any(right <= left for left, right in zip(axis, axis[1:], strict=False)):
                errors.append(f"{self.name} {axis_name} 轴必须严格递增")
        return errors

    def clone(self, *, value: Any = _UNSET) -> CalibrationParameter:
        data = asdict(self)
        if value is not _UNSET:
            data["value"] = _json_value(value)
        data["kind"] = CalibrationKind(data["kind"])
        return CalibrationParameter(**data)


@dataclass(slots=True)
class CalibrationDataset:
    parameters: dict[str, CalibrationParameter] = field(default_factory=dict)
    identity: dict[str, str] = field(default_factory=dict)
    source: str = ""
    created_utc: str = field(default_factory=_utc_now)
    schema_version: int = 1

    @classmethod
    def from_values(
        cls,
        values: Mapping[str, Any],
        *,
        metadata: Mapping[str, Mapping[str, Any]] | None = None,
        identity: Mapping[str, str] | None = None,
        source: str = "",
    ) -> CalibrationDataset:
        definitions = metadata or {}
        parameters = {}
        for name, value in values.items():
            item = dict(definitions.get(name, {}))
            kind = CalibrationKind(item.pop("kind", CalibrationKind.SCALAR))
            parameters[name] = CalibrationParameter(
                name=name,
                value=_json_value(value),
                kind=kind,
                **item,
            )
        return cls(parameters=parameters, identity=dict(identity or {}), source=source)

    def validate(self) -> dict[str, list[str]]:
        issues = {}
        for name, parameter in self.parameters.items():
            errors = parameter.validate()
            if errors:
                issues[name] = errors
        return issues

    def require_valid(self) -> None:
        issues = self.validate()
        if issues:
            messages = [message for errors in issues.values() for message in errors]
            raise ValueError("; ".join(messages))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "created_utc": self.created_utc,
            "source": self.source,
            "identity": dict(self.identity),
            "parameters": {
                name: {
                    **asdict(parameter),
                    "kind": parameter.kind.value,
                    "value": _json_value(parameter.value),
                }
                for name, parameter in sorted(self.parameters.items())
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CalibrationDataset:
        parameters = {}
        for name, raw in data.get("parameters", {}).items():
            item = dict(raw)
            item["name"] = name
            item["kind"] = CalibrationKind(item.get("kind", "scalar"))
            parameters[name] = CalibrationParameter(**item)
        return cls(
            parameters=parameters,
            identity=dict(data.get("identity", {})),
            source=str(data.get("source", "")),
            created_utc=str(data.get("created_utc", _utc_now())),
            schema_version=int(data.get("schema_version", 1)),
        )

    def save(self, path: str | Path) -> Path:
        output = Path(path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.suffix.casefold() == ".json":
            output.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        elif output.suffix.casefold() == ".csv":
            with output.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=(
                        "name", "kind", "value", "unit", "minimum", "maximum",
                        "x_axis", "y_axis", "address", "conversion", "comment", "metadata",
                    ),
                )
                writer.writeheader()
                for name, parameter in sorted(self.parameters.items()):
                    row = asdict(parameter)
                    row["name"] = name
                    row["kind"] = parameter.kind.value
                    for field_name in ("value", "x_axis", "y_axis", "metadata"):
                        row[field_name] = json.dumps(
                            _json_value(row[field_name]), ensure_ascii=False
                        )
                    writer.writerow(row)
        elif output.suffix.casefold() in {".cdfx", ".dcm", ".par"}:
            from .calibration_formats import CalibrationDatasetIO

            return CalibrationDatasetIO.save(self, output)
        else:
            raise ValueError("标定数据集仅支持 .json、.csv、.cdfx、.dcm 和 .par")
        return output

    @classmethod
    def load(cls, path: str | Path) -> CalibrationDataset:
        source = Path(path).expanduser().resolve()
        if source.suffix.casefold() == ".json":
            return cls.from_dict(json.loads(source.read_text(encoding="utf-8")))
        if source.suffix.casefold() == ".csv":
            parameters = {}
            with source.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    for field_name in ("value", "x_axis", "y_axis", "metadata"):
                        row[field_name] = json.loads(row[field_name])
                    for field_name in ("minimum", "maximum"):
                        row[field_name] = (
                            float(row[field_name]) if row[field_name] not in {"", None} else None
                        )
                    row["address"] = (
                        int(row["address"]) if row["address"] not in {"", None} else None
                    )
                    row["kind"] = CalibrationKind(row["kind"])
                    name = row["name"]
                    parameters[name] = CalibrationParameter(**row)
            return cls(parameters=parameters, source=str(source))
        if source.suffix.casefold() in {".cdfx", ".dcm", ".par"}:
            from .calibration_formats import CalibrationDatasetIO

            return CalibrationDatasetIO.load(source)
        raise ValueError("标定数据集仅支持 .json、.csv、.cdfx、.dcm 和 .par")

    def digest(self) -> str:
        payload = json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def values(self) -> dict[str, Any]:
        return {name: _json_value(item.value) for name, item in self.parameters.items()}

    def diff(
        self, other: CalibrationDataset, *, tolerance: float = 1e-9
    ) -> dict[str, dict[str, Any]]:
        differences = {}
        for name in sorted(set(self.parameters) | set(other.parameters)):
            left = self.parameters.get(name)
            right = other.parameters.get(name)
            if left is None or right is None:
                differences[name] = {
                    "before": None if left is None else _json_value(left.value),
                    "after": None if right is None else _json_value(right.value),
                    "status": "added" if left is None else "removed",
                    "before_parameter": _parameter_payload(left),
                    "after_parameter": _parameter_payload(right),
                }
            elif not _same_parameter(left, right, tolerance):
                before_parameter = _parameter_payload(left) or {}
                after_parameter = _parameter_payload(right) or {}
                changed_fields = [
                    field_name
                    for field_name in before_parameter
                    if not _same_value(
                        before_parameter[field_name],
                        after_parameter[field_name],
                        tolerance,
                    )
                ]
                differences[name] = {
                    "before": _json_value(left.value),
                    "after": _json_value(right.value),
                    "status": "changed",
                    "changed_fields": changed_fields,
                    "before_parameter": before_parameter,
                    "after_parameter": after_parameter,
                }
        return differences

    def apply_patch(
        self,
        patch: Mapping[str, Any],
        *,
        strict: bool = True,
    ) -> CalibrationDataset:
        result = CalibrationDataset.from_dict(self.to_dict())
        for name, value in patch.items():
            if name not in result.parameters:
                if strict:
                    raise KeyError(f"数据集中不存在标定量：{name}")
                if isinstance(value, Mapping):
                    item = dict(value)
                    item["kind"] = CalibrationKind(item.get("kind", "scalar"))
                    result.parameters[name] = CalibrationParameter(name=name, **item)
                else:
                    result.parameters[name] = CalibrationParameter(name=name, value=value)
            else:
                if isinstance(value, Mapping):
                    updates = dict(value)
                    if "kind" in updates:
                        updates["kind"] = CalibrationKind(updates["kind"])
                    if "value" in updates:
                        updates["value"] = _json_value(updates["value"])
                    result.parameters[name] = replace(
                        result.parameters[name],
                        **updates,
                    )
                else:
                    result.parameters[name].value = _json_value(value)
        result.created_utc = _utc_now()
        result.require_valid()
        return result

    @staticmethod
    def three_way_merge(
        base: CalibrationDataset,
        current: CalibrationDataset,
        incoming: CalibrationDataset,
        *,
        conflict: str = "error",
    ) -> tuple[CalibrationDataset, dict[str, dict[str, Any]]]:
        if conflict not in {"error", "current", "incoming"}:
            raise ValueError("conflict 必须是 error/current/incoming")
        names = set(base.parameters) | set(current.parameters) | set(incoming.parameters)
        result = CalibrationDataset.from_dict(current.to_dict())
        conflicts = {}
        for name in sorted(names):
            base_value = base.parameters.get(name)
            current_value = current.parameters.get(name)
            incoming_value = incoming.parameters.get(name)
            current_changed = not _same_parameter(base_value, current_value)
            incoming_changed = not _same_parameter(base_value, incoming_value)
            if (
                current_changed
                and incoming_changed
                and not _same_parameter(current_value, incoming_value)
            ):
                conflicts[name] = {
                    "base": _parameter_payload(base_value),
                    "current": _parameter_payload(current_value),
                    "incoming": _parameter_payload(incoming_value),
                }
                if conflict == "error":
                    continue
                chosen = current_value if conflict == "current" else incoming_value
            elif incoming_changed:
                chosen = incoming_value
            else:
                continue
            if chosen is None:
                result.parameters.pop(name, None)
            else:
                result.parameters[name] = chosen.clone()
        if conflicts and conflict == "error":
            raise ValueError(f"标定数据集合并冲突：{', '.join(conflicts)}")
        result.created_utc = _utc_now()
        return result, conflicts


@dataclass(frozen=True, slots=True)
class CalibrationIdentity:
    """标定数据、描述文件与程序文件之间的可验证身份。"""

    vehicle: str = ""
    ecu: str = ""
    software: str = ""
    calibration: str = ""
    a2l_sha256: str = ""
    hex_sha256: str = ""

    @staticmethod
    def file_sha256(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).expanduser().resolve().open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def from_assets(
        cls,
        *,
        vehicle: str = "",
        ecu: str = "",
        software: str = "",
        calibration: str = "",
        a2l: str | Path | None = None,
        hex_file: str | Path | None = None,
    ) -> CalibrationIdentity:
        return cls(
            vehicle=vehicle,
            ecu=ecu,
            software=software,
            calibration=calibration,
            a2l_sha256=cls.file_sha256(a2l) if a2l else "",
            hex_sha256=cls.file_sha256(hex_file) if hex_file else "",
        )

    def to_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value
        }

    def bind(self, dataset: CalibrationDataset) -> CalibrationDataset:
        result = CalibrationDataset.from_dict(dataset.to_dict())
        result.identity.update(self.to_dict())
        result.created_utc = _utc_now()
        return result

    @classmethod
    def verify(
        cls,
        dataset: CalibrationDataset,
        *,
        a2l: str | Path | None = None,
        hex_file: str | Path | None = None,
        expected: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        actual = dict(dataset.identity)
        mismatches: dict[str, dict[str, str]] = {}
        checks = dict(expected or {})
        if a2l:
            checks["a2l_sha256"] = cls.file_sha256(a2l)
        if hex_file:
            checks["hex_sha256"] = cls.file_sha256(hex_file)
        for name, value in checks.items():
            if actual.get(name, "") != value:
                mismatches[name] = {
                    "expected": value,
                    "actual": actual.get(name, ""),
                }
        return {
            "passed": not mismatches,
            "identity": actual,
            "mismatches": mismatches,
        }


@dataclass(frozen=True, slots=True)
class ParameterConstraint:
    name: str
    minimum: float | None = None
    maximum: float | None = None
    monotonic: str = ""
    maximum_gradient: float | None = None

    def validate(self, dataset: CalibrationDataset) -> list[str]:
        parameter = dataset.parameters.get(self.name)
        if parameter is None:
            return [f"约束引用了不存在的标定量：{self.name}"]
        try:
            values = _flatten_numeric(parameter.value)
        except TypeError as exc:
            return [str(exc)]
        errors = []
        for value in values:
            if self.minimum is not None and value < self.minimum:
                errors.append(f"{self.name}={value} 小于联动下限 {self.minimum}")
            if self.maximum is not None and value > self.maximum:
                errors.append(f"{self.name}={value} 大于联动上限 {self.maximum}")
        if self.monotonic:
            if self.monotonic not in {"increasing", "decreasing"}:
                errors.append(f"{self.name} monotonic 必须是 increasing/decreasing")
            else:
                invalid = (
                    any(right < left for left, right in zip(values, values[1:], strict=False))
                    if self.monotonic == "increasing"
                    else any(right > left for left, right in zip(values, values[1:], strict=False))
                )
                if invalid:
                    errors.append(f"{self.name} 不满足 {self.monotonic} 单调约束")
        if self.maximum_gradient is not None:
            if self.maximum_gradient < 0:
                errors.append(f"{self.name} maximum_gradient 不能为负数")
            elif any(
                abs(right - left) > self.maximum_gradient
                for left, right in zip(values, values[1:], strict=False)
            ):
                errors.append(
                    f"{self.name} 相邻值变化超过 {self.maximum_gradient}"
                )
        return errors


@dataclass(frozen=True, slots=True)
class RelationConstraint:
    left: str
    operator: str
    right: str
    factor: float = 1.0
    offset: float = 0.0
    tolerance: float = 1e-9

    def validate(self, dataset: CalibrationDataset) -> list[str]:
        missing = [name for name in (self.left, self.right) if name not in dataset.parameters]
        if missing:
            return [f"联动约束引用了不存在的标定量：{', '.join(missing)}"]
        left_values = _flatten_numeric(dataset.parameters[self.left].value)
        right_values = _flatten_numeric(dataset.parameters[self.right].value)
        if len(left_values) != 1 or len(right_values) != 1:
            return [f"{self.left}/{self.right} 联动关系当前仅支持标量"]
        left = left_values[0]
        right = self.factor * right_values[0] + self.offset
        passed = {
            "<": left < right + self.tolerance,
            "<=": left <= right + self.tolerance,
            "==": math.isclose(left, right, abs_tol=self.tolerance),
            ">=": left >= right - self.tolerance,
            ">": left > right - self.tolerance,
        }.get(self.operator)
        if passed is None:
            return [f"不支持的联动运算符：{self.operator}"]
        if passed:
            return []
        return [
            f"联动约束失败：{self.left}({left}) {self.operator} "
            f"{self.factor}*{self.right}+{self.offset}({right})"
        ]


@dataclass(slots=True)
class CalibrationConstraintSet:
    parameters: list[ParameterConstraint] = field(default_factory=list)
    relations: list[RelationConstraint] = field(default_factory=list)

    def validate(self, dataset: CalibrationDataset) -> list[str]:
        errors = [
            error
            for constraint in (*self.parameters, *self.relations)
            for error in constraint.validate(dataset)
        ]
        return errors

    def require_valid(self, dataset: CalibrationDataset) -> None:
        errors = self.validate(dataset)
        if errors:
            raise SafetyViolationError("; ".join(errors))


class CalibrationRepository:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.root / ".repository.lock"

    @contextmanager
    def _lock(self, *, timeout: float = 10.0) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            try:
                descriptor = os.open(
                    self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                os.write(
                    descriptor,
                    json.dumps({"pid": os.getpid(), "created_utc": _utc_now()}).encode(),
                )
                os.close(descriptor)
                break
            except (FileExistsError, PermissionError):
                try:
                    if (
                        self._lock_path.exists()
                        and time.time() - self._lock_path.stat().st_mtime > 60
                    ):
                        self._lock_path.unlink()
                        continue
                except (OSError, PermissionError):
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待标定仓库写锁超时") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            self._lock_path.unlink(missing_ok=True)

    @staticmethod
    def _validate_version(version: str) -> str:
        if (
            not version
            or version in {".", ".."}
            or any(char in version for char in r'\/:*?"<>|')
        ):
            raise ValueError("标定版本名称无效")
        return version

    def save(
        self,
        dataset: CalibrationDataset,
        version: str,
        *,
        tags: Sequence[str] = (),
        note: str = "",
    ) -> dict[str, Any]:
        version = self._validate_version(version)
        dataset.require_valid()
        token = f"{os.getpid()}.{time.time_ns()}"
        with self._lock():
            dataset_path = self.root / f"{version}.json"
            if dataset_path.exists():
                raise FileExistsError(f"标定版本已存在：{version}")
            temporary_dataset = self.root / f".{version}.{token}.tmp.json"
            dataset.save(temporary_dataset)
            record = {
                "version": version,
                "file": dataset_path.name,
                "sha256": dataset.digest(),
                "created_utc": _utc_now(),
                "tags": sorted(set(tags)),
                "note": note,
                "identity": dict(dataset.identity),
            }
            manifest_path = self.root / "manifest.jsonl"
            temporary_manifest = self.root / f".manifest.{token}.tmp"
            existing_manifest = (
                manifest_path.read_text(encoding="utf-8") if manifest_path.exists() else ""
            )
            temporary_manifest.write_text(
                existing_manifest + json.dumps(record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            try:
                temporary_dataset.replace(dataset_path)
                temporary_manifest.replace(manifest_path)
            finally:
                temporary_dataset.unlink(missing_ok=True)
                temporary_manifest.unlink(missing_ok=True)
        return record

    def load(self, version: str) -> CalibrationDataset:
        version = self._validate_version(version)
        records = {
            str(item.get("version")): item for item in self.list_versions()
        }
        if version not in records:
            raise KeyError(f"标定版本未登记：{version}")
        dataset = CalibrationDataset.load(self.root / f"{version}.json")
        expected = str(records[version].get("sha256", ""))
        actual = dataset.digest()
        if not expected or actual != expected:
            raise SafetyViolationError(
                f"标定版本完整性校验失败：{version}，"
                f"expected={expected or '<missing>'} actual={actual}"
            )
        return dataset

    def list_versions(self) -> list[dict[str, Any]]:
        manifest = self.root / "manifest.jsonl"
        if not manifest.exists():
            return []
        states = self._read_states()
        records = [
            json.loads(line)
            for line in manifest.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        for record in records:
            state = states.get(str(record.get("version")), {})
            record["frozen"] = bool(state.get("frozen"))
            if state:
                record["freeze"] = state
        return records

    def _read_states(self) -> dict[str, dict[str, Any]]:
        path = self.root / "states.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def freeze(self, version: str, *, actor: str, reason: str) -> dict[str, Any]:
        version = self._validate_version(version)
        if not actor.strip() or not reason.strip():
            raise ValueError("冻结基线必须提供 actor 和 reason")
        with self._lock():
            versions = {item["version"] for item in self.list_versions()}
            if version not in versions:
                raise KeyError(f"标定版本未登记：{version}")
            states = self._read_states()
            existing = states.get(version)
            if existing and existing.get("frozen"):
                return existing
            state = {
                "frozen": True,
                "actor": actor.strip(),
                "reason": reason.strip(),
                "frozen_utc": _utc_now(),
            }
            states[version] = state
            temporary = self.root / f".states.{os.getpid()}.{time.time_ns()}.tmp"
            temporary.write_text(
                json.dumps(states, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.root / "states.json")
            return state

    def verify_all(self) -> dict[str, Any]:
        records = self.list_versions()
        issues = []
        registered = set()
        for record in records:
            version = str(record["version"])
            registered.add(str(record["file"]))
            try:
                self.load(version)
            except (OSError, ValueError, SafetyViolationError) as exc:
                issues.append(f"{version}: {exc}")
        orphan_files = sorted(
            path.name
            for path in self.root.glob("*.json")
            if path.name != "states.json" and path.name not in registered
        )
        if orphan_files:
            issues.append(f"未登记数据文件：{', '.join(orphan_files)}")
        return {
            "passed": not issues,
            "version_count": len(records),
            "issues": issues,
            "orphan_files": orphan_files,
        }

    def compare(self, left: str, right: str) -> dict[str, dict[str, Any]]:
        return self.load(left).diff(self.load(right))

    def restore(self, version: str, output: str | Path) -> Path:
        return self.load(version).save(output)


class CalibrationBackend(Protocol):
    def read_calibration_value(
        self, device: str, name: str, *, physical: bool = True
    ) -> Any: ...

    def write_calibration_value(
        self,
        device: str,
        name: str,
        value: Any,
        *,
        physical: bool = True,
        verify: bool = True,
    ) -> Any: ...

    def get_calibration_metadata(self, device: str, name: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CalibrationChange:
    name: str
    value: Any
    reason: str
    expected_before: Any = None
    enforce_expected: bool = False


@dataclass(slots=True)
class CalibrationPlan:
    changes: list[CalibrationChange]
    name: str = "calibration-plan"
    author: str = ""
    ticket: str = ""
    constraints: CalibrationConstraintSet | None = None
    approved_by: str = ""
    approved_utc: str = ""

    def approve(self, approver: str) -> None:
        if not approver.strip():
            raise ValueError("审批人不能为空")
        self.approved_by = approver.strip()
        self.approved_utc = _utc_now()

    def validate(self, dataset: CalibrationDataset) -> list[str]:
        errors: list[str] = []
        seen = set()
        for change in self.changes:
            if change.name in seen:
                errors.append(f"变更计划包含重复标定量：{change.name}")
            seen.add(change.name)
            parameter = dataset.parameters.get(change.name)
            if parameter is None:
                errors.append(f"标定量不存在：{change.name}")
                continue
            candidate = parameter.clone(value=change.value)
            errors.extend(candidate.validate())
            if (
                change.enforce_expected
                and not _same_value(parameter.value, change.expected_before)
            ):
                errors.append(
                    f"{change.name} 当前值与计划基线不一致："
                    f"{parameter.value!r} != {change.expected_before!r}"
                )
        if self.constraints and not any(
            error.startswith("标定量不存在") for error in errors
        ):
            candidate_dataset = CalibrationDataset.from_dict(dataset.to_dict())
            for change in self.changes:
                if change.name in candidate_dataset.parameters:
                    candidate_dataset.parameters[change.name].value = _json_value(change.value)
            errors.extend(self.constraints.validate(candidate_dataset))
        return errors

    def preview(self, dataset: CalibrationDataset) -> dict[str, Any]:
        errors = self.validate(dataset)
        return {
            "name": self.name,
            "author": self.author,
            "ticket": self.ticket,
            "approved": bool(self.approved_by),
            "errors": errors,
            "changes": [
                {
                    "name": change.name,
                    "before": (
                        _json_value(dataset.parameters[change.name].value)
                        if change.name in dataset.parameters
                        else None
                    ),
                    "after": _json_value(change.value),
                    "reason": change.reason,
                }
                for change in self.changes
            ],
        }

    def apply(
        self,
        backend: CalibrationBackend,
        device: str,
        *,
        require_approval: bool = True,
        physical: bool = True,
    ) -> dict[str, Any]:
        if require_approval and not self.approved_by:
            raise SafetyViolationError("标定计划尚未审批")
        names = [change.name for change in self.changes]
        before = {
            name: backend.read_calibration_value(device, name, physical=physical)
            for name in names
        }
        written: dict[str, Any] = {}
        attempted: list[str] = []
        try:
            for change in self.changes:
                if change.enforce_expected and not _same_value(
                    before[change.name], change.expected_before
                ):
                    raise SafetyViolationError(
                        f"{change.name} 在线值与计划基线不一致"
                    )
                attempted.append(change.name)
                written[change.name] = backend.write_calibration_value(
                    device,
                    change.name,
                    change.value,
                    physical=physical,
                    verify=True,
                )
        except Exception:
            for name in reversed(attempted):
                try:
                    backend.write_calibration_value(
                        device, name, before[name], physical=physical, verify=True
                    )
                except Exception:
                    # 回滚必须继续处理其余已写对象，原始写入异常仍由调用方接收。
                    continue
            raise
        return {
            "plan": self.name,
            "device": device,
            "before": before,
            "after": written,
            "approved_by": self.approved_by,
            "applied_utc": _utc_now(),
        }


class CalibrationSession:
    def __init__(
        self,
        backend: CalibrationBackend,
        device: str,
        *,
        identity: Mapping[str, str] | None = None,
    ) -> None:
        self.backend = backend
        self.device = device
        self.identity = dict(identity or {})
        self.baseline: CalibrationDataset | None = None
        self.original_baseline: CalibrationDataset | None = None
        self.staged: dict[str, CalibrationChange] = {}
        self.history: list[dict[str, Any]] = []

    def begin(self, names: Sequence[str]) -> CalibrationDataset:
        parameters = {}
        for name in names:
            value = self.backend.read_calibration_value(self.device, name)
            metadata = dict(self.backend.get_calibration_metadata(self.device, name))
            info = metadata.pop("info", None)
            kind = CalibrationKind.SCALAR
            if info is not None:
                x_dim = int(getattr(info, "x_dimension", 0))
                y_dim = int(getattr(info, "y_dimension", 0))
                kind = (
                    CalibrationKind.MAP if y_dim > 1
                    else CalibrationKind.CURVE if x_dim > 1
                    else CalibrationKind.SCALAR
                )
            parameters[name] = CalibrationParameter(
                name=name,
                value=_json_value(value),
                kind=kind,
                unit=str(metadata.pop("unit", "") or ""),
                minimum=metadata.pop("minimum", None),
                maximum=metadata.pop("maximum", None),
                address=metadata.pop("address", None),
                conversion=str(metadata.pop("conversion", "") or ""),
                comment=str(metadata.pop("comment", "") or ""),
                metadata=metadata,
            )
        self.baseline = CalibrationDataset(
            parameters=parameters,
            identity=dict(self.identity),
            source=f"CANape:{self.device}",
        )
        self.original_baseline = CalibrationDataset.from_dict(self.baseline.to_dict())
        self.staged.clear()
        return self.baseline

    def stage(
        self,
        name: str,
        value: Any,
        *,
        reason: str,
        enforce_baseline: bool = True,
    ) -> CalibrationChange:
        if self.baseline is None:
            raise RuntimeError("请先调用 begin()")
        if name not in self.baseline.parameters:
            raise KeyError(name)
        change = CalibrationChange(
            name=name,
            value=_json_value(value),
            reason=reason,
            expected_before=_json_value(self.baseline.parameters[name].value),
            enforce_expected=enforce_baseline,
        )
        self.staged[name] = change
        return change

    def plan(self, *, name: str, author: str = "", ticket: str = "") -> CalibrationPlan:
        return CalibrationPlan(
            changes=list(self.staged.values()),
            name=name,
            author=author,
            ticket=ticket,
        )

    def commit(self, plan: CalibrationPlan) -> dict[str, Any]:
        result = plan.apply(self.backend, self.device)
        self.history.append(result)
        if self.baseline is not None:
            self.baseline = self.baseline.apply_patch(result["after"])
        self.staged.clear()
        return result

    def rollback(self) -> dict[str, Any]:
        if self.original_baseline is None:
            raise RuntimeError("没有可回滚的标定基线")
        plan = CalibrationPlan(
            name="session-rollback",
            changes=[
                CalibrationChange(name=name, value=parameter.value, reason="rollback")
                for name, parameter in self.original_baseline.parameters.items()
            ],
            approved_by="session",
            approved_utc=_utc_now(),
        )
        return plan.apply(self.backend, self.device, require_approval=False)


@dataclass(frozen=True, slots=True)
class SweepParameter:
    name: str
    values: tuple[float, ...]


class CalibrationExperiment:
    @staticmethod
    def full_factorial(parameters: Sequence[SweepParameter]) -> list[dict[str, float]]:
        if not parameters:
            return []
        return [
            dict(zip((item.name for item in parameters), values, strict=True))
            for values in itertools.product(*(item.values for item in parameters))
        ]

    @staticmethod
    def one_factor_at_a_time(
        baseline: Mapping[str, float],
        parameters: Sequence[SweepParameter],
    ) -> list[dict[str, float]]:
        cases = [dict(baseline)]
        for parameter in parameters:
            for value in parameter.values:
                case = dict(baseline)
                case[parameter.name] = value
                if case not in cases:
                    cases.append(case)
        return cases

    @staticmethod
    def latin_hypercube(
        bounds: Mapping[str, tuple[float, float]],
        samples: int,
        *,
        seed: int = 0,
    ) -> list[dict[str, float]]:
        if samples <= 0:
            raise ValueError("samples 必须大于 0")
        rng = random.Random(seed)
        dimensions: dict[str, list[float]] = {}
        for name, (lower, upper) in bounds.items():
            if upper <= lower:
                raise ValueError(f"{name} 上限必须大于下限")
            values = [
                lower + ((index + rng.random()) / samples) * (upper - lower)
                for index in range(samples)
            ]
            rng.shuffle(values)
            dimensions[name] = values
        return [
            {name: values[index] for name, values in dimensions.items()}
            for index in range(samples)
        ]

    @staticmethod
    def run(
        backend: CalibrationBackend,
        device: str,
        cases: Sequence[Mapping[str, Any]],
        evaluator: Callable[[int, Mapping[str, Any]], Mapping[str, float]],
    ) -> list[dict[str, Any]]:
        names = sorted({name for case in cases for name in case})
        baseline = {
            name: backend.read_calibration_value(device, name) for name in names
        }
        results = []
        try:
            for index, case in enumerate(cases):
                for name, value in case.items():
                    backend.write_calibration_value(device, name, value, verify=True)
                metrics = dict(evaluator(index, case))
                results.append(
                    {"case": index, "parameters": dict(case), "metrics": metrics}
                )
        finally:
            for name, value in baseline.items():
                backend.write_calibration_value(device, name, value, verify=True)
        return results


class CalibrationOptimizer:
    @staticmethod
    def weighted_score(
        metrics: Mapping[str, float],
        targets: Mapping[str, float],
        *,
        weights: Mapping[str, float] | None = None,
        scales: Mapping[str, float] | None = None,
    ) -> float:
        score = 0.0
        for name, target in targets.items():
            if name not in metrics:
                raise KeyError(f"缺少优化指标：{name}")
            scale = (scales or {}).get(name, max(abs(target), 1.0))
            if scale <= 0:
                raise ValueError(f"{name} 的 scale 必须大于 0")
            error = (float(metrics[name]) - float(target)) / scale
            score += (weights or {}).get(name, 1.0) * error * error
        return score

    @staticmethod
    def coordinate_search(
        initial: Mapping[str, float],
        bounds: Mapping[str, tuple[float, float]],
        objective: Callable[[Mapping[str, float]], float],
        *,
        initial_step: float | Mapping[str, float] = 1.0,
        tolerance: float = 1e-3,
        max_iterations: int = 100,
    ) -> dict[str, Any]:
        point = {name: float(value) for name, value in initial.items()}
        steps = {
            name: (
                float(initial_step[name])
                if isinstance(initial_step, Mapping)
                else float(initial_step)
            )
            for name in point
        }
        for name, value in point.items():
            lower, upper = bounds[name]
            if not lower <= value <= upper:
                raise ValueError(f"{name} 初值不在边界内")
        best = float(objective(point))
        evaluations = 1
        iterations = 0
        while iterations < max_iterations and max(steps.values(), default=0) > tolerance:
            improved = False
            iterations += 1
            for name in point:
                lower, upper = bounds[name]
                for direction in (-1.0, 1.0):
                    candidate = dict(point)
                    candidate[name] = min(
                        upper, max(lower, point[name] + direction * steps[name])
                    )
                    if candidate[name] == point[name]:
                        continue
                    score = float(objective(candidate))
                    evaluations += 1
                    if score < best:
                        point, best = candidate, score
                        improved = True
                        break
                if improved:
                    break
            if not improved:
                steps = {name: value / 2.0 for name, value in steps.items()}
        return {
            "parameters": point,
            "objective": best,
            "iterations": iterations,
            "evaluations": evaluations,
            "converged": max(steps.values(), default=0) <= tolerance,
        }


class CalibrationMath:
    @staticmethod
    def interpolate_curve(
        x_axis: Sequence[float],
        values: Sequence[float],
        x: float,
    ) -> float:
        if len(x_axis) != len(values) or not x_axis:
            raise ValueError("曲线轴和值长度必须一致且非空")
        if any(right <= left for left, right in zip(x_axis, x_axis[1:], strict=False)):
            raise ValueError("曲线轴必须严格递增")
        if x <= x_axis[0]:
            return float(values[0])
        if x >= x_axis[-1]:
            return float(values[-1])
        for index, (left, right) in enumerate(zip(x_axis, x_axis[1:], strict=False)):
            if left <= x <= right:
                ratio = (x - left) / (right - left)
                return float(values[index] + ratio * (values[index + 1] - values[index]))
        raise RuntimeError("曲线插值失败")

    @staticmethod
    def interpolate_map(
        x_axis: Sequence[float],
        y_axis: Sequence[float],
        values: Sequence[Sequence[float]],
        x: float,
        y: float,
    ) -> float:
        if len(values) != len(y_axis) or any(len(row) != len(x_axis) for row in values):
            raise ValueError("MAP 维度与轴不一致")
        rows = [
            CalibrationMath.interpolate_curve(x_axis, row, x)
            for row in values
        ]
        return CalibrationMath.interpolate_curve(y_axis, rows, y)

    @staticmethod
    def limit_gradient(values: Sequence[float], maximum_delta: float) -> list[float]:
        if maximum_delta < 0:
            raise ValueError("maximum_delta 不能为负数")
        if not values:
            return []
        result = [float(values[0])]
        for value in values[1:]:
            lower = result[-1] - maximum_delta
            upper = result[-1] + maximum_delta
            result.append(min(upper, max(lower, float(value))))
        return result
