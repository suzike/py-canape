"""空调舒适性问题的 Agent2Canape 全链路验收案例。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if SOURCE_ROOT.is_dir() and str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent2canape import (  # noqa: E402
    ApprovalStore,
    AssetManager,
    AuditTrail,
    CalibrationChange,
    CalibrationDataset,
    CalibrationKind,
    CalibrationParameter,
    CalibrationPlan,
    CANape,
    CANapeAIToolkit,
    CANapeTopologyAuditor,
    CapabilityRegistry,
    DiagnosticManifest,
    DiagnosticSequenceRunner,
    DTCSnapshot,
    MeasurementChannelSpec,
    MeasurementManifest,
    MeasurementRecorderSpec,
    MeasurementSessionManager,
    MeasurementTaskLimit,
    MeasurementTriggerSpec,
    NetworkTopologyManifest,
    Reporter,
    SignalAnalyzer,
    WorkflowEngine,
    WorkflowError,
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SimulatedHVACCANape:
    """危险动作专用模拟适配器；不会访问真实 ECU。"""

    def __init__(self) -> None:
        self.connected = True
        self.values = {"EvapTempTarget": 7.0, "BlowerGain": 1.0}
        self.parameters = {
            "EvapTempTarget": CalibrationParameter(
                "EvapTempTarget",
                7.0,
                CalibrationKind.SCALAR,
                unit="degC",
                minimum=2.0,
                maximum=12.0,
                comment="蒸发器目标温度",
            ),
            "BlowerGain": CalibrationParameter(
                "BlowerGain",
                1.0,
                CalibrationKind.SCALAR,
                minimum=0.5,
                maximum=1.5,
                comment="鼓风机响应增益",
            ),
        }
        self.measurement_running = False
        self.measurement_configuration = {
            "sample_size": 4,
            "fifo_size": 64,
            "sync_mode": False,
            "resume_mode": False,
            "use_nan": False,
        }
        self.measurement_output_file = "baseline.mf4"
        self.measurement_channels = {"HVAC::10ms": ["CabinTemp"]}
        self.recorders = {
            "HVACRecorder": {
                "name": "HVACRecorder",
                "state": 0,
                "recorder_type": 1,
                "mdf_filename": "baseline.mf4",
                "data_reduction": 2,
            }
        }
        self.fail_channel = ""
        self.tester_present = False
        self.calls: list[dict[str, Any]] = []

    def connect(self) -> None:
        self.connected = True

    def get_project_info(self) -> dict[str, Any]:
        return {
            "working_directory": "SIMULATED/HVAC",
            "cna_filename": "HVAC_Comfort_Acceptance.cna",
            "application_name": "Agent2Canape simulated CANape",
        }

    def get_network_topology(self) -> dict[str, Any]:
        return {
            "networks": [{"name": "CAN1", "active": True}],
            "devices": [
                {
                    "name": "HVAC",
                    "driver_type": "XCP",
                    "channel": 1,
                    "online": True,
                    "network": "CAN1",
                    "database_filename": "hvac.a2l",
                    "databases": ["hvac.a2l"],
                }
            ],
            "com_visible_fields": {
                "network": ["name", "active"],
                "device": ["name", "driver_type", "channel", "online", "network"],
            },
        }

    def read_calibration_parameter(
        self, device: str, name: str
    ) -> CalibrationParameter:
        assert device == "HVAC"
        return replace(self.parameters[name], value=self.values[name])

    def read_calibration_value(
        self, device: str, name: str, *, physical: bool = True
    ) -> Any:
        assert device == "HVAC"
        return self.values[name]

    def write_calibration_value(
        self,
        device: str,
        name: str,
        value: Any,
        *,
        physical: bool = True,
        verify: bool = True,
    ) -> Any:
        assert device == "HVAC"
        candidate = replace(self.parameters[name], value=value)
        errors = candidate.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self.values[name] = value
        self.calls.append({"action": "calibration_write", "name": name, "value": value})
        return self.values[name]

    def write_calibration_parameter(
        self,
        device: str,
        parameter: CalibrationParameter,
        *,
        verify: bool = True,
    ) -> CalibrationParameter:
        self.write_calibration_value(device, parameter.name, parameter.value, verify=verify)
        return self.read_calibration_parameter(device, parameter.name)

    def is_measurement_running(self) -> bool:
        return self.measurement_running

    def start_measurement(self) -> bool:
        self.measurement_running = True
        return True

    def stop_measurement(self) -> bool:
        self.measurement_running = False
        return True

    def get_measurement_configuration(self) -> dict[str, Any]:
        return dict(self.measurement_configuration)

    def configure_measurement(self, **values: Any) -> None:
        self.measurement_configuration.update(
            {name: value for name, value in values.items() if value is not None}
        )

    def get_measurement_output_file(self) -> str:
        return self.measurement_output_file

    def set_measurement_output_file(self, path: str) -> None:
        self.measurement_output_file = str(path)

    def list_measurement_channels(self, device: str, task: str) -> list[str]:
        return list(self.measurement_channels.get(f"{device}::{task}", ()))

    def configure_measurement_channels(
        self,
        device: str,
        task: str,
        channels: list[str] | tuple[str, ...],
        *,
        clear: bool = True,
    ) -> list[str]:
        key = f"{device}::{task}"
        if clear:
            self.measurement_channels[key] = []
        for channel in channels:
            self.measurement_channels[key].append(channel)
            if channel == self.fail_channel:
                raise RuntimeError("simulated DAQ allocation failure")
        return list(self.measurement_channels[key])

    def get_recorder_configuration(self, name: str) -> dict[str, Any]:
        return dict(self.recorders[name])

    def set_recorder_output_file(self, name: str, path: str) -> None:
        self.recorders[name]["mdf_filename"] = str(path)

    def set_recorder_data_reduction(self, name: str, value: int) -> None:
        self.recorders[name]["data_reduction"] = int(value)

    def set_tester_present(self, device: str, *, enabled: bool) -> int:
        assert device == "HVAC"
        self.tester_present = enabled
        self.calls.append({"action": "tester_present", "enabled": enabled})
        return int(enabled)

    def send_raw_diagnostic_request(
        self, device: str, payload: Any, *, timeout: float = 5.0
    ) -> list[Any]:
        assert device == "HVAC"
        request = tuple(int(item) for item in payload)
        responses = {
            (0x10, 0x03): (0x50, 0x03),
            (0x22, 0xF1, 0x90): (0x62, 0xF1, 0x90, *b"LHVAC00000000001"),
            (0x22, 0xF1, 0x88): (0x62, 0xF1, 0x88, *b"HVAC_SW_2026.08"),
            (0x19, 0x02, 0xFF): (0x59, 0x02, 0xFF, 0x12, 0x34, 0x56, 0x09),
        }
        stream = responses.get(request, (0x7F, request[0], 0x31))
        self.calls.append(
            {"action": "diagnostic", "payload": list(request), "timeout": timeout}
        )
        return [
            SimpleNamespace(
                positive=stream[0] != 0x7F,
                response_code=0,
                sender="HVAC",
                stream=stream,
            )
        ]


def _measurement_manifest(output: Path) -> MeasurementManifest:
    return MeasurementManifest(
        name="hvac-comfort-investigation",
        channels=(
            MeasurementChannelSpec("HVAC", "10ms", "CabinTemp"),
            MeasurementChannelSpec("HVAC", "10ms", "CabinTempTarget"),
            MeasurementChannelSpec("HVAC", "10ms", "CompressorRequest"),
            MeasurementChannelSpec("HVAC", "10ms", "CompressorSpeed"),
            MeasurementChannelSpec("HVAC", "10ms", "EvapTemp"),
        ),
        task_limits=(
            MeasurementTaskLimit(
                "HVAC",
                "10ms",
                sampling_time_seconds=0.01,
                max_channels=16,
                max_bytes_per_second=10000,
                minimum_fifo_samples=128,
                event=1,
            ),
        ),
        recorders=(
            MeasurementRecorderSpec(
                "HVACRecorder", str(output / "hvac-comfort.mf4"), 1
            ),
        ),
        triggers=(
            MeasurementTriggerSpec(
                "comfort-error",
                "HVACRecorder",
                "CabinTemp - CabinTempTarget > 4",
                pre_trigger_seconds=5.0,
                post_trigger_seconds=20.0,
            ),
        ),
        sample_size=8,
        fifo_size=4096,
        sync_mode=True,
        use_nan=True,
        measurement_output_file=str(output / "hvac-comfort.mf4"),
        start_after_apply=True,
    )


def _topology_manifest() -> NetworkTopologyManifest:
    return NetworkTopologyManifest.from_mapping(
        {
            "name": "hvac-vehicle-topology",
            "allow_unexpected_networks": False,
            "allow_unexpected_devices": False,
            "networks": [
                {
                    "name": "CAN1",
                    "bus_type": "can",
                    "bitrate": 500000,
                    "expected_active": True,
                }
            ],
            "devices": [
                {
                    "name": "HVAC",
                    "network": "CAN1",
                    "channel": 1,
                    "driver_type": "XCP",
                    "expected_online": True,
                }
            ],
        }
    )


def _diagnostic_manifest() -> DiagnosticManifest:
    return DiagnosticManifest.from_mapping(
        {
            "name": "hvac-diagnostic-health",
            "default_device": "HVAC",
            "p2_timeout_seconds": 0.05,
            "p2_star_timeout_seconds": 5.0,
            "tester_present": True,
            "steps": [
                {
                    "id": "extended-session",
                    "payload": [0x10, 0x03],
                    "transition_session": "extended",
                },
                {
                    "id": "read-vin",
                    "payload": [0x22, 0xF1, 0x90],
                    "required_session": "extended",
                },
                {
                    "id": "read-software",
                    "payload": [0x22, 0xF1, 0x88],
                    "required_session": "extended",
                },
                {
                    "id": "read-dtc",
                    "payload": [0x19, 0x02, 0xFF],
                    "required_session": "extended",
                },
            ],
        }
    )


def _signal_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    time_axis = np.arange(0.0, 121.0, 1.0)
    target = np.full_like(time_axis, 22.0)
    baseline_temp = 22.0 + 13.0 * np.exp(-time_axis / 65.0)
    candidate_temp = 22.0 + 13.0 * np.exp(-time_axis / 30.0)
    request = (time_axis >= 2.0).astype(int)
    compressor = (time_axis >= 4.0).astype(int)
    baseline = pd.DataFrame(
        {
            "time": time_axis,
            "CabinTempTarget": target,
            "CabinTemp": baseline_temp,
            "CompressorRequest": request,
            "CompressorRunning": compressor,
        }
    )
    candidate = baseline.copy()
    candidate["CabinTemp"] = candidate_temp
    return baseline, candidate


def run_case(
    output_directory: str | Path,
    *,
    real_canape_project: str | Path | None = None,
) -> dict[str, Any]:
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    canape = SimulatedHVACCANape()
    stages: dict[str, dict[str, Any]] = {}
    audit = AuditTrail(output / "audit.jsonl")

    identity = {
        "vehicle": "HVAC-DEMO-001",
        "ecu": "HVAC",
        "software": "HVAC_SW_2026.08",
        "calibration": "CAL_BASE_017",
        "ticket": "COMFORT-248",
    }
    stages["identity"] = {"passed": all(identity.values()), "result": identity}

    assets = AssetManager()
    repo = Path(__file__).resolve().parents[1]
    preflight = assets.preflight(
        required_paths=[repo / "examples" / "vehicle_network.dbc"],
        output_directory=output,
        minimum_free_bytes=1,
    )
    stages["asset_preflight"] = {
        "passed": preflight.passed,
        "result": asdict(preflight),
    }

    if real_canape_project is not None:
        project = Path(real_canape_project).expanduser().resolve()
        live_canape = CANape()
        connection_mode: str | None = None
        try:
            connection_mode = "opened"
            try:
                live_canape.open(project)
            except Exception as open_error:
                if "Another client is already connected" not in str(open_error):
                    raise
                live_canape.attach_to_active_application()
                connection_mode = "attached_existing"
            project_info = live_canape.get_project_info()
            actual_directory = Path(project_info["working_directory"]).resolve()
            if actual_directory != project:
                raise RuntimeError(
                    "活动 CANape 工程与案例要求不一致："
                    f"{actual_directory} != {project}"
                )
            live_result = {
                "project": project_info,
                "devices": [asdict(item) for item in live_canape.list_devices()],
                "connection_mode": connection_mode,
                "operations": ["open_project", "read_project_info", "list_devices"],
                "write_operations": 0,
            }
            stages["real_canape_read_only"] = {
                "passed": True,
                "result": live_result,
            }
        except Exception as exc:
            stages["real_canape_read_only"] = {
                "passed": False,
                "result": {"error": f"{type(exc).__name__}: {exc}"},
            }
        finally:
            if live_canape.connected:
                if connection_mode == "opened":
                    live_canape.quit(non_modal=True)
                else:
                    live_canape.disconnect()

    topology = CANapeTopologyAuditor(canape).audit(_topology_manifest())
    stages["topology"] = {"passed": topology["passed"], "result": topology}

    measurement = _measurement_manifest(output)
    measurement_plan = measurement.plan()
    manager = MeasurementSessionManager(canape)
    baseline_snapshot = manager.capture(measurement)
    canape.fail_channel = "CompressorSpeed"
    rollback_observed = False
    try:
        manager.apply(measurement)
    except WorkflowError:
        rollback_observed = (
            manager.capture(measurement).digest() == baseline_snapshot.digest()
        )
    canape.fail_channel = ""
    measurement_apply = manager.apply(measurement)
    stages["measurement"] = {
        "passed": measurement_plan["passed"]
        and rollback_observed
        and measurement_apply["status"] == "applied"
        and canape.measurement_running,
        "result": {
            "plan": measurement_plan,
            "failure_injection_rollback": rollback_observed,
            "apply": measurement_apply,
        },
    }

    baseline_dataset = CalibrationDataset(
        parameters={
            name: canape.read_calibration_parameter("HVAC", name)
            for name in ("EvapTempTarget", "BlowerGain")
        },
        identity=identity,
        source="simulated-canape",
    )
    calibration_plan = CalibrationPlan(
        name="comfort-response-calibration",
        author="calibration-engineer",
        ticket=identity["ticket"],
        changes=[
            CalibrationChange(
                "EvapTempTarget",
                5.0,
                "降低蒸发器目标以缩短降温时间",
                expected_before=7.0,
                enforce_expected=True,
            ),
            CalibrationChange(
                "BlowerGain",
                1.15,
                "提高初始风量响应",
                expected_before=1.0,
                enforce_expected=True,
            ),
        ],
    )
    preview = calibration_plan.preview(baseline_dataset)
    calibration_plan.approve("lead-calibrator")
    calibration_result = calibration_plan.apply(canape, "HVAC")
    stages["calibration"] = {
        "passed": not preview["errors"]
        and calibration_result["after"] == {
            "EvapTempTarget": 5.0,
            "BlowerGain": 1.15,
        },
        "result": {"preview": preview, "commit": calibration_result},
    }

    approvals = ApprovalStore(output / "approvals.json")
    toolkit = CANapeAIToolkit(canape, approvals=approvals)
    ai_arguments = {
        "device": "HVAC",
        "name": "BlowerGain",
        "value": 1.2,
        "reason": "舒适性候选微调",
    }
    ai_plan = toolkit.registry.invoke("calibration_write", ai_arguments, dry_run=True)
    action_id = ai_plan["action_plan"]["id"]
    approvals.approve(action_id, "independent-reviewer")
    ai_result = toolkit.registry.invoke(
        "calibration_write",
        ai_arguments,
        dry_run=False,
        action_plan_id=action_id,
    )
    ai_reuse_rejected = False
    try:
        toolkit.registry.invoke(
            "calibration_write",
            ai_arguments,
            dry_run=False,
            action_plan_id=action_id,
        )
    except Exception:
        ai_reuse_rejected = True
    stages["ai_action_plan"] = {
        "passed": ai_result["executed"]
        and ai_reuse_rejected
        and canape.values["BlowerGain"] == 1.2,
        "result": {
            "risk": ai_plan["risk"],
            "approved_by": "independent-reviewer",
            "single_use_rejected": ai_reuse_rejected,
            "engineering_tool_count": len(toolkit.registry.manifest()),
        },
    }

    diagnostic = DiagnosticSequenceRunner(canape).execute(_diagnostic_manifest())
    stages["diagnostic"] = {
        "passed": diagnostic["passed"] and not canape.tester_present,
        "result": diagnostic,
    }
    dtc_before = DTCSnapshot.parse_uds(
        [0x59, 0x02, 0xFF, 0x12, 0x34, 0x56, 0x09], source="before-calibration"
    )
    dtc_after = DTCSnapshot.parse_uds(
        [0x59, 0x02, 0xFF], source="after-calibration"
    )
    dtc_diff = dtc_before.diff(dtc_after)
    stages["dtc_evidence"] = {
        "passed": [item["code"] for item in dtc_diff["removed"]] == ["123456"],
        "result": {
            "before": dtc_before.public(),
            "after": dtc_after.public(),
            "diff": dtc_diff,
        },
    }

    baseline_frame, candidate_frame = _signal_data()
    analyzer = SignalAnalyzer()
    quality = analyzer.quality(
        candidate_frame,
        {
            "CabinTemp": {
                "minimum": 15.0,
                "maximum": 45.0,
                "max_rate": 2.0,
                "expected_period": 1.0,
            }
        },
    )
    baseline_metrics = analyzer.control_metrics(
        baseline_frame, "CabinTempTarget", "CabinTemp", tolerance=0.05
    )
    candidate_metrics = analyzer.control_metrics(
        candidate_frame, "CabinTempTarget", "CabinTemp", tolerance=0.05
    )
    causality = analyzer.causality(
        candidate_frame,
        ["CompressorRequest", "CompressorRunning"],
        maximum_delay=3.0,
    )
    comparison = analyzer.compare(
        baseline_frame, candidate_frame, ["CabinTemp"]
    )
    stages["signal_analysis"] = {
        "passed": not quality
        and candidate_metrics["rmse"] < baseline_metrics["rmse"]
        and causality["passed"],
        "result": {
            "quality_findings": [asdict(item) for item in quality],
            "baseline_control": baseline_metrics,
            "candidate_control": candidate_metrics,
            "causality": causality,
            "comparison": comparison,
        },
    }

    workflow = WorkflowEngine()
    workflow.register("case.identity", lambda: identity)
    workflow.register("case.result", lambda passed: {"passed": bool(passed)})
    workflow.register("case.write", lambda: {"unexpected": True}, write=True)
    write_guard = False
    try:
        workflow.execute({"steps": [{"id": "blocked", "action": "case.write"}]})
    except WorkflowError:
        write_guard = True
    workflow_result = workflow.execute(
        {
            "steps": [
                {"id": "identity", "action": "case.identity"},
                {
                    "id": "acceptance",
                    "action": "case.result",
                    "with": {"passed": True},
                },
            ]
        },
        checkpoint=output / "workflow-checkpoint.json",
        operator="acceptance-runner",
    )
    stages["workflow"] = {
        "passed": write_guard and workflow_result.status == "passed",
        "result": {
            "write_guard": write_guard,
            "summary": workflow_result.machine_summary(),
        },
    }

    capabilities = CapabilityRegistry.default().validate()
    stages["capabilities"] = {
        "passed": capabilities["passed"] and capabilities["count"] == 140,
        "result": capabilities,
    }

    for name, stage in stages.items():
        audit.append(
            f"case.{name}",
            actor="acceptance-runner",
            target=identity["ticket"],
            status="passed" if stage["passed"] else "failed",
            details={"passed": stage["passed"]},
        )
    audit_result = audit.verify()
    stages["audit_chain"] = {"passed": audit_result["passed"], "result": audit_result}

    result: dict[str, Any] = {
        "case": "HVAC cabin pull-down response and historical DTC",
        "case_id": identity["ticket"],
        "execution_mode": {
            "canape_ecu_actions": "simulated",
            "real_canape_validation": (
                "included read-only gate"
                if real_canape_project is not None
                else "not requested"
            ),
        },
        "created_utc": datetime.now(UTC).isoformat(),
        "passed": all(stage["passed"] for stage in stages.values()),
        "stage_count": len(stages),
        "passed_stage_count": sum(stage["passed"] for stage in stages.values()),
        "stages": stages,
    }
    result_file = output / "full-case-result.json"
    report_file = output / "full-case-report.html"
    manifest_file = output / "asset-manifest.json"
    bundle = output / "hvac-comfort-evidence.zip"
    result["artifacts"] = {
        "result": str(result_file),
        "html_report": str(report_file),
        "audit": str(audit.path),
        "asset_manifest": str(manifest_file),
        "evidence_bundle": str(bundle),
    }
    result_file.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    report_file = Reporter.html(
        report_file,
        title="Agent2Canape 空调舒适性全链路验收",
        sections=[
            ("案例身份与安全边界", {**identity, **result["execution_mode"]}),
            ("阶段结论", {name: stage["passed"] for name, stage in stages.items()}),
            ("标定结果", stages["calibration"]["result"]),
            ("诊断与 DTC", {"diagnostic": diagnostic, "dtc": dtc_diff}),
            ("舒适性分析", stages["signal_analysis"]["result"]),
        ],
    )
    asset_manifest = assets.create_manifest(
        [
            repo / "examples" / "vehicle_network.dbc",
            repo / "examples" / "diagnostic_sequence.yaml",
            result_file,
            report_file,
        ],
        manifest_file,
    )
    known_paths = {item["path"] for item in asset_manifest["assets"]}
    for generated in (result_file, report_file):
        resolved = str(generated.resolve())
        if resolved not in known_paths:
            asset_manifest["assets"].append(
                {
                    "path": resolved,
                    "kind": "evidence",
                    "size": generated.stat().st_size,
                    "modified_utc": datetime.fromtimestamp(
                        generated.stat().st_mtime, UTC
                    ).isoformat(),
                    "sha256": _file_sha256(generated),
                }
            )
    manifest_file.write_text(
        json.dumps(asset_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    bundle = Reporter.create_evidence_bundle(
        bundle,
        files=[result_file, report_file, audit.path, manifest_file],
        metadata={
            "case_id": identity["ticket"],
            "passed": result["passed"],
            "simulated_dangerous_actions": True,
        },
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 HVAC 舒适性全链路验收案例")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--real-canape-project", type=Path)
    args = parser.parse_args(argv)
    output = args.output or (
        Path("build")
        / "full-vehicle-case"
        / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    result = run_case(output, real_canape_project=args.real_canape_project)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
