from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent2canape.calibration import (
    CalibrationChange,
    CalibrationConstraintSet,
    CalibrationDataset,
    CalibrationKind,
    CalibrationParameter,
    CalibrationPlan,
)
from agent2canape.calibration_design import (
    CalibrationExperimentReport,
    CalibrationObservation,
    ExperimentQualityPolicy,
    MapNeighborhoodConstraint,
    MetricAcceptanceRule,
    OutlierRule,
    PhysicalModelConstraint,
    SafeBayesianCalibrationOptimizer,
    SteadyStateRule,
)
from agent2canape.calibration_operations import (
    CalibrationExperimentRunner,
    CalibrationExperimentStore,
    ExperimentCaseStatus,
)
from agent2canape.cli import main


def map_dataset(values: list[list[float]]) -> CalibrationDataset:
    return CalibrationDataset(
        parameters={
            "TorqueMap": CalibrationParameter(
                "TorqueMap",
                values,
                kind=CalibrationKind.MAP,
                x_axis=[0.0, 10.0, 20.0],
                y_axis=[0.0, 5.0],
            ),
            "PumpSpeed": CalibrationParameter(
                "PumpSpeed", 2000.0, minimum=0.0, maximum=5000.0
            ),
            "CoolantFlow": CalibrationParameter(
                "CoolantFlow", 10.0, minimum=0.0, maximum=30.0
            ),
        }
    )


def test_map_neighborhood_constraint_and_plan_integration() -> None:
    constraint = MapNeighborhoodConstraint(
        "TorqueMap",
        maximum_x_gradient=1.0,
        maximum_y_gradient=2.0,
        maximum_diagonal_delta=15.0,
    )
    valid = map_dataset([[0.0, 5.0, 10.0], [5.0, 10.0, 15.0]])
    assert constraint.validate(valid) == []
    invalid = map_dataset([[0.0, 5.0, 40.0], [5.0, 10.0, 15.0]])
    errors = constraint.validate(invalid)
    assert any("X 梯度" in error for error in errors)
    assert any("对角变化" in error for error in errors)

    plan = CalibrationPlan(
        [
            CalibrationChange(
                "TorqueMap",
                [[0.0, 5.0, 40.0], [5.0, 10.0, 15.0]],
                "test surface",
            )
        ],
        constraints=CalibrationConstraintSet(additional=[constraint]),
    )
    assert any("X 梯度" in error for error in plan.validate(valid))


def test_physical_model_constraint_checks_derived_outputs() -> None:
    constraint = PhysicalModelConstraint(
        "cooling-hydraulics",
        evaluator=lambda data: {
            "flow_margin": (
                float(data.parameters["CoolantFlow"].value)
                - float(data.parameters["PumpSpeed"].value) / 400.0
            )
        },
        limits={"flow_margin": (3.0, None)},
        required_parameters=("CoolantFlow", "PumpSpeed"),
    )
    assert constraint.validate(map_dataset([[0, 1, 2], [1, 2, 3]])) == []
    unsafe = map_dataset([[0, 1, 2], [1, 2, 3]])
    unsafe.parameters["CoolantFlow"].value = 6.0
    assert "flow_margin" in constraint.validate(unsafe)[0]


def test_steady_state_rule_uses_span_slope_and_range() -> None:
    rule = SteadyStateRule(
        "CoolantTemp",
        minimum_samples=4,
        maximum_span=1.0,
        maximum_absolute_slope=0.2,
        minimum=80.0,
        maximum=100.0,
    )
    passed = rule.evaluate(
        [
            {"time": 0.0, "CoolantTemp": 90.0},
            {"time": 1.0, "CoolantTemp": 90.1},
            {"time": 2.0, "CoolantTemp": 90.0},
            {"time": 3.0, "CoolantTemp": 90.2},
        ]
    )
    assert passed["passed"] is True
    failed = rule.evaluate(
        [
            {"time": 0.0, "CoolantTemp": 85.0},
            {"time": 1.0, "CoolantTemp": 87.0},
            {"time": 2.0, "CoolantTemp": 89.0},
            {"time": 3.0, "CoolantTemp": 91.0},
        ]
    )
    assert failed["passed"] is False
    assert any("波动范围" in issue for issue in failed["issues"])
    assert any("斜率" in issue for issue in failed["issues"])
    invalid_time = rule.evaluate(
        [
            {"time": 0.0, "CoolantTemp": 90.0},
            {"time": 1.0, "CoolantTemp": 90.0},
            {"time": 1.0, "CoolantTemp": 90.0},
            {"time": 2.0, "CoolantTemp": 90.0},
        ]
    )
    assert "时间轴必须严格递增" in invalid_time["issues"][0]


