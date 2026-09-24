"""封测线每日 JSONL 文件的字段、单位与逐行校验。

文件每行是一条芯片封测记录，字段名携带计量单位：

- ``chip_id``：芯片编号（非空文本）；
- ``wavelength_nm``：波长，单位 nm；
- ``responsivity_a_w``：响应度，单位 A/W；
- ``dark_current_a``：暗电流，单位 A；
- ``instrument_id``：仪器编号（非空文本）。

单位安全策略：只接受上述精确字段名，任何未知字段（例如把微米值
写成 ``wavelength_um``）都会令该行失败；数值必须是 JSON 数字、有限且落在
物理量程内，布尔值和字符串形式的数字一律拒绝。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, NamedTuple


class ChipRecordError(ValueError):
    """单行记录无法满足封测数据契约。"""


# 光电芯片常见工作波段（紫外边缘到长波红外），单位 nm。
WAVELENGTH_MIN = Decimal("200")
WAVELENGTH_MAX = Decimal("10000")
# 响应度非负；上限用于拦截把 mA/W 误当 A/W 等单位错配。
RESPONSIVITY_MIN = Decimal("0")
RESPONSIVITY_MAX = Decimal("100")
# 暗电流非负；1 A 量级只可能是单位录入错误。
DARK_CURRENT_MIN = Decimal("0")
DARK_CURRENT_MAX_EXCLUSIVE = Decimal("1")

TEXT_FIELDS: dict[str, int] = {"chip_id": 64, "instrument_id": 40}
NUMERIC_FIELDS: dict[str, tuple[str, Decimal, Decimal | None, Decimal | None]] = {
    "wavelength_nm": ("nm", WAVELENGTH_MIN, WAVELENGTH_MAX, None),
    "responsivity_a_w": ("A/W", RESPONSIVITY_MIN, RESPONSIVITY_MAX, None),
    "dark_current_a": ("A", DARK_CURRENT_MIN, None, DARK_CURRENT_MAX_EXCLUSIVE),
}
EXPECTED_FIELDS = tuple(TEXT_FIELDS) + tuple(NUMERIC_FIELDS)


@dataclass(frozen=True, slots=True)
class ChipRecord:
    """通过校验的一条封测记录，数值保留 Decimal 以便精确比对。"""

    chip_id: str
    wavelength_nm: Decimal
    responsivity_a_w: Decimal
    dark_current_a: Decimal
    instrument_id: str

    def measurement_values(self) -> tuple[float, float, float]:
        return (float(self.wavelength_nm), float(self.responsivity_a_w), float(self.dark_current_a))


class ParsedLine(NamedTuple):
    line_number: int
    value: Any | None
    error: str | None


def _reject_constant(value: str) -> None:
    raise ChipRecordError(f"不允许非有限数值 {value}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChipRecordError(f"JSON 对象含重复键 {key}")
        result[key] = value
    return result


def parse_jsonl(content: str) -> list[ParsedLine]:
    """按物理行解析 JSONL，空行跳过但保留真实行号。"""

    lines: list[ParsedLine] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(
                line,
                parse_float=Decimal,
                parse_constant=_reject_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
        except json.JSONDecodeError as exc:
            lines.append(ParsedLine(line_number, None, f"不是有效 JSON：{exc.msg}"))
        except ChipRecordError as exc:
            lines.append(ParsedLine(line_number, None, str(exc)))
        else:
            lines.append(ParsedLine(line_number, value, None))
    return lines


def _require_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChipRecordError(f"{field} 必须是非空字符串")
    text = value.strip()
    if len(text) > max_length:
        raise ChipRecordError(f"{field} 长度不能超过 {max_length}")
    return text


def _require_number(value: Any, field: str, unit: str,
                    lower: Decimal, upper: Decimal | None,
                    upper_exclusive: Decimal | None) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ChipRecordError(f"{field} 必须是数值（单位 {unit}）")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ChipRecordError(f"{field} 必须是十进制数值（单位 {unit}）") from exc
    if not number.is_finite():
        raise ChipRecordError(f"{field} 必须是有限数值")
    if number < lower:
        raise ChipRecordError(f"{field} 不能小于 {lower} {unit}")
    if upper is not None and number > upper:
        raise ChipRecordError(f"{field} 不能大于 {upper} {unit}")
    if upper_exclusive is not None and number >= upper_exclusive:
        raise ChipRecordError(f"{field} 必须小于 {upper_exclusive} {unit}")
    return number


def validate_record(raw: Any) -> ChipRecord:
    """校验并归一化单行；不满足契约时抛 :class:`ChipRecordError`。"""

    if not isinstance(raw, dict):
        raise ChipRecordError("记录必须是 JSON 对象")
    missing = [field for field in EXPECTED_FIELDS if field not in raw]
    if missing:
        raise ChipRecordError(f"缺少必填字段：{', '.join(missing)}")
    extra = sorted(set(raw) - set(EXPECTED_FIELDS))
    if extra:
        raise ChipRecordError(
            f"未知字段 {', '.join(extra)}；只允许 {', '.join(EXPECTED_FIELDS)}，请确认单位是否写在字段名中"
        )
    chip_id = _require_text(raw["chip_id"], "chip_id", TEXT_FIELDS["chip_id"])
    instrument_id = _require_text(raw["instrument_id"], "instrument_id", TEXT_FIELDS["instrument_id"])
    numeric: dict[str, Decimal] = {}
    for field, (unit, lower, upper, upper_exclusive) in NUMERIC_FIELDS.items():
        numeric[field] = _require_number(raw[field], field, unit, lower, upper, upper_exclusive)
    return ChipRecord(
        chip_id=chip_id,
        wavelength_nm=numeric["wavelength_nm"],
        responsivity_a_w=numeric["responsivity_a_w"],
        dark_current_a=numeric["dark_current_a"],
        instrument_id=instrument_id,
    )


def content_digest(content: str) -> str:
    """计算原始 JSONL 文本的 SHA-256，用于导入审计追溯。"""

    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _fixed(decimal: Decimal) -> str:
    """定点、去尾零的规范文本：1.00 与 1.0、450 与 450.0 视为同一数值。"""

    return format(decimal.normalize(), "f")


def canonical_record(record: ChipRecord) -> str:
    """记录的规范化紧凑 JSON，字段排序，作为逐行内容指纹的输入。"""

    return json.dumps(
        {
            "chip_id": record.chip_id,
            "wavelength_nm": _fixed(record.wavelength_nm),
            "responsivity_a_w": _fixed(record.responsivity_a_w),
            "dark_current_a": _fixed(record.dark_current_a),
            "instrument_id": record.instrument_id,
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def record_digest(record: ChipRecord) -> str:
    """单条记录的 SHA-256 指纹，用于重复行文件内与跨批次比对。"""

    return hashlib.sha256(canonical_record(record).encode("utf-8")).hexdigest()


def record_view(record: ChipRecord) -> dict[str, Any]:
    """对外返回的记录视图（含单位字段名，不含内部指纹列）。"""

    return {
        "chip_id": record.chip_id,
        "wavelength_nm": float(record.wavelength_nm),
        "responsivity_a_w": float(record.responsivity_a_w),
        "dark_current_a": float(record.dark_current_a),
        "instrument_id": record.instrument_id,
    }


def stored_record_view(row: Any) -> dict[str, Any]:
    """从 chip_test_records 数据库行构造对外原记录视图。"""

    return {
        "chip_id": row["chip_id"],
        "wavelength_nm": row["wavelength_nm"],
        "responsivity_a_w": row["responsivity_a_w"],
        "dark_current_a": row["dark_current_a"],
        "instrument_id": row["instrument_id"],
    }
