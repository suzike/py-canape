"""CANape 在线测量的有界订阅、滚动质量统计与增量证据写入。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import statistics
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .errors import SafetyViolationError, WorkflowError


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class MeasurementSubscriptionSpec:
    name: str
    device: str
    task: str
    channels: tuple[str, ...]
    mode: str = "next"
    timestamp_scale: float = 1.0
    expected_period_seconds: float | None = None
    buffer_samples: int = 10_000
    max_age_seconds: float | None = 60.0
    poll_interval_seconds: float = 0.0
    max_consecutive_errors: int = 3

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> MeasurementSubscriptionSpec:
        return cls(
            name=str(value.get("name", "measurement-subscription")),
            device=str(value["device"]),
            task=str(value["task"]),
            channels=tuple(str(item) for item in value.get("channels", ())),
            mode=str(value.get("mode", "next")),
            timestamp_scale=float(value.get("timestamp_scale", 1.0)),
            expected_period_seconds=(
                float(value["expected_period_seconds"])
                if value.get("expected_period_seconds") is not None
                else None
            ),
            buffer_samples=int(value.get("buffer_samples", 10_000)),
            max_age_seconds=(
                float(value["max_age_seconds"])
                if value.get("max_age_seconds") is not None
                else None
            ),
            poll_interval_seconds=float(value.get("poll_interval_seconds", 0.0)),
            max_consecutive_errors=int(value.get("max_consecutive_errors", 3)),
        )

    @classmethod
    def load(cls, path: str | Path) -> MeasurementSubscriptionSpec:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        text = source.read_text(encoding="utf-8")
        value = (
            yaml.safe_load(text)
            if source.suffix.casefold() in {".yaml", ".yml"}
            else json.loads(text)
        )
        if not isinstance(value, Mapping):
            raise ValueError("订阅规格根节点必须是对象")
        return cls.from_mapping(value)

    def public(self) -> dict[str, Any]:
        return asdict(self)

    def digest(self) -> str:
        return _digest(self.public())

    def plan(self) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []
        if not all((self.name.strip(), self.device.strip(), self.task.strip())):
            errors.append("name/device/task 不能为空")
        folded = [item.casefold() for item in self.channels]
        if not self.channels:
            errors.append("channels 不能为空")
        if len(folded) != len(set(folded)):
            errors.append("channels 不能重复")
        if any(not item.strip() for item in self.channels):
            errors.append("channels 不能包含空名称")
        if self.mode not in {"current", "next"}:
            errors.append("mode 必须是 current 或 next")
        if self.timestamp_scale <= 0:
            errors.append("timestamp_scale 必须大于0")
        if self.expected_period_seconds is not None and self.expected_period_seconds <= 0:
            errors.append("expected_period_seconds 必须大于0")
        if not 1 <= self.buffer_samples <= 1_000_000:
            errors.append("buffer_samples 必须在 1 到 1000000 之间")
        if self.max_age_seconds is not None and self.max_age_seconds <= 0:
            errors.append("max_age_seconds 必须大于0")
        if self.poll_interval_seconds < 0:
            errors.append("poll_interval_seconds 不能为负")
        if not 1 <= self.max_consecutive_errors <= 100:
            errors.append("max_consecutive_errors 必须在 1 到 100 之间")
        if self.mode == "current" and self.poll_interval_seconds == 0:
            warnings.append("current 模式未设置轮询间隔，重复时间戳可能被大量丢弃")
        estimated_buffer_bytes = self.buffer_samples * (80 + len(self.channels) * 24)
        if estimated_buffer_bytes > 256 * 1024 * 1024:
            warnings.append("估算缓冲内存超过 256 MiB")
        return {
            "status": "passed" if not errors else "failed",
            "passed": not errors,
            "name": self.name,
            "spec_digest": self.digest(),
            "channel_count": len(self.channels),
            "estimated_buffer_bytes": estimated_buffer_bytes,
            "bounded": True,
            "threading_model": "caller_thread",
            "errors": errors,
            "warnings": warnings,
        }

    def require_valid(self) -> dict[str, Any]:
        plan = self.plan()
        if not plan["passed"]:
            raise SafetyViolationError("; ".join(plan["errors"]))
        return plan


@dataclass(frozen=True, slots=True)
class MeasurementStreamSample:
    sequence: int
    source_timestamp: float
    time_seconds: float
    received_utc: str
    values: dict[str, Any]

    def public(self) -> dict[str, Any]:
        return asdict(self)


class BoundedMeasurementBuffer:
    """同时受样本数和时间跨度约束的测量环形缓冲。"""

    def __init__(self, spec: MeasurementSubscriptionSpec) -> None:
        spec.require_valid()
        self.spec = spec
        self._samples: deque[MeasurementStreamSample] = deque()
        self.accepted = 0
        self.evicted_capacity = 0
        self.evicted_age = 0
        self.duplicates = 0
        self.out_of_order = 0

    def append(self, sample: MeasurementStreamSample) -> bool:
        if self._samples:
            latest = self._samples[-1].time_seconds
            if sample.time_seconds == latest:
                self.duplicates += 1
                return False
            if sample.time_seconds < latest:
                self.out_of_order += 1
                return False
        if len(self._samples) >= self.spec.buffer_samples:
            self._samples.popleft()
            self.evicted_capacity += 1
        self._samples.append(sample)
        self.accepted += 1
        if self.spec.max_age_seconds is not None:
            threshold = sample.time_seconds - self.spec.max_age_seconds
            while self._samples and self._samples[0].time_seconds < threshold:
                self._samples.popleft()
                self.evicted_age += 1
        return True

    def recent(self, limit: int | None = None) -> list[MeasurementStreamSample]:
        if limit is None:
            return list(self._samples)
        if limit < 0:
            raise ValueError("limit 不能为负")
        return list(self._samples)[-limit:] if limit else []

    def report(self, *, window_seconds: float | None = None) -> dict[str, Any]:
        if window_seconds is not None and window_seconds <= 0:
            raise ValueError("window_seconds 必须大于0")
        samples = list(self._samples)
        if samples and window_seconds is not None:
            threshold = samples[-1].time_seconds - window_seconds
            samples = [item for item in samples if item.time_seconds >= threshold]
        timestamps = [item.time_seconds for item in samples]
        periods = [
            right - left for left, right in zip(timestamps, timestamps[1:], strict=False)
        ]
        expected = self.spec.expected_period_seconds
        missing_estimate = (
            sum(max(round(period / expected) - 1, 0) for period in periods)
            if expected is not None
            else 0
        )
        jitters = (
            [abs(period - expected) for period in periods]
            if expected is not None
            else []
        )
        channel_reports: dict[str, dict[str, Any]] = {}
        for channel in self.spec.channels:
            raw = [item.values.get(channel) for item in samples]
            numeric = [
                float(value)
                for value in raw
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            ]
            missing = sum(
                value is None
                or (isinstance(value, float) and not math.isfinite(value))
                for value in raw
            )
            changes = sum(
                left != right for left, right in zip(raw, raw[1:], strict=False)
            )
            longest_freeze = 0.0
            freeze_start = 0
            for index in range(1, len(raw)):
                if raw[index] != raw[index - 1]:
                    freeze_start = index
                    continue
                longest_freeze = max(
                    longest_freeze,
                    timestamps[index] - timestamps[freeze_start],
                )
            channel_reports[channel] = {
                "samples": len(raw),
                "numeric_samples": len(numeric),
                "missing_samples": missing,
                "non_numeric_samples": len(raw) - len(numeric) - missing,
                "minimum": min(numeric) if numeric else None,
                "maximum": max(numeric) if numeric else None,
                "mean": statistics.fmean(numeric) if numeric else None,
                "standard_deviation": (
                    statistics.pstdev(numeric) if len(numeric) > 1 else 0.0
                ),
                "rms": (
                    math.sqrt(statistics.fmean(value * value for value in numeric))
                    if numeric
                    else None
                ),
                "change_count": changes,
                "longest_freeze_seconds": longest_freeze,
            }
        duration = timestamps[-1] - timestamps[0] if len(timestamps) > 1 else 0.0
        return {
            "status": "available" if samples else "empty",
            "spec_digest": self.spec.digest(),
            "sample_count": len(samples),
            "duration_seconds": duration,
            "first_timestamp_seconds": timestamps[0] if timestamps else None,
            "last_timestamp_seconds": timestamps[-1] if timestamps else None,
            "sampling": {
                "expected_period_seconds": expected,
                "mean_period_seconds": statistics.fmean(periods) if periods else None,
                "maximum_gap_seconds": max(periods) if periods else None,
                "jitter_p95_seconds": _percentile(jitters, 0.95) if jitters else None,
                "jitter_max_seconds": max(jitters) if jitters else None,
                "estimated_missing_samples": missing_estimate,
            },
            "buffer": {
                "retained_samples": len(self._samples),
                "maximum_samples": self.spec.buffer_samples,
                "accepted_samples": self.accepted,
                "evicted_capacity": self.evicted_capacity,
                "evicted_age": self.evicted_age,
                "duplicates_dropped": self.duplicates,
                "out_of_order_dropped": self.out_of_order,
            },
            "channels": channel_reports,
        }


class RotatingMeasurementWriter:
    """带原子检查点的 JSONL/CSV 分卷写入器；达到上限时停止而不删除旧证据。"""

    def __init__(
        self,
        output_file: str | Path,
        channels: Sequence[str],
        *,
        max_part_bytes: int = 64 * 1024 * 1024,
        max_parts: int = 100,
        flush_every: int = 1,
        resume: bool = True,
    ) -> None:
        self.output_file = Path(output_file).expanduser().resolve()
        self.channels = tuple(str(item) for item in channels)
        self.format = self.output_file.suffix.casefold().lstrip(".")
        if self.format not in {"jsonl", "csv"}:
            raise ValueError("流式输出文件必须是 .jsonl 或 .csv")
        if not self.channels or len({item.casefold() for item in self.channels}) != len(
            self.channels
        ):
            raise ValueError("channels 必须非空且不能重复")
        if max_part_bytes <= 0 or max_parts <= 0 or flush_every <= 0:
            raise ValueError("max_part_bytes/max_parts/flush_every 必须大于0")
        self.max_part_bytes = int(max_part_bytes)
        self.max_parts = int(max_parts)
        self.flush_every = int(flush_every)
        self.checkpoint_path = self.output_file.with_suffix(".state.json")
        self.output_file.parent.mkdir(parents=True, exist_ok=True)
        self.part_index = 1
        self.last_sequence = 0
        self.total_samples = 0
        self._pending = 0
        self._stream: Any = None
        if resume and self.checkpoint_path.is_file():
            self._resume()
        self._open_current()

    def _part_path(self, index: int | None = None) -> Path:
        selected = self.part_index if index is None else index
        return self.output_file.with_name(
            f"{self.output_file.stem}.part{selected:04d}{self.output_file.suffix}"
        )

    def _checkpoint(self) -> dict[str, Any]:
        part = self._part_path()
        return {
            "version": 1,
            "output_file": str(self.output_file),
            "format": self.format,
            "channels": list(self.channels),
            "part_index": self.part_index,
            "part_path": str(part),
            "part_bytes": part.stat().st_size if part.is_file() else 0,
            "last_sequence": self.last_sequence,
            "total_samples": self.total_samples,
            "updated_utc": datetime.now(UTC).isoformat(),
        }

    def _save_checkpoint(self) -> None:
        temporary = self.checkpoint_path.with_suffix(self.checkpoint_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._checkpoint(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.checkpoint_path)

    def _resume(self) -> None:
        state = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        if state.get("version") != 1:
            raise WorkflowError("流式检查点版本不受支持")
        if str(state.get("output_file")) != str(self.output_file):
            raise WorkflowError("流式检查点输出路径不匹配")
        if tuple(state.get("channels", ())) != self.channels:
            raise WorkflowError("流式检查点通道清单不匹配")
        self.part_index = int(state["part_index"])
        self.last_sequence = int(state["last_sequence"])
        self.total_samples = int(state["total_samples"])
        part = self._part_path()
        actual_bytes = part.stat().st_size if part.is_file() else 0
        if actual_bytes != int(state["part_bytes"]):
            raise WorkflowError(
                "流式分卷与检查点字节数不一致；拒绝自动截断或覆盖，请人工核对证据"
            )

    def _open_current(self) -> None:
        part = self._part_path()
        existed = part.is_file() and part.stat().st_size > 0
        self._stream = part.open("a", encoding="utf-8", newline="")
        if self.format == "csv" and not existed:
            writer = csv.writer(self._stream, lineterminator="\n")
            writer.writerow(
                ["sequence", "source_timestamp", "time_seconds", "received_utc", *self.channels]
            )
            self._stream.flush()
            os.fsync(self._stream.fileno())

    def _encode(self, sample: MeasurementStreamSample) -> str:
        if self.format == "jsonl":
            return json.dumps(sample.public(), ensure_ascii=False, separators=(",", ":")) + "\n"
        buffer = io.StringIO(newline="")
        csv.writer(buffer, lineterminator="\n").writerow(
            [
                sample.sequence,
                sample.source_timestamp,
                sample.time_seconds,
                sample.received_utc,
                *(sample.values.get(channel) for channel in self.channels),
            ]
        )
        return buffer.getvalue()

    def _rotate_if_needed(self, encoded: str) -> None:
        part = self._part_path()
        current_bytes = part.stat().st_size if part.is_file() else 0
        encoded_bytes = len(encoded.encode("utf-8"))
        if current_bytes == 0 or current_bytes + encoded_bytes <= self.max_part_bytes:
            return
        if self.part_index >= self.max_parts:
            raise WorkflowError(
                f"流式证据达到最大分卷数 {self.max_parts}，已停止且未删除历史文件"
            )
        self.flush()
        self._stream.close()
        self.part_index += 1
        self._open_current()

    def write(self, sample: MeasurementStreamSample) -> None:
        if sample.sequence <= self.last_sequence:
            raise WorkflowError(
                f"样本序号 {sample.sequence} 不大于检查点 {self.last_sequence}，拒绝重复写入"
            )
        encoded = self._encode(sample)
        self._rotate_if_needed(encoded)
        self._stream.write(encoded)
        self.last_sequence = sample.sequence
        self.total_samples += 1
        self._pending += 1
        if self._pending >= self.flush_every:
            self.flush()

    def flush(self) -> None:
        if self._stream is None:
            return
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._pending = 0
        self._save_checkpoint()

    def close(self) -> dict[str, Any]:
        if self._stream is not None and not self._stream.closed:
            self.flush()
            self._stream.close()
        parts = [self._part_path(index) for index in range(1, self.part_index + 1)]
        return {
            "status": "closed",
            "output_file": str(self.output_file),
            "checkpoint": str(self.checkpoint_path),
            "total_samples": self.total_samples,
            "last_sequence": self.last_sequence,
            "parts": [
                {
                    "path": str(path),
                    "size": path.stat().st_size,
                    "sha256": _file_sha256(path),
                }
                for path in parts
                if path.is_file()
            ],
        }

    def __enter__(self) -> RotatingMeasurementWriter:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


class MeasurementStreamSubscription:
    """在调用线程中同步读取 CANape 任务采样，避免跨线程 COM Apartment 风险。"""

    def __init__(
        self,
        canape: Any,
        spec: MeasurementSubscriptionSpec,
        *,
        writer: RotatingMeasurementWriter | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        spec.require_valid()
        self.canape = canape
        self.spec = spec
        self.writer = writer
        self.sleeper = sleeper
        configured = tuple(canape.list_measurement_channels(spec.device, spec.task))
        missing = [item for item in spec.channels if item not in configured]
        if missing:
            raise SafetyViolationError(
                "订阅信号未配置到 CANape 任务：" + ", ".join(missing)
            )
        self._configured_channels = configured
        self._indices = tuple(configured.index(item) for item in spec.channels)
        self.buffer = BoundedMeasurementBuffer(spec)
        self.sequence = writer.last_sequence if writer is not None else 0
        self.read_attempts = 0
        self.read_errors = 0
        self.consecutive_errors = 0
        self.started_utc = datetime.now(UTC).isoformat()

    def poll(self) -> MeasurementStreamSample | None:
        self.read_attempts += 1
        try:
            if self.spec.mode == "next":
                values, timestamp = self.canape.read_task_next_sample(
                    self.spec.device, self.spec.task
                )
            else:
                values, timestamp = self.canape.read_task_current_values(
                    self.spec.device, self.spec.task
                )
            if len(values) != len(self._configured_channels):
                raise WorkflowError(
                    f"任务返回 {len(values)} 个值，但配置通道数为 {len(self._configured_channels)}"
                )
        except Exception as exc:
            self.read_errors += 1
            self.consecutive_errors += 1
            if self.consecutive_errors >= self.spec.max_consecutive_errors:
                raise WorkflowError(
                    f"连续 {self.consecutive_errors} 次读取 CANape 测量失败：{exc}"
                ) from exc
            return None
        self.consecutive_errors = 0
        source_timestamp = float(timestamp)
        self.sequence += 1
        sample = MeasurementStreamSample(
            sequence=self.sequence,
            source_timestamp=source_timestamp,
            time_seconds=source_timestamp * self.spec.timestamp_scale,
            received_utc=datetime.now(UTC).isoformat(),
            values={
                channel: values[index]
                for channel, index in zip(self.spec.channels, self._indices, strict=True)
            },
        )
        if not self.buffer.append(sample):
            self.sequence -= 1
            return None
        if self.writer is not None:
            self.writer.write(sample)
        return sample

    def collect(
        self,
        sample_count: int,
        *,
        maximum_attempts: int | None = None,
    ) -> dict[str, Any]:
        if not 1 <= sample_count <= 1_000_000:
            raise ValueError("sample_count 必须在 1 到 1000000 之间")
        attempt_limit = maximum_attempts or max(sample_count * 10, sample_count)
        if attempt_limit < sample_count:
            raise ValueError("maximum_attempts 不能小于 sample_count")
        accepted = 0
        attempts = 0
        while accepted < sample_count and attempts < attempt_limit:
            attempts += 1
            if self.poll() is not None:
                accepted += 1
            if self.spec.poll_interval_seconds:
                self.sleeper(self.spec.poll_interval_seconds)
        if self.writer is not None:
            self.writer.flush()
        return {
            "status": "completed" if accepted == sample_count else "partial",
            "requested_samples": sample_count,
            "accepted_samples": accepted,
            "attempts": attempts,
            "subscription": self.status(),
            "window": self.buffer.report(),
        }

    def status(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "spec_digest": self.spec.digest(),
            "started_utc": self.started_utc,
            "device": self.spec.device,
            "task": self.spec.task,
            "channels": list(self.spec.channels),
            "mode": self.spec.mode,
            "read_attempts": self.read_attempts,
            "read_errors": self.read_errors,
            "consecutive_errors": self.consecutive_errors,
            "next_sequence": self.sequence + 1,
            "buffer": self.buffer.report()["buffer"],
        }
