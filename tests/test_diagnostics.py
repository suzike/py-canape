from types import SimpleNamespace

import pytest

from agent2canape.cli import main as cli_main
from agent2canape.diagnostics import (
    DiagnosticManifest,
    DiagnosticSequenceRunner,
    DTCSnapshot,
    UDSResponse,
    interpret_nrc,
)
from agent2canape.errors import SafetyViolationError


def response(stream, *, positive=True, sender="ECU", response_code=0):
    return SimpleNamespace(
        stream=tuple(stream),
        positive=positive,
        sender=sender,
        response_code=response_code,
    )


class FakeCANape:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def set_tester_present(self, device, *, enabled):
        self.calls.append(("tester_present", device, enabled))
        return int(enabled)

    def send_raw_diagnostic_request(self, device, payload, *, timeout):
        self.calls.append(("raw", device, tuple(payload), timeout))
        return self.responses.pop(0)

    def send_diagnostic_request(self, device, service, *, parameters, timeout):
        self.calls.append(("named", device, service, parameters, timeout))
        return self.responses.pop(0)


def manifest(**overrides):
    value = {
        "name": "ecu-health",
        "default_device": "ECU",
        "p2_timeout_seconds": 0.1,
        "p2_star_timeout_seconds": 3.0,
        "tester_present": True,
        "steps": [
            {
                "id": "extended-session",
                "payload": [0x10, 0x03],
                "transition_session": "extended",
            },
            {
                "id": "read-vin",
                "service": "ReadDataByIdentifier/VIN",
                "parameters": {"DID": 0xF190},
                "required_session": "extended",
            },
        ],
    }
    value.update(overrides)
    return DiagnosticManifest.from_mapping(value)


def test_nrc_interpretation_and_unknown_fallback():
    pending = interpret_nrc(0x78)
    assert pending.name == "responsePending"
    assert pending.retryable is True
    assert pending.category == "timing"
    assert interpret_nrc(0xFE).category == "unknown"


def test_uds_positive_and_negative_response_parsing():
    positive = UDSResponse.parse([0x62, 0xF1, 0x90, 0x31])
    assert positive.positive is True
    assert positive.request_service_id == 0x22
    negative = UDSResponse.parse([0x7F, 0x22, 0x31])
    assert negative.positive is False
    assert negative.request_service_id == 0x22
    assert negative.nrc.name == "requestOutOfRange"


def test_manifest_plan_exposes_state_timeout_and_nrc_semantics():
    spec = manifest(
        steps=[
            {
                "id": "expected-denial",
                "payload": [0x2E, 0xF1, 0x90, 0x00],
                "expected_positive": False,
                "allowed_nrc": ["0x33"],
                "required_session": "extended",
            }
        ]
    )
    plan = spec.plan()
    assert plan["passed"] is True
    assert plan["steps"][0]["allowed_nrc"][0]["name"] == "securityAccessDenied"
    assert plan["steps"][0]["p2_star_timeout_seconds"] == 3.0
    assert len(plan["digest"]) == 64


def test_manifest_rejects_ambiguous_request_and_invalid_session():
    spec = manifest(
        default_device="",
        steps=[
            {
                "id": "bad",
                "service": "Named",
                "payload": [0x22],
                "required_session": "factory",
                "required_security_level": 999,
            }
        ],
    )
    result = spec.validate()
    assert result["passed"] is False
    assert any("只能定义" in item for item in result["errors"])
    assert any("未知诊断会话" in item for item in result["errors"])
    assert any("安全等级" in item for item in result["errors"])
    with pytest.raises(SafetyViolationError):
        spec.require_valid()


def test_sequence_runner_tracks_session_and_tester_present_lifecycle():
    canape = FakeCANape(
        [
            [response([0x50, 0x03])],
            [response([0x62, 0xF1, 0x90, 0x31, 0x32, 0x33])],
        ]
    )
    report = DiagnosticSequenceRunner(canape).execute(manifest())
    assert report["passed"] is True
    assert report["steps"][1]["state_before"]["session"] == "extended"
    assert report["final_states"]["ECU"]["tester_present"] is False
    assert canape.calls[0] == ("tester_present", "ECU", True)
    assert canape.calls[-1] == ("tester_present", "ECU", False)
    assert canape.calls[1][-1] == 3.0


