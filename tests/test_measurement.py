from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from agent2canape import (
    MeasurementArtifactVerifier,
    MeasurementChannelSpec,
    MeasurementManifest,
    MeasurementRecorderSpec,
    MeasurementSessionManager,
    MeasurementTaskLimit,
    MeasurementTriggerSpec,
    WorkflowError,
)
from agent2canape.cli import main


class FakeMeasurementCANape:
    def __init__(self) -> None:
        self.running = False
        self.configuration = {
            "sample_size": 4,
            "fifo_size": 64,
            "sync_mode": False,
            "resume_mode": False,
            "use_nan": False,
        }
        self.output_file = "baseline.mf4"
        self.channels = {"ECU::10ms": ["Existing"]}
        self.recorders = {
            "Recorder1": {
                "name": "Recorder1",
                "state": 0,
                "recorder_type": 1,
                "mdf_filename": "recorder-baseline.mf4",
                "data_reduction": 2,
            }
        }
        self.fail_channel = ""
        self.online = True

    def is_measurement_running(self) -> bool:
        return self.running

    def start_measurement(self) -> bool:
        self.running = True
        return True

    def stop_measurement(self) -> bool:
        self.running = False
        return True

    def get_measurement_configuration(self) -> dict[str, object]:
        return dict(self.configuration)

    def configure_measurement(self, **values: object) -> None:
        self.configuration.update(
            {name: value for name, value in values.items() if value is not None}
        )

    def get_measurement_output_file(self) -> str:
        return self.output_file

    def set_measurement_output_file(self, path: str) -> None:
        self.output_file = str(path)

    def list_measurement_channels(self, device: str, task: str) -> list[str]:
        return list(self.channels.get(f"{device}::{task}", ()))

    def configure_measurement_channels(
        self,
        device: str,
        task: str,
        channels: tuple[str, ...] | list[str],
        *,
        clear: bool = True,
    ) -> list[str]:
        key = f"{device}::{task}"
        if clear:
            self.channels[key] = []
        for channel in channels:
            self.channels[key].append(channel)
            if channel == self.fail_channel:
                raise RuntimeError("simulated channel failure")
        return list(self.channels[key])

    def get_recorder_configuration(self, name: str) -> dict[str, object]:
        return dict(self.recorders[name])

    def set_recorder_output_file(self, name: str, path: str) -> None:
        self.recorders[name]["mdf_filename"] = str(path)

    def set_recorder_data_reduction(self, name: str, value: int) -> None:
        self.recorders[name]["data_reduction"] = int(value)

    def reconnect_device(
        self,
        device: str,
        *,
        download: bool = False,
        restore_measurement: bool = False,
    ) -> bool:
        self.online = True
        self.channels["ECU::10ms"] = []
        return True


@pytest.fixture
def manifest() -> MeasurementManifest:
    return MeasurementManifest(
        name="vehicle-baseline",
        channels=(
            MeasurementChannelSpec("ECU", "10ms", "VehicleSpeed"),
            MeasurementChannelSpec(
                "ECU",
                "10ms",
                "CoolantTemp",
                required=False,
                priority="low",
            ),
        ),
        task_limits=(
            MeasurementTaskLimit(
                "ECU",
                "10ms",
                sampling_time_seconds=0.01,
                max_channels=10,
                max_bytes_per_second=5000,
                minimum_fifo_samples=128,
                event=1,
            ),
        ),
        recorders=(
            MeasurementRecorderSpec("Recorder1", "vehicle-baseline.mf4", 1),
        ),
        triggers=(
            MeasurementTriggerSpec(
                "over-temperature",
                "Recorder1",
                "CoolantTemp > 110",
                pre_trigger_seconds=0.5,
                post_trigger_seconds=0.5,
            ),
        ),
        sample_size=8,
        fifo_size=256,
        sync_mode=True,
        use_nan=True,
        measurement_output_file="measurement.mf4",
        start_after_apply=True,
    )


def test_measurement_manifest_plans_daq_and_trigger_budget(
    manifest: MeasurementManifest,
) -> None:
    plan = manifest.plan()

    assert plan["passed"] is True
    assert len(plan["manifest_digest"]) == 64
    assert plan["tasks"][0]["nominal_rate_hz"] == 100
    assert plan["tasks"][0]["estimated_bytes_per_second"] == 2400
    assert plan["tasks"][0]["utilization"] == pytest.approx(0.48)
    assert "CNA" in plan["trigger_application"]


def test_measurement_manifest_rejects_over_budget_and_invalid_priority(
    manifest: MeasurementManifest,
) -> None:
    overloaded = replace(
        manifest,
        task_limits=(
            replace(manifest.task_limits[0], max_bytes_per_second=1000),
        ),
        channels=(
            manifest.channels[0],
            replace(manifest.channels[1], sample_rate_hz=200, priority="unsupported"),
        ),
        fifo_size=10,
    )

    plan = overloaded.plan()

    assert plan["passed"] is False
    assert any("超过任务" in message for message in plan["errors"])
    assert any("DAQ 利用率" in message for message in plan["errors"])
    assert any("priority" in message for message in plan["errors"])
    assert any("FIFO" in message for message in plan["errors"])
    assert plan["tasks"][0]["degradation_candidates"] == ["CoolantTemp"]


