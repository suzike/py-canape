from __future__ import annotations

from pathlib import Path

import pytest

from agent2canape.calibration import (
    CalibrationChange,
    CalibrationDataset,
    CalibrationParameter,
    CalibrationPlan,
)
from agent2canape.calibration_operations import (
    CalibrationCandidate,
    CalibrationChangeItem,
    CalibrationChangeSet,
    CalibrationExperimentRunner,
    CalibrationExperimentStore,
    CalibrationMemoryLayer,
    CalibrationMemoryLedger,
    CalibrationObjective,
    ChangeSetStatus,
    ECUCalibrationTask,
    ExperimentCaseStatus,
    MultiECUCalibrationCoordinator,
    ParetoCalibrationAnalysis,
    ReviewDecision,
)
from agent2canape.cli import main
from agent2canape.errors import SafetyViolationError


class MultiDeviceBackend:
    def __init__(self) -> None:
        self.values = {
            "VCU": {"Gain": 1.0, "Offset": 0.0},
            "BMS": {"Gain": 2.0, "Offset": 1.0},
        }
        self.fail_once: tuple[str, str] | None = None

    def read_calibration_value(
        self, device: str, name: str, *, physical: bool = True
    ) -> float:
        return self.values[device][name]

    def write_calibration_value(
        self,
        device: str,
        name: str,
        value: float,
        *,
        physical: bool = True,
        verify: bool = True,
    ) -> float:
        if self.fail_once == (device, name):
            self.fail_once = None
            raise RuntimeError("simulated write failure")
        self.values[device][name] = value
        return value

    def get_calibration_metadata(self, device: str, name: str) -> dict[str, object]:
        return {}


def make_dataset(value: float = 1.0) -> CalibrationDataset:
    return CalibrationDataset(
        parameters={
            "Gain": CalibrationParameter(
                "Gain", value, minimum=0.0, maximum=10.0
            )
        },
        identity={"ecu": "VCU", "software": "SW_42"},
    )


def test_change_set_review_separation_and_roundtrip(tmp_path: Path) -> None:
    change_set = CalibrationChangeSet(
        name="thermal-release-42",
        owner="calibrator",
        ticket="CAL-42",
        required_approvals=2,
        items=[
            CalibrationChangeItem(
                name="Gain",
                value=2.0,
                expected_before=1.0,
                enforce_expected=True,
                reason="improve warm-up response",
                function_group="thermal-control",
                page="warm-up",
                owner="calibrator",
                risk="high",
                evidence=("baseline.mf4",),
            )
        ],
    )
    change_set.submit(make_dataset())
    with pytest.raises(SafetyViolationError):
        change_set.review("calibrator", ReviewDecision.APPROVE)

    change_set.review("system-owner", ReviewDecision.COMMENT, comment="bounds checked")
    change_set.review("reviewer-a", ReviewDecision.APPROVE)
    assert change_set.status is ChangeSetStatus.IN_REVIEW
    change_set.review("reviewer-b", ReviewDecision.APPROVE)
    assert change_set.status is ChangeSetStatus.APPROVED
    plan = change_set.build_plan()
    assert plan.approved_by == "reviewer-a, reviewer-b"
    assert plan.validate(make_dataset()) == []

    path = change_set.save(tmp_path / "change-set.json")
    restored = CalibrationChangeSet.load(path)
    assert restored.summary()["functions"] == {"thermal-control": 1}
    assert restored.status is ChangeSetStatus.APPROVED


def test_change_set_rejection_requires_reason() -> None:
    change_set = CalibrationChangeSet(
        "test",
        "owner",
        [
            CalibrationChangeItem(
                "Gain", 2.0, "test", "function", "page", "owner"
            )
        ],
    )
    change_set.submit(make_dataset())
    with pytest.raises(ValueError):
        change_set.review("reviewer", ReviewDecision.REJECT)
    change_set.review("reviewer", ReviewDecision.REJECT, comment="unsafe")
    assert change_set.status is ChangeSetStatus.REJECTED


