from __future__ import annotations

import tempfile
import time
import unittest
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from agent2canape import (
    AssetManager,
    AssetValidationError,
    AuditTrail,
    CapabilityRegistry,
    EngineeringPlatform,
    OfflineData,
    PermissionLevel,
    Reporter,
    SafetyPolicy,
    SafetyViolationError,
    SignalAnalyzer,
    SignalDefinition,
    SignalDictionary,
    ValueRule,
    WorkflowEngine,
    WorkflowError,
)
from agent2canape.capabilities import IMPLEMENTATIONS


class CapabilityTests(unittest.TestCase):
    def test_all_140_capabilities_have_callable_implementations(self):
        matrix = Path(__file__).resolve().parents[1] / "CAPABILITIES.md"
        registry = CapabilityRegistry.from_markdown(matrix)
        packaged = CapabilityRegistry.default()
        validation = registry.validate()
        self.assertTrue(validation["passed"], validation)
        self.assertEqual(len(registry.list()), 140)
        self.assertEqual(validation["unique_contracts"], 140)
        self.assertEqual(set(IMPLEMENTATIONS), set(range(1, 141)))
        self.assertEqual(
            [item.name for item in packaged.list()],
            [item.name for item in registry.list()],
        )
        for capability in registry.list():
            self.assertEqual(capability.contract_id, f"A2C-{capability.id:03d}")
            self.assertIn(capability.name, capability.acceptance)
            self.assertTrue(
                callable(CapabilityRegistry.resolve(capability.implementation)),
                capability,
            )


class AssetTests(unittest.TestCase):
    def test_inventory_manifest_preflight_and_compare(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a2l = root / "ecu.a2l"
            a2l.write_text("test", encoding="utf-8")
            manager = AssetManager()
            records = manager.inventory([root])
            self.assertEqual(records[0].kind, "a2l")
            result = manager.preflight(
                required_paths=[a2l],
                output_directory=root / "out",
                minimum_free_bytes=1,
                required_suffixes=[".a2l"],
            )
            self.assertTrue(result.passed, result.errors)
            manifest_path = root / "manifest.json"
            before = manager.create_manifest([a2l], manifest_path)
            a2l.write_text("changed", encoding="utf-8")
            after = manager.create_manifest([a2l], root / "after.json")
            self.assertEqual(
                manager.compare_manifests(before, after)["changed"], [str(a2l)]
            )

    def test_snapshot_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "config.cna").write_text("x", encoding="utf-8")
            manager = AssetManager()
            snapshot = manager.snapshot(source, root / "snapshot")
            restored = manager.restore(snapshot, root / "restored")
            self.assertTrue((restored / "config.cna").is_file())
            with self.assertRaises(AssetValidationError):
                manager.snapshot(source, source / "nested-snapshot")
            with self.assertRaises(AssetValidationError):
                manager.restore(snapshot, snapshot / "nested-restore")

    def test_topology(self):
        result = AssetManager.validate_topology(
            {"ECU": {"channel": 1, "driver": "XCP"}},
            {"ECU": {"channel": 1, "driver": "XCP"}},
        )
        self.assertTrue(result["passed"])

    def test_dependency_gate_and_environment_providers(self):
        manager = AssetManager()
        inventory = manager.environment_inventory(
            {
                "canape": {"version": "17.0"},
                "license": lambda: {"available": True},
            }
        )
        self.assertEqual(inventory["canape"]["version"], "17.0")
        self.assertTrue(inventory["license"]["available"])
        result = manager.preflight(required_commands=["python"])
        self.assertTrue(result.passed, result.errors)


