"""标定物理约束、DOE 质量门禁、实验报告和安全代理优化。"""

from __future__ import annotations

import itertools
import json
import math
import os
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .calibration import CalibrationDataset, CalibrationKind, _json_value, _utc_now


@dataclass(frozen=True, slots=True)
class MapNeighborhoodConstraint:
    """限制二维 MAP 相邻单元的物理梯度和对角跃变。"""

    name: str
    maximum_x_gradient: float | None = None
    maximum_y_gradient: float | None = None
    maximum_diagonal_delta: float | None = None

    def validate(self, dataset: CalibrationDataset) -> list[str]:
        parameter = dataset.parameters.get(self.name)
        if parameter is None:
            return [f"二维邻域约束引用了不存在的标定量：{self.name}"]
        if parameter.kind is not CalibrationKind.MAP:
            return [f"{self.name} 二维邻域约束只支持 MAP"]
        matrix = _json_value(parameter.value)
        if (
            not isinstance(matrix, list)
            or not matrix
            or not all(isinstance(row, list) for row in matrix)
        ):
            return [f"{self.name} MAP 数据格式无效"]
        widths = {len(row) for row in matrix}
        if len(widths) != 1 or not next(iter(widths), 0):
            return [f"{self.name} MAP 行长度不一致或为空"]
        limits = (
            self.maximum_x_gradient,
            self.maximum_y_gradient,
            self.maximum_diagonal_delta,
        )
        if any(value is not None and value < 0 for value in limits):
            return [f"{self.name} 二维邻域限制不能为负数"]
        rows = len(matrix)
        columns = len(matrix[0])
        x_axis = parameter.x_axis or [float(index) for index in range(columns)]
        y_axis = parameter.y_axis or [float(index) for index in range(rows)]
        if len(x_axis) != columns or len(y_axis) != rows:
            return [f"{self.name} MAP 轴与矩阵维度不一致"]
        if any(
            right <= left
            for axis in (x_axis, y_axis)
            for left, right in zip(axis, axis[1:], strict=False)
        ):
            return [f"{self.name} MAP 轴必须严格递增"]

        errors = []
        if self.maximum_x_gradient is not None:
            for row in range(rows):
                for column in range(columns - 1):
                    gradient = abs(
                        (float(matrix[row][column + 1]) - float(matrix[row][column]))
                        / (x_axis[column + 1] - x_axis[column])
                    )
                    if gradient > self.maximum_x_gradient:
                        errors.append(
                            f"{self.name}[{row},{column}→{row},{column + 1}] "
                            f"X 梯度 {gradient:.6g} 超过 {self.maximum_x_gradient}"
                        )
        if self.maximum_y_gradient is not None:
            for row in range(rows - 1):
                for column in range(columns):
                    gradient = abs(
                        (float(matrix[row + 1][column]) - float(matrix[row][column]))
                        / (y_axis[row + 1] - y_axis[row])
                    )
                    if gradient > self.maximum_y_gradient:
                        errors.append(
                            f"{self.name}[{row},{column}→{row + 1},{column}] "
                            f"Y 梯度 {gradient:.6g} 超过 {self.maximum_y_gradient}"
                        )
        if self.maximum_diagonal_delta is not None:
            for row in range(rows - 1):
                for column in range(columns - 1):
                    deltas = (
                        abs(
                            float(matrix[row + 1][column + 1])
                            - float(matrix[row][column])
                        ),
                        abs(
                            float(matrix[row + 1][column])
                            - float(matrix[row][column + 1])
                        ),
                    )
                    if max(deltas) > self.maximum_diagonal_delta:
                        errors.append(
                            f"{self.name} 单元[{row},{column}] 对角变化 "
                            f"{max(deltas):.6g} 超过 {self.maximum_diagonal_delta}"
                        )
        return errors


