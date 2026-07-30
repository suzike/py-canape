from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent2canape.calibration import CalibrationDataset, CalibrationParameter
from agent2canape.calibration_operations import CalibrationMemoryLedger
from agent2canape.calibration_targets import (
    CalibrationPersistenceCoordinator,
    CalibrationPersistenceJob,
    CANapeCalibrationTarget,
    InMemoryCalibrationTarget,
    PersistenceStatus,
    StagedECUPersistenceTask,
    StagedMultiECUPersistenceCoordinator,
)
from agent2canape.cli import main
from agent2canape.errors import SafetyViolationError


def dataset(device: str, gain: float) -> CalibrationDataset:
    return CalibrationDataset(
        parameters={
            "Gain": CalibrationParameter(
                "Gain", gain, unit="%", minimum=0.0, maximum=10.0
            )
        },
        identity={"ecu": device, "software": "SW_42"},
    )


def coordinator(
    target: InMemoryCalibrationTarget,
    tmp_path: Path,
    device: str = "VCU",
) -> CalibrationPersistenceCoordinator:
    return CalibrationPersistenceCoordinator(
        target,
        CalibrationMemoryLedger(device),
        journal_path=tmp_path / f"{device}-persistence.json",
    )


def test_persistence_job_completes_and_is_idempotent(tmp_path: Path) -> None:
    target = InMemoryCalibrationTarget({"VCU": dataset("VCU", 1.0)})
    operation = coordinator(target, tmp_path)

    result = operation.execute(
        dataset("VCU", 2.0),
        job_id="CAL-42",
        actor="calibrator",
        approved_by="reviewer",
    )

    assert result["status"] == "completed"
    assert result["idempotent"] is False
    assert target.ram["VCU"].parameters["Gain"].value == 2.0
    assert target.rom["VCU"].parameters["Gain"].value == 2.0
    assert operation.ledger.status()["persistent"] is True
    repeated = operation.execute(
        dataset("VCU", 2.0),
        job_id="CAL-42",
        actor="calibrator",
        approved_by="reviewer",
    )
    assert repeated["idempotent"] is True


def test_persistence_status_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = InMemoryCalibrationTarget({"VCU": dataset("VCU", 1.0)})
    operation = coordinator(target, tmp_path)
    operation.execute(
        dataset("VCU", 2.0),
        job_id="CAL-CLI",
        actor="calibrator",
        approved_by="reviewer",
    )
    assert (
        main(
            [
                "calibration-persistence-status",
                str(tmp_path / "VCU-persistence.json"),
            ]
        )
        == 0
    )
    assert '"status": "completed"' in capsys.readouterr().out


def test_persistence_requires_separate_approver(tmp_path: Path) -> None:
    target = InMemoryCalibrationTarget({"VCU": dataset("VCU", 1.0)})
    with pytest.raises(SafetyViolationError):
        coordinator(target, tmp_path).execute(
            dataset("VCU", 2.0),
            job_id="CAL-42",
            actor="same-user",
            approved_by="same-user",
        )


def test_persistence_process_lock_and_explicit_stale_recovery(
    tmp_path: Path,
) -> None:
    target = InMemoryCalibrationTarget({"VCU": dataset("VCU", 1.0)})
    operation = coordinator(target, tmp_path)
    operation.lock_path.write_text(
        json.dumps({"pid": os.getpid(), "job_id": "live"}), encoding="utf-8"
    )
    with pytest.raises(SafetyViolationError, match="仍在运行"):
        operation.recover_stale_lock(actor="engineer", reason="test")
    operation.lock_path.write_text(
        json.dumps({"pid": 2147483647, "job_id": "stale"}), encoding="utf-8"
    )
    recovered = operation.recover_stale_lock(
        actor="engineer", reason="owner process exited"
    )
    assert recovered["recovered"] is True
    assert not operation.lock_path.exists()


@pytest.mark.parametrize(
    "failure",
    ["apply_ram_after_disconnect", "persist_rom_after_disconnect"],
)
def test_write_after_disconnect_is_reconciled(
    tmp_path: Path, failure: str
) -> None:
    target = InMemoryCalibrationTarget({"VCU": dataset("VCU", 1.0)})
    target.inject(failure)

    result = coordinator(target, tmp_path).execute(
        dataset("VCU", 3.0),
        job_id="CAL-43",
        actor="calibrator",
        approved_by="reviewer",
    )

    assert result["status"] == "completed"
    assert any(
        event["action"] == "mutation_reconciled"
        for event in CalibrationPersistenceJob.load(
            tmp_path / "VCU-persistence.json"
        ).events
    )
    assert target.is_online("VCU")


def test_rom_failure_rolls_ram_back(tmp_path: Path) -> None:
    baseline = dataset("VCU", 1.0)
    target = InMemoryCalibrationTarget({"VCU": baseline})
    target.inject("persist_rom_before")
    operation = coordinator(target, tmp_path)

    with pytest.raises(RuntimeError, match="persist_rom_before"):
        operation.execute(
            dataset("VCU", 4.0),
            job_id="CAL-44",
            actor="calibrator",
            approved_by="reviewer",
        )

    assert target.ram["VCU"].parameters["Gain"].value == 1.0
    assert target.rom["VCU"].parameters["Gain"].value == 1.0
    job = CalibrationPersistenceJob.load(tmp_path / "VCU-persistence.json")
    assert job.status is PersistenceStatus.ROLLED_BACK


