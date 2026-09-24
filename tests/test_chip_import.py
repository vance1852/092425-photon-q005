from __future__ import annotations

import unittest

from photon_fab.importer import ImportValidationError, parse_jsonl, parse_line


VALID = (
    '{"chip_id":"C1","wavelength":{"value":1550.0,"unit":"nm"},'
    '"responsivity":{"value":0.9,"unit":"A/W"},'
    '"dark_current":{"value":2.5,"unit":"nA"},"instrument_id":"PD-1"}'
)


class ParseLineTests(unittest.TestCase):
    def test_valid_record_with_units(self) -> None:
        record = parse_line(VALID)
        self.assertEqual(record.chip_id, "C1")
        self.assertEqual(record.wavelength_nm, 1550.0)
        self.assertEqual(record.responsivity_aw, 0.9)
        self.assertAlmostEqual(record.dark_current_a, 2.5e-9)
        self.assertEqual(record.instrument_id, "PD-1")

    def test_dark_current_unit_conversion(self) -> None:
        for unit, value, expected in (
            ("A", 0.0005, 5e-4),
            ("uA", 1.0, 1e-6),
            ("µA", 1.0, 1e-6),
            ("nA", 1.0, 1e-9),
            ("pA", 1.0, 1e-12),
        ):
            line = VALID.replace('"value":2.5,"unit":"nA"', f'"value":{value},"unit":"{unit}"')
            record = parse_line(line)
            self.assertAlmostEqual(record.dark_current_a, expected)

    def test_flat_fields_with_implied_units(self) -> None:
        line = ('{"chip_id":"C2","wavelength_nm":1310,"responsivity_aw":0.8,'
                '"dark_current_a":1e-10,"instrument_id":"PD-2"}')
        record = parse_line(line)
        self.assertEqual(record.wavelength_nm, 1310)
        self.assertEqual(record.responsivity_aw, 0.8)
        self.assertEqual(record.dark_current_a, 1e-10)

    def test_rejects_bad_json(self) -> None:
        with self.assertRaises(ImportValidationError):
            parse_line("{not json")

    def test_rejects_missing_fields(self) -> None:
        with self.assertRaises(ImportValidationError):
            parse_line('{"chip_id":"C1"}')

    def test_rejects_wrong_units(self) -> None:
        line = VALID.replace('"unit":"nm"', '"unit":"um"')
        with self.assertRaises(ImportValidationError):
            parse_line(line)
        line = VALID.replace('"unit":"A/W"', '"unit":"V"')
        with self.assertRaises(ImportValidationError):
            parse_line(line)

    def test_rejects_unsupported_dark_current_unit(self) -> None:
        line = VALID.replace('"unit":"nA"', '"unit":"mA"')
        with self.assertRaises(ImportValidationError):
            parse_line(line)

    def test_rejects_out_of_range(self) -> None:
        # 波长超出范围
        with self.assertRaises(ImportValidationError):
            parse_line(VALID.replace("1550.0", "50.0"))
        # 响应度为负
        with self.assertRaises(ImportValidationError):
            parse_line(VALID.replace("0.9", "-0.1"))
        # 暗电流超过 1 mA
        with self.assertRaises(ImportValidationError):
            parse_line(VALID.replace('"value":2.5,"unit":"nA"', '"value":2,"unit":"A"'))

    def test_rejects_non_finite_and_wrong_types(self) -> None:
        with self.assertRaises(ImportValidationError):
            parse_line(VALID.replace("1550.0", "1e999"))
        with self.assertRaises(ImportValidationError):
            parse_line(VALID.replace('"chip_id":"C1"', '"chip_id": 7'))
        with self.assertRaises(ImportValidationError):
            parse_line(VALID.replace('"C1"', '""'))

    def test_boolean_is_not_a_number(self) -> None:
        with self.assertRaises(ImportValidationError):
            parse_line(VALID.replace("1550.0", "true"))


class ParseJsonlTests(unittest.TestCase):
    def test_blank_lines_skipped_and_line_numbers_preserved(self) -> None:
        content = "\n" + VALID + "\n\n" + VALID.replace("C1", "C2") + "\n"
        entries, failures = parse_jsonl(content)
        self.assertEqual([line for line, _ in entries], [2, 4])
        self.assertEqual(failures, [])

    def test_in_file_duplicate_chip_is_failure(self) -> None:
        content = VALID + "\n" + VALID + "\n"
        entries, failures = parse_jsonl(content)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].line, 2)
        self.assertIn("duplicates line 1", failures[0].reason)


if __name__ == "__main__":
    unittest.main()
