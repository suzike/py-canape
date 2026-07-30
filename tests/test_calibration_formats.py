from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent2canape import (
    A2LCatalog,
    CalibrationConstraintSet,
    CalibrationDataset,
    CalibrationIdentity,
    CalibrationKind,
    CalibrationParameter,
    CalibrationPlan,
    CalibrationRepository,
    ParameterConstraint,
    RelationConstraint,
)
from agent2canape.calibration import CalibrationChange
from agent2canape.cli import main
from agent2canape.errors import SafetyViolationError

A2L_SAMPLE = """
ASAP2_VERSION 1 71
/begin PROJECT DemoProject "demo"
  /begin MODULE ThermalECU "thermal"
    /begin MOD_COMMON "common"
      BYTE_ORDER MSB_LAST
    /end MOD_COMMON
    /begin COMPU_METHOD
      TempConv
      "Temperature"
      RAT_FUNC
      "%6.1"
      "degC"
      COEFFS 0 1 0 0 0 1
    /end COMPU_METHOD
    /begin RECORD_LAYOUT
      CurveLayout
      FNC_VALUES 1 UWORD ROW_DIR DIRECT
    /end RECORD_LAYOUT
    /begin MEASUREMENT
      CoolantTemp
      "Coolant temperature"
      UWORD
      TempConv
      1
      0
      -40
      215
      ECU_ADDRESS 0x1000
    /end MEASUREMENT
    /begin CHARACTERISTIC
      FanDutyMap
      "Fan duty curve"
      CURVE
      0x2000
      CurveLayout
      0
      TempConv
      0
      100
      /begin AXIS_DESCR
        STD_AXIS
        CoolantTemp
        CurveLayout
        TempConv
        8
        -40
        215
      /end AXIS_DESCR
    /end CHARACTERISTIC
    /begin AXIS_PTS
      FanAxis
      "Fan temperature axis"
      0x2100
      CoolantTemp
      CurveLayout
      0
      TempConv
      8
      -40
      215
    /end AXIS_PTS
  /end MODULE
/end PROJECT
"""


@pytest.fixture
def dataset() -> CalibrationDataset:
    return CalibrationDataset(
        parameters={
            "FanEnable": CalibrationParameter("FanEnable", 1.0, minimum=0, maximum=1),
            "FanCurve": CalibrationParameter(
                "FanCurve",
                [10.0, 30.0, 60.0],
                CalibrationKind.CURVE,
                unit="%",
                x_axis=[80.0, 90.0, 100.0],
            ),
            "BlendMap": CalibrationParameter(
                "BlendMap",
                [[1.0, 2.0], [3.0, 4.0]],
                CalibrationKind.MAP,
                x_axis=[0.0, 1.0],
                y_axis=[10.0, 20.0],
            ),
            "Variant": CalibrationParameter("Variant", "EU", CalibrationKind.ASCII),
        },
        identity={"ecu": "VCU", "software": "SW_42"},
    )


def test_a2l_semantic_catalog(tmp_path: Path) -> None:
    path = tmp_path / "demo.a2l"
    path.write_text(A2L_SAMPLE, encoding="latin-1")

    catalog = A2LCatalog.parse(path)

    assert catalog.project == "DemoProject"
    assert catalog.module == "ThermalECU"
    assert catalog.byte_order == "MSB_LAST"
    assert catalog.compu_methods["TempConv"].unit == "degC"
    measurement = catalog.get("coolanttemp")
    assert measurement.address == 0x1000
    assert measurement.minimum == -40
    assert measurement.maximum == 215
    assert measurement.unit == "degC"
    characteristic = catalog.get("FanDutyMap")
    assert characteristic.record_layout == "CurveLayout"
    assert characteristic.axis_descriptors[0].max_points == 8
    axis = catalog.get("FanAxis")
    assert axis.address == 0x2100
    assert axis.minimum == -40
    assert axis.maximum == 215
    assert axis.metadata["max_points"] == 8
    assert catalog.summary()["passed"] is True


def test_offline_a2l_uses_semantic_catalog(tmp_path: Path) -> None:
    from agent2canape import OfflineData

    path = tmp_path / "demo.a2l"
    path.write_text(A2L_SAMPLE, encoding="latin-1")
    definitions = {item.name: item for item in OfflineData.parse_a2l(path)}

    assert definitions["CoolantTemp"].unit == "degC"
    assert definitions["FanDutyMap"].metadata["axis_descriptors"][0]["max_points"] == 8


