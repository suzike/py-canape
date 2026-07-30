from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from py_canape import (
    CalibrationChange,
    CalibrationDataset,
    CalibrationExperiment,
    CalibrationKind,
    CalibrationMath,
    CalibrationOptimizer,
    CalibrationParameter,
    CalibrationPlan,
    CalibrationRepository,
    CalibrationSession,
    SafetyViolationError,
    SweepParameter,
)


class FakeBackend:
    def __init__(self):
        self.values = {"Gain": 1.0, "Offset": 0.0}
        self.fail_on = None

    def read_calibration_value(self, device, name, *, physical=True):
        return self.values[name]

    def write_calibration_value(
        self, device, name, value, *, physical=True, verify=True
    ):
        if name == self.fail_on:
            raise RuntimeError("write failed")
        self.values[name] = value
        return value

    def get_calibration_metadata(self, device, name):
        return {
            "unit": "%",
            "minimum": -10.0,
            "maximum": 10.0,
            "address": 0x1000,
            "conversion": "linear",
            "comment": "test",
        }


class CalibrationDatasetTests(unittest.TestCase):
    def make_dataset(self):
        return CalibrationDataset(
            parameters={
                "Scalar": CalibrationParameter(
                    "Scalar", 1.0, minimum=0.0, maximum=5.0
                ),
                "Curve": CalibrationParameter(
                    "Curve",
                    [1.0, 2.0, 3.0],
                    kind=CalibrationKind.CURVE,
                    x_axis=[0.0, 1.0, 2.0],
                ),
                "Map": CalibrationParameter(
                    "Map",
                    [[1.0, 2.0], [3.0, 4.0]],
                    kind=CalibrationKind.MAP,
                    x_axis=[0.0, 1.0],
                    y_axis=[10.0, 20.0],
                ),
            },
            identity={"software": "1.0", "calibration": "A"},
        )

    def test_dataset_validation_json_csv_and_digest(self):
        dataset = self.make_dataset()
        self.assertEqual(dataset.validate(), {})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            json_path = dataset.save(root / "dataset.json")
            csv_path = dataset.save(root / "dataset.csv")
            self.assertEqual(CalibrationDataset.load(json_path).values(), dataset.values())
            self.assertEqual(CalibrationDataset.load(csv_path).values(), dataset.values())
            self.assertEqual(
                CalibrationDataset.load(json_path).digest(), dataset.digest()
            )

    def test_dataset_diff_patch_and_three_way_merge(self):
        base = self.make_dataset()
        current = base.apply_patch({"Scalar": 2.0})
        incoming = base.apply_patch({"Curve": [2.0, 3.0, 4.0]})
        merged, conflicts = CalibrationDataset.three_way_merge(
            base, current, incoming
        )
        self.assertEqual(conflicts, {})
        self.assertEqual(merged.parameters["Scalar"].value, 2.0)
        self.assertEqual(merged.parameters["Curve"].value, [2.0, 3.0, 4.0])
        conflict = base.apply_patch({"Scalar": 3.0})
        with self.assertRaises(ValueError):
            CalibrationDataset.three_way_merge(base, current, conflict)

    def test_diff_patch_and_merge_include_axes_and_metadata(self):
        base = self.make_dataset()
        changed = base.apply_patch(
            {
                "Curve": {
                    "value": [1.0, 2.0, 3.0],
                    "x_axis": [0.0, 2.0, 4.0],
                    "comment": "axis update",
                }
            }
        )
        difference = base.diff(changed)["Curve"]
        self.assertIn("x_axis", difference["changed_fields"])
        self.assertIn("comment", difference["changed_fields"])
        merged, conflicts = CalibrationDataset.three_way_merge(
            base,
            base,
            changed,
        )
        self.assertFalse(conflicts)
        self.assertEqual(merged.parameters["Curve"].x_axis, [0.0, 2.0, 4.0])

    def test_repository_versions_compare_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = CalibrationRepository(directory)
            first = self.make_dataset()
            second = first.apply_patch({"Scalar": 2.0})
            repository.save(first, "v1", tags=["baseline"])
            repository.save(second, "v2")
            self.assertEqual(len(repository.list_versions()), 2)
            self.assertEqual(repository.compare("v1", "v2")["Scalar"]["after"], 2.0)
            output = repository.restore("v1", Path(directory) / "restored.json")
            self.assertTrue(output.is_file())

    def test_parameter_shape_and_axis_validation(self):
        invalid = CalibrationParameter(
            "BadMap",
            [[1.0, 2.0], [3.0]],
            kind=CalibrationKind.MAP,
            x_axis=[0.0, 0.0],
            y_axis=[1.0, 2.0],
        )
        errors = invalid.validate()
        self.assertTrue(any("行长度" in item for item in errors))
        self.assertTrue(any("严格递增" in item for item in errors))


