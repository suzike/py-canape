"""工程资产、环境预检、版本追踪和快照恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import AssetValidationError


@dataclass(frozen=True, slots=True)
class AssetRecord:
    path: str
    kind: str
    size: int
    modified_utc: str
    sha256: str


@dataclass(slots=True)
class PreflightResult:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def require_passed(self) -> None:
        if not self.passed:
            raise AssetValidationError("; ".join(self.errors) or "工程预检失败")


class AssetManager:
    """管理工程输入、运行环境和可恢复快照。"""

    KNOWN_SUFFIXES = {
        ".a2l": "a2l",
        ".dbc": "dbc",
        ".odx": "odx",
        ".pdx": "pdx",
        ".cdd": "cdd",
        ".cna": "cna",
        ".cnaxml": "cna",
        ".hex": "hex",
        ".s19": "hex",
        ".srec": "hex",
        ".par": "calibration",
        ".cdfx": "calibration",
        ".cns": "script",
        ".blf": "blf",
        ".mf4": "mdf",
        ".mdf": "mdf",
        ".csv": "table",
        ".parquet": "table",
    }

    @staticmethod
    def sha256(path: str | os.PathLike[str]) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def inventory(
        self,
        roots: Iterable[str | os.PathLike[str]],
        *,
        recursive: bool = True,
        exclude_names: Iterable[str] = (
            ".git",
            ".venv",
            ".pytest_cache",
            "__pycache__",
            "build",
            "dist",
        ),
    ) -> list[AssetRecord]:
        records: list[AssetRecord] = []
        excluded = set(exclude_names)
        for root_value in roots:
            root = Path(root_value).expanduser().resolve()
            if not root.exists():
                raise FileNotFoundError(root)
            candidates = (
                root.rglob("*") if root.is_dir() and recursive else root.glob("*")
            )
            if root.is_file():
                candidates = (root,)
            for path in candidates:
                if not path.is_file():
                    continue
                if any(part in excluded for part in path.parts):
                    continue
                stat = path.stat()
                records.append(
                    AssetRecord(
                        path=str(path),
                        kind=self.KNOWN_SUFFIXES.get(
                            path.suffix.casefold(), "other"
                        ),
                        size=stat.st_size,
                        modified_utc=datetime.fromtimestamp(
                            stat.st_mtime, tz=timezone.utc
                        ).isoformat(),
                        sha256=self.sha256(path),
                    )
                )
        return sorted(records, key=lambda item: item.path.casefold())

    @staticmethod
    def environment_inventory() -> dict[str, Any]:
        commands = {}
        for name, command in {
            "python": [sys.executable, "--version"],
            "pip": [sys.executable, "-m", "pip", "--version"],
        }.items():
            result = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
            commands[name] = (result.stdout or result.stderr).strip()
        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "commands": commands,
        }

    def preflight(
        self,
        *,
        required_paths: Iterable[str | os.PathLike[str]] = (),
        output_directory: str | os.PathLike[str] | None = None,
        minimum_free_bytes: int = 0,
        required_suffixes: Iterable[str] = (),
    ) -> PreflightResult:
        result = PreflightResult(passed=True)
        resolved = [Path(value).expanduser().resolve() for value in required_paths]
        for path in resolved:
            key = f"exists:{path}"
            result.checks[key] = path.exists()
            if not path.exists():
                result.errors.append(f"缺少必需路径：{path}")
        suffixes = {suffix.casefold() for suffix in required_suffixes}
        if suffixes:
            found = {path.suffix.casefold() for path in resolved if path.is_file()}
            for suffix in suffixes:
                result.checks[f"suffix:{suffix}"] = suffix in found
                if suffix not in found:
                    result.errors.append(f"缺少 {suffix} 工程资产")
        if output_directory is not None:
            output = Path(output_directory).expanduser().resolve()
            output.mkdir(parents=True, exist_ok=True)
            try:
                with tempfile.NamedTemporaryFile(dir=output, delete=True):
                    pass
                writable = True
            except OSError:
                writable = False
            free = shutil.disk_usage(output).free
            result.checks["output_writable"] = writable
            result.checks["free_space"] = free >= minimum_free_bytes
            result.details["free_bytes"] = free
            if not writable:
                result.errors.append(f"输出目录不可写：{output}")
            if free < minimum_free_bytes:
                result.errors.append(
                    f"磁盘空间不足：{free} < {minimum_free_bytes}"
                )
        result.passed = not result.errors
        return result

    def create_manifest(
        self,
        roots: Iterable[str | os.PathLike[str]],
        output_file: str | os.PathLike[str],
    ) -> dict[str, Any]:
        manifest = {
            "schema": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "environment": self.environment_inventory(),
            "assets": [asdict(item) for item in self.inventory(roots)],
        }
        output = Path(output_file).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    @staticmethod
    def compare_manifests(
        before: dict[str, Any], after: dict[str, Any]
    ) -> dict[str, list[str]]:
        old = {item["path"]: item["sha256"] for item in before.get("assets", [])}
        new = {item["path"]: item["sha256"] for item in after.get("assets", [])}
        return {
            "added": sorted(set(new) - set(old)),
            "removed": sorted(set(old) - set(new)),
            "changed": sorted(
                path for path in set(old) & set(new) if old[path] != new[path]
            ),
        }

    def snapshot(
        self,
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
    ) -> Path:
        source_path = Path(source).expanduser().resolve()
        destination_path = Path(destination).expanduser().resolve()
        if not source_path.is_dir():
            raise NotADirectoryError(source_path)
        if destination_path.exists():
            raise FileExistsError(destination_path)
        shutil.copytree(
            source_path,
            destination_path,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        self.create_manifest([destination_path], destination_path / "manifest.json")
        return destination_path

    def restore(
        self,
        snapshot: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        overwrite: bool = False,
    ) -> Path:
        snapshot_path = Path(snapshot).expanduser().resolve()
        destination_path = Path(destination).expanduser().resolve()
        if destination_path.exists() and not overwrite:
            raise FileExistsError(destination_path)
        if destination_path.exists():
            shutil.rmtree(destination_path)
        shutil.copytree(
            snapshot_path,
            destination_path,
            ignore=shutil.ignore_patterns("manifest.json"),
        )
        return destination_path

    @staticmethod
    def validate_topology(
        expected: dict[str, dict[str, Any]],
        actual: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        missing = sorted(set(expected) - set(actual))
        mismatches: dict[str, dict[str, Any]] = {}
        for name in set(expected) & set(actual):
            differences = {
                key: {"expected": value, "actual": actual[name].get(key)}
                for key, value in expected[name].items()
                if actual[name].get(key) != value
            }
            if differences:
                mismatches[name] = differences
        return {
            "passed": not missing and not mismatches,
            "missing": missing,
            "mismatches": mismatches,
        }
