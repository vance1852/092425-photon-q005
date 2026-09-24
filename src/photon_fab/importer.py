"""封测线 JSONL 的逐行解析、字段与单位校验（纯离线逻辑，不接触数据库）。

文件中的每一行是一条芯片封测记录，包含芯片编号、波长、响应度、暗电流和
仪器编号。带单位的测量值使用 ``{"value": ..., "unit": ...}`` 对象表示；
同时接受单位隐含在字段名中的规范扁平字段（``wavelength_nm`` 等）。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

# 暗电流在文件中允许使用的单位 -> 统一存储为安培时使用的换算系数。
DARK_CURRENT_UNITS: dict[str, float] = {
    "A": 1.0,
    "uA": 1e-6,
    "µA": 1e-6,  # micro sign, U+00B5
    "μA": 1e-6,  # greek mu, U+03BC
    "nA": 1e-9,
    "pA": 1e-12,
}
WAVELENGTH_UNITS = {"nm"}
RESPONSIVITY_UNITS = {"A/W"}

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")

# 物理合理性范围（存储单位）。
WAVELENGTH_NM_RANGE = (200.0, 10_000.0)
RESPONSIVITY_AW_RANGE = (0.0, 10.0)  # 响应度必须为正
DARK_CURRENT_A_RANGE = (0.0, 1e-3)  # 芯片暗电流应小于 1 mA


class ImportValidationError(ValueError):
    """单行记录未通过字段或单位校验。"""


class ImportRejected(ValueError):
    """整批被拒绝（至少一行非法）；``report`` 为导入结果。"""

    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__(f"import rejected with {report['failed']['count']} invalid line(s)")
        self.report = report


@dataclass(frozen=True)
class ChipTestRecord:
    chip_id: str
    wavelength_nm: float
    responsivity_aw: float
    dark_current_a: float
    instrument_id: str


@dataclass(frozen=True)
class LineFailure:
    line: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "reason": self.reason}


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ImportValidationError(f"{field} is required and must be a non-empty string")
    value = value.strip()
    if not _IDENTIFIER.match(value):
        raise ImportValidationError(f"{field} contains unsupported characters or is too long")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImportValidationError(f"{field} must be a JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise ImportValidationError(f"{field} must be finite")
    return number


def _range_check(field: str, value: float, low: float, high: float, low_inclusive: bool = True) -> None:
    ok = low <= value <= high if low_inclusive else low < value <= high
    if not ok:
        raise ImportValidationError(f"{field}={value} is outside the allowed range ({low}, {high}]")


def _measure(obj: Any, field: str, allowed_units: set[str]) -> tuple[float, str]:
    if not isinstance(obj, dict):
        raise ImportValidationError(f"{field} must be an object with value and unit")
    if "value" not in obj or "unit" not in obj:
        raise ImportValidationError(f"{field} requires value and unit")
    unit = obj["unit"]
    if not isinstance(unit, str) or unit not in allowed_units:
        allowed = ", ".join(sorted(allowed_units))
        raise ImportValidationError(f"{field} unit must be one of: {allowed}")
    return _number(obj["value"], field), unit


def parse_line(raw_line: str) -> ChipTestRecord:
    """解析并校验单行，非法时抛出 :class:`ImportValidationError`。"""
    try:
        obj = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise ImportValidationError(f"invalid JSON: {exc.msg}") from None
    if not isinstance(obj, dict):
        raise ImportValidationError("line must be a JSON object")

    chip_id = _identifier(obj.get("chip_id"), "chip_id")
    instrument_id = _identifier(obj.get("instrument_id"), "instrument_id")

    if "wavelength" in obj:
        wavelength_nm, _ = _measure(obj["wavelength"], "wavelength", WAVELENGTH_UNITS)
    elif "wavelength_nm" in obj:
        wavelength_nm = _number(obj["wavelength_nm"], "wavelength_nm")
    else:
        raise ImportValidationError("wavelength (nm) is required")
    _range_check("wavelength_nm", wavelength_nm, *WAVELENGTH_NM_RANGE, low_inclusive=False)

    if "responsivity" in obj:
        responsivity_aw, _ = _measure(obj["responsivity"], "responsivity", RESPONSIVITY_UNITS)
    elif "responsivity_aw" in obj:
        responsivity_aw = _number(obj["responsivity_aw"], "responsivity_aw")
    else:
        raise ImportValidationError("responsivity (A/W) is required")
    _range_check("responsivity_aw", responsivity_aw, *RESPONSIVITY_AW_RANGE, low_inclusive=False)

    if "dark_current" in obj:
        dark_current, unit = _measure(obj["dark_current"], "dark_current", set(DARK_CURRENT_UNITS))
        dark_current_a = dark_current * DARK_CURRENT_UNITS[unit]
    elif "dark_current_a" in obj:
        dark_current_a = _number(obj["dark_current_a"], "dark_current_a")
    else:
        raise ImportValidationError("dark_current is required")
    _range_check("dark_current_a", dark_current_a, *DARK_CURRENT_A_RANGE)

    return ChipTestRecord(chip_id, wavelength_nm, responsivity_aw, dark_current_a, instrument_id)


def parse_jsonl(content: str) -> tuple[list[tuple[int, ChipTestRecord]], list[LineFailure]]:
    """逐行解析整份文件。

    返回 ``(entries, failures)``：``entries`` 为 ``(物理行号, 记录)``；
    空白行跳过且不计入任何结果；同一文件内重复出现的芯片编号，第二行起
    记为失败行。行号从 1 开始，与原始文件一一对应。
    """
    if not isinstance(content, str):
        raise ImportValidationError("content must be a JSONL string")
    entries: list[tuple[int, ChipTestRecord]] = []
    failures: list[LineFailure] = []
    first_seen: dict[str, int] = {}
    for line, raw_line in enumerate(content.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            record = parse_line(raw_line)
        except ImportValidationError as exc:
            failures.append(LineFailure(line, str(exc)))
            continue
        if record.chip_id in first_seen:
            failures.append(
                LineFailure(
                    line,
                    f"chip_id {record.chip_id} duplicates line {first_seen[record.chip_id]} in the same file",
                )
            )
            continue
        first_seen[record.chip_id] = line
        entries.append((line, record))
    return entries, failures
