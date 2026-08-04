from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent2canape import EngineeringContextResolver, EngineeringUnitConverter
from agent2canape.cli import main


def test_temperature_pressure_and_flow_conversion() -> None:
    assert EngineeringUnitConverter.convert(313.15, "K", "°C") == pytest.approx(40.0)
    assert EngineeringUnitConverter.convert(1.2, "bar", "kPa") == pytest.approx(120.0)
    assert EngineeringUnitConverter.convert(3.6, "kg/h", "g/s") == pytest.approx(1.0)


def test_converter_rejects_dimension_mismatch_and_missing_target_unit() -> None:
    with pytest.raises(ValueError, match="维度不一致"):
        EngineeringUnitConverter.convert(12.0, "V", "kPa")
    with pytest.raises(ValueError, match="未声明目标单位"):
        EngineeringUnitConverter.convert(12.0, "V", "")


def test_resolver_uses_explicit_device_to_disambiguate_alias() -> None:
    context = {
        "device": "TMM",
        "objects": [
            {"device": "VCU", "name": "VcuTarget", "aliases": ["目标温度"]},
            {"device": "TMM", "name": "TmmTarget", "aliases": ["目标温度"]},
        ],
    }
    result = EngineeringContextResolver.resolve("读取目标温度", context)
    assert result["status"] == "resolved"
    assert result["device"] == "TMM"
    assert result["name"] == "TmmTarget"


def test_resolver_uses_device_name_in_request_to_disambiguate_alias() -> None:
    context = {
        "objects": [
            {"device": "VCU", "name": "VcuTarget", "aliases": ["目标温度"]},
            {"device": "TMM", "name": "TmmTarget", "aliases": ["目标温度"]},
        ],
    }
    result = EngineeringContextResolver.resolve("读取 VCU 目标温度", context)
    assert result["status"] == "resolved"
    assert result["device"] == "VCU"


def test_resolver_rejects_context_range_violation() -> None:
    result = EngineeringContextResolver.resolve(
        "把目标压力设置为 3 bar",
        {
            "default_device": "VCU",
            "objects": [
                {
                    "name": "PressureTarget",
                    "aliases": ["目标压力"],
                    "unit": "kPa",
                    "minimum": 50,
                    "maximum": 250,
                }
            ],
        },
    )
    assert result["status"] == "target_out_of_range"
    assert result["target_value"] == pytest.approx(300.0)


def test_context_validation_reports_duplicate_alias_as_warning() -> None:
    result = EngineeringContextResolver.validate(
        {
            "objects": [
                {"device": "VCU", "name": "TargetA", "aliases": ["目标值"]},
                {"device": "BMS", "name": "TargetB", "aliases": ["目标值"]},
            ]
        }
    )
    assert result["passed"]
    assert len(result["warnings"]) == 1


def test_context_validation_rejects_invalid_range_and_unit() -> None:
    result = EngineeringContextResolver.validate(
        {
            "objects": [
                {
                    "device": "VCU",
                    "name": "Invalid",
                    "unit": "unsupported-unit",
                    "minimum": 10,
                    "maximum": 1,
                }
            ]
        }
    )
    assert not result["passed"]
    assert len(result["errors"]) == 2


def test_context_validate_and_ai_plan_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "context.json"
    path.write_text(
        json.dumps(
            {
                "default_device": "TMM",
                "objects": [
                    {
                        "name": "CoolantTarget",
                        "aliases": ["目标水温"],
                        "unit": "°C",
                        "minimum": 40,
                        "maximum": 120,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["context-validate", str(path)]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["object_count"] == 1

    assert main(
        [
            "ai-plan",
            "把目标水温修改为 313.15 K",
            "--context-file",
            str(path),
            "--reason",
            "warm-up optimization",
        ]
    ) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["tool"] == "calibration_write"
    assert plan["arguments"]["device"] == "TMM"
    assert plan["arguments"]["value"] == pytest.approx(40.0)
    assert plan["arguments"]["reason"] == "warm-up optimization"
