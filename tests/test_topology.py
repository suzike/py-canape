from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from agent2canape.cli import main
from agent2canape.topology import (
    CANapeTopologyAuditor,
    DatabaseTopologySpec,
    DeviceTopologySpec,
    NetworkTopologyManifest,
    NetworkTopologySpec,
)


class FakeTopologyCANape:
    def __init__(self) -> None:
        self.networks = [{"name": "CAN1", "active": True}]
        self.devices = [
            {
                "name": "VCU",
                "driver_type": "XCP",
                "channel": 1,
                "online": True,
                "network": "CAN1",
                "database_filename": "vehicle.dbc",
                "databases": ["vehicle.dbc"],
            }
        ]

    def get_project_info(self) -> dict[str, str]:
        return {"working_directory": "project", "cna_filename": "vehicle.cna"}

    def get_network_topology(self) -> dict[str, object]:
        return {
            "networks": self.networks,
            "devices": self.devices,
            "com_visible_fields": {
                "network": ["name", "active"],
                "device": ["name", "channel", "network", "databases"],
            },
        }


def write_dbc(path: Path) -> None:
    path.write_text(
        'VERSION ""\n'
        "NS_ :\nBS_:\nBU_: VCU HVAC\n"
        "BO_ 100 VehicleStatus: 8 VCU\n"
        ' SG_ VehicleSpeed : 0|16@1+ (0.1,0) [0|250] "km/h" HVAC\n',
        encoding="latin-1",
    )


def manifest_for(path: Path) -> NetworkTopologyManifest:
    return NetworkTopologyManifest(
        name="vehicle-topology",
        networks=(
            NetworkTopologySpec(
                "CAN1",
                "can",
                expected_active=True,
                bitrate=500000,
            ),
        ),
        devices=(
            DeviceTopologySpec(
                "VCU",
                "CAN1",
                channel=1,
                driver_type="XCP",
                databases=("VehicleDBC",),
                expected_online=True,
            ),
        ),
        databases=(
            DatabaseTopologySpec(
                "VehicleDBC",
                str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                expected_nodes=("VCU", "HVAC"),
                expected_messages=("VehicleStatus",),
                expected_frame_ids=(0x64,),
                expected_signals=("VehicleSpeed",),
            ),
        ),
        allow_unexpected_networks=False,
        allow_unexpected_devices=False,
    )


def test_topology_manifest_deep_dbc_audit(tmp_path: Path) -> None:
    dbc = tmp_path / "vehicle.dbc"
    write_dbc(dbc)

    plan = manifest_for(dbc).plan(deep=True)

    assert plan["passed"] is True
    assert plan["assets"][0]["semantic"]["message_count"] == 1
    assert plan["assets"][0]["semantic"]["signal_count"] == 1
    assert plan["com_boundary"].startswith("CANape 1.9 COM")


def test_topology_manifest_rejects_references_hash_and_semantic_drift(
    tmp_path: Path,
) -> None:
    dbc = tmp_path / "vehicle.dbc"
    write_dbc(dbc)
    valid = manifest_for(dbc)
    invalid_database = replace(
        valid.databases[0],
        sha256="0" * 64,
        expected_signals=("MissingSignal",),
    )
    invalid = replace(
        valid,
        devices=(replace(valid.devices[0], network="Unknown", databases=("Missing",)),),
        databases=(invalid_database,),
    )

    plan = invalid.plan(deep=True)

    assert plan["passed"] is False
    assert any("未定义网络" in message for message in plan["errors"])
    assert any("未定义数据库" in message for message in plan["errors"])
    assert any("SHA-256" in message for message in plan["errors"])
    assert any("MissingSignal" in message for message in plan["errors"])


def test_topology_load_resolves_relative_database_path(tmp_path: Path) -> None:
    dbc = tmp_path / "vehicle.dbc"
    write_dbc(dbc)
    manifest_path = tmp_path / "topology.yaml"
    manifest_path.write_text(
        """name: relative-topology
networks:
  - {name: CAN1, bus_type: can}
devices: []
databases:
  - {name: VehicleDBC, path: vehicle.dbc}
""",
        encoding="utf-8",
    )

    loaded = NetworkTopologyManifest.load(manifest_path)

    assert loaded.plan()["assets"][0]["exists"] is True
    assert loaded.resolve_database_path(loaded.databases[0]) == dbc.resolve()


def test_live_topology_audit_and_drift_report(tmp_path: Path) -> None:
    dbc = tmp_path / "vehicle.dbc"
    write_dbc(dbc)
    canape = FakeTopologyCANape()
    auditor = CANapeTopologyAuditor(canape)

    passed = auditor.audit(manifest_for(dbc), deep=True)
    before = auditor.capture()
    canape.networks[0]["active"] = False
    canape.devices[0]["channel"] = 2
    failed = auditor.audit(manifest_for(dbc))
    after = auditor.capture()
    drift = auditor.diff(before, after)

    assert passed["passed"] is True
    assert failed["passed"] is False
    assert {item["kind"] for item in failed["drifts"]} == {
        "network_active",
        "device_channel",
    }
    assert drift["changed"] is True
    assert len(drift["changes"]) == 2
    assert before.digest() != after.digest()


def test_optional_topology_elements_only_warn(tmp_path: Path) -> None:
    manifest = NetworkTopologyManifest(
        name="optional",
        networks=(NetworkTopologySpec("CAN1", "can"),),
        devices=(DeviceTopologySpec("OptionalECU", "CAN1", required=False),),
        databases=(
            DatabaseTopologySpec("OptionalDBC", str(tmp_path / "missing.dbc"), required=False),
        ),
    )

    plan = manifest.plan()
    report = CANapeTopologyAuditor(FakeTopologyCANape()).audit(manifest)

    assert plan["passed"] is True
    assert any("不存在" in message for message in plan["warnings"])
    assert any("OptionalECU" in message for message in report["warnings"])


def test_topology_plan_cli(tmp_path: Path, capsys) -> None:
    dbc = tmp_path / "vehicle.dbc"
    write_dbc(dbc)
    manifest_path = tmp_path / "topology.yaml"
    manifest_path.write_text(
        """name: cli-topology
networks:
  - {name: CAN1, bus_type: can, bitrate: 500000}
devices: []
databases:
  - name: VehicleDBC
    path: vehicle.dbc
    expected_messages: [VehicleStatus]
""",
        encoding="utf-8",
    )

    assert main(["network-topology-plan", str(manifest_path), "--deep"]) == 0
    output = capsys.readouterr().out
    assert '"passed": true' in output
    assert '"message_count": 1' in output