def test_memory_ledger_tracks_working_ram_rom_and_transition(tmp_path: Path) -> None:
    ledger = CalibrationMemoryLedger("VCU")
    baseline = make_dataset(1.0)
    working = make_dataset(2.0)
    ledger.record("reference", baseline, actor="engineer", verified=True)
    ledger.record("working", working, actor="engineer")
    ledger.record("ram", baseline, actor="tool", source="ECU upload", verified=True)

    status = ledger.status()
    assert status["ram_dirty"] is True
    assert status["persistent"] is False
    plan = ledger.transition_plan("working", "ram")
    assert plan["change_count"] == 1
    assert plan["requires_hardware_action"] is True
    with pytest.raises(SafetyViolationError):
        ledger.require_persistent()

    ledger.record(CalibrationMemoryLayer.RAM, working, actor="tool", verified=True)
    ledger.record(CalibrationMemoryLayer.ROM, working, actor="tool", verified=True)
    ledger.require_persistent()
    restored = CalibrationMemoryLedger.load(ledger.save(tmp_path / "memory.json"))
    assert restored.status()["persistent"] is True


def test_experiment_checkpoint_evidence_and_retry(tmp_path: Path) -> None:
    backend = MultiDeviceBackend()
    store = CalibrationExperimentStore.create(
        tmp_path / "experiment.json",
        name="gain-sweep",
        device="VCU",
        cases=[{"Gain": 1.0}, {"Gain": 2.0}, {"Gain": 3.0}],
        identity={"software": "SW_42"},
    )
    failed = {1}

    def evaluate(index: int, parameters: dict[str, float]) -> dict[str, float]:
        if index in failed:
            raise RuntimeError("measurement invalid")
        return {"score": parameters["Gain"] ** 2}

    def collect(
        index: int,
        parameters: dict[str, float],
        metrics: dict[str, float],
    ) -> list[Path]:
        evidence = tmp_path / f"case-{index}.json"
        evidence.write_text(str({"parameters": parameters, "metrics": metrics}))
        return [evidence]

    result = CalibrationExperimentRunner.run(
        backend,
        store,
        evaluate,
        evidence_collector=collect,
        stop_on_error=False,
    )
    assert result["status_counts"]["passed"] == 2
    assert result["status_counts"]["failed"] == 1
    assert backend.values["VCU"]["Gain"] == 1.0

    failed.clear()
    resumed = CalibrationExperimentRunner.run(
        backend,
        CalibrationExperimentStore.load(store.path),
        evaluate,
        evidence_collector=collect,
        retry_failed=True,
    )
    assert resumed["status_counts"]["passed"] == 3
    assert resumed["invalid_evidence"] == []


def test_experiment_recovers_interrupted_case(tmp_path: Path) -> None:
    store = CalibrationExperimentStore.create(
        tmp_path / "interrupted.json",
        name="resume",
        device="VCU",
        cases=[{"Gain": 2.0}],
    )
    store.cases[0].status = ExperimentCaseStatus.RUNNING
    store.save()

    restored = CalibrationExperimentStore.load(store.path)
    assert restored.recover_interrupted() == 1
    assert restored.cases[0].status is ExperimentCaseStatus.PENDING
    with restored.claim(), pytest.raises(SafetyViolationError):
        CalibrationExperimentRunner.run(
            MultiDeviceBackend(),
            restored,
            lambda index, parameters: {"score": float(index)},
        )
    assert not restored.run_lock_path.exists()


def approved_plan(name: str, value: float) -> CalibrationPlan:
    return CalibrationPlan(
        [CalibrationChange("Gain", value, "coordinated calibration")],
        name=name,
        approved_by="reviewer",
        approved_utc="2026-07-30T00:00:00+00:00",
    )


