"""组合在线控制、离线数据、分析、安全、编排和报告的工程平台。"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .analysis import SignalAnalyzer
from .assets import AssetManager
from .offline import OfflineData, SignalDictionary
from .plugins import PluginRegistry, built_in_domains
from .reporting import AuditTrail, Reporter
from .safety import SafetyPolicy
from .workflow import WorkflowEngine


class EngineeringPlatform:
    def __init__(
        self,
        *,
        safety_policy: SafetyPolicy | None = None,
        plugins: PluginRegistry | None = None,
    ) -> None:
        self.assets = AssetManager()
        self.offline = OfflineData()
        self.signals = SignalDictionary()
        self.analysis = SignalAnalyzer()
        self.reporter = Reporter()
        self.workflow = WorkflowEngine()
        self.safety = safety_policy or SafetyPolicy()
        self.plugins = plugins or built_in_domains()
        self.bindings: dict[str, Any] = {}

    def bind_identity(
        self,
        *,
        vehicle: str,
        ecu: str,
        software: str,
        calibration: str,
        task: str,
    ) -> dict[str, str]:
        self.bindings = {
            "vehicle": vehicle,
            "ecu": ecu,
            "software": software,
            "calibration": calibration,
            "task": task,
        }
        return dict(self.bindings)

    @staticmethod
    def recording_validation(
        path: str | Path,
        *,
        minimum_bytes: int = 1,
        expected_suffixes: Sequence[str] = (".mf4", ".mdf"),
    ) -> dict[str, Any]:
        source = Path(path).expanduser().resolve()
        errors = []
        if not source.is_file():
            errors.append("文件不存在")
            size = 0
        else:
            size = source.stat().st_size
            if size < minimum_bytes:
                errors.append(f"文件过小：{size}")
            if source.suffix.casefold() not in {
                suffix.casefold() for suffix in expected_suffixes
            }:
                errors.append(f"扩展名不符合预期：{source.suffix}")
        return {"passed": not errors, "size": size, "errors": errors}

    @staticmethod
    def trigger_window(
        frame: Any,
        condition: Any,
        *,
        time_column: str = "time",
        before: float = 5.0,
        after: float = 5.0,
    ) -> Any:
        indices = frame.index[condition].tolist()
        if not indices:
            return frame.iloc[0:0].copy()
        trigger_time = float(frame.loc[indices[0], time_column])
        return frame.loc[
            (frame[time_column] >= trigger_time - before)
            & (frame[time_column] <= trigger_time + after)
        ].copy()

    @staticmethod
    def derive_signal(
        frame: Any,
        name: str,
        expression: str,
        *,
        local_dict: Mapping[str, Any] | None = None,
    ) -> Any:
        result = frame.copy()
        result[name] = result.eval(
            expression, engine="python", local_dict=dict(local_dict or {})
        )
        return result

    @staticmethod
    def enumerate_mapping(values: Any, mapping: Mapping[Any, Any]) -> Any:
        return values.map(mapping).fillna(values)

    def preflight_catalog(
        self,
        required: Sequence[str],
        available: Sequence[str],
    ) -> dict[str, Any]:
        missing = sorted(set(required) - set(available))
        return {"passed": not missing, "missing": missing}

    def reproducibility_report(
        self,
        roots: Sequence[str | Path],
        output_file: str | Path,
    ) -> dict[str, Any]:
        manifest = self.assets.create_manifest(roots, output_file)
        manifest["bindings"] = dict(self.bindings)
        Path(output_file).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    @staticmethod
    def provenance(
        *,
        assets: Sequence[Mapping[str, Any]],
        algorithms: Mapping[str, str],
        configurations: Mapping[str, str],
    ) -> dict[str, Any]:
        return {
            "assets": list(assets),
            "algorithms": dict(algorithms),
            "configurations": dict(configurations),
        }

    @staticmethod
    def archive_project(
        source: str | Path, output_file: str | Path
    ) -> Path:
        source_path = Path(source).expanduser().resolve()
        output = Path(output_file).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        archive = shutil.make_archive(
            str(output.with_suffix("")), "zip", root_dir=source_path
        )
        return Path(archive)

    @staticmethod
    def diff_archives(
        left_manifest: Mapping[str, Any], right_manifest: Mapping[str, Any]
    ) -> dict[str, Any]:
        return AssetManager.compare_manifests(
            dict(left_manifest), dict(right_manifest)
        )

    def register_default_workflow_actions(
        self,
        *,
        canape: Any | None = None,
        audit: AuditTrail | None = None,
    ) -> None:
        self.workflow.register("assets.preflight", self.assets.preflight)
        self.workflow.register("assets.manifest", self.assets.create_manifest)
        self.workflow.register("offline.read_table", self.offline.read_table)
        self.workflow.register("analysis.quality", self.analysis.quality)
        self.workflow.register("report.html", self.reporter.html)
        if canape is not None:
            self.workflow.register(
                "canape.start_measurement",
                canape.start_measurement,
                write=True,
                thread_safe=False,
            )
            self.workflow.register(
                "canape.stop_measurement",
                canape.stop_measurement,
                write=True,
                thread_safe=False,
            )
            self.workflow.register(
                "canape.write_calibration",
                canape.write_calibration_value,
                write=True,
                thread_safe=False,
            )
        if audit is not None:
            self.workflow.register("audit.append", audit.append)