def test_incomplete_compensation_requires_manual_recovery(tmp_path: Path) -> None:
    target = InMemoryCalibrationTarget({"VCU": dataset("VCU", 1.0)})
    target.inject("persist_rom_before")
    target.inject("restore_ram")

    with pytest.raises(SafetyViolationError, match="补偿不完整"):
        coordinator(target, tmp_path).execute(
            dataset("VCU", 4.0),
            job_id="CAL-45",
            actor="calibrator",
            approved_by="reviewer",
        )

    job = CalibrationPersistenceJob.load(tmp_path / "VCU-persistence.json")
    assert job.status is PersistenceStatus.RECOVERY_REQUIRED
    assert job.recovery_actions[0].startswith("RAM:")


class FakeCANape:
    def __init__(self) -> None:
        self.values = {"Gain": 1.0}
        self.online = True

    def is_device_online(self, device: str) -> bool:
        return self.online

    def reconnect_device(
        self, device: str, *, download: bool, restore_measurement: bool
    ) -> bool:
        self.online = True
        return True

    def read_calibration_parameter(
        self, device: str, name: str
    ) -> CalibrationParameter:
        return CalibrationParameter(name, self.values[name], unit="%")

    def write_calibration_parameter(
        self, device: str, parameter: CalibrationParameter, *, verify: bool
    ) -> CalibrationParameter:
        self.values[parameter.name] = parameter.value
        return parameter.clone()


def test_canape_target_requires_explicit_rom_adapter() -> None:
    canape = FakeCANape()
    target = CANapeCalibrationTarget(canape)
    captured = target.capture("VCU", ["Gain"], "ram")
    assert captured.parameters["Gain"].value == 1.0
    target.apply_ram("VCU", dataset("VCU", 2.0))
    assert canape.values["Gain"] == 2.0
    with pytest.raises(NotImplementedError, match="RAM→ROM"):
        target.persist_rom("VCU", dataset("VCU", 2.0))


def test_canape_target_uses_project_rom_callbacks() -> None:
    canape = FakeCANape()
    rom = {"VCU": dataset("VCU", 1.0)}

    def read_rom(device: str, names: list[str]) -> CalibrationDataset:
        return rom[device]

    def write_rom(device: str, value: CalibrationDataset) -> None:
        rom[device] = value

    target = CANapeCalibrationTarget(
        canape,
        rom_reader=read_rom,
        rom_persist=write_rom,
        rom_restore=write_rom,
    )
    target.persist_rom("VCU", dataset("VCU", 3.0))
    assert target.capture("VCU", ["Gain"], "rom").parameters["Gain"].value == 3.0


def test_multi_ecu_stages_all_ram_before_rom() -> None:
    target = InMemoryCalibrationTarget(
        {
            "VCU": dataset("VCU", 1.0),
            "BMS": dataset("BMS", 2.0),
        }
    )
    tasks = [
        StagedECUPersistenceTask("VCU", dataset("VCU", 3.0), "reviewer"),
        StagedECUPersistenceTask("BMS", dataset("BMS", 4.0), "reviewer"),
    ]

    result = StagedMultiECUPersistenceCoordinator.apply(
        target, tasks, actor="calibrator"
    )

    assert result["ram_verified"] == ["VCU", "BMS"]
    assert result["rom_verified"] == ["VCU", "BMS"]
    operations = [
        (event["operation"], event["device"])
        for event in target.events
        if event["operation"] in {"apply_ram", "persist_rom"}
    ]
    assert operations == [
        ("apply_ram", "VCU"),
        ("apply_ram", "BMS"),
        ("persist_rom", "VCU"),
        ("persist_rom", "BMS"),
    ]


def test_multi_ecu_rom_failure_restores_all_applied_layers() -> None:
    target = InMemoryCalibrationTarget(
        {
            "VCU": dataset("VCU", 1.0),
            "BMS": dataset("BMS", 2.0),
        }
    )
    target.inject("persist_rom_before")
    tasks = [
        StagedECUPersistenceTask("VCU", dataset("VCU", 3.0), "reviewer"),
        StagedECUPersistenceTask("BMS", dataset("BMS", 4.0), "reviewer"),
    ]

    with pytest.raises(RuntimeError, match="persist_rom_before"):
        StagedMultiECUPersistenceCoordinator.apply(
            target, tasks, actor="calibrator"
        )

    assert target.ram["VCU"].parameters["Gain"].value == 1.0
    assert target.ram["BMS"].parameters["Gain"].value == 2.0
    assert target.rom["VCU"].parameters["Gain"].value == 1.0
    assert target.rom["BMS"].parameters["Gain"].value == 2.0


def test_multi_ecu_reports_failing_device_compensation_error() -> None:
    target = InMemoryCalibrationTarget(
        {
            "VCU": dataset("VCU", 1.0),
            "BMS": dataset("BMS", 2.0),
        }
    )
    target.inject("persist_rom_before")
    target.inject("restore_rom")

    with pytest.raises(
        SafetyViolationError,
        match="VCU/ROM",
    ):
        StagedMultiECUPersistenceCoordinator.apply(
            target,
            [
                StagedECUPersistenceTask(
                    "VCU", dataset("VCU", 3.0), "reviewer"
                ),
                StagedECUPersistenceTask(
                    "BMS", dataset("BMS", 4.0), "reviewer"
                ),
            ],
            actor="calibrator",
        )


def test_multi_ecu_reconciles_uncertain_ram_write() -> None:
    target = InMemoryCalibrationTarget(
        {
            "VCU": dataset("VCU", 1.0),
            "BMS": dataset("BMS", 2.0),
        }
    )
    target.inject("apply_ram_after_disconnect")
    result = StagedMultiECUPersistenceCoordinator.apply(
        target,
        [
            StagedECUPersistenceTask("VCU", dataset("VCU", 3.0), "reviewer"),
            StagedECUPersistenceTask("BMS", dataset("BMS", 4.0), "reviewer"),
        ],
        actor="calibrator",
        persist_rom=False,
    )
    assert result["status"] == "completed"
    assert any(event["action"] == "reconnected" for event in result["events"])
