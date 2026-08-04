"""MCP 多客户端会话、速率限制、跨进程资源租约与摘要审计。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import SafetyViolationError


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _validate_session_id(value: str) -> str:
    session_id = value.strip()
    if not session_id:
        return str(uuid.uuid4())
    if len(session_id) > 64 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", session_id):
        raise ValueError(
            "MCP session_id 只能包含字母、数字、点、下划线、冒号和连字符，最长64字符"
        )
    return session_id


@dataclass(frozen=True, slots=True)
class ResourceLease:
    resource: str
    session_id: str
    lease_id: str
    pid: int
    acquired_utc: str
    expires_utc: str
    path: str


class CrossProcessResourceLeaseManager:
    """使用独占锁文件保护 CANape 等非线程安全资源。"""

    def __init__(
        self,
        directory: str | Path,
        *,
        session_id: str,
        wait_timeout: float = 10.0,
        lease_seconds: float = 3600.0,
    ) -> None:
        if wait_timeout < 0:
            raise ValueError("wait_timeout 不能为负数")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于0")
        self.directory = Path(directory).expanduser().resolve()
        self.session_id = _validate_session_id(session_id)
        self.wait_timeout = float(wait_timeout)
        self.lease_seconds = float(lease_seconds)

    def _path(self, resource: str) -> Path:
        resource_hash = hashlib.sha256(resource.encode("utf-8")).hexdigest()[:20]
        return self.directory / f"{resource_hash}.lease"

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _is_stale(data: Mapping[str, Any]) -> bool:
        pid = data.get("pid")
        if isinstance(pid, int):
            return not _pid_exists(pid)
        try:
            return _utc_now() >= datetime.fromisoformat(str(data["expires_utc"]))
        except (KeyError, TypeError, ValueError):
            return True

    def _recover_stale(self, path: Path) -> bool:
        guard = path.with_suffix(path.suffix + ".recovery")
        descriptor: int | None = None
        try:
            descriptor = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            current = self._read(path)
            if path.exists() and self._is_stale(current):
                path.unlink(missing_ok=True)
                return True
            return False
        finally:
            os.close(descriptor)
            guard.unlink(missing_ok=True)

    @contextmanager
    def acquire(self, resource: str) -> Iterator[ResourceLease]:
        if not resource.strip():
            raise ValueError("resource 不能为空")
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(resource)
        deadline = time.monotonic() + self.wait_timeout
        descriptor: int | None = None
        lease_id = str(uuid.uuid4())
        acquired = _utc_now()
        lease = ResourceLease(
            resource=resource,
            session_id=self.session_id,
            lease_id=lease_id,
            pid=os.getpid(),
            acquired_utc=acquired.isoformat(),
            expires_utc=(acquired + timedelta(seconds=self.lease_seconds)).isoformat(),
            path=str(path),
        )
        while descriptor is None:
            try:
                descriptor = os.open(
                    path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                existing = self._read(path)
                if self._is_stale(existing) and self._recover_stale(path):
                    continue
                if time.monotonic() >= deadline:
                    owner = str(existing.get("session_id", "unknown"))
                    raise SafetyViolationError(
                        f"资源 {resource} 正被 MCP 会话 {owner} 占用"
                    ) from None
                time.sleep(0.02)
        try:
            os.write(descriptor, _canonical(asdict(lease)).encode("utf-8"))
            yield lease
        finally:
            os.close(descriptor)
            current = self._read(path)
            if current.get("lease_id") == lease_id:
                path.unlink(missing_ok=True)


class RollingRateLimiter:
    """单 MCP 会话滚动时间窗速率限制。"""

    def __init__(self, limit: int = 120, *, window_seconds: float = 60.0) -> None:
        if limit < 0:
            raise ValueError("rate limit 不能为负数")
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须大于0")
        self.limit = int(limit)
        self.window_seconds = float(window_seconds)
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def check(self) -> int:
        with self._lock:
            now = time.monotonic()
            threshold = now - self.window_seconds
            while self._calls and self._calls[0] <= threshold:
                self._calls.popleft()
            if self.limit and len(self._calls) >= self.limit:
                raise SafetyViolationError(
                    f"MCP 调用超过速率限制：{self.limit}/{self.window_seconds:g}s"
                )
            self._calls.append(now)
            return len(self._calls)

    def current(self) -> int:
        with self._lock:
            now = time.monotonic()
            threshold = now - self.window_seconds
            while self._calls and self._calls[0] <= threshold:
                self._calls.popleft()
            return len(self._calls)


class MCPAuditJournal:
    """跨进程追加 JSONL 审计；只写摘要，不写参数原文。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._thread_lock = threading.Lock()

    @contextmanager
    def _process_lock(self, timeout: float = 5.0) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                try:
                    if time.time() - self.lock_path.stat().st_mtime > 30.0:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("等待 MCP 审计日志锁超时") from None
                time.sleep(0.02)
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(descriptor)
            self.lock_path.unlink(missing_ok=True)

    def append(self, record: Mapping[str, Any]) -> None:
        line = _canonical(dict(record)) + "\n"
        with self._thread_lock, self._process_lock():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())