@dataclass(frozen=True, slots=True)
class PhysicalModelConstraint:
    """使用项目物理模型计算派生量并校验安全边界。"""

    name: str
    evaluator: Callable[[CalibrationDataset], Mapping[str, float]] = field(
        repr=False, compare=False
    )
    limits: Mapping[str, tuple[float | None, float | None]] = field(
        default_factory=dict
    )
    required_parameters: tuple[str, ...] = ()

    def evaluate(self, dataset: CalibrationDataset) -> dict[str, float]:
        missing = sorted(set(self.required_parameters) - set(dataset.parameters))
        if missing:
            raise KeyError(f"缺少物理模型输入：{', '.join(missing)}")
        values = {
            name: float(value) for name, value in self.evaluator(dataset).items()
        }
        invalid = [name for name, value in values.items() if not math.isfinite(value)]
        if invalid:
            raise ValueError(f"物理模型返回非有限值：{', '.join(invalid)}")
        return values

    def validate(self, dataset: CalibrationDataset) -> list[str]:
        try:
            metrics = self.evaluate(dataset)
        except Exception as exc:
            return [f"物理模型 {self.name} 执行失败：{exc}"]
        errors = []
        for metric, (minimum, maximum) in self.limits.items():
            if metric not in metrics:
                errors.append(f"物理模型 {self.name} 缺少输出：{metric}")
                continue
            value = metrics[metric]
            if minimum is not None and value < minimum:
                errors.append(
                    f"物理模型 {self.name}: {metric}={value:.6g} < {minimum}"
                )
            if maximum is not None and value > maximum:
                errors.append(
                    f"物理模型 {self.name}: {metric}={value:.6g} > {maximum}"
                )
        return errors


def _linear_slope(x_values: Sequence[float], y_values: Sequence[float]) -> float:
    if len(x_values) != len(y_values) or len(x_values) < 2:
        return 0.0
    x_mean = statistics.fmean(x_values)
    y_mean = statistics.fmean(y_values)
    denominator = sum((value - x_mean) ** 2 for value in x_values)
    if denominator <= 0:
        return 0.0
    return sum(
        (x_value - x_mean) * (y_value - y_mean)
        for x_value, y_value in zip(x_values, y_values, strict=True)
    ) / denominator


@dataclass(frozen=True, slots=True)
class SteadyStateRule:
    signal: str
    minimum_samples: int = 5
    window_samples: int | None = None
    maximum_span: float | None = None
    maximum_absolute_slope: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    time_signal: str = "time"

    def evaluate(self, samples: Sequence[Mapping[str, float]]) -> dict[str, Any]:
        if self.minimum_samples < 2:
            raise ValueError(f"{self.signal} minimum_samples 必须至少为 2")
        selected = list(samples)
        if self.window_samples is not None:
            if self.window_samples < self.minimum_samples:
                raise ValueError(
                    f"{self.signal} window_samples 不能小于 minimum_samples"
                )
            selected = selected[-self.window_samples :]
        values = []
        times = []
        issues = []
        for index, sample in enumerate(selected):
            if self.signal not in sample:
                issues.append(f"样本 {index} 缺少 {self.signal}")
                continue
            value = float(sample[self.signal])
            if not math.isfinite(value):
                issues.append(f"样本 {index} 的 {self.signal} 非有限")
                continue
            timestamp = float(sample.get(self.time_signal, index))
            if not math.isfinite(timestamp):
                issues.append(f"样本 {index} 的 {self.time_signal} 非有限")
                continue
            values.append(value)
            times.append(timestamp)
        if len(values) < self.minimum_samples:
            issues.append(
                f"{self.signal} 有效样本 {len(values)} < {self.minimum_samples}"
            )
        if any(
            right <= left
            for left, right in zip(times, times[1:], strict=False)
        ):
            issues.append(f"{self.signal} 的时间轴必须严格递增")
        span = max(values) - min(values) if values else math.inf
        slope = _linear_slope(times, values) if len(values) >= 2 else math.inf
        if self.maximum_span is not None:
            if self.maximum_span < 0:
                raise ValueError(f"{self.signal} maximum_span 不能为负数")
            if span > self.maximum_span:
                issues.append(
                    f"{self.signal} 波动范围 {span:.6g} > {self.maximum_span}"
                )
        if self.maximum_absolute_slope is not None:
            if self.maximum_absolute_slope < 0:
                raise ValueError(
                    f"{self.signal} maximum_absolute_slope 不能为负数"
                )
            if abs(slope) > self.maximum_absolute_slope:
                issues.append(
                    f"{self.signal} 斜率 {slope:.6g} 超过 "
                    f"±{self.maximum_absolute_slope}"
                )
        if self.minimum is not None and values and min(values) < self.minimum:
            issues.append(f"{self.signal} 最小值 {min(values):.6g} < {self.minimum}")
        if self.maximum is not None and values and max(values) > self.maximum:
            issues.append(f"{self.signal} 最大值 {max(values):.6g} > {self.maximum}")
        return {
            "signal": self.signal,
            "passed": not issues,
            "sample_count": len(values),
            "span": span if math.isfinite(span) else None,
            "slope": slope if math.isfinite(slope) else None,
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
            "issues": issues,
        }


