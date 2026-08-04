from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent2canape import (
    ApprovalStore,
    CalibrationChangeItem,
    CalibrationChangeSet,
    CalibrationExperimentStore,
    CalibrationKind,
    CalibrationParameter,
    CalibrationPersistenceJob,
    CANapeAIToolkit,
    ExperimentCaseStatus,
    PersistenceStatus,
    SafetyViolationError,
)


class FakeAICANape:
    def __init__(self):
        self.connected = False
        self.values = {"Gain": 1.0}
        self.measurement = False
        self.online = False

    def connect(self):
        self.connected = True

    def get_project_info(self):
        return {"working_directory": "project", "cna_filename": "config.cna"}

    def open(self, path):
        self.connected = True
        self.path = path

    def list_devices(self):
        return [
            SimpleNamespace(
                name="ECU",
                driver_type="XCP",
                channel=1,
                online=self.online,
                database_filename="ecu.a2l",
            )
        ]

    def list_calibration_objects(self, device, *, limit=None):
        return list(self.values)[:limit]

    def read_calibration_parameter(self, device, name):
        return CalibrationParameter(
            name=name,
            value=self.values[name],
            kind=CalibrationKind.SCALAR,
            minimum=0.0,
            maximum=10.0,
        )

    def read_calibration_value(self, device, name, *, physical=True):
        return self.values[name]

    def write_calibration_value(
        self, device, name, value, *, physical=True, verify=True
    ):
        self.values[name] = value
        return value

    def write_calibration_parameter(self, device, parameter, *, verify=True):
        self.values[parameter.name] = parameter.value
        return parameter

    def set_device_online(self, device, *, download=False):
        self.online = True

    def set_device_offline(self, device):
        self.online = False

    def is_device_online(self, device):
        return self.online

    def start_measurement(self):
        self.measurement = True
        return True

    def stop_measurement(self):
        self.measurement = False
        return True

    def is_measurement_running(self):
        return self.measurement

    def get_measurement_state(self):
        return 2 if self.measurement else 0

    def read_memory(self, device, address, size, *, address_extension=0):
        return tuple(range(size))

    def write_memory(
        self, device, address, data, *, address_extension=0, verify=True
    ):
        return tuple(data)

    def send_raw_diagnostic_request(self, device, payload, *, timeout=5.0):
        return []

    def start_flash(self, device, job, session, *, config_file=None):
        return None


class AIToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.approval_path = Path(self.temporary.name) / "approvals.json"
        self.store = ApprovalStore(self.approval_path)
        self.canape = FakeAICANape()
        self.toolkit = CANapeAIToolkit(self.canape, approvals=self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def test_manifest_and_read_only_execution(self):
        manifest = self.toolkit.registry.manifest()
        self.assertGreaterEqual(len(manifest), 15)
        self.assertEqual(len({item["name"] for item in manifest}), len(manifest))
        result = self.toolkit.registry.invoke("device_list", {})
        self.assertTrue(result["executed"])
        self.assertEqual(result["result"][0]["name"], "ECU")

    def test_write_requires_out_of_band_approval_and_is_single_use(self):
        arguments = {
            "device": "ECU",
            "name": "Gain",
            "value": 2.0,
            "reason": "optimize",
        }
        planned = self.toolkit.registry.invoke(
            "calibration_write", arguments, dry_run=True
        )
        self.assertFalse(planned["executed"])
        plan_id = planned["action_plan"]["id"]
        self.store.approve(plan_id, "engineer")
        result = self.toolkit.registry.invoke(
            "calibration_write",
            arguments,
            dry_run=False,
            action_plan_id=plan_id,
        )
        self.assertTrue(result["executed"])
        self.assertEqual(self.canape.values["Gain"], 2.0)
        with self.assertRaises(SafetyViolationError):
            self.toolkit.registry.invoke(
                "calibration_write",
                arguments,
                dry_run=False,
                action_plan_id=plan_id,
            )

    def test_curve_write_uses_same_approved_parameter_set(self):
        self.canape.values["Curve"] = [1.0, 2.0]

        def read_curve(device, name):
            return CalibrationParameter(
                name=name,
                value=self.canape.values[name],
                kind=CalibrationKind.CURVE,
                x_axis=[0.0, 1.0],
                minimum=0.0,
                maximum=10.0,
            )

        self.canape.read_calibration_parameter = read_curve
        arguments = {
            "device": "ECU",
            "name": "Curve",
            "value": [2.0, 3.0],
            "x_axis": [0.0, 2.0],
            "reason": "curve calibration",
        }
        planned = self.toolkit.registry.invoke("calibration_write", arguments)
        plan_id = planned["action_plan"]["id"]
        self.store.approve(plan_id, "calibrator")
        result = self.toolkit.registry.invoke(
            "calibration_write",
            arguments,
            dry_run=False,
            action_plan_id=plan_id,
        )
        self.assertEqual(result["result"]["kind"], "curve")
        self.assertEqual(self.canape.values["Curve"], [2.0, 3.0])

    def test_argument_tampering_invalidates_approval(self):
        arguments = {"device": "ECU", "address": 4096, "data": [1, 2]}
        planned = self.toolkit.registry.invoke("memory_write", arguments)
        plan_id = planned["action_plan"]["id"]
        self.store.approve(plan_id, "engineer")
        changed = {**arguments, "address": 8192}
        with self.assertRaises(SafetyViolationError):
            self.toolkit.registry.invoke(
                "memory_write",
                changed,
                dry_run=False,
                action_plan_id=plan_id,
            )
        self.assertEqual(self.store.get(plan_id).status, "stale")

    def test_failed_action_plan_cannot_be_retried(self):
        def fail_write(device, parameter, *, verify=True):
            raise RuntimeError("simulated write failure")

        self.canape.write_calibration_parameter = fail_write
        arguments = {
            "device": "ECU",
            "name": "Gain",
            "value": 2.0,
            "reason": "runtime failure",
        }
        planned = self.toolkit.registry.invoke("calibration_write", arguments)
        plan_id = planned["action_plan"]["id"]
        self.store.approve(plan_id, "engineer")
        with self.assertRaises(RuntimeError):
            self.toolkit.registry.invoke(
                "calibration_write",
                arguments,
                dry_run=False,
                action_plan_id=plan_id,
            )
        self.assertEqual(self.store.get(plan_id).status, "failed")
        with self.assertRaises(SafetyViolationError):
            self.toolkit.registry.invoke(
                "calibration_write",
                arguments,
                dry_run=False,
                action_plan_id=plan_id,
            )

    def test_calibration_plan_previews_difference_and_recovery(self):
        arguments = {
            "device": "ECU",
            "name": "Gain",
            "value": 2.5,
            "reason": "response optimization",
        }
        result = self.toolkit.registry.invoke("calibration_write", arguments)
        preview = result["execution_preview"]
        self.assertEqual(preview["current"]["value"], 1.0)
        self.assertEqual(preview["target"]["value"], 2.5)
        self.assertEqual(preview["difference"]["delta"], 1.5)
        self.assertTrue(preview["validation"]["passed"])
        self.assertEqual(
            preview["recovery"]["snapshot"]["value"],
            1.0,
        )
        self.assertEqual(
            result["action_plan"]["precondition_digest"],
            preview["precondition_digest"],
        )

    def test_calibration_plan_rejects_out_of_range_target_before_approval(self):
        with self.assertRaisesRegex(ValueError, "大于上限"):
            self.toolkit.registry.invoke(
                "calibration_write",
                {
                    "device": "ECU",
                    "name": "Gain",
                    "value": 20.0,
                    "reason": "invalid target",
                },
            )
        self.assertFalse(self.approval_path.exists())

    def test_calibration_plan_rejects_stale_current_value(self):
        arguments = {
            "device": "ECU",
            "name": "Gain",
            "value": 2.0,
            "reason": "response optimization",
        }
        planned = self.toolkit.registry.invoke("calibration_write", arguments)
        plan_id = planned["action_plan"]["id"]
        self.store.approve(plan_id, "engineer")
        self.canape.values["Gain"] = 1.5
        with self.assertRaisesRegex(SafetyViolationError, "前置状态已改变"):
            self.toolkit.registry.invoke(
                "calibration_write",
                arguments,
                dry_run=False,
                action_plan_id=plan_id,
            )
        self.assertEqual(self.canape.values["Gain"], 1.5)
        self.assertEqual(self.store.get(plan_id).status, "stale")
        self.canape.values["Gain"] = 1.0
        with self.assertRaisesRegex(SafetyViolationError, "stale"):
            self.toolkit.registry.invoke(
                "calibration_write",
                arguments,
                dry_run=False,
                action_plan_id=plan_id,
            )

    def test_planner_resolves_alias_and_converts_units(self):
        result = self.toolkit.planner.plan(
            "把冷却液目标温度修改为 313.15 K",
            context={
                "default_device": "VCU",
                "reason": "warm-up calibration",
                "objects": [
                    {
                        "name": "CoolantTargetTemp",
                        "aliases": ["冷却液目标温度"],
                        "unit": "°C",
                        "minimum": 40,
                        "maximum": 120,
                    }
                ],
            },
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["tool"], "calibration_write")
        self.assertEqual(result["arguments"]["device"], "VCU")
        self.assertEqual(result["arguments"]["name"], "CoolantTargetTemp")
        self.assertAlmostEqual(result["arguments"]["value"], 40.0, places=6)
        self.assertTrue(result["resolution"]["unit_converted"])

    def test_planner_requires_disambiguation_for_shared_alias(self):
        result = self.toolkit.planner.plan(
            "读取标定 冷却液目标温度",
            context={
                "objects": [
                    {
                        "device": "VCU",
                        "name": "VcuCoolantTarget",
                        "aliases": ["冷却液目标温度"],
                        "unit": "°C",
                    },
                    {
                        "device": "TMM",
                        "name": "TmmCoolantTarget",
                        "aliases": ["冷却液目标温度"],
                        "unit": "°C",
                    },
                ]
            },
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["resolution"]["candidates"]), 2)

    def test_planner_resolves_a2l_enum_label_to_raw_value(self):
        result = self.toolkit.planner.plan(
            "把风扇模式设置为强制",
            context={
                "default_device": "HVAC",
                "reason": "thermal calibration",
                "objects": [
                    {
                        "name": "FanMode",
                        "aliases": ["风扇模式"],
                        "metadata": {
                            "enum_values": {"0": "关闭", "1": "自动", "2": "强制"}
                        },
                    }
                ],
            },
        )

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["arguments"]["name"], "FanMode")
        self.assertEqual(result["arguments"]["value"], 2)
        self.assertTrue(result["resolution"]["enum_resolved"])

        rejected = self.toolkit.planner.plan(
            "把风扇模式设置为 9",
            context={
                "default_device": "HVAC",
                "reason": "negative test",
                "objects": [
                    {
                        "name": "FanMode",
                        "aliases": ["风扇模式"],
                        "minimum": 0,
                        "maximum": 10,
                        "metadata": {"enum_values": {"0": "关闭", "1": "自动"}},
                    }
                ],
            },
        )
        self.assertEqual(rejected["status"], "enum_error")

    def test_natural_language_planner_never_executes(self):
        result = self.toolkit.planner.plan(
            "请读取标定量",
            context={"device": "ECU", "name": "Gain"},
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["tool"], "calibration_read")
        self.assertEqual(self.canape.values["Gain"], 1.0)
        missing = self.toolkit.planner.plan("启动测量")
        self.assertEqual(missing["status"], "ready")
        self.assertTrue(missing["approval_required"])
        flash = self.toolkit.planner.plan(
            "查询刷写状态",
            context={"device": "ECU"},
        )
        self.assertEqual(flash["tool"], "flash_state")

    def test_schema_rejects_missing_and_extra_arguments(self):
        with self.assertRaises(ValueError):
            self.toolkit.registry.invoke("calibration_read", {"device": "ECU"})
        with self.assertRaises(ValueError):
            self.toolkit.registry.invoke(
                "project_info", {"arbitrary_com_method": "Quit"}
            )
        with self.assertRaises(TypeError):
            self.toolkit.registry.invoke(
                "memory_read",
                {"device": "ECU", "address": True, "size": 1},
            )

    def test_measurement_state_serializes_integer_com_state(self):
        result = self.toolkit.registry.invoke("measurement_state")
        self.assertEqual(result["result"], {"state": 0, "running": False})

    def test_ai_tools_expose_calibration_operations_and_pareto(self):
        change_set = CalibrationChangeSet(
            "ai-review",
            "owner",
            [
                CalibrationChangeItem(
                    "Gain", 2.0, "AI review", "thermal", "warm-up", "owner"
                )
            ],
        )
        path = change_set.save(Path(self.temporary.name) / "change-set.json")
        review = self.toolkit.registry.invoke(
            "calibration_change_set_status", {"path": str(path)}
        )
        self.assertEqual(review["result"]["functions"], {"thermal": 1})

        pareto = self.toolkit.registry.invoke(
            "calibration_pareto_analyze",
            {
                "candidates": [
                    {
                        "identifier": "A",
                        "parameters": {"gain": 1.0},
                        "metrics": {"comfort": 1.0, "energy": 2.0},
                    },
                    {
                        "identifier": "B",
                        "parameters": {"gain": 2.0},
                        "metrics": {"comfort": 2.0, "energy": 1.0},
                    },
                ],
                "objectives": [
                    {"name": "comfort", "direction": "minimize"},
                    {"name": "energy", "direction": "minimize"},
                ],
            },
        )
        self.assertEqual(len(pareto["result"]["pareto_front"]), 2)
        planned = self.toolkit.planner.plan(
            "查看标定评审", context={"path": str(path)}
        )
        self.assertEqual(planned["tool"], "calibration_change_set_status")

        persistence_path = Path(self.temporary.name) / "persistence.json"
        CalibrationPersistenceJob(
            job_id="CAL-42",
            device="VCU",
            actor="calibrator",
            approved_by="reviewer",
            working_digest="abc",
            persist_rom=True,
            status=PersistenceStatus.COMPLETED,
        ).save(persistence_path)
        persistence = self.toolkit.registry.invoke(
            "calibration_persistence_status",
            {"path": str(persistence_path)},
        )
        self.assertEqual(persistence["result"]["status"], "completed")
        persistence_plan = self.toolkit.planner.plan(
            "查看持久化作业",
            context={"path": str(persistence_path)},
        )
        self.assertEqual(
            persistence_plan["tool"], "calibration_persistence_status"
        )

    def test_ai_tools_expose_experiment_report_and_safe_suggestion(self):
        store = CalibrationExperimentStore.create(
            Path(self.temporary.name) / "experiment.json",
            name="ai-experiment",
            device="VCU",
            cases=[{"Gain": 1.0}],
        )
        store.cases[0].status = ExperimentCaseStatus.PASSED
        store.cases[0].metrics = {"score": 1.0}
        store.save()
        report = self.toolkit.registry.invoke(
            "calibration_experiment_report", {"path": str(store.path)}
        )
        self.assertEqual(report["result"]["metrics"]["score"]["count"], 1)

        suggestion = self.toolkit.registry.invoke(
            "calibration_safe_suggest",
            {
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
            },
        )
        self.assertEqual(
            suggestion["result"]["suggested"]["identifier"], "C"
        )
        plan = self.toolkit.planner.plan(
            "推荐下一组标定",
            context={
                "observations": [],
                "candidates": [],
                "bounds": {},
                "objective": "error",
            },
        )
        self.assertEqual(plan["tool"], "calibration_safe_suggest")


if __name__ == "__main__":
    unittest.main()
