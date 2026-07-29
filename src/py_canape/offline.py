"""离线工程数据读取、统一信号字典、重采样和时间对齐。"""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .errors import OptionalDependencyError


@dataclass(frozen=True, slots=True)
class SignalDefinition:
    name: str
    source: str
    unit: str = ""
    data_type: str = ""
    minimum: float | None = None
    maximum: float | None = None
    address: int | None = None
    message: str = ""
    channel: str = ""
    aliases: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class SignalDictionary:
    def __init__(self) -> None:
        self._signals: dict[str, SignalDefinition] = {}
        self._aliases: dict[str, str] = {}

    def add(self, definition: SignalDefinition, *, replace: bool = False) -> None:
        key = definition.name.casefold()
        if key in self._signals and not replace:
            raise ValueError(f"信号重复：{definition.name}")
        self._signals[key] = definition
        self._aliases[key] = key
        for alias in definition.aliases:
            alias_key = alias.casefold()
            existing = self._aliases.get(alias_key)
            if existing is not None and existing != key:
                raise ValueError(f"别名冲突：{alias}")
            self._aliases[alias_key] = key

    def get(self, name: str) -> SignalDefinition:
        canonical = self._aliases.get(name.casefold())
        if canonical is None:
            raise KeyError(name)
        return self._signals[canonical]

    def merge(self, definitions: Iterable[SignalDefinition]) -> list[str]:
        conflicts: list[str] = []
        for definition in definitions:
            try:
                self.add(definition)
            except ValueError:
                conflicts.append(definition.name)
        return conflicts

    def list(self) -> list[SignalDefinition]:
        return sorted(self._signals.values(), key=lambda item: item.name.casefold())

    def validate(self) -> dict[str, Any]:
        issues: list[str] = []
        units: dict[str, set[str]] = {}
        for signal in self._signals.values():
            units.setdefault(signal.name.casefold(), set()).add(signal.unit)
            if (
                signal.minimum is not None
                and signal.maximum is not None
                and signal.minimum > signal.maximum
            ):
                issues.append(f"{signal.name}: minimum > maximum")
        for name, values in units.items():
            normalized = {value for value in values if value}
            if len(normalized) > 1:
                issues.append(f"{name}: 单位冲突 {sorted(normalized)}")
        return {"passed": not issues, "issues": issues}

    def to_json(self, path: str | Path) -> None:
        output = Path(path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps([asdict(item) for item in self.list()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class OfflineData:
    """统一表格式数据接口；时间列默认命名为 ``time``。"""

    @staticmethod
    def _pandas() -> Any:
        try:
            return importlib.import_module("pandas")
        except ImportError as exc:
            raise OptionalDependencyError(
                "表格与时序能力需要安装 py-canape-local[data]"
            ) from exc

    def read_table(self, path: str | Path, **kwargs: Any) -> Any:
        pd = self._pandas()
        source = Path(path).expanduser().resolve()
        suffix = source.suffix.casefold()
        if suffix == ".csv":
            return pd.read_csv(source, **kwargs)
        if suffix in {".json", ".jsonl"}:
            return pd.read_json(source, lines=suffix == ".jsonl", **kwargs)
        if suffix in {".parquet", ".pq"}:
            return pd.read_parquet(source, **kwargs)
        if suffix in {".xlsx", ".xlsm"}:
            return pd.read_excel(source, **kwargs)
        raise ValueError(f"不支持的表格格式：{suffix}")

    def write_table(self, frame: Any, path: str | Path, **kwargs: Any) -> Path:
        output = Path(path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        suffix = output.suffix.casefold()
        if suffix == ".csv":
            frame.to_csv(output, index=False, **kwargs)
        elif suffix in {".parquet", ".pq"}:
            frame.to_parquet(output, index=False, **kwargs)
        elif suffix in {".xlsx", ".xlsm"}:
            frame.to_excel(output, index=False, **kwargs)
        elif suffix == ".json":
            frame.to_json(output, orient="records", force_ascii=False, **kwargs)
        else:
            raise ValueError(f"不支持的输出格式：{suffix}")
        return output

    def read_mdf(
        self,
        path: str | Path,
        *,
        channels: Sequence[str] | None = None,
        raster: float | None = None,
    ) -> Any:
        try:
            asammdf = importlib.import_module("asammdf")
        except ImportError as exc:
            raise OptionalDependencyError(
                "MDF/MF4 需要安装 py-canape-local[vector-files]"
            ) from exc
        with asammdf.MDF(str(Path(path).expanduser().resolve())) as mdf:
            return mdf.to_dataframe(
                channels=channels, raster=raster, time_from_zero=True
            )

    def mdf_metadata(self, path: str | Path) -> dict[str, Any]:
        try:
            asammdf = importlib.import_module("asammdf")
        except ImportError as exc:
            raise OptionalDependencyError(
                "MDF/MF4 需要安装 py-canape-local[vector-files]"
            ) from exc
        source = Path(path).expanduser().resolve()
        with asammdf.MDF(str(source)) as mdf:
            channels = sorted({name for name in mdf.channels_db})
            return {
                "path": str(source),
                "version": mdf.version,
                "channel_count": len(channels),
                "channels": channels,
                "start_time": str(mdf.header.start_time),
            }

    def read_blf(self, path: str | Path, *, channel: int | None = None) -> Any:
        try:
            can = importlib.import_module("can")
        except ImportError as exc:
            raise OptionalDependencyError(
                "BLF 需要安装 py-canape-local[vector-files]"
            ) from exc
        pd = self._pandas()
        rows = []
        with can.BLFReader(str(Path(path).expanduser().resolve())) as reader:
            for message in reader:
                if channel is not None and message.channel != channel:
                    continue
                rows.append(
                    {
                        "time": float(message.timestamp),
                        "channel": message.channel,
                        "arbitration_id": int(message.arbitration_id),
                        "is_extended_id": bool(message.is_extended_id),
                        "data": bytes(message.data),
                        "dlc": int(message.dlc),
                    }
                )
        return pd.DataFrame(rows)

    def blf_metadata(self, path: str | Path) -> dict[str, Any]:
        frame = self.read_blf(path)
        if frame.empty:
            return {"frame_count": 0, "channels": [], "ids": [], "time_range": None}
        return {
            "frame_count": int(len(frame)),
            "channels": sorted(frame["channel"].dropna().unique().tolist()),
            "ids": sorted(int(value) for value in frame["arbitration_id"].unique()),
            "time_range": [float(frame["time"].min()), float(frame["time"].max())],
        }

    def decode_blf(self, frame: Any, dbc_file: str | Path) -> Any:
        try:
            cantools = importlib.import_module("cantools")
        except ImportError as exc:
            raise OptionalDependencyError(
                "DBC 解码需要安装 py-canape-local[vector-files]"
            ) from exc
        pd = self._pandas()
        database = cantools.database.load_file(str(Path(dbc_file).resolve()))
        rows = []
        for row in frame.itertuples(index=False):
            try:
                decoded = database.decode_message(
                    int(row.arbitration_id), bytes(row.data), decode_choices=True
                )
            except (KeyError, ValueError):
                continue
            rows.append({"time": row.time, "channel": row.channel, **decoded})
        return pd.DataFrame(rows)

    @staticmethod
    def parse_a2l(path: str | Path) -> list[SignalDefinition]:
        source = Path(path).expanduser().resolve()
        text = source.read_text(encoding="latin-1", errors="replace")
        definitions: list[SignalDefinition] = []
        pattern = re.compile(
            r"/begin\s+(MEASUREMENT|CHARACTERISTIC)\s+(\S+)(.*?)/end\s+\1",
            re.IGNORECASE | re.DOTALL,
        )
        for kind, name, body in pattern.findall(text):
            address_match = re.search(r"\b0x([0-9A-Fa-f]+)\b", body)
            range_match = re.findall(
                r"(?<![\w.])[-+]?(?:\d+\.\d+|\d+)(?:[Ee][-+]?\d+)?", body
            )
            minimum = maximum = None
            if len(range_match) >= 2:
                with suppress(ValueError):
                    minimum, maximum = map(float, range_match[-2:])
            definitions.append(
                SignalDefinition(
                    name=name,
                    source=str(source),
                    data_type=kind.casefold(),
                    minimum=minimum,
                    maximum=maximum,
                    address=(
                        int(address_match.group(1), 16) if address_match else None
                    ),
                )
            )
        return definitions

    @staticmethod
    def parse_dbc(path: str | Path) -> list[SignalDefinition]:
        source = Path(path).expanduser().resolve()
        definitions: list[SignalDefinition] = []
        current_message = ""
        message_pattern = re.compile(r"^BO_\s+\d+\s+(\S+):")
        signal_pattern = re.compile(
            r'^\s*SG_\s+(\S+).*?\[([-+\d.eE]+)\|([-+\d.eE]+)\]\s+"([^"]*)"'
        )
        for line in source.read_text(
            encoding="latin-1", errors="replace"
        ).splitlines():
            message_match = message_pattern.match(line)
            if message_match:
                current_message = message_match.group(1)
                continue
            signal_match = signal_pattern.match(line)
            if signal_match:
                definitions.append(
                    SignalDefinition(
                        name=signal_match.group(1),
                        source=str(source),
                        unit=signal_match.group(4),
                        minimum=float(signal_match.group(2)),
                        maximum=float(signal_match.group(3)),
                        message=current_message,
                    )
                )
        return definitions

    @staticmethod
    def compare_a2l_symbols(
        definitions: Sequence[SignalDefinition],
        software_symbols: Iterable[str],
    ) -> dict[str, Any]:
        a2l_names = {item.name for item in definitions}
        symbols = set(software_symbols)
        return {
            "passed": a2l_names == symbols,
            "missing_in_software": sorted(a2l_names - symbols),
            "missing_in_a2l": sorted(symbols - a2l_names),
        }

    def resample(
        self,
        frame: Any,
        period: str | float,
        *,
        time_column: str = "time",
        method: str = "linear",
    ) -> Any:
        pd = self._pandas()
        data = frame.copy()
        if isinstance(period, (int, float)):
            period = f"{float(period)}s"
        data[time_column] = pd.to_timedelta(data[time_column], unit="s")
        data = data.set_index(time_column).sort_index()
        result = data.resample(period).mean(numeric_only=True)
        if method == "linear":
            result = result.interpolate(method="linear")
        elif method == "ffill":
            result = result.ffill()
        else:
            raise ValueError(f"未知重采样方法：{method}")
        result.index = result.index.total_seconds()
        return result.reset_index()

    def align(
        self,
        left: Any,
        right: Any,
        *,
        time_column: str = "time",
        tolerance: float = 0.05,
        suffixes: tuple[str, str] = ("_left", "_right"),
    ) -> Any:
        pd = self._pandas()
        left_data = left.sort_values(time_column)
        right_data = right.sort_values(time_column)
        return pd.merge_asof(
            left_data,
            right_data,
            on=time_column,
            tolerance=tolerance,
            direction="nearest",
            suffixes=suffixes,
        )

    @staticmethod
    def check_channel_mapping(
        frames: Any, expected: Mapping[int, Iterable[int]]
    ) -> dict[str, Any]:
        missing: dict[int, list[int]] = {}
        for channel, ids in expected.items():
            actual_ids = set(
                frames.loc[frames["channel"] == channel, "arbitration_id"].tolist()
            )
            absent = sorted(set(ids) - actual_ids)
            if absent:
                missing[channel] = absent
        return {"passed": not missing, "missing": missing}