def test_sequence_runner_blocks_state_mismatch_before_sending():
    spec = manifest(
        tester_present=False,
        steps=[
            {
                "id": "secured-read",
                "payload": [0x22, 0xF1, 0x90],
                "required_security_level": 3,
            }
        ],
    )
    canape = FakeCANape([])
    report = DiagnosticSequenceRunner(canape).execute(spec)
    assert report["passed"] is False
    assert report["steps"][0]["executed"] is False
    assert canape.calls == []


def test_sequence_runner_accepts_explicit_expected_negative_response():
    spec = manifest(
        tester_present=False,
        steps=[
            {
                "id": "security-denial",
                "payload": [0x27, 0x01],
                "expected_positive": False,
                "allowed_nrc": [0x33],
            }
        ],
    )
    canape = FakeCANape([[response([0x7F, 0x27, 0x33], positive=False)]])
    report = DiagnosticSequenceRunner(canape).execute(spec)
    assert report["passed"] is True
    assert report["steps"][0]["responses"][0]["nrc"]["category"] == "security"


def test_sequence_runner_reports_transport_error_and_stops_tester_present():
    class FailingCANape(FakeCANape):
        def send_raw_diagnostic_request(self, device, payload, *, timeout):
            self.calls.append(("raw", device, tuple(payload), timeout))
            raise TimeoutError("P2* expired")

    canape = FailingCANape([])
    report = DiagnosticSequenceRunner(canape).execute(
        manifest(steps=[{"id": "read-vin", "payload": [0x22, 0xF1, 0x90]}])
    )
    assert report["passed"] is False
    assert "TimeoutError" in report["steps"][0]["errors"][0]
    assert canape.calls[-1] == ("tester_present", "ECU", False)


def test_sequence_runner_rejects_raw_positive_service_mismatch():
    canape = FakeCANape([[response([0x6E, 0xF1, 0x90])]])
    report = DiagnosticSequenceRunner(canape).execute(
        manifest(
            tester_present=False,
            steps=[{"id": "read-vin", "payload": [0x22, 0xF1, 0x90]}],
        )
    )
    assert report["passed"] is False
    assert "服务 ID" in report["steps"][0]["errors"][0]


def test_dtc_snapshot_parse_digest_and_diff():
    before = DTCSnapshot.parse_uds(
        [0x59, 0x02, 0xAF, 0x12, 0x34, 0x56, 0x09, 0xAB, 0xCD, 0xEF, 0x01],
        source="pre-flash",
    )
    after = DTCSnapshot.parse_uds(
        [0x59, 0x02, 0xAF, 0x12, 0x34, 0x56, 0x08, 0x01, 0x02, 0x03, 0x01],
        source="post-flash",
    )
    assert before.public()["count"] == 2
    assert before.records[0].code == "123456"
    delta = before.diff(after)
    assert delta["removed"][0]["code"] == "ABCDEF"
    assert delta["added"][0]["code"] == "010203"
    assert delta["status_changed"][0] == {
        "code": "123456",
        "before": 9,
        "after": 8,
    }
    assert len(before.digest()) == 64


def test_dtc_snapshot_rejects_malformed_payload():
    with pytest.raises(ValueError, match="0x59"):
        DTCSnapshot.parse_uds([0x7F, 0x19, 0x22])
    with pytest.raises(ValueError, match="记录区"):
        DTCSnapshot.parse_uds([0x59, 0x02, 0xFF, 0x01])


def test_diagnostic_cli_plan_and_dtc_decode(tmp_path, capsys):
    path = tmp_path / "diagnostic.yaml"
    path.write_text(
        """name: cli-diagnostic
default_device: ECU
steps:
  - {id: read-vin, payload: [0x22, 0xF1, 0x90]}
""",
        encoding="utf-8",
    )
    assert cli_main(["diagnostic-plan", str(path)]) == 0
    assert '"passed": true' in capsys.readouterr().out
    assert (
        cli_main(
            [
                "diagnostic-dtc-decode",
                "0x59",
                "0x02",
                "0xFF",
                "0x12",
                "0x34",
                "0x56",
                "0x09",
            ]
        )
        == 0
    )
    assert '"code": "123456"' in capsys.readouterr().out
