from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent2canape.cli import main
from agent2canape.errors import SafetyViolationError, WorkflowError
from agent2canape.streaming import (
    BoundedMeasurementBuffer,
    MeasurementStreamSample,
    MeasurementStreamSubscription,
    MeasurementSubscriptionSpec,
    RotatingMeasurementWriter,
)


def sample(sequence: int, timestamp: float, value: float) -> MeasurementStreamSample:
    return MeasurementStreamSample(
        sequence=sequence,
        source_timestamp=timestamp,
        time_seconds=timestamp,
        received_utc="2026-08-04T00:00:00+00:00",
        values={"Speed": value, "State": "RUN"},
    )


class FakeStreamCANape:
    def __init__(self, samples: list[tuple[tuple[object, ...], float]]) -> None:
        self.samples = list(samples)
        self.current = 0
        self.failures = 0

    def list_measurement_channels(self, device: str, task: str) -> list[str]:
        return ["Speed", "State", "Unused"]

    def _read(self) -> tuple[tuple[object, ...], float]:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("simulated COM error")
        if self.current >= len(self.samples):
            return self.samples[-1]
        value = self.samples[self.current]
        self.current += 1
        return value

    def read_task_next_sample(
        self, device: str, task: str
    ) -> tuple[tuple[object, ...], float]:
        return self._read()

    def read_task_current_values(
        self, device: str, task: str
    ) -> tuple[tuple[object, ...], float]:
        return self._read()


@pytest.fixture
def spec() -> MeasurementSubscriptionSpec:
    return MeasurementSubscriptionSpec(
        name="vehicle-stream",
        device="ECU",
        task="10ms",
        channels=("Speed", "State"),
        mode="next",
        expected_period_seconds=0.1,
        buffer_samples=3,
        max_age_seconds=0.25,
    )


def test_subscription_spec_validation_and_budget(spec: MeasurementSubscriptionSpec) -> None:
    plan = spec.plan()
    invalid = replace(spec, channels=("Speed", "speed"), buffer_samples=0).plan()

    assert plan["passed"] is True
    assert plan["bounded"] is True
    assert plan["threading_model"] == "caller_thread"
    assert len(plan["spec_digest"]) == 64
    assert invalid["passed"] is False
    assert any("重复" in message for message in invalid["errors"])


def test_bounded_buffer_tracks_eviction_order_and_quality(
    spec: MeasurementSubscriptionSpec,
) -> None:
    buffer = BoundedMeasurementBuffer(replace(spec, max_age_seconds=0.35))

    assert buffer.append(sample(1, 0.0, 10.0)) is True
    assert buffer.append(sample(2, 0.1, 10.0)) is True
    assert buffer.append(sample(3, 0.1, 11.0)) is False
    assert buffer.append(sample(3, 0.05, 11.0)) is False
    assert buffer.append(sample(3, 0.3, 12.0)) is True
    assert buffer.append(sample(4, 0.4, 13.0)) is True

    report = buffer.report()
    assert report["sample_count"] == 3
    assert report["buffer"]["duplicates_dropped"] == 1
    assert report["buffer"]["out_of_order_dropped"] == 1
    assert report["buffer"]["evicted_capacity"] == 1
    assert report["sampling"]["estimated_missing_samples"] == 1
    assert report["channels"]["Speed"]["mean"] == pytest.approx(35 / 3)
    assert report["channels"]["Speed"]["longest_freeze_seconds"] == 0.0

    age_buffer = BoundedMeasurementBuffer(
        replace(spec, buffer_samples=10, max_age_seconds=0.15)
    )
    age_buffer.append(sample(1, 0.0, 10.0))
    age_buffer.append(sample(2, 0.2, 11.0))
    assert age_buffer.report()["buffer"]["evicted_age"] == 1


def test_subscription_collect_maps_channels_and_drops_duplicate(
    spec: MeasurementSubscriptionSpec,
) -> None:
    canape = FakeStreamCANape(
        [
            ((10.0, "IDLE", 99), 0.0),
            ((11.0, "RUN", 99), 0.1),
            ((12.0, "RUN", 99), 0.1),
            ((13.0, "RUN", 99), 0.2),
        ]
    )
    subscription = MeasurementStreamSubscription(canape, spec, sleeper=lambda _: None)

    result = subscription.collect(3)

    assert result["status"] == "completed"
    assert result["attempts"] == 4
    assert result["subscription"]["buffer"]["duplicates_dropped"] == 1
    assert subscription.buffer.recent()[-1].values == {"Speed": 13.0, "State": "RUN"}


def test_subscription_rejects_missing_channels_and_stops_after_errors(
    spec: MeasurementSubscriptionSpec,
) -> None:
    canape = FakeStreamCANape([((10.0, "RUN", 99), 0.0)])
    with pytest.raises(SafetyViolationError, match="未配置"):
        MeasurementStreamSubscription(canape, replace(spec, channels=("Unknown",)))

    canape.failures = 3
    subscription = MeasurementStreamSubscription(
        canape,
        replace(spec, max_consecutive_errors=2),
        sleeper=lambda _: None,
    )
    assert subscription.poll() is None
    with pytest.raises(WorkflowError, match="连续 2 次"):
        subscription.poll()


def test_rotating_jsonl_writer_checkpoint_and_resume(tmp_path: Path) -> None:
    output = tmp_path / "stream.jsonl"
    writer = RotatingMeasurementWriter(
        output,
        ("Speed", "State"),
        max_part_bytes=250,
        flush_every=1,
    )
    writer.write(sample(1, 0.0, 10.0))
    writer.write(sample(2, 0.1, 11.0))
    first = writer.close()

    resumed = RotatingMeasurementWriter(
        output,
        ("Speed", "State"),
        max_part_bytes=250,
        flush_every=1,
    )
    resumed.write(sample(3, 0.2, 12.0))
    final = resumed.close()

    assert first["total_samples"] == 2
    assert final["total_samples"] == 3
    assert final["last_sequence"] == 3
    assert len(final["parts"]) >= 2
    assert all(len(item["sha256"]) == 64 for item in final["parts"])
    checkpoint = json.loads((tmp_path / "stream.state.json").read_text(encoding="utf-8"))
    assert checkpoint["total_samples"] == 3


def test_rotating_writer_never_deletes_when_part_limit_reached(tmp_path: Path) -> None:
    output = tmp_path / "stream.jsonl"
    writer = RotatingMeasurementWriter(
        output,
        ("Speed", "State"),
        max_part_bytes=1,
        max_parts=1,
    )

    writer.write(sample(1, 0.0, 10.0))
    with pytest.raises(WorkflowError, match="未删除历史文件"):
        writer.write(sample(2, 0.1, 11.0))
    assert (tmp_path / "stream.part0001.jsonl").is_file()
    writer.close()


def test_csv_writer_has_stable_header(tmp_path: Path) -> None:
    output = tmp_path / "stream.csv"
    with RotatingMeasurementWriter(output, ("Speed", "State")) as writer:
        writer.write(sample(1, 0.0, 10.0))

    lines = (tmp_path / "stream.part0001.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "sequence,source_timestamp,time_seconds,received_utc,Speed,State"
    assert lines[1].endswith(",10.0,RUN")


def test_stream_plan_cli(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "subscription.yaml"
    path.write_text(
        """name: cli-stream
device: ECU
task: 10ms
channels: [Speed]
mode: current
timestamp_scale: 0.001
buffer_samples: 100
max_age_seconds: 10
poll_interval_seconds: 0.01
""",
        encoding="utf-8",
    )

    assert main(["measurement-stream-plan", str(path)]) == 0
    assert '"bounded": true' in capsys.readouterr().out
