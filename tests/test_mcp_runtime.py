from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from agent2canape import SafetyViolationError
from agent2canape.mcp_runtime import (
    CrossProcessResourceLeaseManager,
    MCPRuntimeGovernor,
    RollingRateLimiter,
)


def test_rate_limiter_rejects_calls_above_window_limit() -> None:
    limiter = RollingRateLimiter(2, window_seconds=60)
    assert limiter.check() == 1
    assert limiter.check() == 2
    with pytest.raises(SafetyViolationError, match="速率限制"):
        limiter.check()


def test_cross_process_lease_rejects_another_session() -> None:
    with tempfile.TemporaryDirectory() as directory:
        first = CrossProcessResourceLeaseManager(
            directory,
            session_id="codex-one",
            wait_timeout=0,
        )
        second = CrossProcessResourceLeaseManager(
            directory,
            session_id="claude-two",
            wait_timeout=0,
        )
        with first.acquire("canape-com") as lease:
            assert Path(lease.path).is_file()
            with (
                pytest.raises(SafetyViolationError, match="codex-one"),
                second.acquire("canape-com"),
            ):
                pass
        assert not Path(lease.path).exists()


def test_cross_process_lease_recovers_dead_owner() -> None:
    with tempfile.TemporaryDirectory() as directory:
        manager = CrossProcessResourceLeaseManager(
            directory,
            session_id="new-session",
            wait_timeout=0,
        )
        path = manager._path("canape-com")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "pid": max(os.getpid() + 10_000_000, 99_999_999),
                    "session_id": "dead-session",
                    "expires_utc": "2999-01-01T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        with manager.acquire("canape-com") as lease:
            assert lease.session_id == "new-session"


def test_live_process_lease_is_not_stolen_after_expiry() -> None:
    with tempfile.TemporaryDirectory() as directory:
        first = CrossProcessResourceLeaseManager(
            directory,
            session_id="long-call",
            wait_timeout=0,
            lease_seconds=0.01,
        )
        second = CrossProcessResourceLeaseManager(
            directory,
            session_id="other-call",
            wait_timeout=0,
        )
        with first.acquire("canape-com"):
            time.sleep(0.02)
            with (
                pytest.raises(SafetyViolationError, match="long-call"),
                second.acquire("canape-com"),
            ):
                pass


def test_runtime_governor_adds_session_metadata_and_redacted_audit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        audit = root / "audit.jsonl"
        governor = MCPRuntimeGovernor(
            session_id="codex-calibration",
            lock_directory=root / "locks",
            audit_file=audit,
            rate_limit=10,
            lock_timeout=0,
        )
        result = governor.execute(
            "diagnostic_named",
            {"device": "ECU", "secret_parameter": "must-not-leak"},
            lambda: {"status": "ok"},
            resource="canape-com",
        )
        assert result["_runtime"]["session_id"] == "codex-calibration"
        assert result["_runtime"]["resource"] == "canape-com"
        content = audit.read_text(encoding="utf-8")
        assert "must-not-leak" not in content
        record = json.loads(content)
        assert record["tool"] == "diagnostic_named"
        assert len(record["argument_digest"]) == 64
        assert record["status"] == "passed"
        status = governor.status()
        assert status["completed_calls"] == 1
        assert status["failed_calls"] == 0


def test_runtime_governor_audits_handler_failure_without_error_text() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        audit = root / "audit.jsonl"
        governor = MCPRuntimeGovernor(
            session_id="failure-test",
            lock_directory=root / "locks",
            audit_file=audit,
        )

        def fail() -> dict[str, object]:
            raise RuntimeError("sensitive failure details")

        with pytest.raises(RuntimeError, match="sensitive failure details"):
            governor.execute("calibration_read", {"name": "Gain"}, fail)
        content = audit.read_text(encoding="utf-8")
        assert "sensitive failure details" not in content
        record = json.loads(content)
        assert record["status"] == "failed"
        assert record["error_type"] == "RuntimeError"


def test_runtime_rejects_unsafe_session_identifier() -> None:
    with pytest.raises(ValueError, match="session_id"):
        MCPRuntimeGovernor(session_id="../../unsafe")


def test_runtime_governor_audits_rate_limit_rejection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        audit = root / "audit.jsonl"
        governor = MCPRuntimeGovernor(
            session_id="rate-test",
            lock_directory=root / "locks",
            audit_file=audit,
            rate_limit=1,
        )
        governor.execute("first", {}, lambda: {"status": "ok"})
        with pytest.raises(SafetyViolationError, match="速率限制"):
            governor.execute("second", {"secret": "do-not-log"}, lambda: {})

        records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        assert [record["status"] for record in records] == ["passed", "rejected"]
        assert "do-not-log" not in audit.read_text(encoding="utf-8")
        status = governor.status()
        assert status["active_calls"] == 0
        assert status["rejected_calls"] == 1