class Backend:
    def __init__(self) -> None:
        self.values = {"Gain": 1.0}

    def read_calibration_value(
        self, device: str, name: str, *, physical: bool = True
    ) -> float:
        return self.values[name]

    def write_calibration_value(
        self,
        device: str,
        name: str,
        value: float,
        *,
        physical: bool = True,
        verify: bool = True,
    ) -> float:
        self.values[name] = value
        return value

    def get_calibration_metadata(
        self, device: str, name: str
    ) -> dict[str, object]:
        return {}


def test_experiment_quality_rejects_unstable_and_metric_cases(
    tmp_path: Path,
) -> None:
    backend = Backend()
    store = CalibrationExperimentStore.create(
        tmp_path / "quality-experiment.json",
        name="quality",
        device="VCU",
        cases=[{"Gain": 1.0}, {"Gain": 2.0}, {"Gain": 3.0}],
    )
    policy = ExperimentQualityPolicy(
        steady_state_rules=[
            SteadyStateRule(
                "AmbientTemp",
                minimum_samples=4,
                maximum_span=1.0,
                maximum_absolute_slope=0.2,
            )
        ],
        metric_rules=[MetricAcceptanceRule("score", maximum=50.0)],
    )

    def stability(
        index: int, parameters: dict[str, float]
    ) -> list[dict[str, float]]:
        values = [20.0, 22.0, 24.0, 26.0] if index == 0 else [20.0] * 4
        return [
            {"time": float(sample), "AmbientTemp": value}
            for sample, value in enumerate(values)
        ]

    result = CalibrationExperimentRunner.run(
        backend,
        store,
        lambda index, parameters: {
            "score": 100.0 if index == 1 else 10.0
        },
        quality_policy=policy,
        stability_probe=stability,
    )

    assert result["status_counts"]["rejected"] == 2
    assert result["status_counts"]["passed"] == 1
    assert result["rejected_this_run"] == 2
    assert backend.values["Gain"] == 1.0
    assert store.cases[0].status is ExperimentCaseStatus.REJECTED
    assert any("波动范围" in item for item in store.cases[0].rejection_reasons)
    assert store.cases[1].rejection_reasons == ["score=100 > 50.0"]


def test_experiment_requires_probe_for_steady_policy(tmp_path: Path) -> None:
    store = CalibrationExperimentStore.create(
        tmp_path / "missing-probe.json",
        name="missing-probe",
        device="VCU",
        cases=[{"Gain": 1.0}],
    )
    policy = ExperimentQualityPolicy(
        steady_state_rules=[SteadyStateRule("AmbientTemp")]
    )
    with pytest.raises(ValueError, match="stability_probe"):
        CalibrationExperimentRunner.run(
            Backend(),
            store,
            lambda index, parameters: {"score": 1.0},
            quality_policy=policy,
        )


def test_outlier_rule_rejects_case_and_report_preserves_reason(
    tmp_path: Path,
) -> None:
    backend = Backend()
    store = CalibrationExperimentStore.create(
        tmp_path / "outliers.json",
        name="outlier-study",
        device="VCU",
        cases=[{"Gain": float(index)} for index in range(7)],
        identity={"vehicle": "P301", "software": "SW_42"},
    )
    scores = [10.0, 11.0, 9.0, 10.0, 10.5, 9.5, 100.0]
    policy = ExperimentQualityPolicy(
        outlier_rules=[
            OutlierRule("score", method="mad", threshold=3.5, minimum_samples=5)
        ]
    )
    result = CalibrationExperimentRunner.run(
        backend,
        store,
        lambda index, parameters: {"score": scores[index]},
        quality_policy=policy,
    )
    assert result["status_counts"]["rejected"] == 1
    assert store.cases[6].status is ExperimentCaseStatus.REJECTED
    assert "队列异常值" in store.cases[6].rejection_reasons[0]
    restored = CalibrationExperimentStore.load(store.path)
    assert restored.quality_summary["outliers"][0]["outlier_indices"] == [6]

    report = CalibrationExperimentReport.build(store)
    assert report["metrics"]["score"]["count"] == 6
    assert report["rejected_cases"][0]["index"] == 6
    json_path = CalibrationExperimentReport.save(
        store, tmp_path / "report.json"
    )
    markdown_path = CalibrationExperimentReport.save(
        store, tmp_path / "report.md"
    )
    assert '"outlier-study"' in json_path.read_text(encoding="utf-8")
    assert "# outlier-study 标定实验报告" in markdown_path.read_text(
        encoding="utf-8"
    )


