"""可插拔标定目标、RAM/ROM 持久化和掉线恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from .calibration import CalibrationDataset, _json_value, _utc_now
from .calibration_operations import CalibrationMemoryLayer, CalibrationMemoryLedger
from .errors import SafetyViolationError


def _content_digest(dataset: CalibrationDataset) -> str:
    payload = dataset.to_dict()["parameters"]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _clone(dataset: CalibrationDataset, *, source: str | None = None) -> CalibrationDataset:
    clone = CalibrationDataset.from_dict(dataset.to_dict())
    if source is not None:
        clone.source = source
    return clone


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)
    return path


class CalibrationTargetAdapter(Protocol):
    """车型/ECU 适配器必须明确提供的标定目标动作。"""

    def is_online(self, device: str) -> bool: ...

    def reconnect(self, device: str) -> bool: ...

    def capture(
        self,
        device: str,
        names: Sequence[str],
        layer: CalibrationMemoryLayer,
    ) -> CalibrationDataset: ...

    def apply_ram(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset: ...

    def persist_rom(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset: ...

    def restore_ram(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset: ...

    def restore_rom(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset: ...


class PersistenceStatus(str, Enum):
    PLANNED = "planned"
    CAPTURED = "captured"
    RAM_VERIFIED = "ram_verified"
    ROM_VERIFIED = "rom_verified"
    COMPLETED = "completed"
    ROLLED_BACK = "rolled_back"
    RECOVERY_REQUIRED = "recovery_required"
    FAILED = "failed"


@dataclass(slots=True)
class CalibrationPersistenceJob:
    job_id: str
    device: str
    actor: str
    approved_by: str
    working_digest: str
    persist_rom: bool
    status: PersistenceStatus = PersistenceStatus.PLANNED
    created_utc: str = field(default_factory=_utc_now)
    updated_utc: str = field(default_factory=_utc_now)
    events: list[dict[str, Any]] = field(default_factory=list)
    recovery_actions: list[str] = field(default_factory=list)
    error: str = ""

    def event(self, action: str, **details: Any) -> None:
        self.events.append(
            {
                "action": action,
                "created_utc": _utc_now(),
                **_json_value(details),
            }
        )
        self.updated_utc = _utc_now()

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "status": self.status.value,
        }

    def save(self, path: str | Path) -> Path:
        return _atomic_json(Path(path).expanduser().resolve(), self.to_dict())

    @classmethod
    def load(cls, path: str | Path) -> CalibrationPersistenceJob:
        data = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
        data["status"] = PersistenceStatus(data["status"])
        return cls(**data)

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "device": self.device,
            "actor": self.actor,
            "approved_by": self.approved_by,
            "working_digest": self.working_digest,
            "persist_rom": self.persist_rom,
            "status": self.status.value,
            "event_count": len(self.events),
            "recovery_actions": list(self.recovery_actions),
            "error": self.error,
            "created_utc": self.created_utc,
            "updated_utc": self.updated_utc,
        }


class CalibrationPersistenceCoordinator:
    """执行 Working→RAM→ROM，并处理写后掉线形成的不确定状态。"""

    def __init__(
        self,
        adapter: CalibrationTargetAdapter,
        ledger: CalibrationMemoryLedger,
        *,
        journal_path: str | Path,
        reconnect_attempts: int = 2,
    ) -> None:
        if reconnect_attempts < 0:
            raise ValueError("reconnect_attempts 不能为负数")
        self.adapter = adapter
        self.ledger = ledger
        self.journal_path = Path(journal_path).expanduser().resolve()
        self.lock_path = self.journal_path.with_suffix(
            self.journal_path.suffix + ".lock"
        )
        self.reconnect_attempts = reconnect_attempts

    def _save(self, job: CalibrationPersistenceJob) -> None:
        job.save(self.journal_path)

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return ctypes.get_last_error() == 5
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @contextmanager
    def _claim(self, job_id: str) -> Any:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            raise SafetyViolationError(
                f"持久化目标正被其他进程占用：{self.lock_path}"
            ) from None
        try:
            payload = json.dumps(
                {
                    "pid": os.getpid(),
                    "job_id": job_id,
                    "created_utc": _utc_now(),
                }
            ).encode("utf-8")
            os.write(descriptor, payload)
            yield
        finally:
            os.close(descriptor)
            self.lock_path.unlink(missing_ok=True)

    def recover_stale_lock(self, *, actor: str, reason: str) -> dict[str, Any]:
        if not actor.strip() or not reason.strip():
            raise ValueError("恢复锁必须提供 actor 和 reason")
        if not self.lock_path.exists():
            return {"recovered": False, "reason": "lock_not_found"}
        try:
            lock_data = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            lock_data = {"pid": None, "job_id": ""}
        pid = lock_data.get("pid")
        if isinstance(pid, int) and self._pid_exists(pid):
            raise SafetyViolationError(f"锁所属进程 {pid} 仍在运行")
        self.lock_path.unlink()
        if self.journal_path.exists():
            job = CalibrationPersistenceJob.load(self.journal_path)
            job.event(
                "stale_lock_recovered",
                actor=actor.strip(),
                reason=reason.strip(),
                previous_lock=lock_data,
            )
            self._save(job)
        return {
            "recovered": True,
            "actor": actor.strip(),
            "reason": reason.strip(),
            "previous_lock": lock_data,
        }

    def _reconnect(self, job: CalibrationPersistenceJob) -> None:
        errors = []
        for attempt in range(1, self.reconnect_attempts + 1):
            try:
                if self.adapter.is_online(job.device) or self.adapter.reconnect(job.device):
                    job.event("reconnected", attempt=attempt)
                    self._save(job)
                    return
            except Exception as exc:
                errors.append(str(exc))
                job.event("reconnect_failed", attempt=attempt, error=str(exc))
                self._save(job)
        suffix = f"：{'; '.join(errors)}" if errors else ""
        raise SafetyViolationError(f"{job.device} 掉线且重连失败{suffix}")

    def _capture(
        self,
        job: CalibrationPersistenceJob,
        names: Sequence[str],
        layer: CalibrationMemoryLayer,
    ) -> CalibrationDataset:
        try:
            return self.adapter.capture(job.device, names, layer)
        except Exception:
            if self.adapter.is_online(job.device):
                raise
            self._reconnect(job)
            return self.adapter.capture(job.device, names, layer)

    def _reconcile_mutation(
        self,
        job: CalibrationPersistenceJob,
        names: Sequence[str],
        layer: CalibrationMemoryLayer,
        expected: CalibrationDataset,
        original_error: Exception,
    ) -> CalibrationDataset:
        if not self.adapter.is_online(job.device):
            self._reconnect(job)
        actual = self.adapter.capture(job.device, names, layer)
        if _content_digest(actual) != _content_digest(expected):
            raise original_error
        job.event(
            "mutation_reconciled",
            layer=layer.value,
            disposition="target_already_applied",
            original_error=str(original_error),
        )
        self._save(job)
        return actual

    def execute(
        self,
        working: CalibrationDataset,
        *,
        job_id: str,
        actor: str,
        approved_by: str,
        persist_rom: bool = True,
    ) -> dict[str, Any]:
        with self._claim(job_id):
            return self._execute_locked(
                working,
                job_id=job_id,
                actor=actor,
                approved_by=approved_by,
                persist_rom=persist_rom,
            )

    def _execute_locked(
        self,
        working: CalibrationDataset,
        *,
        job_id: str,
        actor: str,
        approved_by: str,
        persist_rom: bool,
    ) -> dict[str, Any]:
        working.require_valid()
        if not job_id.strip() or not actor.strip() or not approved_by.strip():
            raise ValueError("job_id、actor 和 approved_by 均不能为空")
        if actor.casefold() == approved_by.casefold():
            raise SafetyViolationError("执行人不能审批自己的持久化作业")
        digest = _content_digest(working)
        if self.journal_path.exists():
            existing = CalibrationPersistenceJob.load(self.journal_path)
            if existing.job_id != job_id or existing.working_digest != digest:
                raise SafetyViolationError("作业日志已被其他 job_id 或数据集占用")
            if existing.status is PersistenceStatus.COMPLETED:
                return {**existing.summary(), "idempotent": True}
            raise SafetyViolationError(
                f"作业 {job_id} 已存在且状态为 {existing.status.value}，必须先处置恢复项"
            )

        job = CalibrationPersistenceJob(
            job_id=job_id.strip(),
            device=self.ledger.device,
            actor=actor.strip(),
            approved_by=approved_by.strip(),
            working_digest=digest,
            persist_rom=bool(persist_rom),
        )
        job.event("planned", parameter_count=len(working.parameters))
        self._save(job)
        names = sorted(working.parameters)
        baseline_ram: CalibrationDataset | None = None
        baseline_rom: CalibrationDataset | None = None
        ram_changed = False
        rom_changed = False
        try:
            baseline_ram = self._capture(
                job, names, CalibrationMemoryLayer.RAM
            )
            self.ledger.record(
                CalibrationMemoryLayer.WORKING,
                working,
                actor=actor,
                source=f"job:{job_id}",
            )
            self.ledger.record(
                CalibrationMemoryLayer.RAM,
                baseline_ram,
                actor="target-adapter",
                source="pre-write capture",
                verified=True,
            )
            if persist_rom:
                baseline_rom = self._capture(
                    job, names, CalibrationMemoryLayer.ROM
                )
                self.ledger.record(
                    CalibrationMemoryLayer.ROM,
                    baseline_rom,
                    actor="target-adapter",
                    source="pre-persist capture",
                    verified=True,
                )
            job.status = PersistenceStatus.CAPTURED
            job.event(
                "baseline_captured",
                ram_digest=_content_digest(baseline_ram),
                rom_digest=_content_digest(baseline_rom) if baseline_rom else "",
            )
            self._save(job)

            ram_changed = True
            try:
                self.adapter.apply_ram(job.device, working)
                actual_ram = self._capture(
                    job, names, CalibrationMemoryLayer.RAM
                )
            except Exception as exc:
                actual_ram = self._reconcile_mutation(
                    job,
                    names,
                    CalibrationMemoryLayer.RAM,
                    working,
                    exc,
                )
            if _content_digest(actual_ram) != digest:
                raise SafetyViolationError("RAM 写入后回读与 Working 数据集不一致")
            self.ledger.record(
                CalibrationMemoryLayer.RAM,
                actual_ram,
                actor="target-adapter",
                source=f"job:{job_id}:ram-verify",
                verified=True,
            )
            job.status = PersistenceStatus.RAM_VERIFIED
            job.event("ram_verified", digest=_content_digest(actual_ram))
            self._save(job)

            if persist_rom:
                rom_changed = True
                try:
                    self.adapter.persist_rom(job.device, working)
                    actual_rom = self._capture(
                        job, names, CalibrationMemoryLayer.ROM
                    )
                except Exception as exc:
                    actual_rom = self._reconcile_mutation(
                        job,
                        names,
                        CalibrationMemoryLayer.ROM,
                        working,
                        exc,
                    )
                if _content_digest(actual_rom) != digest:
                    raise SafetyViolationError("ROM 持久化后回读与 Working 数据集不一致")
                self.ledger.record(
                    CalibrationMemoryLayer.ROM,
                    actual_rom,
                    actor="target-adapter",
                    source=f"job:{job_id}:rom-verify",
                    verified=True,
                )
                job.status = PersistenceStatus.ROM_VERIFIED
                job.event("rom_verified", digest=_content_digest(actual_rom))
                self._save(job)

            job.status = PersistenceStatus.COMPLETED
            job.event("completed", persistent=persist_rom)
            self._save(job)
            return {**job.summary(), "idempotent": False}
        except Exception as exc:
            recovery_errors = []
            if rom_changed and baseline_rom is not None:
                try:
                    self.adapter.restore_rom(job.device, baseline_rom)
                    restored_rom = self._capture(
                        job, names, CalibrationMemoryLayer.ROM
                    )
                    if _content_digest(restored_rom) != _content_digest(baseline_rom):
                        raise SafetyViolationError("ROM 补偿回读不一致")
                    self.ledger.record(
                        CalibrationMemoryLayer.ROM,
                        restored_rom,
                        actor="target-adapter",
                        source=f"job:{job_id}:rom-rollback",
                        verified=True,
                    )
                    job.event("rom_rolled_back")
                except Exception as rollback_error:
                    recovery_errors.append(f"ROM: {rollback_error}")
            if ram_changed and baseline_ram is not None:
                try:
                    self.adapter.restore_ram(job.device, baseline_ram)
                    restored_ram = self._capture(
                        job, names, CalibrationMemoryLayer.RAM
                    )
                    if _content_digest(restored_ram) != _content_digest(baseline_ram):
                        raise SafetyViolationError("RAM 补偿回读不一致")
                    self.ledger.record(
                        CalibrationMemoryLayer.RAM,
                        restored_ram,
                        actor="target-adapter",
                        source=f"job:{job_id}:ram-rollback",
                        verified=True,
                    )
                    job.event("ram_rolled_back")
                except Exception as rollback_error:
                    recovery_errors.append(f"RAM: {rollback_error}")
            job.error = str(exc)
            job.recovery_actions = recovery_errors
            job.status = (
                PersistenceStatus.RECOVERY_REQUIRED
                if recovery_errors
                else PersistenceStatus.ROLLED_BACK
                if ram_changed or rom_changed
                else PersistenceStatus.FAILED
            )
            job.event(
                "failed",
                error=str(exc),
                recovery_actions=recovery_errors,
            )
            self._save(job)
            if recovery_errors:
                raise SafetyViolationError(
                    f"持久化失败且补偿不完整：{'; '.join(recovery_errors)}"
                ) from exc
            raise


class CANapeCalibrationTarget:
    """把 CANape 在线标定 API 接入统一目标协议。

    CANape 通用 COM API 可以确定地读写 RAM 标定对象。ROM 读取和持久化动作依赖
    ECU/CNA/脚本，因此必须由项目显式注入回调。
    """

    def __init__(
        self,
        canape: Any,
        *,
        rom_reader: Callable[[str, Sequence[str]], CalibrationDataset] | None = None,
        rom_persist: Callable[[str, CalibrationDataset], None] | None = None,
        rom_restore: Callable[[str, CalibrationDataset], None] | None = None,
    ) -> None:
        self.canape = canape
        self.rom_reader = rom_reader
        self.rom_persist = rom_persist
        self.rom_restore = rom_restore

    def is_online(self, device: str) -> bool:
        return bool(self.canape.is_device_online(device))

    def reconnect(self, device: str) -> bool:
        return bool(
            self.canape.reconnect_device(
                device, download=False, restore_measurement=True
            )
        )

    def capture(
        self,
        device: str,
        names: Sequence[str],
        layer: CalibrationMemoryLayer,
    ) -> CalibrationDataset:
        layer = CalibrationMemoryLayer(layer)
        if layer is CalibrationMemoryLayer.ROM:
            if self.rom_reader is None:
                raise NotImplementedError("当前项目未配置 ROM 回读适配器")
            dataset = self.rom_reader(device, names)
            dataset.require_valid()
            return _clone(dataset, source=f"CANape:{device}:ROM")
        if layer is not CalibrationMemoryLayer.RAM:
            raise ValueError("CANape 在线适配器只直接捕获 RAM 或项目定义的 ROM")
        parameters = {}
        for name in names:
            parameter = self.canape.read_calibration_parameter(device, name)
            parameters[name] = parameter.clone()
        return CalibrationDataset(
            parameters=parameters,
            identity={"ecu": device},
            source=f"CANape:{device}:RAM",
        )

    def apply_ram(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset:
        dataset.require_valid()
        for parameter in dataset.parameters.values():
            self.canape.write_calibration_parameter(device, parameter, verify=True)
        return self.capture(
            device, sorted(dataset.parameters), CalibrationMemoryLayer.RAM
        )

    def persist_rom(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset:
        if self.rom_persist is None or self.rom_reader is None:
            raise NotImplementedError("当前项目未配置 RAM→ROM 持久化和回读适配器")
        self.rom_persist(device, dataset)
        return self.capture(
            device, sorted(dataset.parameters), CalibrationMemoryLayer.ROM
        )

    def restore_ram(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset:
        return self.apply_ram(device, dataset)

    def restore_rom(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset:
        if self.rom_restore is None or self.rom_reader is None:
            raise NotImplementedError("当前项目未配置 ROM 恢复适配器")
        self.rom_restore(device, dataset)
        return self.capture(
            device, sorted(dataset.parameters), CalibrationMemoryLayer.ROM
        )


class InMemoryCalibrationTarget:
    """用于 CI、培训和故障注入验收的确定性目标适配器。"""

    def __init__(
        self,
        devices: Mapping[str, CalibrationDataset],
        *,
        rom: Mapping[str, CalibrationDataset] | None = None,
    ) -> None:
        self.ram = {
            name: _clone(dataset, source=f"memory:{name}:RAM")
            for name, dataset in devices.items()
        }
        self.rom = {
            name: _clone(
                (rom or {}).get(name, dataset),
                source=f"memory:{name}:ROM",
            )
            for name, dataset in devices.items()
        }
        self.online = {name: True for name in devices}
        self.failures: dict[str, int] = {}
        self.events: list[dict[str, Any]] = []

    def inject(self, operation: str, *, times: int = 1) -> None:
        if times <= 0:
            raise ValueError("times 必须大于 0")
        self.failures[operation] = self.failures.get(operation, 0) + times

    def _fail(self, operation: str, device: str) -> None:
        remaining = self.failures.get(operation, 0)
        if remaining <= 0:
            return
        self.failures[operation] = remaining - 1
        self.events.append({"operation": operation, "device": device, "failed": True})
        if "disconnect" in operation:
            self.online[device] = False
        raise RuntimeError(f"injected failure: {operation}")

    def is_online(self, device: str) -> bool:
        return bool(self.online[device])

    def reconnect(self, device: str) -> bool:
        self._fail("reconnect", device)
        self.online[device] = True
        self.events.append({"operation": "reconnect", "device": device})
        return True

    def capture(
        self,
        device: str,
        names: Sequence[str],
        layer: CalibrationMemoryLayer,
    ) -> CalibrationDataset:
        if not self.online[device]:
            raise ConnectionError(f"{device} offline")
        layer = CalibrationMemoryLayer(layer)
        self._fail(f"capture_{layer.value}", device)
        source = self.ram if layer is CalibrationMemoryLayer.RAM else self.rom
        if layer not in {CalibrationMemoryLayer.RAM, CalibrationMemoryLayer.ROM}:
            raise ValueError("内存适配器只捕获 RAM/ROM")
        missing = sorted(set(names) - set(source[device].parameters))
        if missing:
            raise KeyError(f"{device} 缺少标定量：{', '.join(missing)}")
        result = _clone(source[device], source=f"memory:{device}:{layer.value}")
        result.parameters = {name: result.parameters[name] for name in names}
        return result

    def apply_ram(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset:
        if not self.online[device]:
            raise ConnectionError(f"{device} offline")
        self._fail("apply_ram_before", device)
        self.ram[device] = _clone(dataset, source=f"memory:{device}:RAM")
        self.events.append({"operation": "apply_ram", "device": device})
        self._fail("apply_ram_after_disconnect", device)
        return _clone(self.ram[device])

    def persist_rom(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset:
        if not self.online[device]:
            raise ConnectionError(f"{device} offline")
        self._fail("persist_rom_before", device)
        self.rom[device] = _clone(dataset, source=f"memory:{device}:ROM")
        self.events.append({"operation": "persist_rom", "device": device})
        self._fail("persist_rom_after_disconnect", device)
        return _clone(self.rom[device])

    def restore_ram(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset:
        self._fail("restore_ram", device)
        self.ram[device] = _clone(dataset, source=f"memory:{device}:RAM")
        self.events.append({"operation": "restore_ram", "device": device})
        return _clone(self.ram[device])

    def restore_rom(
        self, device: str, dataset: CalibrationDataset
    ) -> CalibrationDataset:
        self._fail("restore_rom", device)
        self.rom[device] = _clone(dataset, source=f"memory:{device}:ROM")
        self.events.append({"operation": "restore_rom", "device": device})
        return _clone(self.rom[device])


@dataclass(frozen=True, slots=True)
class StagedECUPersistenceTask:
    device: str
    working: CalibrationDataset
    approved_by: str


class StagedMultiECUPersistenceCoordinator:
    """多 ECU 先全部 RAM 验证，再进入 ROM 持久化阶段。"""

    @classmethod
    def apply(
        cls,
        adapter: CalibrationTargetAdapter,
        tasks: Sequence[StagedECUPersistenceTask],
        *,
        actor: str,
        persist_rom: bool = True,
    ) -> dict[str, Any]:
        if not actor.strip():
            raise ValueError("actor 不能为空")
        devices = [task.device for task in tasks]
        if not tasks or len(devices) != len(set(devices)):
            raise ValueError("多 ECU 持久化任务为空或包含重复设备")
        for task in tasks:
            task.working.require_valid()
            if not task.approved_by.strip():
                raise SafetyViolationError(f"{task.device} 尚未审批")
            if task.approved_by.casefold() == actor.casefold():
                raise SafetyViolationError(f"{task.device} 执行人不能审批自己的作业")

        baselines_ram = {}
        baselines_rom = {}
        ram_applied: list[str] = []
        rom_applied: list[str] = []
        ram_touched: list[str] = []
        rom_touched: list[str] = []
        events = []

        def capture(
            task: StagedECUPersistenceTask,
            layer: CalibrationMemoryLayer,
        ) -> CalibrationDataset:
            names = sorted(task.working.parameters)
            try:
                return adapter.capture(task.device, names, layer)
            except Exception:
                if adapter.is_online(task.device) or not adapter.reconnect(task.device):
                    raise
                events.append({"device": task.device, "action": "reconnected"})
                return adapter.capture(task.device, names, layer)

        try:
            for task in tasks:
                baselines_ram[task.device] = capture(
                    task, CalibrationMemoryLayer.RAM
                )
                if persist_rom:
                    baselines_rom[task.device] = capture(
                        task, CalibrationMemoryLayer.ROM
                    )
            events.append({"action": "all_baselines_captured"})

            for task in tasks:
                ram_touched.append(task.device)
                try:
                    adapter.apply_ram(task.device, task.working)
                except Exception as mutation_error:
                    if not adapter.is_online(task.device):
                        if not adapter.reconnect(task.device):
                            raise
                        events.append(
                            {"device": task.device, "action": "reconnected"}
                        )
                    uncertain = capture(task, CalibrationMemoryLayer.RAM)
                    if _content_digest(uncertain) != _content_digest(task.working):
                        raise mutation_error
                actual = capture(task, CalibrationMemoryLayer.RAM)
                if _content_digest(actual) != _content_digest(task.working):
                    raise SafetyViolationError(f"{task.device} RAM 回读不一致")
                ram_applied.append(task.device)
                events.append({"device": task.device, "action": "ram_verified"})

            if persist_rom:
                for task in tasks:
                    rom_touched.append(task.device)
                    try:
                        adapter.persist_rom(task.device, task.working)
                    except Exception as mutation_error:
                        if not adapter.is_online(task.device):
                            if not adapter.reconnect(task.device):
                                raise
                            events.append(
                                {"device": task.device, "action": "reconnected"}
                            )
                        uncertain = capture(task, CalibrationMemoryLayer.ROM)
                        if _content_digest(uncertain) != _content_digest(task.working):
                            raise mutation_error
                    actual = capture(task, CalibrationMemoryLayer.ROM)
                    if _content_digest(actual) != _content_digest(task.working):
                        raise SafetyViolationError(f"{task.device} ROM 回读不一致")
                    rom_applied.append(task.device)
                    events.append({"device": task.device, "action": "rom_verified"})
        except Exception as exc:
            recovery_errors = []
            for device in reversed(rom_touched):
                try:
                    adapter.restore_rom(device, baselines_rom[device])
                except Exception as rollback_error:
                    recovery_errors.append(f"{device}/ROM: {rollback_error}")
            for device in reversed(ram_touched):
                try:
                    adapter.restore_ram(device, baselines_ram[device])
                except Exception as rollback_error:
                    recovery_errors.append(f"{device}/RAM: {rollback_error}")
            if recovery_errors:
                raise SafetyViolationError(
                    "多 ECU 分阶段持久化失败且补偿不完整："
                    + "; ".join(recovery_errors)
                ) from exc
            raise

        return {
            "status": "completed",
            "actor": actor,
            "persist_rom": persist_rom,
            "devices": devices,
            "ram_verified": ram_applied,
            "rom_verified": rom_applied,
            "events": events,
            "completed_utc": _utc_now(),
        }
