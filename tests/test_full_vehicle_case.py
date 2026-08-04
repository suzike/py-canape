from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

from examples.hvac_comfort_full_case import run_case


def test_hvac_comfort_full_case_generates_complete_evidence(tmp_path: Path):
    result = run_case(tmp_path / "hvac-case")

    assert result["passed"] is True
    assert result["stage_count"] == 12
    assert result["passed_stage_count"] == 12
    assert result["execution_mode"]["canape_ecu_actions"] == "simulated"
    assert result["stages"]["measurement"]["result"][
        "failure_injection_rollback"
    ]
    assert result["stages"]["ai_action_plan"]["result"][
        "single_use_rejected"
    ]
    assert result["stages"]["diagnostic"]["result"]["passed"]
    assert result["stages"]["dtc_evidence"]["result"]["diff"]["removed"][0][
        "code"
    ] == "123456"
    assert (
        result["stages"]["signal_analysis"]["result"]["candidate_control"][
            "rmse"
        ]
        < result["stages"]["signal_analysis"]["result"]["baseline_control"][
            "rmse"
        ]
    )

    artifacts = {name: Path(path) for name, path in result["artifacts"].items()}
    assert all(path.is_file() for path in artifacts.values())
    saved = json.loads(artifacts["result"].read_text(encoding="utf-8"))
    assert saved["passed"] is True
    assert saved["artifacts"]["evidence_bundle"] == str(
        artifacts["evidence_bundle"]
    )
    asset_manifest = json.loads(
        artifacts["asset_manifest"].read_text(encoding="utf-8")
    )
    result_asset = next(
        item
        for item in asset_manifest["assets"]
        if Path(item["path"]).name == "full-case-result.json"
    )
    assert result_asset["sha256"] == hashlib.sha256(
        artifacts["result"].read_bytes()
    ).hexdigest()
    with zipfile.ZipFile(artifacts["evidence_bundle"]) as archive:
        names = set(archive.namelist())
        assert "manifest.json" in names
        assert "evidence/full-case-result.json" in names
        assert "evidence/full-case-report.html" in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["metadata"]["simulated_dangerous_actions"] is True
        for item in manifest["files"]:
            payload = archive.read(f"evidence/{item['name']}")
            assert len(payload) == item["size"]
            assert hashlib.sha256(payload).hexdigest() == item["sha256"]