class CalibrationPlanTests(unittest.TestCase):
    def test_plan_requires_approval_and_rolls_back(self):
        backend = FakeBackend()
        plan = CalibrationPlan(
            [
                CalibrationChange("Gain", 2.0, "optimize"),
                CalibrationChange("Offset", 1.0, "optimize"),
            ]
        )
        with self.assertRaises(SafetyViolationError):
            plan.apply(backend, "ECU")
        plan.approve("calibration-owner")
        backend.fail_on = "Offset"
        with self.assertRaises(RuntimeError):
            plan.apply(backend, "ECU")
        self.assertEqual(backend.values["Gain"], 1.0)

    def test_session_stage_commit_and_rollback(self):
        backend = FakeBackend()
        session = CalibrationSession(
            backend, "ECU", identity={"vehicle": "V1", "software": "S1"}
        )
        baseline = session.begin(["Gain", "Offset"])
        self.assertEqual(baseline.parameters["Gain"].value, 1.0)
        session.stage("Gain", 2.0, reason="target improvement")
        plan = session.plan(name="session-1", author="engineer")
        plan.approve("owner")
        session.commit(plan)
        self.assertEqual(backend.values["Gain"], 2.0)
        session.rollback()
        self.assertEqual(backend.values["Gain"], 1.0)


class CalibrationExperimentTests(unittest.TestCase):
    def test_designs_and_experiment_restore_baseline(self):
        parameters = [
            SweepParameter("Gain", (1.0, 2.0)),
            SweepParameter("Offset", (0.0, 1.0)),
        ]
        cases = CalibrationExperiment.full_factorial(parameters)
        self.assertEqual(len(cases), 4)
        lhs = CalibrationExperiment.latin_hypercube(
            {"Gain": (0.0, 10.0), "Offset": (-1.0, 1.0)}, 5, seed=7
        )
        self.assertEqual(len(lhs), 5)
        backend = FakeBackend()
        results = CalibrationExperiment.run(
            backend,
            "ECU",
            cases,
            lambda index, case: {"score": case["Gain"] + case["Offset"]},
        )
        self.assertEqual(len(results), 4)
        self.assertEqual(backend.values, {"Gain": 1.0, "Offset": 0.0})

    def test_math_and_optimizer(self):
        self.assertEqual(
            CalibrationMath.interpolate_curve([0.0, 10.0], [0.0, 100.0], 5.0),
            50.0,
        )
        self.assertEqual(
            CalibrationMath.interpolate_map(
                [0.0, 1.0],
                [0.0, 1.0],
                [[0.0, 10.0], [20.0, 30.0]],
                0.5,
                0.5,
            ),
            15.0,
        )
        self.assertEqual(
            CalibrationMath.limit_gradient([0.0, 5.0, -5.0], 2.0),
            [0.0, 2.0, 0.0],
        )
        result = CalibrationOptimizer.coordinate_search(
            {"x": 0.0},
            {"x": (-5.0, 5.0)},
            lambda point: (point["x"] - 3.0) ** 2,
            initial_step=2.0,
            tolerance=0.01,
        )
        self.assertAlmostEqual(result["parameters"]["x"], 3.0, places=2)
        score = CalibrationOptimizer.weighted_score(
            {"temperature": 22.0}, {"temperature": 20.0}, scales={"temperature": 2.0}
        )
        self.assertEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