def test_multi_ecu_coordinator_applies_all_devices() -> None:
    backend = MultiDeviceBackend()
    tasks = [
        ECUCalibrationTask("VCU", approved_plan("vcu-plan", 3.0)),
        ECUCalibrationTask("BMS", approved_plan("bms-plan", 4.0)),
    ]

    result = MultiECUCalibrationCoordinator.apply(backend, tasks)

    assert result["status"] == "applied"
    assert backend.values["VCU"]["Gain"] == 3.0
    assert backend.values["BMS"]["Gain"] == 4.0


def test_multi_ecu_coordinator_rolls_back_every_device() -> None:
    backend = MultiDeviceBackend()
    backend.fail_once = ("BMS", "Gain")
    tasks = [
        ECUCalibrationTask("VCU", approved_plan("vcu-plan", 3.0)),
        ECUCalibrationTask("BMS", approved_plan("bms-plan", 4.0)),
    ]

    with pytest.raises(RuntimeError):
        MultiECUCalibrationCoordinator.apply(backend, tasks)

    assert backend.values["VCU"]["Gain"] == 1.0
    assert backend.values["BMS"]["Gain"] == 2.0


def test_multi_ecu_requires_all_plans_approved() -> None:
    backend = MultiDeviceBackend()
    unapproved = CalibrationPlan(
        [CalibrationChange("Gain", 3.0, "not reviewed")], name="draft"
    )
    with pytest.raises(SafetyViolationError):
        MultiECUCalibrationCoordinator.apply(
            backend, [ECUCalibrationTask("VCU", unapproved)]
        )


def test_pareto_analysis_filters_safety_and_selects_balanced() -> None:
    candidates = [
        CalibrationCandidate("A", {"gain": 1.0}, {"comfort": 20.0, "energy": 10.0}),
        CalibrationCandidate("B", {"gain": 2.0}, {"comfort": 22.0, "energy": 7.0}),
        CalibrationCandidate("C", {"gain": 1.5}, {"comfort": 21.0, "energy": 8.0}),
        CalibrationCandidate("D", {"gain": 3.0}, {"comfort": 24.0, "energy": 12.0}),
        CalibrationCandidate("E", {"gain": 4.0}, {"comfort": 19.0, "energy": 17.0}),
    ]
    objectives = [
        CalibrationObjective("comfort", "minimize"),
        CalibrationObjective("energy", "minimize"),
    ]

    analysis = ParetoCalibrationAnalysis.analyze(
        candidates,
        objectives,
        safety_limits={"energy": (None, 15.0)},
    )

    assert {item["identifier"] for item in analysis["pareto_front"]} == {
        "A",
        "B",
        "C",
    }
    assert analysis["rejected"][0]["identifier"] == "E"
    balanced = ParetoCalibrationAnalysis.select_balanced(
        candidates[:3], objectives
    )
    assert balanced.identifier == "C"


def test_operation_status_cli_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    change_set = CalibrationChangeSet(
        "review",
        "owner",
        [
            CalibrationChangeItem(
                "Gain", 2.0, "review", "thermal", "warm-up", "owner"
            )
        ],
    )
    review_path = change_set.save(tmp_path / "review.json")
    assert main(["calibration-review", str(review_path)]) == 0
    assert '"functions"' in capsys.readouterr().out

    ledger = CalibrationMemoryLedger("VCU")
    for layer in ("working", "ram", "rom"):
        ledger.record(layer, make_dataset(), actor="test", verified=True)
    memory_path = ledger.save(tmp_path / "memory.json")
    assert main(["calibration-memory-status", str(memory_path)]) == 0
    assert '"persistent": true' in capsys.readouterr().out

    store = CalibrationExperimentStore.create(
        tmp_path / "experiment-status.json",
        name="status",
        device="VCU",
        cases=[{"Gain": 1.0}],
    )
    store.cases[0].status = ExperimentCaseStatus.PASSED
    store.save()
    assert main(["calibration-experiment-status", str(store.path)]) == 0
    assert '"passed": true' in capsys.readouterr().out