@dataclass(frozen=True, slots=True)
class MetricAcceptanceRule:
    metric: str
    minimum: float | None = None
    maximum: float | None = None
    required: bool = True

    def evaluate(self, metrics: Mapping[str, float]) -> list[str]:
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError(f"{self.metric} 验收下限不能大于上限")
        if self.metric not in metrics:
            return [f"缺少验收指标 {self.metric}"] if self.required else []
        value = float(metrics[self.metric])
        if not math.isfinite(value):
            return [f"{self.metric} 为非有限值"]
        issues = []
        if self.minimum is not None and value < self.minimum:
            issues.append(f"{self.metric}={value:.6g} < {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            issues.append(f"{self.metric}={value:.6g} > {self.maximum}")
        return issues


@dataclass(frozen=True, slots=True)
class OutlierRule:
    metric: str
    method: str = "mad"
    threshold: float = 3.5
    minimum_samples: int = 5

    def detect(self, values: Sequence[tuple[int, float]]) -> dict[str, Any]:
        if self.threshold <= 0:
            raise ValueError(f"{self.metric} threshold 必须大于 0")
        if self.minimum_samples < 3:
            raise ValueError(f"{self.metric} minimum_samples 必须至少为 3")
        finite = [(index, float(value)) for index, value in values if math.isfinite(value)]
        if len(finite) < self.minimum_samples:
            return {
                "metric": self.metric,
                "method": self.method,
                "applied": False,
                "reason": (
                    f"有效样本 {len(finite)} < {self.minimum_samples}，不执行异常剔除"
                ),
                "outlier_indices": [],
            }
        raw = [value for _, value in finite]
        scores: list[float]
        details: dict[str, Any]
        if self.method == "mad":
            center = statistics.median(raw)
            mad = statistics.median(abs(value - center) for value in raw)
            if mad == 0:
                return {
                    "metric": self.metric,
                    "method": self.method,
                    "applied": False,
                    "reason": "MAD 为 0，无法形成稳健尺度",
                    "outlier_indices": [],
                }
            scores = [0.67448975 * abs(value - center) / mad for value in raw]
            details = {"center": center, "mad": mad}
        elif self.method == "zscore":
            center = statistics.fmean(raw)
            deviation = statistics.pstdev(raw)
            if deviation == 0:
                return {
                    "metric": self.metric,
                    "method": self.method,
                    "applied": False,
                    "reason": "标准差为 0",
                    "outlier_indices": [],
                }
            scores = [abs(value - center) / deviation for value in raw]
            details = {"center": center, "standard_deviation": deviation}
        elif self.method == "iqr":
            quartiles = statistics.quantiles(raw, n=4, method="inclusive")
            first, third = quartiles[0], quartiles[2]
            iqr = third - first
            lower = first - self.threshold * iqr
            upper = third + self.threshold * iqr
            scores = [
                self.threshold + 1.0 if value < lower or value > upper else 0.0
                for value in raw
            ]
            details = {
                "q1": first,
                "q3": third,
                "iqr": iqr,
                "lower": lower,
                "upper": upper,
            }
        else:
            raise ValueError(f"不支持的异常方法：{self.method}")
        outliers = [
            index
            for (index, _), score in zip(finite, scores, strict=True)
            if score > self.threshold
        ]
        return {
            "metric": self.metric,
            "method": self.method,
            "applied": True,
            "threshold": self.threshold,
            "outlier_indices": outliers,
            **details,
        }


@dataclass(slots=True)
class ExperimentQualityPolicy:
    steady_state_rules: list[SteadyStateRule] = field(default_factory=list)
    metric_rules: list[MetricAcceptanceRule] = field(default_factory=list)
    outlier_rules: list[OutlierRule] = field(default_factory=list)

    @property
    def requires_environment_samples(self) -> bool:
        return bool(self.steady_state_rules)

    def evaluate_environment(
        self, samples: Sequence[Mapping[str, float]]
    ) -> dict[str, Any]:
        results = [rule.evaluate(samples) for rule in self.steady_state_rules]
        return {
            "passed": all(result["passed"] for result in results),
            "rules": results,
            "issues": [
                issue for result in results for issue in result["issues"]
            ],
        }

    def evaluate_metrics(self, metrics: Mapping[str, float]) -> dict[str, Any]:
        issues = [
            issue
            for rule in self.metric_rules
            for issue in rule.evaluate(metrics)
        ]
        return {"passed": not issues, "issues": issues}

    def apply_outliers(self, cases: Sequence[Any]) -> list[dict[str, Any]]:
        results = []
        for rule in self.outlier_rules:
            values = [
                (int(case.index), float(case.metrics[rule.metric]))
                for case in cases
                if getattr(case.status, "value", case.status) == "passed"
                and rule.metric in case.metrics
            ]
            result = rule.detect(values)
            results.append(result)
            outliers = set(result["outlier_indices"])
            for case in cases:
                if case.index not in outliers:
                    continue
                case.status = type(case.status)("rejected")
                reason = (
                    f"{rule.metric} 被 {rule.method} 规则判定为队列异常值"
                )
                case.rejection_reasons.append(reason)
                case.quality.setdefault("outliers", []).append(result)
        return results


class CalibrationExperimentReport:
    @staticmethod
    def build(store: Any) -> dict[str, Any]:
        cases = list(store.cases)
        accepted = [
            case
            for case in cases
            if getattr(case.status, "value", case.status) == "passed"
        ]
        rejected = [
            case
            for case in cases
            if getattr(case.status, "value", case.status) == "rejected"
        ]
        metric_names = sorted(
            {name for case in accepted for name in case.metrics}
        )
        metric_summary = {}
        for name in metric_names:
            values = [
                float(case.metrics[name]) for case in accepted if name in case.metrics
            ]
            metric_summary[name] = {
                "count": len(values),
                "minimum": min(values),
                "maximum": max(values),
                "mean": statistics.fmean(values),
                "median": statistics.median(values),
                "standard_deviation": (
                    statistics.stdev(values) if len(values) > 1 else 0.0
                ),
            }
        return {
            "schema_version": 1,
            "generated_utc": _utc_now(),
            "experiment": {
                "name": store.name,
                "device": store.device,
                "identity": dict(store.identity),
                "created_utc": store.created_utc,
                "updated_utc": store.updated_utc,
            },
            "summary": store.summary(),
            "quality_summary": _json_value(
                getattr(store, "quality_summary", {})
            ),
            "metrics": metric_summary,
            "accepted_cases": [
                {
                    "index": case.index,
                    "parameters": _json_value(case.parameters),
                    "metrics": dict(case.metrics),
                    "attempts": case.attempts,
                    "evidence": [asdict(item) for item in case.evidence],
                }
                for case in accepted
            ],
            "rejected_cases": [
                {
                    "index": case.index,
                    "parameters": _json_value(case.parameters),
                    "metrics": dict(case.metrics),
                    "reasons": list(case.rejection_reasons),
                    "quality": _json_value(case.quality),
                }
                for case in rejected
            ],
            "failed_cases": [
                {"index": case.index, "error": case.error}
                for case in cases
                if getattr(case.status, "value", case.status) == "failed"
            ],
        }

    @staticmethod
    def to_markdown(report: Mapping[str, Any]) -> str:
        experiment = report["experiment"]
        summary = report["summary"]
        lines = [
            f"# {experiment['name']} 标定实验报告",
            "",
            f"- ECU：`{experiment['device']}`",
            f"- 生成时间：`{report['generated_utc']}`",
            f"- Case 数：{summary['case_count']}",
            f"- 通过：{summary['status_counts']['passed']}",
            f"- 拒绝：{summary['status_counts']['rejected']}",
            f"- 失败：{summary['status_counts']['failed']}",
            "",
            "## 指标摘要",
            "",
            "| 指标 | 样本数 | 最小值 | 最大值 | 均值 | 中位数 | 标准差 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for name, metric in report["metrics"].items():
            lines.append(
                f"| {name} | {metric['count']} | {metric['minimum']:.6g} | "
                f"{metric['maximum']:.6g} | {metric['mean']:.6g} | "
                f"{metric['median']:.6g} | "
                f"{metric['standard_deviation']:.6g} |"
            )
        lines.extend(["", "## 被拒绝的 Case", ""])
        if report["rejected_cases"]:
            for case in report["rejected_cases"]:
                reasons = "；".join(case["reasons"]) or "未记录原因"
                lines.append(f"- Case {case['index']}：{reasons}")
        else:
            lines.append("- 无")
        lines.extend(["", "## 失败的 Case", ""])
        if report["failed_cases"]:
            for case in report["failed_cases"]:
                lines.append(f"- Case {case['index']}：{case['error']}")
        else:
            lines.append("- 无")
        return "\n".join(lines) + "\n"

    @classmethod
    def save(cls, store: Any, path: str | Path) -> Path:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        report = cls.build(store)
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        if target.suffix.casefold() == ".json":
            temporary.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        elif target.suffix.casefold() in {".md", ".markdown"}:
            temporary.write_text(cls.to_markdown(report), encoding="utf-8")
        else:
            raise ValueError("实验报告仅支持 .json、.md 或 .markdown")
        temporary.replace(target)
        return target


@dataclass(frozen=True, slots=True)
class CalibrationObservation:
    identifier: str
    parameters: Mapping[str, float]
    metrics: Mapping[str, float]


def _cholesky(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    size = len(matrix)
    lower = [[0.0] * size for _ in range(size)]
    for row in range(size):
        for column in range(row + 1):
            value = float(matrix[row][column]) - sum(
                lower[row][index] * lower[column][index]
                for index in range(column)
            )
            if row == column:
                if value <= 0:
                    raise ValueError("代理模型核矩阵不是正定矩阵")
                lower[row][column] = math.sqrt(value)
            else:
                lower[row][column] = value / lower[column][column]
    return lower


def _solve_cholesky(
    lower: Sequence[Sequence[float]], values: Sequence[float]
) -> list[float]:
    size = len(lower)
    forward = [0.0] * size
    for row in range(size):
        forward[row] = (
            values[row]
            - sum(lower[row][column] * forward[column] for column in range(row))
        ) / lower[row][row]
    result = [0.0] * size
    for row in range(size - 1, -1, -1):
        result[row] = (
            forward[row]
            - sum(
                lower[column][row] * result[column]
                for column in range(row + 1, size)
            )
        ) / lower[row][row]
    return result


class SafeBayesianCalibrationOptimizer:
    """纯 Python 高斯过程代理模型和安全置信边界候选推荐。"""

    @staticmethod
    def candidate_grid(
        bounds: Mapping[str, tuple[float, float]],
        *,
        levels: int | Mapping[str, int] = 5,
        maximum_candidates: int = 10_000,
    ) -> list[dict[str, float]]:
        if not bounds:
            raise ValueError("bounds 不能为空")
        axes = {}
        for name, (lower, upper) in bounds.items():
            count = int(levels[name]) if isinstance(levels, Mapping) else int(levels)
            if upper <= lower or count < 2:
                raise ValueError(f"{name} 边界无效或 levels 小于 2")
            axes[name] = [
                lower + index * (upper - lower) / (count - 1)
                for index in range(count)
            ]
        total = math.prod(len(values) for values in axes.values())
        if total > maximum_candidates:
            raise ValueError(
                f"候选网格 {total} 超过 maximum_candidates={maximum_candidates}"
            )
        names = list(axes)
        return [
            dict(zip(names, values, strict=True))
            for values in itertools.product(*(axes[name] for name in names))
        ]

    @classmethod
    def suggest_from_spec(cls, spec: Mapping[str, Any]) -> dict[str, Any]:
        observations = [
            CalibrationObservation(
                identifier=str(item["identifier"]),
                parameters={
                    name: float(value)
                    for name, value in item["parameters"].items()
                },
                metrics={
                    name: float(value)
                    for name, value in item["metrics"].items()
                },
            )
            for item in spec["observations"]
        ]
        candidates = [
            {
                **{
                    name: float(value)
                    for name, value in item.items()
                    if name != "identifier"
                },
                **(
                    {"identifier": str(item["identifier"])}
                    if "identifier" in item
                    else {}
                ),
            }
            for item in spec["candidates"]
        ]
        bounds = {
            name: (float(values[0]), float(values[1]))
            for name, values in spec["bounds"].items()
        }
        safety_limits = {
            name: (
                None if values[0] is None else float(values[0]),
                None if values[1] is None else float(values[1]),
            )
            for name, values in spec.get("safety_limits", {}).items()
        }
        options = {
            name: spec[name]
            for name in (
                "direction",
                "length_scale",
                "observation_noise",
                "exploration_weight",
                "safety_sigma",
                "max_extrapolation_distance",
                "limit",
            )
            if name in spec
        }
        return cls.suggest(
            observations,
            candidates,
            bounds,
            objective=str(spec["objective"]),
            safety_limits=safety_limits,
            **options,
        )

    @staticmethod
    def suggest(
        observations: Sequence[CalibrationObservation],
        candidates: Sequence[Mapping[str, float]],
        bounds: Mapping[str, tuple[float, float]],
        *,
        objective: str,
        direction: str = "minimize",
        safety_limits: Mapping[
            str, tuple[float | None, float | None]
        ] | None = None,
        length_scale: float = 0.35,
        observation_noise: float = 1e-6,
        exploration_weight: float = 1.0,
        safety_sigma: float = 2.0,
        max_extrapolation_distance: float = 0.75,
        limit: int = 10,
    ) -> dict[str, Any]:
        if len(observations) < 2:
            raise ValueError("安全代理优化至少需要 2 个历史观测")
        if not candidates:
            raise ValueError("候选集合不能为空")
        if direction not in {"minimize", "maximize"}:
            raise ValueError("direction 必须是 minimize/maximize")
        if (
            length_scale <= 0
            or observation_noise <= 0
            or exploration_weight < 0
            or safety_sigma < 0
            or max_extrapolation_distance <= 0
            or limit <= 0
        ):
            raise ValueError("代理模型超参数无效")
        names = list(bounds)
        if not names:
            raise ValueError("bounds 不能为空")
        for name, (lower, upper) in bounds.items():
            if upper <= lower:
                raise ValueError(f"{name} 上限必须大于下限")
        required_metrics = {objective, *(safety_limits or {})}
        for observation in observations:
            missing_parameters = set(names) - set(observation.parameters)
            missing_metrics = required_metrics - set(observation.metrics)
            if missing_parameters:
                raise KeyError(
                    f"{observation.identifier} 缺少参数："
                    f"{', '.join(sorted(missing_parameters))}"
                )
            if missing_metrics:
                raise KeyError(
                    f"{observation.identifier} 缺少指标："
                    f"{', '.join(sorted(missing_metrics))}"
                )

        def normalize(point: Mapping[str, float]) -> tuple[float, ...]:
            values = []
            for name in names:
                if name not in point:
                    raise KeyError(f"候选缺少参数：{name}")
                lower, upper = bounds[name]
                value = float(point[name])
                if not lower <= value <= upper:
                    raise ValueError(f"{name}={value} 不在边界 [{lower}, {upper}]")
                values.append((value - lower) / (upper - lower))
            return tuple(values)

        def kernel(left: Sequence[float], right: Sequence[float]) -> float:
            distance_squared = sum(
                (left_value - right_value) ** 2
                for left_value, right_value in zip(left, right, strict=True)
            )
            return math.exp(-0.5 * distance_squared / (length_scale**2))

        observed_points = [normalize(item.parameters) for item in observations]
        kernel_matrix = [
            [
                kernel(left, right)
                + (observation_noise if row == column else 0.0)
                for column, right in enumerate(observed_points)
            ]
            for row, left in enumerate(observed_points)
        ]
        lower_matrix = _cholesky(kernel_matrix)
        models = {}
        for metric in required_metrics:
            raw = [float(item.metrics[metric]) for item in observations]
            if any(not math.isfinite(value) for value in raw):
                raise ValueError(f"{metric} 历史观测包含非有限值")
            center = statistics.fmean(raw)
            scale = statistics.pstdev(raw) or max(abs(center) * 0.01, 1e-6)
            normalized_values = [(value - center) / scale for value in raw]
            models[metric] = {
                "center": center,
                "scale": scale,
                "alpha": _solve_cholesky(lower_matrix, normalized_values),
            }

        safe_observed_points = []
        for point, observation in zip(
            observed_points, observations, strict=True
        ):
            violations = []
            for metric, (minimum, maximum) in (safety_limits or {}).items():
                value = float(observation.metrics[metric])
                if minimum is not None and value < minimum:
                    violations.append(metric)
                if maximum is not None and value > maximum:
                    violations.append(metric)
            if not violations:
                safe_observed_points.append(point)
        if safety_limits and not safe_observed_points:
            raise ValueError("历史观测中没有满足安全边界的锚点")
        anchors = safe_observed_points or observed_points
        observed_keys = {
            tuple(round(value, 12) for value in point) for point in observed_points
        }

        def predict(point: Sequence[float], metric: str) -> tuple[float, float]:
            vector = [kernel(point, observed) for observed in observed_points]
            model = models[metric]
            mean_normalized = sum(
                weight * alpha
                for weight, alpha in zip(
                    vector, model["alpha"], strict=True
                )
            )
            forward = [0.0] * len(lower_matrix)
            for row in range(len(lower_matrix)):
                forward[row] = (
                    vector[row]
                    - sum(
                        lower_matrix[row][column] * forward[column]
                        for column in range(row)
                    )
                ) / lower_matrix[row][row]
            variance = max(0.0, 1.0 - sum(value * value for value in forward))
            return (
                model["center"] + model["scale"] * mean_normalized,
                model["scale"] * math.sqrt(variance),
            )

        ranked = []
        rejected = []
        for index, candidate in enumerate(candidates):
            point = normalize(candidate)
            identifier = str(candidate.get("identifier", f"candidate-{index}"))
            key = tuple(round(value, 12) for value in point)
            reasons = []
            if key in observed_keys:
                reasons.append("候选与已观测点重复")
            nearest = min(
                math.sqrt(
                    sum(
                        (left - right) ** 2
                        for left, right in zip(point, anchor, strict=True)
                    )
                )
                for anchor in anchors
            )
            if nearest > max_extrapolation_distance:
                reasons.append(
                    f"距安全观测 {nearest:.6g} > {max_extrapolation_distance}"
                )
            predictions = {}
            for metric in required_metrics:
                mean, standard_deviation = predict(point, metric)
                predictions[metric] = {
                    "mean": mean,
                    "standard_deviation": standard_deviation,
                    "lower_confidence": mean - safety_sigma * standard_deviation,
                    "upper_confidence": mean + safety_sigma * standard_deviation,
                }
            for metric, (minimum, maximum) in (safety_limits or {}).items():
                prediction = predictions[metric]
                if (
                    minimum is not None
                    and prediction["lower_confidence"] < minimum
                ):
                    reasons.append(
                        f"{metric} 安全下置信界 "
                        f"{prediction['lower_confidence']:.6g} < {minimum}"
                    )
                if (
                    maximum is not None
                    and prediction["upper_confidence"] > maximum
                ):
                    reasons.append(
                        f"{metric} 安全上置信界 "
                        f"{prediction['upper_confidence']:.6g} > {maximum}"
                    )
            objective_prediction = predictions[objective]
            acquisition = (
                objective_prediction["mean"]
                - exploration_weight
                * objective_prediction["standard_deviation"]
                if direction == "minimize"
                else objective_prediction["mean"]
                + exploration_weight
                * objective_prediction["standard_deviation"]
            )
            item = {
                "identifier": identifier,
                "parameters": {
                    name: float(candidate[name]) for name in names
                },
                "predictions": predictions,
                "nearest_safe_distance": nearest,
                "acquisition": acquisition,
            }
            if reasons:
                rejected.append({**item, "reasons": reasons})
            else:
                ranked.append(item)
        ranked.sort(
            key=(
                (lambda item: (item["acquisition"], item["identifier"]))
                if direction == "minimize"
                else (lambda item: (-item["acquisition"], item["identifier"]))
            )
        )
        return {
            "passed": bool(ranked),
            "objective": objective,
            "direction": direction,
            "observation_count": len(observations),
            "candidate_count": len(candidates),
            "safe_candidate_count": len(ranked),
            "suggested": ranked[0] if ranked else None,
            "ranked": ranked[:limit],
            "rejected": rejected,
            "model": {
                "type": "gaussian_process_rbf",
                "length_scale": length_scale,
                "observation_noise": observation_noise,
                "exploration_weight": exploration_weight,
                "safety_sigma": safety_sigma,
                "max_extrapolation_distance": max_extrapolation_distance,
            },
        }
