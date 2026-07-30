"""二维物理约束、DOE 质量门禁和安全代理优化示例。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from agent2canape import (
    CalibrationConstraintSet,
    CalibrationDataset,
    CalibrationExperimentReport,
    CalibrationExperimentRunner,
    CalibrationExperimentStore,
    CalibrationKind,
    CalibrationObservation,
    CalibrationParameter,
    ExperimentQualityPolicy,
    MapNeighborhoodConstraint,
    MetricAcceptanceRule,
    OutlierRule,
    PhysicalModelConstraint,
    SafeBayesianCalibrationOptimizer,
    SteadyStateRule,
)


class DemoBackend:
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


dataset = CalibrationDataset(
    parameters={
        "TorqueMap": CalibrationParameter(
            "TorqueMap",
            [[100.0, 105.0, 110.0], [105.0, 110.0, 115.0]],
            kind=CalibrationKind.MAP,
            x_axis=[0.0, 1000.0, 2000.0],
            y_axis=[0.0, 1.0],
        ),
        "PumpSpeed": CalibrationParameter("PumpSpeed", 2000.0),
        "CoolantFlow": CalibrationParameter("CoolantFlow", 10.0),
    }
)
constraints = CalibrationConstraintSet(
    additional=[
        MapNeighborhoodConstraint(
            "TorqueMap",
            maximum_x_gradient=0.02,
            maximum_y_gradient=10.0,
            maximum_diagonal_delta=15.0,
        ),
        PhysicalModelConstraint(
            "cooling-margin",
            evaluator=lambda data: {
                "flow_margin": (
                    float(data.parameters["CoolantFlow"].value)
                    - float(data.parameters["PumpSpeed"].value) / 400.0
                )
            },
            limits={"flow_margin": (3.0, None)},
            required_parameters=("CoolantFlow", "PumpSpeed"),
        ),
    ]
)
print({"constraint_errors": constraints.validate(dataset)})

policy = ExperimentQualityPolicy(
    steady_state_rules=[
        SteadyStateRule(
            "AmbientTemp",
            minimum_samples=5,
            maximum_span=0.5,
            maximum_absolute_slope=0.1,
        )
    ],
    metric_rules=[MetricAcceptanceRule("temperature", maximum=100.0)],
    outlier_rules=[OutlierRule("energy", method="mad", minimum_samples=5)],
)

with TemporaryDirectory() as directory:
    store = CalibrationExperimentStore.create(
        Path(directory) / "experiment.json",
        name="safe-gain-study",
        device="VCU",
        cases=[{"Gain": value} for value in (1.0, 1.5, 2.0, 2.5, 3.0)],
    )
    result = CalibrationExperimentRunner.run(
        DemoBackend(),
        store,
        lambda index, parameters: {
            "temperature": 90.0 + parameters["Gain"],
            "energy": 10.0 - parameters["Gain"],
        },
        quality_policy=policy,
        stability_probe=lambda index, parameters: [
            {"time": float(sample), "AmbientTemp": 25.0 + 0.02 * sample}
            for sample in range(5)
        ],
    )
    report = CalibrationExperimentReport.save(
        store, Path(directory) / "report.md"
    )
    print({"experiment": result, "report": str(report)})

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
suggestion = SafeBayesianCalibrationOptimizer.suggest(
    observations,
    [
        {"identifier": "candidate-1", "gain": 0.25},
        {"identifier": "candidate-2", "gain": 0.65},
        {"identifier": "candidate-3", "gain": 0.9},
    ],
    {"gain": (0.0, 1.0)},
    objective="error",
    safety_limits={"temperature": (None, 100.0)},
    safety_sigma=1.0,
)
print({"suggested": suggestion["suggested"]})