def test_outlier_rule_records_insufficient_sample_reason() -> None:
    result = OutlierRule("score", minimum_samples=5).detect(
        [(0, 1.0), (1, 2.0)]
    )
    assert result["applied"] is False
    assert result["outlier_indices"] == []


def test_safe_bayesian_optimizer_filters_and_ranks_candidates() -> None:
    observations = [
        CalibrationObservation(
            "A", {"gain": 0.0}, {"error": 0.49, "temperature": 80.0}
        ),
        CalibrationObservation(
            "B", {"gain": 0.5}, {"error": 0.04, "temperature": 85.0}
        ),
        CalibrationObservation(
            "C", {"gain": 1.0}, {"error": 0.09, "temperature": 105.0}
        ),
    ]
    candidates = [
        {"identifier": "duplicate", "gain": 0.5},
        {"identifier": "near-safe", "gain": 0.65},
        {"identifier": "hot-side", "gain": 0.9},
        {"identifier": "conservative", "gain": 0.25},
    ]
    result = SafeBayesianCalibrationOptimizer.suggest(
        observations,
        candidates,
        {"gain": (0.0, 1.0)},
        objective="error",
        safety_limits={"temperature": (None, 100.0)},
        safety_sigma=1.0,
        max_extrapolation_distance=0.5,
    )
    assert result["passed"] is True
    assert result["suggested"]["identifier"] in {
        "near-safe",
        "conservative",
    }
    duplicate = next(
        item for item in result["rejected"] if item["identifier"] == "duplicate"
    )
    assert "候选与已观测点重复" in duplicate["reasons"]
    assert result["model"]["type"] == "gaussian_process_rbf"


def test_safe_bayesian_candidate_grid_and_limits() -> None:
    grid = SafeBayesianCalibrationOptimizer.candidate_grid(
        {"gain": (0.0, 1.0), "offset": (-1.0, 1.0)},
        levels={"gain": 3, "offset": 2},
    )
    assert len(grid) == 6
    with pytest.raises(ValueError, match="maximum_candidates"):
        SafeBayesianCalibrationOptimizer.candidate_grid(
            {"x": (0.0, 1.0), "y": (0.0, 1.0)},
            levels=101,
            maximum_candidates=10_000,
        )


def test_design_cli_report_and_safe_suggestion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = CalibrationExperimentStore.create(
        tmp_path / "cli-experiment.json",
        name="cli-design",
        device="VCU",
        cases=[{"Gain": 1.0}],
    )
    store.cases[0].status = ExperimentCaseStatus.PASSED
    store.cases[0].metrics = {"score": 1.0}
    store.save()
    report_path = tmp_path / "cli-report.md"
    assert (
        main(
            [
                "calibration-experiment-report",
                str(store.path),
                "--output",
                str(report_path),
            ]
        )
        == 0
    )
    assert report_path.is_file()
    assert '"status": "passed"' in capsys.readouterr().out

    spec = {
        "observations": [
            {
                "identifier": "A",
                "parameters": {"gain": 0.0},
                "metrics": {"error": 1.0},
            },
            {
                "identifier": "B",
                "parameters": {"gain": 1.0},
                "metrics": {"error": 0.2},
            },
        ],
        "candidates": [{"identifier": "C", "gain": 0.5}],
        "bounds": {"gain": [0.0, 1.0]},
        "objective": "error",
    }
    spec_path = tmp_path / "safe-suggest.json"
    spec_path.write_text(
        json.dumps(spec, ensure_ascii=False), encoding="utf-8"
    )
    assert main(["calibration-safe-suggest", str(spec_path)]) == 0
    assert '"suggested"' in capsys.readouterr().out
