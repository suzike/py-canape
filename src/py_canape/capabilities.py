"""140 项能力的机器可读注册与实现绑定。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class Capability:
    id: int
    name: str
    implementation: str
    verification: str


IMPLEMENTATIONS: dict[int, str] = {
    1: "CANape.connect", 2: "CANape.get_canape_version_info",
    3: "CANape.connect", 4: "CANape.attach_to_active_application",
    5: "CANape.open", 6: "CANape.load_cna_file",
    7: "CANape.get_project_info", 8: "CANape.quit",
    9: "CANape.save_debug_window", 10: "AssetManager.inventory",
    11: "CANape.list_devices", 12: "CANape.get_device",
    13: "CANape.add_device", 14: "CANape.remove_device",
    15: "CANape.set_device_online", 16: "CANape.set_device_offline",
    17: "CANape.list_devices", 18: "CANape.read_memory",
    19: "CANape.read_memory", 20: "CANape.write_memory",
    21: "CANape.upload_device_data", 22: "CANape.reconnect_device",
    23: "CANape.start_measurement", 24: "CANape.stop_measurement",
    25: "CANape.get_measurement_state", 26: "CANape.set_measurement_output_file",
    27: "CANape.list_tasks", 28: "CANape.list_measurement_channels",
    29: "CANape.add_measurement_channel", 30: "CANape.read_measurement_channel",
    31: "CANape.configure_measurement_channels", 32: "CANape.get_task_info",
    33: "CANape.read_task_current_values", 34: "CANape.read_task_next_sample",
    35: "CANape.configure_measurement", 36: "CANape.list_calibration_objects",
    37: "CANape.read_calibration_value", 38: "CANape.read_calibration_value",
    39: "CANape.write_calibration_value", 40: "CANape.write_calibration_value",
    41: "CANape.read_calibration_values", 42: "CANape.write_calibration_values",
    43: "CANape.create_calibration_snapshot", 44: "CANape.diff_calibration_snapshots",
    45: "CANape.restore_calibration_snapshot", 46: "CANape.get_calibration_object_info",
    47: "CANape.get_calibration_metadata", 48: "CANape.validate_calibration_value",
    49: "AuditTrail.append", 50: "OfflineData.write_table",
    51: "EngineeringPlatform.bind_identity", 52: "CANape.list_recorders",
    53: "CANape.set_recorder_output_file", 54: "CANape.pause_recorder",
    55: "CANape.add_recorder", 56: "CANape.set_recorder_data_reduction",
    57: "AssetManager.preflight", 58: "EngineeringPlatform.recording_validation",
    59: "EngineeringPlatform.trigger_window", 60: "SignalAnalyzer.quality",
    61: "CANape.send_diagnostic_request", 62: "CANape.send_raw_diagnostic_request",
    63: "CANape.send_diagnostic_request", 64: "CANape.send_diagnostic_request",
    65: "CANape.start_tester_present", 66: "CANape.execute_diagnostic_job",
    67: "CANape.list_security_profiles", 68: "CANape.start_flash",
    69: "CANape.get_flash_state", 70: "SafetyPolicy.authorize",
    71: "CANape.list_networks", 72: "OfflineData.blf_metadata",
    73: "OfflineData.decode_blf", 74: "OfflineData.check_channel_mapping",
    75: "OfflineData.mdf_metadata", 76: "OfflineData.resample",
    77: "OfflineData.parse_a2l", 78: "OfflineData.compare_a2l_symbols",
    79: "SignalDictionary.merge", 80: "OfflineData.align",
    81: "SignalAnalyzer.quality", 82: "SignalAnalyzer.conversion_chain",
    83: "SignalAnalyzer.causality", 84: "SignalAnalyzer.state_transitions",
    85: "SignalAnalyzer.hysteresis", 86: "SignalAnalyzer.control_metrics",
    87: "SignalAnalyzer.timing", 88: "SignalAnalyzer.independence",
    89: "SignalAnalyzer.compare", 90: "SignalAnalyzer.anomaly_candidates",
    91: "CANape.run_script", 92: "AuditTrail.append",
    93: "WorkflowEngine.execute", 94: "WorkflowEngine.execute",
    95: "Reporter.generate", 96: "EngineeringPlatform.archive_project",
    97: "AssetManager.environment_inventory", 98: "AssetManager.create_manifest",
    99: "AssetManager.preflight", 100: "AssetManager.snapshot",
    101: "AssetManager.validate_topology", 102: "EngineeringPlatform.preflight_catalog",
    103: "SignalDictionary.validate", 104: "OfflineData.align",
    105: "AssetManager.preflight", 106: "EngineeringPlatform.reproducibility_report",
    107: "WorkflowEngine.load", 108: "WorkflowEngine.merge_variables",
    109: "WorkflowEngine.execute", 110: "WorkflowEngine.execute",
    111: "WorkflowEngine.execute", 112: "WorkflowEngine.execute",
    113: "WorkflowEngine.execute", 114: "WorkflowEngine.execute",
    115: "WorkflowEngine.batch", 116: "WorkflowEngine.execute",
    117: "SignalDictionary.merge", 118: "EngineeringPlatform.derive_signal",
    119: "SignalAnalyzer.quality", 120: "SignalAnalyzer.state_transitions",
    121: "SignalAnalyzer.causality", 122: "SignalAnalyzer.hysteresis",
    123: "SignalAnalyzer.control_metrics", 124: "SignalAnalyzer.independence",
    125: "SignalAnalyzer.energy_balance", 126: "SignalAnalyzer.compare",
    127: "SignalAnalyzer.anomaly_candidates", 128: "Reporter.create_evidence_bundle",
    129: "SafetyPolicy.authorize", 130: "SafetyPolicy.authorize",
    131: "SafetyPolicy.authorize", 132: "EnvironmentSecretProvider.get_secret",
    133: "AuditTrail.verify", 134: "EngineeringPlatform.bind_identity",
    135: "EngineeringPlatform.provenance", 136: "Reporter.generate",
    137: "Reporter.publish_issue", 138: "Reporter.anonymize",
    139: "PluginRegistry.discover", 140: "built_in_domains",
}


VERIFICATION: dict[int, str] = {
    **{number: "automated" for number in range(1, 141)},
    **{
        number: "hardware-required"
        for number in (
            15, 18, 19, 20, 21, 22, 23, 24, 30, 37, 38, 39, 40, 42, 45, 61,
            62, 63, 64, 65, 66, 68, 69, 70,
        )
    },
    **{
        number: "external-adapter-required"
        for number in (51, 95, 97, 101, 105, 131, 132, 134, 137, 138, 140)
    },
}


class CapabilityRegistry:
    TABLE_PATTERN = re.compile(
        r"^\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|$"
    )

    def __init__(self, capabilities: list[Capability]) -> None:
        self.capabilities = {item.id: item for item in capabilities}

    @classmethod
    def from_markdown(cls, path: str | Path) -> CapabilityRegistry:
        capabilities = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            match = cls.TABLE_PATTERN.match(line)
            if not match:
                continue
            number = int(match.group(1))
            if number not in IMPLEMENTATIONS:
                continue
            capabilities.append(
                Capability(
                    id=number,
                    name=match.group(2).strip(),
                    implementation=IMPLEMENTATIONS[number],
                    verification=VERIFICATION[number],
                )
            )
        return cls(capabilities)

    def validate(self) -> dict[str, Any]:
        expected = set(range(1, 141))
        actual = set(self.capabilities)
        missing_implementations = sorted(expected - set(IMPLEMENTATIONS))
        missing_capabilities = sorted(expected - actual)
        extra = sorted(actual - expected)
        return {
            "passed": not missing_implementations and not missing_capabilities and not extra,
            "count": len(actual),
            "missing_implementations": missing_implementations,
            "missing_capabilities": missing_capabilities,
            "extra": extra,
        }

    def list(self) -> list[Capability]:
        return [self.capabilities[number] for number in sorted(self.capabilities)]

    @staticmethod
    def resolve(implementation: str) -> Callable[..., Any]:
        from .analysis import SignalAnalyzer
        from .assets import AssetManager
        from .canape import CANape
        from .offline import OfflineData, SignalDictionary
        from .platform import EngineeringPlatform
        from .plugins import PluginRegistry, built_in_domains
        from .reporting import AuditTrail, Reporter
        from .safety import EnvironmentSecretProvider, SafetyPolicy
        from .workflow import WorkflowEngine

        namespace = {
            item.__name__: item
            for item in (
                CANape, SignalAnalyzer, AssetManager, OfflineData,
                SignalDictionary, EngineeringPlatform, PluginRegistry,
                AuditTrail, Reporter, SafetyPolicy, EnvironmentSecretProvider,
                WorkflowEngine,
            )
        }
        namespace["built_in_domains"] = built_in_domains
        if "." not in implementation:
            value = namespace[implementation]
        else:
            owner, member = implementation.split(".", 1)
            value = getattr(namespace[owner], member)
        if not callable(value):
            raise TypeError(f"能力实现不可调用：{implementation}")
        return value
