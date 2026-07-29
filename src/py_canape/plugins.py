"""领域包和企业扩展的稳定插件协议。"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


class DomainAdapter(Protocol):
    name: str
    version: str

    def signal_aliases(self) -> Mapping[str, str]: ...

    def rules(self) -> Mapping[str, Mapping[str, Any]]: ...

    def metrics(self) -> Mapping[str, Callable[..., Any]]: ...


@dataclass(slots=True)
class BasicDomainAdapter:
    name: str
    version: str = "1.0"
    aliases: dict[str, str] = field(default_factory=dict)
    quality_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    metric_functions: dict[str, Callable[..., Any]] = field(default_factory=dict)

    def signal_aliases(self) -> Mapping[str, str]:
        return self.aliases

    def rules(self) -> Mapping[str, Mapping[str, Any]]:
        return self.quality_rules

    def metrics(self) -> Mapping[str, Callable[..., Any]]:
        return self.metric_functions


class PluginRegistry:
    ENTRY_POINT_GROUP = "py_canape.domain_adapters"

    def __init__(self) -> None:
        self.adapters: dict[str, DomainAdapter] = {}

    def register(self, adapter: DomainAdapter) -> None:
        key = adapter.name.casefold()
        if key in self.adapters:
            raise ValueError(f"领域适配器重复：{adapter.name}")
        self.adapters[key] = adapter

    def discover(self) -> list[str]:
        loaded = []
        entry_points = importlib.metadata.entry_points()
        for entry in entry_points.select(group=self.ENTRY_POINT_GROUP):
            adapter = entry.load()()
            self.register(adapter)
            loaded.append(adapter.name)
        return loaded

    def get(self, name: str) -> DomainAdapter:
        return self.adapters[name.casefold()]

    def compatibility(self, name: str, minimum_version: str) -> bool:
        from packaging.version import Version

        return Version(self.get(name).version) >= Version(minimum_version)


def built_in_domains() -> PluginRegistry:
    registry = PluginRegistry()
    for name in (
        "powertrain",
        "chassis",
        "body",
        "electric_powertrain",
        "thermal_hvac",
        "adas",
        "network_diagnostics",
    ):
        registry.register(BasicDomainAdapter(name=name))
    return registry