class OfflineTests(unittest.TestCase):
    def test_signal_dictionary_and_parsers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dbc = root / "network.dbc"
            dbc.write_text(
                'BO_ 100 MSG: 8 ECU\n SG_ Speed : 0|16@1+ (1,0) [0|250] "km/h" ECU\n',
                encoding="latin-1",
            )
            definitions = OfflineData.parse_dbc(dbc)
            self.assertEqual(definitions[0].name, "Speed")
            dictionary = SignalDictionary()
            dictionary.add(definitions[0])
            dictionary.add(
                SignalDefinition(
                    "CabinTemp", "ecu.a2l", unit="degC", aliases=("InsdT",)
                )
            )
            self.assertEqual(dictionary.get("InsdT").name, "CabinTemp")
            self.assertTrue(dictionary.validate()["passed"])

    def test_table_resample_align_and_mapping(self):
        offline = OfflineData()
        frame = pd.DataFrame({"time": [0.0, 0.1, 0.2], "value": [0.0, 1.0, 2.0]})
        result = offline.resample(frame, 0.1)
        self.assertEqual(len(result), 3)
        aligned = offline.align(frame, frame.rename(columns={"value": "other"}))
        self.assertEqual(aligned["other"].tolist(), [0.0, 1.0, 2.0])
        messages = pd.DataFrame(
            {"channel": [1, 1], "arbitration_id": [100, 101]}
        )
        self.assertTrue(
            offline.check_channel_mapping(messages, {1: [100, 101]})["passed"]
        )

    def test_table_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.csv"
            frame = pd.DataFrame({"time": [0, 1], "value": [2, 3]})
            offline = OfflineData()
            offline.write_table(frame, path)
            self.assertEqual(offline.read_table(path)["value"].tolist(), [2, 3])

    def test_odx_and_time_source_report(self):
        with tempfile.TemporaryDirectory() as directory:
            odx = Path(directory) / "diagnostics.odx"
            odx.write_text(
                "<ODX><DIAG-SERVICE ID='S1'><SHORT-NAME>ReadVIN</SHORT-NAME>"
                "</DIAG-SERVICE></ODX>",
                encoding="utf-8",
            )
            definitions = OfflineData.parse_odx(odx)
            self.assertEqual(definitions[0].name, "ReadVIN")
        report = OfflineData.time_source_report(
            {
                "canape": {
                    "clock_domain": "ptp",
                    "sync_method": "ptp",
                    "offset_seconds": 0.0,
                    "drift_ppm": 1.0,
                },
                "canoe": {
                    "clock_domain": "ptp",
                    "sync_method": "ptp",
                    "offset_seconds": 0.01,
                    "drift_ppm": 2.0,
                },
            }
        )
        self.assertTrue(report["passed"], report)

    def test_real_mdf_adapter(self):
        from asammdf import MDF, Signal

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "measurement.mf4"
            signal = Signal(
                samples=np.array([20.0, 21.0, 22.0]),
                timestamps=np.array([0.0, 0.1, 0.2]),
                name="CabinTemp",
                unit="degC",
            )
            with MDF(version="4.10") as mdf:
                mdf.append([signal])
                mdf.save(path, overwrite=True)
            offline = OfflineData()
            metadata = offline.mdf_metadata(path)
            self.assertIn("CabinTemp", metadata["channels"])
            frame = offline.read_mdf(path, channels=["CabinTemp"])
            self.assertEqual(frame["CabinTemp"].tolist(), [20.0, 21.0, 22.0])
            output = offline.extract_mdf(path, Path(directory) / "extract.csv")
            self.assertTrue(output.is_file())

    def test_real_blf_and_dbc_adapters(self):
        import can

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dbc = root / "network.dbc"
            dbc.write_text(
                "VERSION \"\"\n"
                "NS_ :\nBS_:\nBU_: ECU\n"
                "BO_ 100 Vehicle: 8 ECU\n"
                ' SG_ Speed : 0|16@1+ (0.1,0) [0|250] "km/h" ECU\n',
                encoding="latin-1",
            )
            blf = root / "trace.blf"
            writer = can.BLFWriter(blf)
            writer.on_message_received(
                can.Message(
                    timestamp=1.0,
                    arbitration_id=100,
                    is_extended_id=False,
                    channel=0,
                    data=bytes([0xE8, 0x03, 0, 0, 0, 0, 0, 0]),
                )
            )
            writer.stop()
            offline = OfflineData()
            frames = offline.read_blf(blf)
            self.assertEqual(int(frames.iloc[0]["arbitration_id"]), 100)
            decoded = offline.decode_blf(frames, dbc)
            self.assertEqual(float(decoded.iloc[0]["Speed"]), 100.0)
            self.assertEqual(offline.blf_metadata(blf)["frame_count"], 1)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame(
            {
                "time": [0.0, 1.0, 2.0, 3.0],
                "request": [0, 1, 1, 1],
                "output": [0, 0, 1, 1],
                "state": [0, 1, 2, 1],
                "target": [0.0, 10.0, 10.0, 10.0],
                "actual": [0.0, 8.0, 11.0, 10.0],
                "power_in": [1.0, 1.0, 1.0, 1.0],
                "power_out": [0.5, 0.5, 0.5, 0.5],
            }
        )
        self.analyzer = SignalAnalyzer()

    def test_quality_state_timing_and_causality(self):
        findings = self.analyzer.quality(
            self.frame,
            {"actual": {"minimum": 0, "maximum": 10, "max_rate": 20}},
        )
        self.assertEqual(findings[0].rule, "above_maximum")
        states = self.analyzer.state_transitions(
            self.frame, "state", allowed={(0, 1), (1, 2)}
        )
        self.assertEqual(len(states["illegal"]), 1)
        timing = self.analyzer.timing(self.frame, "request", "output")
        self.assertEqual(timing[0]["delay"], 1.0)
        self.assertTrue(
            self.analyzer.causality(self.frame, ["request", "output"])["passed"]
        )
        oscillation = self.analyzer.state_transitions(
            pd.DataFrame({"time": [0.0, 1.0, 2.0], "state": [0, 1, 0]}),
            "state",
        )
        self.assertEqual(len(oscillation["oscillations"]), 1)
        stale_response = pd.DataFrame(
            {"time": [0.0, 1.0, 2.0], "request": [0, 1, 1], "output": [1, 1, 1]}
        )
        self.assertEqual(
            self.analyzer.timing(stale_response, "request", "output"),
            [],
        )

    def test_conversion_control_energy_compare(self):
        chain = self.analyzer.conversion_chain(
            [0, 255], [-40.0, 87.5], [-40.0, 87.5], factor=0.5, offset=-40
        )
        self.assertTrue(chain["passed"])
        metrics = self.analyzer.control_metrics(self.frame, "target", "actual")
        self.assertIn("rmse", metrics)
        energy = self.analyzer.energy_balance(
            self.frame, ["power_in"], ["power_out"]
        )
        self.assertEqual(energy["efficiency"], 0.5)
        comparison = self.analyzer.compare(
            self.frame, self.frame.assign(actual=self.frame["actual"] + 1), ["actual"]
        )
        self.assertEqual(comparison["actual"]["mean_delta"], 1.0)

    def test_strategy_and_explainable_anomaly_clusters(self):
        result = self.analyzer.strategy_validation(
            self.frame,
            "request",
            "output",
            activation_timeout=0.5,
        )
        self.assertFalse(result["passed"])
        findings = self.analyzer.quality(
            self.frame,
            {
                "actual": {"maximum": 10},
                "power_in": {"maximum": 0.5},
            },
        )
        clusters = self.analyzer.cluster_anomalies(findings)
        self.assertGreaterEqual(clusters[0]["score"], 2)
        self.assertIn("explanation", clusters[0])


