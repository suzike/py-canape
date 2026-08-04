"""CANape 网络、设备、通道与数据库资产的一致性审计。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import OptionalDependencyError, SafetyViolationError


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class NetworkTopologySpec:
    name: str
    bus_type: str
    expected_active: bool | None = None
    required: bool = True
    bitrate: int | None = None
    data_bitrate: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> NetworkTopologySpec:
        return cls(
            name=str(value["name"]),
            bus_type=str(value.get("bus_type", "other")).casefold(),
            expected_active=(
                bool(value["expected_active"])
                if value.get("expected_active") is not None
                else None
            ),
            required=bool(value.get("required", True)),
            bitrate=int(value["bitrate"]) if value.get("bitrate") is not None else None,
            data_bitrate=(
                int(value["data_bitrate"])
                if value.get("data_bitrate") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class DeviceTopologySpec:
    name: str
    network: str
    channel: int | None = None
    driver_type: str = ""
    databases: tuple[str, ...] = ()
    expected_online: bool | None = None
    required: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DeviceTopologySpec:
        return cls(
            name=str(value["name"]),
            network=str(value["network"]),
            channel=int(value["channel"]) if value.get("channel") is not None else None,
            driver_type=str(value.get("driver_type", "")),
            databases=tuple(str(item) for item in value.get("databases", ())),
            expected_online=(
                bool(value["expected_online"])
                if value.get("expected_online") is not None
                else None
            ),
            required=bool(value.get("required", True)),
        )


@dataclass(frozen=True, slots=True)
class DatabaseTopologySpec:
    name: str
    path: str
    kind: str = "auto"
    sha256: str = ""
    required: bool = True
    expected_nodes: tuple[str, ...] = ()
    expected_messages: tuple[str, ...] = ()
    expected_frame_ids: tuple[int, ...] = ()
    expected_signals: tuple[str, ...] = ()
    expected_objects: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> DatabaseTopologySpec:
        frame_ids = []
        for item in value.get("expected_frame_ids", ()):
            frame_ids.append(int(item, 0) if isinstance(item, str) else int(item))
        return cls(
            name=str(value["name"]),
            path=str(value["path"]),
            kind=str(value.get("kind", "auto")).casefold(),
            sha256=str(value.get("sha256", "")).casefold(),
            required=bool(value.get("required", True)),
            expected_nodes=tuple(str(item) for item in value.get("expected_nodes", ())),
            expected_messages=tuple(
                str(item) for item in value.get("expected_messages", ())
            ),
            expected_frame_ids=tuple(frame_ids),
            expected_signals=tuple(str(item) for item in value.get("expected_signals", ())),
            expected_objects=tuple(str(item) for item in value.get("expected_objects", ())),
        )

    def resolved_kind(self) -> str:
        if self.kind != "auto":
            return self.kind
        return Path(self.path).suffix.casefold().lstrip(".") or "unknown"


@dataclass(frozen=True, slots=True)
class NetworkTopologyManifest:
    name: str
    networks: tuple[NetworkTopologySpec, ...]
    devices: tuple[DeviceTopologySpec, ...]
    databases: tuple[DatabaseTopologySpec, ...] = ()
    allow_unexpected_networks: bool = True
    allow_unexpected_devices: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    source_directory: str = field(default="", repr=False, compare=False)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        source_directory: str | Path = "",
    ) -> NetworkTopologyManifest:
        return cls(
            name=str(value.get("name", "network-topology")),
            networks=tuple(
                NetworkTopologySpec.from_mapping(item)
                for item in value.get("networks", ())
            ),
            devices=tuple(
                DeviceTopologySpec.from_mapping(item) for item in value.get("devices", ())
            ),
            databases=tuple(
                DatabaseTopologySpec.from_mapping(item)
                for item in value.get("databases", ())
            ),
            allow_unexpected_networks=bool(value.get("allow_unexpected_networks", True)),
            allow_unexpected_devices=bool(value.get("allow_unexpected_devices", True)),
            metadata=dict(value.get("metadata", {})),
            source_directory=str(source_directory),
        )

    @classmethod
    def load(cls, path: str | Path) -> NetworkTopologyManifest:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        text = source.read_text(encoding="utf-8")
        value = (
            yaml.safe_load(text)
            if source.suffix.casefold() in {".yaml", ".yml"}
            else json.loads(text)
        )
        if not isinstance(value, Mapping):
            raise ValueError("拓扑清单根节点必须是对象")
        return cls.from_mapping(value, source_directory=source.parent)

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "networks": [asdict(item) for item in self.networks],
            "devices": [asdict(item) for item in self.devices],
            "databases": [asdict(item) for item in self.databases],
            "allow_unexpected_networks": self.allow_unexpected_networks,
            "allow_unexpected_devices": self.allow_unexpected_devices,
            "metadata": self.metadata,
        }

    def digest(self) -> str:
        return _digest(self.public())

    def resolve_database_path(self, database: DatabaseTopologySpec) -> Path:
        path = Path(database.path).expanduser()
        if not path.is_absolute():
            root = Path(self.source_directory) if self.source_directory else Path.cwd()
            path = root / path
        return path.resolve()

    @staticmethod
    def _duplicates(values: Sequence[str]) -> list[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            folded = value.casefold()
            if folded in seen:
                duplicates.add(value)
            seen.add(folded)
        return sorted(duplicates, key=str.casefold)

    def plan(self, *, deep: bool = False) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not self.name.strip():
            errors.append("拓扑清单 name 不能为空")
        network_names = [item.name for item in self.networks]
        device_names = [item.name for item in self.devices]
        database_names = [item.name for item in self.databases]
        for label, values in (
            ("网络", network_names),
            ("设备", device_names),
            ("数据库", database_names),
        ):
            duplicates = self._duplicates(values)
            if duplicates:
                errors.append(f"{label}名称重复：{', '.join(duplicates)}")
        if not self.networks:
            errors.append("networks 不能为空")
        supported_buses = {"can", "canfd", "lin", "flexray", "ethernet", "other"}
        network_set = {item.casefold() for item in network_names}
        database_set = {item.casefold() for item in database_names}
        for network in self.networks:
            if not network.name.strip():
                errors.append("网络名称不能为空")
            if network.bus_type not in supported_buses:
                errors.append(f"{network.name} bus_type 不受支持：{network.bus_type}")
            if network.bitrate is not None and network.bitrate <= 0:
                errors.append(f"{network.name} bitrate 必须大于0")
            if network.data_bitrate is not None and network.data_bitrate <= 0:
                errors.append(f"{network.name} data_bitrate 必须大于0")
            if network.bus_type == "canfd" and network.data_bitrate is None:
                warnings.append(f"{network.name} 为 CAN FD，但未声明 data_bitrate")
        for device in self.devices:
            if not device.name.strip():
                errors.append("设备名称不能为空")
            if device.network.casefold() not in network_set:
                errors.append(f"{device.name} 引用未定义网络：{device.network}")
            if device.channel is not None and device.channel <= 0:
                errors.append(f"{device.name} channel 必须大于0")
            for database in device.databases:
                if database.casefold() not in database_set:
                    errors.append(f"{device.name} 引用未定义数据库：{database}")
        asset_reports: list[dict[str, Any]] = []
        for database in self.databases:
            report = self._audit_database(database, deep=deep)
            asset_reports.append(report)
            errors.extend(report["errors"])
            warnings.extend(report["warnings"])
        return {
            "status": "passed" if not errors else "failed",
            "passed": not errors,
            "name": self.name,
            "manifest_digest": self.digest(),
            "network_count": len(self.networks),
            "device_count": len(self.devices),
            "database_count": len(self.databases),
            "deep": deep,
            "com_boundary": (
                "CANape 1.9 COM 网络仅可靠提供 name/active；总线类型和波特率来自受控清单"
            ),
            "assets": asset_reports,
            "errors": errors,
            "warnings": warnings,
        }

    def require_valid(self, *, deep: bool = False) -> dict[str, Any]:
        plan = self.plan(deep=deep)
        if not plan["passed"]:
            raise SafetyViolationError("; ".join(plan["errors"]))
        return plan

    def _audit_database(
        self,
        database: DatabaseTopologySpec,
        *,
        deep: bool,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        path = self.resolve_database_path(database)
        kind = database.resolved_kind()
        if kind not in {"dbc", "a2l", "odx", "xml", "arxml", "ldf"}:
            warnings.append(f"{database.name} 数据库类型未深度支持：{kind}")
        if not path.is_file():
            message = f"数据库文件不存在：{database.name} -> {path}"
            (errors if database.required else warnings).append(message)
            return {
                "name": database.name,
                "path": str(path),
                "kind": kind,
                "exists": False,
                "size": 0,
                "sha256": "",
                "semantic": {},
                "errors": errors,
                "warnings": warnings,
            }
        actual_digest = _file_digest(path)
        if database.sha256 and actual_digest != database.sha256:
            errors.append(f"{database.name} SHA-256 与清单不一致")
        semantic: dict[str, Any] = {}
        if deep and kind in {"dbc", "a2l"}:
            audit = self._audit_semantics_isolated(path, database)
            semantic = dict(audit.get("semantic", {}))
            errors.extend(str(item) for item in audit.get("errors", ()))
            warnings.extend(str(item) for item in audit.get("warnings", ()))
        elif deep and any(
            (
                database.expected_nodes,
                database.expected_messages,
                database.expected_frame_ids,
                database.expected_signals,
                database.expected_objects,
            )
        ):
            errors.append(f"{database.name} 的 {kind} 语义期望当前无法验证")
        return {
            "name": database.name,
            "path": str(path),
            "kind": kind,
            "exists": True,
            "size": path.stat().st_size,
            "sha256": actual_digest,
            "semantic": semantic,
            "errors": errors,
            "warnings": warnings,
        }

    @staticmethod
    def _audit_semantics_isolated(
        path: Path,
        database: DatabaseTopologySpec,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """在独立进程中解析第三方数据库，避免 MCP 工作线程被解析器无限阻塞。"""
        payload = json.dumps(
            {"path": str(path), "database": asdict(database)},
            ensure_ascii=False,
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "agent2canape.topology_worker"],
                input=payload.encode("utf-8"),
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "semantic": {},
                "errors": [
                    f"{database.name} 深度语义审计超过 {timeout_seconds:g}s，已终止隔离进程"
                ],
                "warnings": [],
            }
        try:
            stdout = completed.stdout.decode("utf-8", errors="strict")
            result = json.loads(stdout)
        except json.JSONDecodeError:
            message = completed.stderr.decode("utf-8", errors="replace").strip()
            message = message or f"exit={completed.returncode}"
            return {
                "semantic": {},
                "errors": [f"{database.name} 深度语义审计进程返回无效结果：{message}"],
                "warnings": [],
            }
        except UnicodeDecodeError:
            return {
                "semantic": {},
                "errors": [f"{database.name} 深度语义审计进程输出不是 UTF-8"],
                "warnings": [],
            }
        if completed.returncode and not result.get("errors"):
            result.setdefault("errors", []).append(
                f"{database.name} 深度语义审计进程失败：exit={completed.returncode}"
            )
        return result

    @staticmethod
    def _audit_dbc(
        path: Path,
        spec: DatabaseTopologySpec,
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        try:
            import cantools
        except ImportError as exc:
            raise OptionalDependencyError(
                "DBC 深度拓扑审计需要安装 Agent2Canape[vector-files]"
            ) from exc
        errors: list[str] = []
        warnings: list[str] = []
        database = cantools.database.load_file(path)
        nodes = sorted(node.name for node in database.nodes)
        messages = sorted(message.name for message in database.messages)
        frame_ids = sorted(message.frame_id for message in database.messages)
        signals = sorted(
            {signal.name for message in database.messages for signal in message.signals}
        )
        for label, expected, actual in (
            ("节点", spec.expected_nodes, nodes),
            ("报文", spec.expected_messages, messages),
            ("信号", spec.expected_signals, signals),
        ):
            missing = sorted(set(expected) - set(actual))
            if missing:
                errors.append(f"{spec.name} 缺少{label}：{', '.join(missing)}")
        missing_ids = sorted(set(spec.expected_frame_ids) - set(frame_ids))
        if missing_ids:
            errors.append(
                f"{spec.name} 缺少帧 ID：{', '.join(f'0x{item:X}' for item in missing_ids)}"
            )
        duplicate_ids = sorted(
            frame_id for frame_id in set(frame_ids) if frame_ids.count(frame_id) > 1
        )
        if duplicate_ids:
            warnings.append(
                f"{spec.name} 存在重复帧 ID："
                + ", ".join(f"0x{item:X}" for item in duplicate_ids)
            )
        return {
            "node_count": len(nodes),
            "message_count": len(messages),
            "signal_count": len(signals),
            "nodes": nodes,
            "messages": messages,
            "frame_ids": frame_ids,
        }, errors, warnings

    @staticmethod
    def _audit_a2l(
        path: Path,
        spec: DatabaseTopologySpec,
    ) -> tuple[dict[str, Any], list[str]]:
        from .calibration_formats import A2LCatalog

        catalog = A2LCatalog.parse(path)
        summary = catalog.summary()
        errors = [f"{spec.name}: {issue}" for issue in summary.get("issues", ())]
        missing = sorted(set(spec.expected_objects) - set(catalog.objects))
        if missing:
            errors.append(f"{spec.name} 缺少 A2L 对象：{', '.join(missing)}")
        return summary, errors


@dataclass(frozen=True, slots=True)
class NetworkTopologySnapshot:
    captured_utc: str
    project: dict[str, Any]
    networks: tuple[dict[str, Any], ...]
    devices: tuple[dict[str, Any], ...]
    com_visible_fields: dict[str, list[str]]

    def public(self) -> dict[str, Any]:
        return asdict(self)

    def digest(self) -> str:
        value = self.public()
        value.pop("captured_utc", None)
        return _digest(value)

    def save(self, path: str | Path) -> Path:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.public(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return target


class CANapeTopologyAuditor:
    def __init__(self, canape: Any) -> None:
        self.canape = canape

    def capture(self) -> NetworkTopologySnapshot:
        topology = self.canape.get_network_topology()
        return NetworkTopologySnapshot(
            captured_utc=datetime.now(UTC).isoformat(),
            project=dict(self.canape.get_project_info()),
            networks=tuple(dict(item) for item in topology["networks"]),
            devices=tuple(dict(item) for item in topology["devices"]),
            com_visible_fields={
                key: list(value)
                for key, value in topology.get("com_visible_fields", {}).items()
            },
        )

    def audit(
        self,
        manifest: NetworkTopologyManifest,
        *,
        deep: bool = False,
    ) -> dict[str, Any]:
        plan = manifest.plan(deep=deep)
        snapshot = self.capture()
        errors = list(plan["errors"])
        warnings = list(plan["warnings"])
        drifts: list[dict[str, Any]] = []
        actual_networks = {
            str(item["name"]).casefold(): item for item in snapshot.networks
        }
        expected_networks = {item.name.casefold(): item for item in manifest.networks}
        for network in manifest.networks:
            actual = actual_networks.get(network.name.casefold())
            if actual is None:
                message = f"CANape 缺少网络：{network.name}"
                (errors if network.required else warnings).append(message)
                drifts.append({"kind": "network_missing", "name": network.name})
                continue
            if (
                network.expected_active is not None
                and bool(actual.get("active")) != network.expected_active
            ):
                errors.append(f"网络 {network.name} 激活态不一致")
                drifts.append(
                    {
                        "kind": "network_active",
                        "name": network.name,
                        "expected": network.expected_active,
                        "actual": bool(actual.get("active")),
                    }
                )
        if not manifest.allow_unexpected_networks:
            for actual in snapshot.networks:
                if str(actual["name"]).casefold() not in expected_networks:
                    errors.append(f"CANape 存在清单外网络：{actual['name']}")
                    drifts.append({"kind": "network_unexpected", "name": actual["name"]})

        actual_devices = {str(item["name"]).casefold(): item for item in snapshot.devices}
        expected_devices = {item.name.casefold(): item for item in manifest.devices}
        database_specs = {item.name.casefold(): item for item in manifest.databases}
        for device in manifest.devices:
            actual = actual_devices.get(device.name.casefold())
            if actual is None:
                message = f"CANape 缺少设备：{device.name}"
                (errors if device.required else warnings).append(message)
                drifts.append({"kind": "device_missing", "name": device.name})
                continue
            comparisons = (
                ("channel", device.channel, actual.get("channel")),
                ("online", device.expected_online, bool(actual.get("online"))),
            )
            for field_name, expected, actual_value in comparisons:
                if expected is not None and expected != actual_value:
                    errors.append(f"设备 {device.name} {field_name} 不一致")
                    drifts.append(
                        {
                            "kind": f"device_{field_name}",
                            "name": device.name,
                            "expected": expected,
                            "actual": actual_value,
                        }
                    )
            if device.driver_type and device.driver_type.casefold() != str(
                actual.get("driver_type", "")
            ).casefold():
                errors.append(f"设备 {device.name} driver_type 不一致")
                drifts.append(
                    {
                        "kind": "device_driver_type",
                        "name": device.name,
                        "expected": device.driver_type,
                        "actual": actual.get("driver_type", ""),
                    }
                )
            actual_network = str(actual.get("network", ""))
            if actual_network and actual_network.casefold() != device.network.casefold():
                errors.append(f"设备 {device.name} 网络绑定不一致")
                drifts.append(
                    {
                        "kind": "device_network",
                        "name": device.name,
                        "expected": device.network,
                        "actual": actual_network,
                    }
                )
            actual_databases = {
                Path(str(item)).name.casefold()
                for item in (
                    *actual.get("databases", ()),
                    actual.get("database_filename", ""),
                )
                if item
            }
            for database_name in device.databases:
                database = database_specs.get(database_name.casefold())
                if database is None:
                    continue
                candidates = {
                    database.name.casefold(),
                    Path(database.path).name.casefold(),
                }
                if actual_databases.isdisjoint(candidates):
                    errors.append(
                        f"设备 {device.name} 未加载数据库：{database.name}"
                    )
                    drifts.append(
                        {
                            "kind": "device_database",
                            "name": device.name,
                            "expected": database.name,
                            "actual": sorted(actual_databases),
                        }
                    )
        if not manifest.allow_unexpected_devices:
            for actual in snapshot.devices:
                if str(actual["name"]).casefold() not in expected_devices:
                    errors.append(f"CANape 存在清单外设备：{actual['name']}")
                    drifts.append({"kind": "device_unexpected", "name": actual["name"]})
        return {
            "status": "passed" if not errors else "failed",
            "passed": not errors,
            "manifest_digest": manifest.digest(),
            "snapshot_digest": snapshot.digest(),
            "snapshot": snapshot.public(),
            "assets": plan["assets"],
            "drifts": drifts,
            "errors": errors,
            "warnings": warnings,
        }

    @staticmethod
    def diff(
        before: NetworkTopologySnapshot,
        after: NetworkTopologySnapshot,
    ) -> dict[str, Any]:
        before_value = before.public()
        after_value = after.public()
        before_value.pop("captured_utc", None)
        after_value.pop("captured_utc", None)
        changes: list[dict[str, Any]] = []
        for category in ("networks", "devices"):
            left = {str(item["name"]).casefold(): item for item in before_value[category]}
            right = {str(item["name"]).casefold(): item for item in after_value[category]}
            for name in sorted(set(left) | set(right)):
                if left.get(name) != right.get(name):
                    changes.append(
                        {
                            "category": category,
                            "name": name,
                            "before": left.get(name),
                            "after": right.get(name),
                        }
                    )
        return {
            "changed": bool(changes),
            "before_digest": before.digest(),
            "after_digest": after.digest(),
            "changes": changes,
        }
