"""A2L 语义目录与常用标定数据交换格式。

该模块只处理离线文件，不连接 CANape，也不会执行 ECU 写入。
"""

from __future__ import annotations

import json
import re
import shlex
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .calibration import CalibrationDataset


_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?$")
_A2L_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _tokens(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _number(value: str | None) -> float | None:
    if value is None or not _NUMBER.fullmatch(value):
        return None
    return float(value)


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        return None


@dataclass(slots=True)
class _A2LBlock:
    kind: str
    lines: list[str] = field(default_factory=list)
    children: list[_A2LBlock] = field(default_factory=list)

    @property
    def tokens(self) -> list[str]:
        return _tokens("\n".join(self.lines))


def _parse_blocks(text: str) -> _A2LBlock:
    root = _A2LBlock("ROOT")
    stack = [root]
    for raw_line in _A2L_COMMENT.sub("", text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        begin = re.match(r"/begin\s+(\S+)(.*)", line, re.IGNORECASE)
        if begin:
            block = _A2LBlock(begin.group(1).upper())
            remainder = begin.group(2).strip()
            if remainder:
                block.lines.append(remainder)
            stack[-1].children.append(block)
            stack.append(block)
            continue
        end = re.match(r"/end\s+(\S+)", line, re.IGNORECASE)
        if end:
            if len(stack) == 1 or stack[-1].kind != end.group(1).upper():
                raise ValueError(f"A2L 块不匹配：{line}")
            stack.pop()
            continue
        stack[-1].lines.append(line)
    if len(stack) != 1:
        raise ValueError(f"A2L 块未闭合：{stack[-1].kind}")
    return root


def _walk(block: _A2LBlock, kind: str) -> list[_A2LBlock]:
    result: list[_A2LBlock] = []
    for child in block.children:
        if child.kind == kind:
            result.append(child)
        result.extend(_walk(child, kind))
    return result


def _statements(block: _A2LBlock) -> dict[str, list[list[str]]]:
    result: dict[str, list[list[str]]] = {}
    for line in block.lines:
        values = _tokens(line)
        if values:
            result.setdefault(values[0].upper(), []).append(values[1:])
    return result


@dataclass(frozen=True, slots=True)
class A2LCompuMethod:
    name: str
    long_identifier: str = ""
    conversion_type: str = ""
    format: str = ""
    unit: str = ""
    coefficients: tuple[float, ...] = ()
    table_ref: str = ""


@dataclass(frozen=True, slots=True)
class A2LRecordLayout:
    name: str
    fields: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class A2LAxisDescriptor:
    input_quantity: str = ""
    record_layout: str = ""
    conversion: str = ""
    max_points: int | None = None
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True, slots=True)
class A2LObject:
    name: str
    kind: str
    long_identifier: str = ""
    data_type: str = ""
    address: int | None = None
    record_layout: str = ""
    conversion: str = ""
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    byte_order: str = ""
    dimensions: tuple[int, ...] = ()
    axis_descriptors: tuple[A2LAxisDescriptor, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class A2LCatalog:
    source: str
    project: str = ""
    module: str = ""
    byte_order: str = ""
    compu_methods: dict[str, A2LCompuMethod] = field(default_factory=dict)
    record_layouts: dict[str, A2LRecordLayout] = field(default_factory=dict)
    objects: dict[str, A2LObject] = field(default_factory=dict)

    @classmethod
    def parse(cls, path: str | Path) -> A2LCatalog:
        source = Path(path).expanduser().resolve()
        text = source.read_text(encoding="latin-1", errors="replace")
        root = _parse_blocks(text)
        catalog = cls(source=str(source))
        project = _walk(root, "PROJECT")
        module = _walk(root, "MODULE")
        if project and project[0].tokens:
            catalog.project = project[0].tokens[0]
        if module and module[0].tokens:
            catalog.module = module[0].tokens[0]
        common = _walk(root, "MOD_COMMON")
        if common:
            byte_order = _statements(common[0]).get("BYTE_ORDER", [[]])[0]
            catalog.byte_order = byte_order[0] if byte_order else ""
        catalog._parse_compu_methods(root)
        catalog._parse_record_layouts(root)
        catalog._parse_objects(root)
        return catalog

    def _parse_compu_methods(self, root: _A2LBlock) -> None:
        for block in _walk(root, "COMPU_METHOD"):
            values = block.tokens
            if not values:
                continue
            statements = _statements(block)
            coefficients = statements.get("COEFFS", [])
            coeff_values = tuple(
                float(value)
                for value in (coefficients[0] if coefficients else [])
                if _NUMBER.fullmatch(value)
            )
            table = statements.get("COMPU_TAB_REF", [[]])[0]
            self.compu_methods[values[0]] = A2LCompuMethod(
                name=values[0],
                long_identifier=values[1] if len(values) > 1 else "",
                conversion_type=values[2] if len(values) > 2 else "",
                format=values[3] if len(values) > 3 else "",
                unit=values[4] if len(values) > 4 else "",
                coefficients=coeff_values,
                table_ref=table[0] if table else "",
            )

    def _parse_record_layouts(self, root: _A2LBlock) -> None:
        for block in _walk(root, "RECORD_LAYOUT"):
            values = block.tokens
            if not values:
                continue
            fields = {
                name: tuple(entries[0])
                for name, entries in _statements(block).items()
                if name != values[0].upper() and entries
            }
            self.record_layouts[values[0]] = A2LRecordLayout(values[0], fields)

    @staticmethod
    def _axis(block: _A2LBlock) -> A2LAxisDescriptor:
        values = block.tokens
        return A2LAxisDescriptor(
            input_quantity=values[1] if len(values) > 1 else "",
            record_layout=values[2] if len(values) > 2 else "",
            conversion=values[3] if len(values) > 3 else "",
            max_points=_integer(values[4] if len(values) > 4 else None),
            minimum=_number(values[5] if len(values) > 5 else None),
            maximum=_number(values[6] if len(values) > 6 else None),
        )

    def _parse_objects(self, root: _A2LBlock) -> None:
        for kind in ("MEASUREMENT", "CHARACTERISTIC", "AXIS_PTS", "BLOB"):
            for block in _walk(root, kind):
                values = block.tokens
                if not values:
                    continue
                statements = _statements(block)
                if kind == "MEASUREMENT":
                    data_type = values[2] if len(values) > 2 else ""
                    conversion = values[3] if len(values) > 3 else ""
                    address = _integer((statements.get("ECU_ADDRESS") or [[None]])[0][0])
                    minimum = _number(values[6] if len(values) > 6 else None)
                    maximum = _number(values[7] if len(values) > 7 else None)
                    record_layout = ""
                    object_metadata: dict[str, Any] = {}
                elif kind == "CHARACTERISTIC":
                    data_type = values[2] if len(values) > 2 else ""
                    address = _integer(values[3] if len(values) > 3 else None)
                    record_layout = values[4] if len(values) > 4 else ""
                    conversion = values[6] if len(values) > 6 else ""
                    minimum = _number(values[7] if len(values) > 7 else None)
                    maximum = _number(values[8] if len(values) > 8 else None)
                    object_metadata = {}
                elif kind == "AXIS_PTS":
                    data_type = "AXIS_PTS"
                    address = _integer(values[2] if len(values) > 2 else None)
                    record_layout = values[4] if len(values) > 4 else ""
                    conversion = values[6] if len(values) > 6 else ""
                    minimum = _number(values[8] if len(values) > 8 else None)
                    maximum = _number(values[9] if len(values) > 9 else None)
                    object_metadata = {
                        "input_quantity": values[3] if len(values) > 3 else "",
                        "max_points": _integer(values[7] if len(values) > 7 else None),
                    }
                else:
                    data_type = "BLOB"
                    address = _integer(values[2] if len(values) > 2 else None)
                    record_layout = ""
                    conversion = ""
                    minimum = None
                    maximum = None
                    object_metadata = {
                        "size": _integer(values[3] if len(values) > 3 else None)
                    }
                method = self.compu_methods.get(conversion)
                physical_unit = statements.get("PHYS_UNIT", [[]])[0]
                matrix_dim = statements.get("MATRIX_DIM", [[]])[0]
                byte_order = statements.get("BYTE_ORDER", [[]])[0]
                axes = tuple(
                    self._axis(child) for child in block.children if child.kind == "AXIS_DESCR"
                )
                self.objects[values[0]] = A2LObject(
                    name=values[0],
                    kind=kind.casefold(),
                    long_identifier=values[1] if len(values) > 1 else "",
                    data_type=data_type,
                    address=address,
                    record_layout=record_layout,
                    conversion=conversion,
                    minimum=minimum,
                    maximum=maximum,
                    unit=(physical_unit[0] if physical_unit else method.unit if method else ""),
                    byte_order=byte_order[0] if byte_order else self.byte_order,
                    dimensions=tuple(
                        int(value, 0) for value in matrix_dim if _integer(value) is not None
                    ),
                    axis_descriptors=axes,
                    metadata={
                        "format": (statements.get("FORMAT", [[""]])[0] or [""])[0],
                        "read_only": "READ_ONLY" in statements,
                        **object_metadata,
                    },
                )

    def get(self, name: str) -> A2LObject:
        try:
            return self.objects[name]
        except KeyError:
            folded = name.casefold()
            for key, value in self.objects.items():
                if key.casefold() == folded:
                    return value
            raise

    def validate(self) -> dict[str, Any]:
        issues: list[str] = []
        for item in self.objects.values():
            if (
                item.minimum is not None
                and item.maximum is not None
                and item.minimum > item.maximum
            ):
                issues.append(f"{item.name}: minimum > maximum")
            if (
                item.conversion
                and item.conversion != "NO_COMPU_METHOD"
                and item.conversion not in self.compu_methods
            ):
                issues.append(f"{item.name}: 未定义转换方法 {item.conversion}")
            if item.record_layout and item.record_layout not in self.record_layouts:
                issues.append(f"{item.name}: 未定义记录布局 {item.record_layout}")
            for axis in item.axis_descriptors:
                if axis.record_layout and axis.record_layout not in self.record_layouts:
                    issues.append(
                        f"{item.name}: 轴未定义记录布局 {axis.record_layout}"
                    )
                if (
                    axis.conversion
                    and axis.conversion != "NO_COMPU_METHOD"
                    and axis.conversion not in self.compu_methods
                ):
                    issues.append(
                        f"{item.name}: 轴未定义转换方法 {axis.conversion}"
                    )
        return {"passed": not issues, "issues": issues}

    def summary(self) -> dict[str, Any]:
        kinds: dict[str, int] = {}
        for item in self.objects.values():
            kinds[item.kind] = kinds.get(item.kind, 0) + 1
        return {
            "source": self.source,
            "project": self.project,
            "module": self.module,
            "byte_order": self.byte_order,
            "object_count": len(self.objects),
            "object_kinds": dict(sorted(kinds.items())),
            "compu_method_count": len(self.compu_methods),
            "record_layout_count": len(self.record_layouts),
            **self.validate(),
        }

    def to_signal_definitions(self) -> list[Any]:
        from .offline import SignalDefinition

        return [
            SignalDefinition(
                name=item.name,
                source=self.source,
                unit=item.unit,
                data_type=item.kind,
                minimum=item.minimum,
                maximum=item.maximum,
                address=item.address,
                metadata={
                    "a2l_data_type": item.data_type,
                    "conversion": item.conversion,
                    "record_layout": item.record_layout,
                    "byte_order": item.byte_order,
                    "dimensions": list(item.dimensions),
                    "axis_descriptors": [
                        {
                            "input_quantity": axis.input_quantity,
                            "record_layout": axis.record_layout,
                            "conversion": axis.conversion,
                            "max_points": axis.max_points,
                            "minimum": axis.minimum,
                            "maximum": axis.maximum,
                        }
                        for axis in item.axis_descriptors
                    ],
                },
            )
            for item in self.objects.values()
        ]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if _local_name(child.tag) == name]


def _child_text(element: ET.Element, name: str, default: str = "") -> str:
    child = next(iter(_children(element, name)), None)
    return (child.text or "").strip() if child is not None else default


def _format_number(value: Any) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else format(number, ".15g")


class CalibrationDatasetIO:
    """CDFX、DCM 和文本 PAR 的确定性离线适配器。"""

    @classmethod
    def load(cls, path: str | Path) -> CalibrationDataset:
        source = Path(path).expanduser().resolve()
        suffix = source.suffix.casefold()
        if suffix == ".cdfx":
            return cls._load_cdfx(source)
        if suffix == ".dcm":
            return cls._load_dcm(source)
        if suffix == ".par":
            return cls._load_par(source)
        raise ValueError(f"不支持的标定交换格式：{suffix}")

    @classmethod
    def save(cls, dataset: CalibrationDataset, path: str | Path) -> Path:
        output = Path(path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        suffix = output.suffix.casefold()
        if suffix == ".cdfx":
            cls._save_cdfx(dataset, output)
        elif suffix == ".dcm":
            cls._save_dcm(dataset, output)
        elif suffix == ".par":
            cls._save_par(dataset, output)
        else:
            raise ValueError(f"不支持的标定交换格式：{suffix}")
        return output

    @staticmethod
    def _parameter_values(element: ET.Element) -> tuple[Any, list[float], list[float]]:
        from .calibration import CalibrationKind

        category = _child_text(element, "CATEGORY", "VALUE").upper()
        value_container = next(iter(_children(element, "SW-VALUE-CONT")), element)
        physical = next(iter(_children(value_container, "SW-VALUES-PHYS")), value_container)
        texts = [(item.text or "") for item in physical.iter() if _local_name(item.tag) == "VT"]
        values = [
            float(item.text)
            for item in physical.iter()
            if _local_name(item.tag) == "V" and item.text
        ]
        axes = []
        for axis in _children(element, "SW-AXIS-CONT"):
            axis_values = [
                float(item.text)
                for item in axis.iter()
                if _local_name(item.tag) == "V" and item.text
            ]
            axes.append(axis_values)
        kind = {
            "VALUE": CalibrationKind.SCALAR,
            "CURVE": CalibrationKind.CURVE,
            "MAP": CalibrationKind.MAP,
            "ASCII": CalibrationKind.ASCII,
        }.get(category, CalibrationKind.BLOCK)
        if kind is CalibrationKind.ASCII:
            value: Any = texts[0] if texts else ""
        elif kind is CalibrationKind.SCALAR:
            value = values[0] if values else 0.0
        elif kind is CalibrationKind.MAP and len(axes) >= 2 and axes[0]:
            width = len(axes[0])
            value = [values[index : index + width] for index in range(0, len(values), width)]
        else:
            value = values
        return (kind, value, axes[0] if axes else [], axes[1] if len(axes) > 1 else [])

    @classmethod
    def _load_cdfx(cls, source: Path) -> CalibrationDataset:
        from .calibration import CalibrationDataset, CalibrationParameter

        root = ET.parse(source).getroot()
        identity = {
            item.attrib["key"]: item.attrib.get("value", "")
            for item in root.iter()
            if _local_name(item.tag) == "SD" and "key" in item.attrib
        }
        parameters = {}
        for instance in _children(root, "SW-INSTANCE"):
            name = _child_text(instance, "SHORT-NAME")
            if not name:
                continue
            kind, value, x_axis, y_axis = cls._parameter_values(instance)
            parameters[name] = CalibrationParameter(
                name=name,
                value=value,
                kind=kind,
                unit=_child_text(instance, "UNIT-DISPLAY-NAME"),
                x_axis=x_axis,
                y_axis=y_axis,
            )
        return CalibrationDataset(parameters, identity=identity, source=str(source))

    @staticmethod
    def _save_cdfx(dataset: CalibrationDataset, output: Path) -> None:
        from .calibration import CalibrationKind, _flatten_numeric

        root = ET.Element("MSRSW")
        systems = ET.SubElement(root, "SW-SYSTEMS")
        system = ET.SubElement(systems, "SW-SYSTEM")
        ET.SubElement(system, "SHORT-NAME").text = dataset.identity.get("ecu", "Agent2Canape")
        data = ET.SubElement(system, "SW-DATA-DICTIONARY-SPEC")
        instances = ET.SubElement(data, "SW-INSTANCE-SPEC")
        for parameter in sorted(dataset.parameters.values(), key=lambda item: item.name):
            instance = ET.SubElement(instances, "SW-INSTANCE")
            ET.SubElement(instance, "SHORT-NAME").text = parameter.name
            ET.SubElement(instance, "CATEGORY").text = {
                CalibrationKind.SCALAR: "VALUE",
                CalibrationKind.CURVE: "CURVE",
                CalibrationKind.MAP: "MAP",
                CalibrationKind.ASCII: "ASCII",
            }.get(parameter.kind, "VAL_BLK")
            if parameter.unit:
                ET.SubElement(instance, "UNIT-DISPLAY-NAME").text = parameter.unit
            container = ET.SubElement(instance, "SW-VALUE-CONT")
            values = ET.SubElement(container, "SW-VALUES-PHYS")
            if parameter.kind is CalibrationKind.ASCII:
                ET.SubElement(values, "VT").text = str(parameter.value)
            else:
                for value in _flatten_numeric(parameter.value):
                    ET.SubElement(values, "V").text = _format_number(value)
            if parameter.x_axis or parameter.y_axis:
                axes = ET.SubElement(instance, "SW-AXIS-CONTS")
                for category, axis_values in (
                    ("X-AXIS", parameter.x_axis),
                    ("Y-AXIS", parameter.y_axis),
                ):
                    if not axis_values:
                        continue
                    axis = ET.SubElement(axes, "SW-AXIS-CONT")
                    ET.SubElement(axis, "CATEGORY").text = category
                    axis_physical = ET.SubElement(axis, "SW-VALUES-PHYS")
                    for value in axis_values:
                        ET.SubElement(axis_physical, "V").text = _format_number(value)
        sdgs = ET.SubElement(root, "SDGS")
        for key, value in sorted(dataset.identity.items()):
            ET.SubElement(sdgs, "SD", key=key, value=value)
        ET.indent(root, space="  ")
        ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _load_dcm(source: Path) -> CalibrationDataset:
        from .calibration import CalibrationDataset, CalibrationKind, CalibrationParameter

        lines = [
            line.strip()
            for line in source.read_text(encoding="latin-1", errors="replace").splitlines()
        ]
        parameters: dict[str, CalibrationParameter] = {}
        index = 0
        section_kinds = {
            "FESTWERT": CalibrationKind.SCALAR,
            "KENNLINIE": CalibrationKind.CURVE,
            "KENNFELD": CalibrationKind.MAP,
            "TEXTSTRING": CalibrationKind.ASCII,
        }
        while index < len(lines):
            header = _tokens(lines[index])
            if not header or header[0].upper() not in section_kinds or len(header) < 2:
                index += 1
                continue
            section = header[0].upper()
            name = header[1]
            index += 1
            x_axis: list[float] = []
            y_axis: list[float] = []
            values: list[float] = []
            text_value = ""
            unit = ""
            while index < len(lines) and lines[index].upper() != "END":
                entry = _tokens(lines[index])
                if entry:
                    key = entry[0].upper()
                    numeric = [float(value) for value in entry[1:] if _NUMBER.fullmatch(value)]
                    if key == "ST/X":
                        x_axis.extend(numeric)
                    elif key == "ST/Y":
                        y_axis.extend(numeric)
                    elif key == "WERT":
                        values.extend(numeric)
                    elif key == "TEXT" and len(entry) > 1:
                        text_value = entry[1]
                    elif key == "EINHEIT_W" and len(entry) > 1:
                        unit = entry[1]
                index += 1
            kind = section_kinds[section]
            if kind is CalibrationKind.ASCII:
                value: Any = text_value
            elif kind is CalibrationKind.SCALAR:
                value = values[0] if values else 0.0
            elif kind is CalibrationKind.MAP and x_axis:
                width = len(x_axis)
                value = [values[pos : pos + width] for pos in range(0, len(values), width)]
            else:
                value = values
            parameters[name] = CalibrationParameter(
                name, value, kind, unit=unit, x_axis=x_axis, y_axis=y_axis
            )
            index += 1
        return CalibrationDataset(parameters, source=str(source))

    @staticmethod
    def _save_dcm(dataset: CalibrationDataset, output: Path) -> None:
        from .calibration import CalibrationKind, _flatten_numeric

        lines = ["KONSERVIERUNG_FORMAT 2.0", ""]
        section = {
            CalibrationKind.SCALAR: "FESTWERT",
            CalibrationKind.CURVE: "KENNLINIE",
            CalibrationKind.MAP: "KENNFELD",
            CalibrationKind.ASCII: "TEXTSTRING",
        }
        for parameter in sorted(dataset.parameters.values(), key=lambda item: item.name):
            if parameter.kind not in section:
                raise ValueError(f"DCM 不支持参数类型：{parameter.kind.value}")
            lines.append(f"{section[parameter.kind]} {parameter.name}")
            if parameter.unit:
                lines.append(f'  EINHEIT_W "{parameter.unit}"')
            if parameter.x_axis:
                lines.append("  ST/X " + " ".join(map(_format_number, parameter.x_axis)))
            if parameter.y_axis:
                lines.append("  ST/Y " + " ".join(map(_format_number, parameter.y_axis)))
            if parameter.kind is CalibrationKind.ASCII:
                escaped = str(parameter.value).replace('"', '\\"')
                lines.append(f'  TEXT "{escaped}"')
            else:
                values = " ".join(map(_format_number, _flatten_numeric(parameter.value)))
                lines.append(f"  WERT {values}")
            lines.extend(("END", ""))
        output.write_text("\n".join(lines), encoding="latin-1", errors="replace")

    @staticmethod
    def _load_par(source: Path) -> CalibrationDataset:
        from .calibration import CalibrationDataset, CalibrationKind, CalibrationParameter

        parameters = {}
        metadata: dict[str, dict[str, Any]] = {}
        for raw in source.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith(";@"):
                payload = json.loads(line[2:])
                metadata[str(payload["name"])] = payload
                continue
            if line.startswith((";", "#")) or "=" not in line:
                continue
            name, raw_value = (part.strip() for part in line.split("=", 1))
            item = metadata.get(name, {})
            if raw_value.startswith('"') and raw_value.endswith('"'):
                value: Any = json.loads(raw_value)
                kind = CalibrationKind.ASCII
            else:
                value = json.loads(raw_value)
                kind = CalibrationKind(item.get("kind", "scalar"))
            parameters[name] = CalibrationParameter(
                name=name,
                value=value,
                kind=kind,
                unit=str(item.get("unit", "")),
                x_axis=list(item.get("x_axis", [])),
                y_axis=list(item.get("y_axis", [])),
            )
        return CalibrationDataset(parameters, source=str(source))

    @staticmethod
    def _save_par(dataset: CalibrationDataset, output: Path) -> None:
        lines = ["; Agent2Canape deterministic PAR 1.0"]
        for parameter in sorted(dataset.parameters.values(), key=lambda item: item.name):
            metadata = {
                "name": parameter.name,
                "kind": parameter.kind.value,
                "unit": parameter.unit,
                "x_axis": parameter.x_axis,
                "y_axis": parameter.y_axis,
            }
            lines.append(";@" + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
            lines.append(
                f"{parameter.name} = "
                + json.dumps(parameter.value, ensure_ascii=False, separators=(",", ":"))
            )
        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