class SafetyWorkflowReportingTests(unittest.TestCase):
    def test_safety_policy(self):
        policy = SafetyPolicy(
            maximum_permission=PermissionLevel.CALIBRATION_WRITE,
            object_rules={"Gain": ValueRule(0.0, 5.0)},
            preconditions={"vehicle_speed": ValueRule(allowed=frozenset({0}))},
            allowed_devices={"ECU"},
        )
        authorized = policy.authorize(
            PermissionLevel.CALIBRATION_WRITE,
            device="ECU",
            target="Gain",
            value=2.0,
            vehicle_state={"vehicle_speed": 0},
            confirmed=True,
        )
        self.assertTrue(authorized["authorized"])
        with self.assertRaises(SafetyViolationError):
            policy.authorize(
                PermissionLevel.CALIBRATION_WRITE,
                device="ECU",
                target="Gain",
                value=8.0,
                vehicle_state={"vehicle_speed": 0},
                confirmed=True,
            )

    def test_workflow_retry_dry_run_and_checkpoint(self):
        engine = WorkflowEngine()
        attempts = {"count": 0}

        def flaky(value):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("retry")
            return value * 2

        engine.register("flaky", flaky)
        definition = {
            "variables": {"value": 3},
            "steps": [
                {
                    "id": "calculate",
                    "action": "flaky",
                    "with": {"value": "${variables.value}"},
                    "retries": 1,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.json"
            result = engine.execute(definition, checkpoint=checkpoint)
            self.assertEqual(result.status, "passed")
            self.assertEqual(result.steps[0].output, 6)
            self.assertTrue(checkpoint.is_file())
        dry = engine.execute(definition, dry_run=True)
        self.assertEqual(dry.steps[0].status, "dry-run")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.machine_summary()["passed"], 1)

    def test_workflow_definition_rejects_silent_argument_typo(self):
        with self.assertRaises(WorkflowError):
            WorkflowEngine().execute(
                {
                    "steps": [
                        {
                            "id": "bad",
                            "action": "missing",
                            "arguments": {"value": 1},
                        }
                    ]
                }
            )

    def test_workflow_timeout_returns_without_waiting_for_worker(self):
        engine = WorkflowEngine()
        engine.register("slow", lambda: time.sleep(0.5))
        started = time.monotonic()
        result = engine.execute(
            {"steps": [{"id": "slow", "action": "slow", "timeout": 0.02}]}
        )
        self.assertEqual(result.steps[0].error_category, "timeout")
        self.assertLess(time.monotonic() - started, 0.3)

    def test_workflow_compensation_and_multi_ecu(self):
        engine = WorkflowEngine()
        actions = []
        engine.register("write", lambda value: actions.append(("write", value)), write=True)
        engine.register("restore", lambda value: actions.append(("restore", value)), write=True)

        def fail():
            raise RuntimeError("boom")

        engine.register("fail", fail)
        result = engine.execute(
            {
                "steps": [
                    {
                        "id": "change",
                        "action": "write",
                        "with": {"value": 2},
                        "compensate": {"action": "restore", "with": {"value": 1}},
                    },
                    {"id": "fail", "action": "fail"},
                ]
            },
            operator="tester",
            allow_writes=True,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(actions, [("write", 2), ("restore", 1)])
        self.assertEqual(result.compensation_steps[0].status, "passed")
        definition = engine.multi_ecu_definition(["VCU", "BMS"])
        self.assertEqual(len(definition["steps"]), 4)

    def test_workflow_postcondition_failure_compensates_and_ci_fails(self):
        engine = WorkflowEngine()
        actions = []
        engine.register("change", lambda: {"ok": False}, write=True)
        engine.register("restore", lambda: actions.append("restored"), write=True)
        result = engine.execute(
            {
                "steps": [
                    {
                        "id": "change",
                        "action": "change",
                        "compensate": {"action": "restore"},
                        "postconditions": [
                            {
                                "path": "steps.change.output.ok",
                                "operator": "eq",
                                "value": True,
                            }
                        ],
                    }
                ]
            },
            allow_writes=True,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.steps[0].error_category, "validation")
        self.assertEqual(actions, ["restored"])
        self.assertEqual(engine.ci_summary(result)["exit_code"], 1)

    def test_workflow_write_requires_explicit_authorization(self):
        engine = WorkflowEngine()
        called = []
        engine.register("write", lambda: called.append(True), write=True)
        with self.assertRaises(WorkflowError):
            engine.execute({"steps": [{"id": "write", "action": "write"}]})
        self.assertEqual(called, [])

    def test_audit_evidence_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = AuditTrail(root / "audit.jsonl")
            audit.append("measure", actor="tester", target="ECU")
            audit.append("analyze", actor="tester", target="log.mf4")
            self.assertTrue(audit.verify()["passed"])
            evidence = root / "evidence.txt"
            evidence.write_text("proof", encoding="utf-8")
            bundle = Reporter.create_evidence_bundle(
                root / "bundle.zip", files=[evidence], metadata={"issue": "1"}
            )
            with zipfile.ZipFile(bundle) as archive:
                self.assertIn("manifest.json", archive.namelist())
            other = root / "other"
            other.mkdir()
            duplicate = other / evidence.name
            duplicate.write_text("different", encoding="utf-8")
            with self.assertRaises(ValueError):
                Reporter.create_evidence_bundle(
                    root / "duplicate.zip", files=[evidence, duplicate]
                )
            html = Reporter.html(
                root / "report.html", title="Report", sections=[("Result", {"ok": True})]
            )
            self.assertTrue(html.is_file())
            workbook = Reporter.excel(
                root / "report.xlsx", {"Findings": [{"status": "passed"}]}
            )
            self.assertTrue(workbook.is_file())
            pdf = Reporter.generate(
                root / "report.pdf",
                title="Report",
                sections=[("Result", {"ok": True})],
            )
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))

    def test_platform_helpers_and_domains(self):
        platform = EngineeringPlatform()
        self.assertEqual(len(platform.plugins.adapters), 7)
        for adapter in platform.plugins.adapters.values():
            self.assertTrue(adapter.validate()["passed"])
            self.assertTrue(adapter.metrics())
        identity = platform.bind_identity(
            vehicle="V1", ecu="ECU", software="S1", calibration="C1", task="T1"
        )
        self.assertEqual(identity["ecu"], "ECU")
        frame = pd.DataFrame({"time": [0, 1], "a": [1, 2]})
        derived = platform.derive_signal(frame, "b", "a * 2")
        self.assertEqual(derived["b"].tolist(), [2, 4])


if __name__ == "__main__":
    unittest.main()