@pytest.mark.parametrize("suffix", [".cdfx", ".dcm", ".par"])
def test_calibration_exchange_roundtrip(
    tmp_path: Path, dataset: CalibrationDataset, suffix: str
) -> None:
    path = tmp_path / f"dataset{suffix}"
    dataset.save(path)
    restored = CalibrationDataset.load(path)

    assert restored.values() == dataset.values()
    assert restored.parameters["FanCurve"].x_axis == [80.0, 90.0, 100.0]
    assert restored.parameters["BlendMap"].y_axis == [10.0, 20.0]
    if suffix in {".cdfx", ".par"}:
        assert restored.parameters["Variant"].value == "EU"
    if suffix == ".cdfx":
        assert restored.identity["software"] == "SW_42"


def test_calibration_identity_detects_asset_mismatch(
    tmp_path: Path, dataset: CalibrationDataset
) -> None:
    a2l = tmp_path / "demo.a2l"
    hex_file = tmp_path / "software.hex"
    a2l.write_text(A2L_SAMPLE, encoding="latin-1")
    hex_file.write_bytes(b":020000040000FA\n")
    identity = CalibrationIdentity.from_assets(
        vehicle="P301",
        ecu="VCU",
        software="SW_42",
        calibration="CAL_7",
        a2l=a2l,
        hex_file=hex_file,
    )
    bound = identity.bind(dataset)

    assert CalibrationIdentity.verify(bound, a2l=a2l, hex_file=hex_file)["passed"]
    hex_file.write_bytes(b":020000040001F9\n")
    result = CalibrationIdentity.verify(bound, a2l=a2l, hex_file=hex_file)
    assert result["passed"] is False
    assert "hex_sha256" in result["mismatches"]


def test_constraints_cover_shape_gradient_and_relation(
    dataset: CalibrationDataset,
) -> None:
    rules = CalibrationConstraintSet(
        parameters=[
            ParameterConstraint(
                "FanCurve",
                minimum=0,
                maximum=100,
                monotonic="increasing",
                maximum_gradient=35,
            )
        ],
        relations=[RelationConstraint("FanEnable", "<=", "FanEnable")],
    )
    assert rules.validate(dataset) == []

    invalid = dataset.apply_patch({"FanCurve": [10, 70, 20]})
    with pytest.raises(SafetyViolationError):
        rules.require_valid(invalid)


def test_plan_validates_resulting_linked_constraints(
    dataset: CalibrationDataset,
) -> None:
    plan = CalibrationPlan(
        [CalibrationChange("FanEnable", 2, "negative test")],
        constraints=CalibrationConstraintSet(
            parameters=[ParameterConstraint("FanEnable", maximum=1)]
        ),
    )
    assert any("联动上限" in message for message in plan.validate(dataset))


def test_repository_concurrent_save_freeze_and_verify(
    tmp_path: Path, dataset: CalibrationDataset
) -> None:
    repository = CalibrationRepository(tmp_path / "repository")

    def save(index: int) -> None:
        repository.save(dataset, f"v{index}", tags=("concurrent",))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(save, range(12)))

    assert len(repository.list_versions()) == 12
    freeze = repository.freeze("v0", actor="calibrator", reason="vehicle release")
    assert freeze["frozen"] is True
    records = {item["version"]: item for item in repository.list_versions()}
    assert records["v0"]["frozen"] is True
    assert repository.verify_all()["passed"] is True


def test_cli_a2l_summary_and_convert(
    tmp_path: Path, dataset: CalibrationDataset, capsys: pytest.CaptureFixture[str]
) -> None:
    a2l = tmp_path / "demo.a2l"
    a2l.write_text(A2L_SAMPLE, encoding="latin-1")
    assert main(["a2l-summary", str(a2l)]) == 0
    assert '"object_count": 3' in capsys.readouterr().out

    source = tmp_path / "source.json"
    target = tmp_path / "target.cdfx"
    dataset.save(source)
    assert main(["calibration-convert", str(source), str(target)]) == 0
    assert target.exists()