class MCPRuntimeGovernor:
    """为一个 MCP Server 进程提供会话隔离与调用治理。"""

    def __init__(
        self,
        *,
        session_id: str = "",
        lock_directory: str | Path | None = None,
        audit_file: str | Path | None = None,
        rate_limit: int = 120,
        lock_timeout: float = 10.0,
        lease_seconds: float = 3600.0,
    ) -> None:
        self.session_id = _validate_session_id(session_id)
        base = Path.home() / ".agent2canape"
        self.leases = CrossProcessResourceLeaseManager(
            lock_directory or base / "mcp-locks",
            session_id=self.session_id,
            wait_timeout=lock_timeout,
            lease_seconds=lease_seconds,
        )
        self.audit = MCPAuditJournal(audit_file or base / "mcp-audit.jsonl")
        self.rate_limiter = RollingRateLimiter(rate_limit)
        self.created_utc = _utc_now().isoformat()
        self._state_lock = threading.Lock()
        self._active_calls = 0
        self._completed_calls = 0
        self._failed_calls = 0
        self._rejected_calls = 0

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "session_id": self.session_id,
                "pid": os.getpid(),
                "created_utc": self.created_utc,
                "active_calls": self._active_calls,
                "completed_calls": self._completed_calls,
                "failed_calls": self._failed_calls,
                "rejected_calls": self._rejected_calls,
                "calls_in_rate_window": self.rate_limiter.current(),
                "rate_limit": self.rate_limiter.limit,
                "rate_window_seconds": self.rate_limiter.window_seconds,
                "lock_directory": str(self.leases.directory),
                "audit_file": str(self.audit.path),
            }

    def execute(
        self,
        tool: str,
        arguments: Mapping[str, Any],
        callback: Callable[[], dict[str, Any]],
        *,
        resource: str = "",
    ) -> dict[str, Any]:
        call_id = str(uuid.uuid4())
        started = _utc_now()
        start_clock = time.monotonic()
        status = "failed"
        error_type = ""
        result: dict[str, Any] | None = None
        active = False
        try:
            try:
                self.rate_limiter.check()
            except SafetyViolationError:
                status = "rejected"
                raise
            with self._state_lock:
                self._active_calls += 1
            active = True
            if resource:
                with self.leases.acquire(resource):
                    result = callback()
            else:
                result = callback()
            status = "passed"
            return {
                **result,
                "_runtime": {
                    "session_id": self.session_id,
                    "call_id": call_id,
                    "resource": resource or None,
                },
            }
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            finished = _utc_now()
            with self._state_lock:
                if active:
                    self._active_calls -= 1
                if status == "passed":
                    self._completed_calls += 1
                elif status == "rejected":
                    self._rejected_calls += 1
                else:
                    self._failed_calls += 1
            self.audit.append(
                {
                    "schema_version": 1,
                    "call_id": call_id,
                    "session_id": self.session_id,
                    "pid": os.getpid(),
                    "tool": tool,
                    "resource": resource,
                    "status": status,
                    "error_type": error_type,
                    "started_utc": started.isoformat(),
                    "finished_utc": finished.isoformat(),
                    "duration_seconds": time.monotonic() - start_clock,
                    "argument_digest": _digest(dict(arguments)),
                    "result_digest": _digest(result) if result is not None else "",
                }
            )
