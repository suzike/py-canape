"""通用整车信号质量、时序、状态机、控制性能和根因分析。"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    severity: str
    signal: str
    start: float | None
    end: float | None
    message: str
    evidence: dict[str, Any]


class SignalAnalyzer:
    @staticmethod
    def _numeric(series: Any) -> Any:
        import pandas as pd

        return pd.to_numeric(series, errors="coerce")

    def quality(
        self,
        frame: Any,
        rules: Mapping[str, Mapping[str, Any]],
        *,
        time_column: str = "time",
    ) -> list[Finding]:
        findings: list[Finding] = []
        time = self._numeric(frame[time_column])
        for signal, rule in rules.items():
            values = self._numeric(frame[signal])
            minimum = rule.get("minimum")
            maximum = rule.get("maximum")
            invalid_values = set(rule.get("invalid_values", ()))
            max_rate = rule.get("max_rate")
            freeze_seconds = rule.get("freeze_seconds")
            expected_period = rule.get("expected_period")
            masks = []
            if minimum is not None:
                masks.append(("below_minimum", values < minimum))
            if maximum is not None:
                masks.append(("above_maximum", values > maximum))
            if invalid_values:
                masks.append(("invalid_value", values.isin(invalid_values)))
            for name, mask in masks:
                indices = frame.index[mask.fillna(False)].tolist()
                if indices:
                    findings.append(
                        Finding(
                            name,
                            "error",
                            signal,
                            float(time.loc[indices[0]]),
                            float(time.loc[indices[-1]]),
                            f"{signal} 违反 {name}",
                            {"count": len(indices)},
                        )
                    )
            if max_rate is not None:
                rate = values.diff().abs() / time.diff().replace(0, math.nan)
                mask = rate > max_rate
                if mask.any():
                    index = mask[mask].index[0]
                    findings.append(
                        Finding(
                            "rate",
                            "warning",
                            signal,
                            float(time.loc[index]),
                            float(time.loc[index]),
                            f"{signal} 变化率超过 {max_rate}",
                            {"actual": float(rate.loc[index])},
                        )
                    )
            if freeze_seconds is not None and len(values) > 1:
                groups = values.ne(values.shift()).cumsum()
                for _, group in frame.assign(
                    _time=time, _group=groups
                ).groupby("_group"):
                    duration = float(group["_time"].iloc[-1] - group["_time"].iloc[0])
                    if duration >= freeze_seconds:
                        findings.append(
                            Finding(
                                "freeze",
                                "warning",
                                signal,
                                float(group["_time"].iloc[0]),
                                float(group["_time"].iloc[-1]),
                                f"{signal} 冻结 {duration:.3f}s",
                                {"value": group[signal].iloc[0]},
                            )
                        )
            if expected_period is not None:
                gaps = time.diff()
                mask = gaps > expected_period * 1.5
                if mask.any():
                    findings.append(
                        Finding(
                            "sampling_gap",
                            "warning",
                            signal,
                            float(time.loc[mask[mask].index[0]]),
                            float(time.loc[mask[mask].index[-1]]),
                            f"{signal} 存在采样断点",
                            {"max_gap": float(gaps.max())},
                        )
                    )
        return findings

    def state_transitions(
        self,
        frame: Any,
        signal: str,
        *,
        time_column: str = "time",
        allowed: Iterable[tuple[Any, Any]] | None = None,
        minimum_dwell: float = 0.0,
    ) -> dict[str, Any]:
        allowed_set = set(allowed or ())
        transitions = []
        illegal = []
        values = frame[signal].tolist()
        times = frame[time_column].tolist()
        if not values:
            return {"transitions": [], "illegal": [], "oscillations": []}
        state_start = float(times[0])
        for index in range(1, len(values)):
            if values[index] == values[index - 1]:
                continue
            item = {
                "from": values[index - 1],
                "to": values[index],
                "time": float(times[index]),
                "dwell": float(times[index] - state_start),
            }
            transitions.append(item)
            if allowed_set and (item["from"], item["to"]) not in allowed_set:
                illegal.append(item)
            if item["dwell"] < minimum_dwell:
                item["short_dwell"] = True
            state_start = float(times[index])
        oscillations = [
            transitions[index - 1 : index + 2]
            for index in range(1, len(transitions) - 1)
            if transitions[index - 1]["from"] == transitions[index]["to"]
            and transitions[index - 1]["to"] == transitions[index]["from"]
        ]
        return {
            "transitions": transitions,
            "illegal": illegal,
            "oscillations": oscillations,
        }

    def conversion_chain(
        self,
        raw: Sequence[float],
        physical: Sequence[float],
        application: Sequence[float],
        *,
        factor: float,
        offset: float,
        tolerance: float = 1e-6,
    ) -> dict[str, Any]:
        expected = [value * factor + offset for value in raw]
        physical_errors = [
            index
            for index, (left, right) in enumerate(zip(expected, physical, strict=False))
            if abs(left - right) > tolerance
        ]
        application_errors = [
            index
            for index, (left, right) in enumerate(zip(physical, application, strict=False))
            if abs(left - right) > tolerance
        ]
        return {
            "passed": not physical_errors and not application_errors,
            "raw_to_physical_errors": physical_errors,
            "physical_to_application_errors": application_errors,
        }

    def timing(
        self,
        frame: Any,
        trigger: str,
        response: str,
        *,
        time_column: str = "time",
        trigger_value: Any = 1,
        response_value: Any = 1,
    ) -> list[dict[str, float]]:
        trigger_edges = frame.index[
            (frame[trigger] == trigger_value)
            & (frame[trigger].shift() != trigger_value)
        ].tolist()
        results = []
        for index in trigger_edges:
            later = frame.loc[index:]
            response_rows = later.index[later[response] == response_value].tolist()
            if response_rows:
                response_index = response_rows[0]
                results.append(
                    {
                        "trigger_time": float(frame.loc[index, time_column]),
                        "response_time": float(
                            frame.loc[response_index, time_column]
                        ),
                        "delay": float(
                            frame.loc[response_index, time_column]
                            - frame.loc[index, time_column]
                        ),
                    }
                )
        return results

    def hysteresis(
        self,
        frame: Any,
        input_signal: str,
        output_signal: str,
    ) -> dict[str, Any]:
        rising = frame.loc[
            (frame[output_signal].diff() > 0), input_signal
        ].dropna()
        falling = frame.loc[
            (frame[output_signal].diff() < 0), input_signal
        ].dropna()
        return {
            "rising_thresholds": rising.tolist(),
            "falling_thresholds": falling.tolist(),
            "mean_hysteresis": (
                float(rising.mean() - falling.mean())
                if not rising.empty and not falling.empty
                else None
            ),
        }

    def strategy_validation(
        self,
        frame: Any,
        request: str,
        output: str,
        *,
        time_column: str = "time",
        debounce_seconds: float = 0.0,
        activation_timeout: float | None = None,
        maximum_on_seconds: float | None = None,
        degraded_signal: str | None = None,
        recovery_signal: str | None = None,
    ) -> dict[str, Any]:
        """Quantify debounce, activation delay, time limit, degradation and recovery."""
        events = self.timing(frame, request, output, time_column=time_column)
        violations = []
        request_values = frame[request]
        times = self._numeric(frame[time_column])
        request_edges = frame.index[
            request_values.ne(request_values.shift()) & request_values.astype(bool)
        ].tolist()
        for index in request_edges:
            next_change = frame.index[
                (frame.index > index) & request_values.ne(request_values.loc[index])
            ].tolist()
            end = next_change[0] if next_change else frame.index[-1]
            duration = float(times.loc[end] - times.loc[index])
            if duration < debounce_seconds:
                violations.append(
                    {
                        "rule": "debounce",
                        "time": float(times.loc[index]),
                        "actual": duration,
                    }
                )
        if activation_timeout is not None:
            for event in events:
                if event["delay"] > activation_timeout:
                    violations.append({"rule": "activation_timeout", **event})
        if maximum_on_seconds is not None:
            active = frame[output].astype(bool)
            groups = active.ne(active.shift()).cumsum()
            for _, group in frame.assign(_active=active, _time=times).groupby(groups):
                if not bool(group["_active"].iloc[0]):
                    continue
                duration = float(group["_time"].iloc[-1] - group["_time"].iloc[0])
                if duration > maximum_on_seconds:
                    violations.append({"rule": "maximum_on_time", "actual": duration})
        degradation_events = (
            int(frame[degraded_signal].astype(bool).sum()) if degraded_signal else 0
        )
        recovery_events = (
            int(frame[recovery_signal].astype(bool).sum()) if recovery_signal else 0
        )
        if degradation_events and not recovery_events:
            violations.append(
                {
                    "rule": "missing_recovery",
                    "degradation_events": degradation_events,
                }
            )
        return {
            "passed": not violations,
            "activation_events": events,
            "degradation_events": degradation_events,
            "recovery_events": recovery_events,
            "violations": violations,
        }

    def control_metrics(
        self,
        frame: Any,
        target: str,
        actual: str,
        *,
        time_column: str = "time",
        tolerance: float = 0.02,
    ) -> dict[str, Any]:
        error = self._numeric(frame[actual]) - self._numeric(frame[target])
        target_values = self._numeric(frame[target])
        actual_values = self._numeric(frame[actual])
        target_final = float(target_values.iloc[-1])
        actual_final = float(actual_values.iloc[-1])
        scale = max(abs(target_final), 1e-12)
        overshoot = max(0.0, float(actual_values.max() - target_final))
        within = error.abs() <= max(abs(target_final) * tolerance, tolerance)
        settling_time = None
        for index in range(len(frame)):
            if bool(within.iloc[index:].all()):
                settling_time = float(
                    frame[time_column].iloc[index] - frame[time_column].iloc[0]
                )
                break
        return {
            "mae": float(error.abs().mean()),
            "rmse": float((error.pow(2).mean()) ** 0.5),
            "steady_state_error": actual_final - target_final,
            "overshoot": overshoot,
            "overshoot_percent": overshoot / scale * 100.0,
            "settling_time": settling_time,
        }

    def causality(
        self,
        frame: Any,
        chain: Sequence[str],
        *,
        time_column: str = "time",
        maximum_delay: float | None = None,
    ) -> dict[str, Any]:
        links = []
        for source, target in zip(chain, chain[1:], strict=False):
            timings = self.timing(frame, source, target, time_column=time_column)
            passed = bool(timings)
            if maximum_delay is not None and timings:
                passed = all(item["delay"] <= maximum_delay for item in timings)
            links.append(
                {"source": source, "target": target, "events": timings, "passed": passed}
            )
        return {"passed": all(link["passed"] for link in links), "links": links}

    def independence(
        self, frame: Any, groups: Mapping[str, Sequence[str]]
    ) -> dict[str, Any]:
        numeric = frame[[signal for values in groups.values() for signal in values]]
        correlation = numeric.corr().fillna(0.0)
        cross = {}
        names = list(groups)
        for left_index, left_name in enumerate(names):
            for right_name in names[left_index + 1 :]:
                values = [
                    abs(float(correlation.loc[left, right]))
                    for left in groups[left_name]
                    for right in groups[right_name]
                ]
                cross[f"{left_name}:{right_name}"] = max(values, default=0.0)
        return {"cross_group_correlation": cross}

    def energy_balance(
        self,
        frame: Any,
        inputs: Sequence[str],
        outputs: Sequence[str],
        *,
        time_column: str = "time",
    ) -> dict[str, Any]:
        import numpy as np

        time = self._numeric(frame[time_column]).to_numpy()
        energy_in = sum(
            float(np.trapezoid(self._numeric(frame[name]), time)) for name in inputs
        )
        energy_out = sum(
            float(np.trapezoid(self._numeric(frame[name]), time)) for name in outputs
        )
        return {
            "energy_in": energy_in,
            "energy_out": energy_out,
            "balance": energy_in - energy_out,
            "efficiency": energy_out / energy_in if energy_in else None,
        }

    def compare(
        self,
        baseline: Any,
        candidate: Any,
        signals: Sequence[str],
    ) -> dict[str, Any]:
        result = {}
        for signal in signals:
            left = self._numeric(baseline[signal])
            right = self._numeric(candidate[signal])
            count = min(len(left), len(right))
            delta = right.iloc[:count].reset_index(drop=True) - left.iloc[
                :count
            ].reset_index(drop=True)
            result[signal] = {
                "mean_delta": float(delta.mean()),
                "max_abs_delta": float(delta.abs().max()),
                "rmse": float((delta.pow(2).mean()) ** 0.5),
            }
        return result

    def anomaly_candidates(
        self, findings: Sequence[Finding]
    ) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[Finding]] = {}
        for finding in findings:
            grouped.setdefault((finding.signal, finding.rule), []).append(finding)
        ranked = [
            {
                "signal": key[0],
                "rule": key[1],
                "count": len(values),
                "severity": values[0].severity,
                "evidence": [asdict(value) for value in values],
            }
            for key, values in grouped.items()
        ]
        return sorted(ranked, key=lambda item: item["count"], reverse=True)

    def cluster_anomalies(
        self,
        findings: Sequence[Finding],
        *,
        temporal_tolerance: float = 1.0,
    ) -> list[dict[str, Any]]:
        """Cluster findings by overlapping time windows and return explainable candidates."""
        ordered = sorted(
            findings,
            key=lambda item: (
                float("-inf") if item.start is None else item.start,
                item.signal,
                item.rule,
            ),
        )
        clusters: list[list[Finding]] = []
        for finding in ordered:
            start = finding.start if finding.start is not None else float("-inf")
            if not clusters:
                clusters.append([finding])
                continue
            current_end = max(
                item.end if item.end is not None else item.start or float("-inf")
                for item in clusters[-1]
            )
            if start <= current_end + temporal_tolerance:
                clusters[-1].append(finding)
            else:
                clusters.append([finding])
        severity_weight = {"info": 1, "warning": 2, "error": 3, "critical": 4}
        result = []
        for index, cluster in enumerate(clusters, start=1):
            signals = sorted({item.signal for item in cluster})
            rules = sorted({item.rule for item in cluster})
            score = sum(severity_weight.get(item.severity, 1) for item in cluster)
            result.append(
                {
                    "cluster_id": index,
                    "start": min(
                        (item.start for item in cluster if item.start is not None),
                        default=None,
                    ),
                    "end": max(
                        (item.end for item in cluster if item.end is not None),
                        default=None,
                    ),
                    "signals": signals,
                    "rules": rules,
                    "score": score,
                    "explanation": (
                        f"{len(cluster)} 个异常在同一时间窗重叠，"
                        f"涉及 {', '.join(signals)}"
                    ),
                    "evidence": [asdict(item) for item in cluster],
                }
            )
        return sorted(result, key=lambda item: item["score"], reverse=True)
