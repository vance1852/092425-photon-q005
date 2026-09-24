from __future__ import annotations

import json
import unittest
from decimal import Decimal

from photon_fab.chip_import import (
    ChipRecordError,
    parse_jsonl,
    record_digest,
    validate_record,
)


def record(**overrides) -> dict:
    base = {
        "chip_id": "PIC-1",
        "wavelength_nm": 1310.0,
        "responsivity_a_w": 0.85,
        "dark_current_a": 0.00000001,
        "instrument_id": "ils-07",
    }
    base.update(overrides)
    return base


class ValidationTests(unittest.TestCase):
    def test_valid_record(self) -> None:
        item = validate_record(record())
        self.assertEqual(item.chip_id, "PIC-1")
        self.assertEqual(item.wavelength_nm, Decimal("1310.0"))
        self.assertEqual(item.responsivity_a_w, Decimal("0.85"))
        self.assertEqual(item.instrument_id, "ils-07")

    def test_strips_whitespace_in_text_fields(self) -> None:
        item = validate_record(record(chip_id="  PIC-2  ", instrument_id=" ils-08 "))
        self.assertEqual(item.chip_id, "PIC-2")
        self.assertEqual(item.instrument_id, "ils-08")

    def test_missing_field(self) -> None:
        raw = record()
        del raw["wavelength_nm"]
        with self.assertRaises(ChipRecordError):
            validate_record(raw)

    def test_unknown_field_is_rejected(self) -> None:
        # 单位写在错误的字段名上必须失败，而不是被静默忽略。
        with self.assertRaises(ChipRecordError):
            validate_record(record(wavelength_um=1.31))
        with self.assertRaises(ChipRecordError):
            validate_record(record(extra=1))

    def test_text_must_be_nonempty_string(self) -> None:
        with self.assertRaises(ChipRecordError):
            validate_record(record(chip_id="   "))
        with self.assertRaises(ChipRecordError):
            validate_record(record(chip_id=123))

    def test_numbers_reject_strings_and_bools(self) -> None:
        with self.assertRaises(ChipRecordError):
            validate_record(record(wavelength_nm="1310"))
        with self.assertRaises(ChipRecordError):
            validate_record(record(responsivity_a_w=True))

    def test_wavelength_range_enforced_in_nm(self) -> None:
        with self.assertRaises(ChipRecordError):
            validate_record(record(wavelength_nm=100))
        with self.assertRaises(ChipRecordError):
            validate_record(record(wavelength_nm=20000))

    def test_responsivity_must_be_nonnegative(self) -> None:
        with self.assertRaises(ChipRecordError):
            validate_record(record(responsivity_a_w=-0.1))

    def test_dark_current_range(self) -> None:
        with self.assertRaises(ChipRecordError):
            validate_record(record(dark_current_a=-1e-9))  # 负值不允许
        validate_record(record(dark_current_a=0.0))  # 零允许
        with self.assertRaises(ChipRecordError):
            validate_record(record(dark_current_a=1.0))  # 上界不含

    def test_non_object_rejected(self) -> None:
        with self.assertRaises(ChipRecordError):
            validate_record([1, 2, 3])

    def test_digest_equivalent_numeric_spelling(self) -> None:
        # 1310 与 1310.0、0.85 与 Decimal("0.850") 物理等价，指纹必须一致。
        first = validate_record(record(wavelength_nm=1310, responsivity_a_w=0.85))
        second = validate_record(record(wavelength_nm=1310.0, responsivity_a_w=Decimal("0.850")))
        self.assertEqual(record_digest(first), record_digest(second))

    def test_digest_detects_real_change(self) -> None:
        first = validate_record(record(wavelength_nm=1310))
        second = validate_record(record(wavelength_nm=1550))
        self.assertNotEqual(record_digest(first), record_digest(second))


class JsonlParsingTests(unittest.TestCase):
    def test_blank_lines_skipped_but_line_numbers_preserved(self) -> None:
        content = "\n" + json.dumps(record()) + "\n\n" + json.dumps(record(chip_id="PIC-2")) + "\n"
        parsed = parse_jsonl(content)
        self.assertEqual([p.line_number for p in parsed], [2, 4])

    def test_invalid_json_reports_physical_line(self) -> None:
        parsed = parse_jsonl(json.dumps(record()) + "\n{not json}\n")
        self.assertEqual(len(parsed), 2)
        self.assertIsNone(parsed[0].error)
        self.assertEqual(parsed[1].line_number, 2)
        self.assertIn("JSON", parsed[1].error)

    def test_non_finite_constants_rejected(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            parsed = parse_jsonl(json.dumps(record(wavelength_nm=1))[:-1] + ",\"x\":" + constant + "}\n")
            self.assertTrue(parsed[0].error, constant)

    def test_duplicate_json_keys_rejected_at_parse(self) -> None:
        text = '{"chip_id":"a","chip_id":"b","wavelength_nm":1310,' \
               '"responsivity_a_w":0.8,"dark_current_a":0.0,"instrument_id":"i"}'
        parsed = parse_jsonl(text + "\n")
        self.assertIsNotNone(parsed[0].error)


if __name__ == "__main__":
    unittest.main()