def test_measurement_apply_uses_snapshot_and_starts_measurement(
    manifest: MeasurementManifest,
) -> None:
    canape = FakeMeasurementCANape()
    manager = MeasurementSessionManager(canape)

    preview = manager.preview(manifest)
    repeated_preview = manager.preview(manifest)
    result = manager.apply(manifest)

    assert len(preview["precondition_digest"]) == 64
    assert preview["precondition_digest"] == repeated_preview["precondition_digest"]
    assert canape.channels["ECU::10ms"] == ["VehicleSpeed", "CoolantTemp"]
    assert canape.configuration["fifo_size"] == 256
    assert canape.recorders["Recorder1"]["data_reduction"] == 1
    assert canape.running is True
    assert result["status"] == "applied"


def test_measurement_apply_rolls_back_partial_configuration(
    manifest: MeasurementManifest,
) -> None:
    canape = FakeMeasurementCANape()
    canape.running = True
    canape.fail_channel = "CoolantTemp"
    manager = MeasurementSessionManager(canape)

    with pytest.raises(WorkflowError, match="已恢复快照"):
        manager.apply(manifest)

    assert canape.channels["ECU::10ms"] == ["Existing"]
    assert canape.configuration["fifo_size"] == 64
    assert canape.output_file == "baseline.mf4"
    assert canape.recorders["Recorder1"]["data_reduction"] == 2
    assert canape.running is True


def test_reconnect_restores_manifest_and_previous_running_state(
    manifest: MeasurementManifest,
) -> None:
    canape = FakeMeasurementCANape()
    canape.running = True
    manager = MeasurementSessionManager(canape)

    result = manager.reconnect_and_restore("ECU", manifest)

    assert result["reconnected"] is True
    assert result["restored_running_state"] is True
    assert canape.channels["ECU::10ms"] == ["VehicleSpeed", "CoolantTemp"]
    assert canape.running is True


def test_reconnect_failure_restores_pre_reconnect_snapshot(
    manifest: MeasurementManifest,
) -> None:
    canape = FakeMeasurementCANape()
    canape.running = True
    canape.fail_channel = "CoolantTemp"

    with pytest.raises(WorkflowError, match="已恢复快照"):
        MeasurementSessionManager(canape).reconnect_and_restore("ECU", manifest)

    assert canape.channels["ECU::10ms"] == ["Existing"]
    assert canape.configuration["fifo_size"] == 64
    assert canape.running is True


def test_measurement_artifact_basic_verification(tmp_path: Path) -> None:
    artifact = tmp_path / "measurement.mf4"
    artifact.write_bytes(b"measurement-evidence")

    passed = MeasurementArtifactVerifier.verify(artifact, minimum_bytes=10)
    missing = MeasurementArtifactVerifier.verify(tmp_path / "missing.mf4")

    assert passed["passed"] is True
    assert len(passed["sha256"]) == 64
    assert missing["passed"] is False
    assert "录制文件不存在" in missing["errors"]


def test_measurement_artifact_deep_verification(tmp_path: Path) -> None:
    from asammdf import MDF, Signal

    artifact = tmp_path / "measurement.mf4"
    signal = Signal(
        samples=np.array([20.0, 21.0, 22.0]),
        timestamps=np.array([0.0, 0.1, 0.2]),
        name="CabinTemp",
        unit="degC",
    )
    with MDF(version="4.10") as mdf:
        mdf.append([signal])
        mdf.save(artifact, overwrite=True)

    result = MeasurementArtifactVerifier.verify(
        artifact,
        expected_channels=("CabinTemp",),
        minimum_duration_seconds=0.2,
        deep=True,
    )

    assert result["passed"] is True
    assert result["channel_count"] >= 1
    assert result["duration_seconds"] == pytest.approx(0.2)


def test_measurement_cli_plan_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(
        """name: cli-measurement
fifo_size: 100
channels:
  - {device: ECU, task: 10ms, name: VehicleSpeed}
task_limits:
  - device: ECU
    task: 10ms
    sampling_time_seconds: 0.01
    max_channels: 10
    max_bytes_per_second: 5000
    minimum_fifo_samples: 100
""",
        encoding="utf-8",
    )
    artifact = tmp_path / "measurement.mf4"
    artifact.write_bytes(b"measurement-evidence")

    assert main(["measurement-plan", str(manifest_path)]) == 0
    assert main(
        ["measurement-verify", str(artifact), "--minimum-bytes", "10"]
    ) == 0
    output = capsys.readouterr().out
    assert '"passed": true' in output
