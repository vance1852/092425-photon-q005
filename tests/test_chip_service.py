from __future__ import annotations

import json
import unittest

from photon_fab.service import BatchRejected, PhotonService


def line(chip_id: str = "PIC-1", **overrides) -> str:
    row = {
        "chip_id": chip_id,
        "wavelength_nm": 1310.0,
        "responsivity_a_w": 0.85,
        "dark_current_a": 1e-8,
        "instrument_id": "ils-07",
    }
    row.update(overrides)
    return json.dumps(row, ensure_ascii=False)


class ImportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService(":memory:")
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")

    # ---- 正常路径 ----------------------------------------------------

    def test_imports_all_new_rows_in_one_commit(self) -> None:
        content = "\n".join([
            line("PIC-1"),
            line("PIC-2", wavelength_nm=1550.0),
            line("PIC-3", instrument_id="ils-08"),
        ]) + "\n"
        report = self.service.import_chip_records(self.token, content, "day-1")
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(report["total_lines"], 3)
        self.assertEqual(report["inserted_count"], 3)
        self.assertEqual(report["inserted_lines"], [1, 2, 3])
        self.assertEqual(report["duplicate_count"], 0)
        self.assertEqual(report["failed_count"], 0)
        self.assertEqual(len(report["source_sha256"]), 64)
        count = self.service.db.execute("SELECT count(*) FROM chip_test_records").fetchone()[0]
        self.assertEqual(count, 3)

    def test_accepted_import_writes_audit_event(self) -> None:
        report = self.service.import_chip_records(self.token, line() + "\n", "daily")
        stored = self.service.get_import(self.token, report["import_id"])
        self.assertEqual(stored["status"], "accepted")
        self.assertEqual(stored["note"], "daily")
        self.assertEqual(stored["actor"], "admin")
        self.assertEqual(stored["inserted_lines"], [1])
        self.assertEqual(stored["failed_lines"], [])

    # ---- 重复行 ------------------------------------------------------

    def test_replaying_identical_file_reports_duplicates_and_originals(self) -> None:
        content = line("PIC-1") + "\n" + line("PIC-2", wavelength_nm=1550.0) + "\n"
        self.service.import_chip_records(self.token, content)
        replay = self.service.import_chip_records(self.token, content)
        self.assertEqual(replay["status"], "accepted")
        self.assertEqual(replay["inserted_count"], 0)
        self.assertEqual(replay["duplicate_count"], 2)
        self.assertEqual(replay["duplicate_lines"], [1, 2])
        originals = {d["line"]: d["record"] for d in replay["duplicates"]}
        self.assertEqual(originals[1]["chip_id"], "PIC-1")
        self.assertEqual(originals[2]["wavelength_nm"], 1550.0)
        # 原记录不暴露内部指纹/导入列。
        self.assertEqual(
            set(originals[1]),
            {"chip_id", "wavelength_nm", "responsivity_a_w", "dark_current_a", "instrument_id"},
        )
        count = self.service.db.execute("SELECT count(*) FROM chip_test_records").fetchone()[0]
        self.assertEqual(count, 2)

    def test_identical_duplicate_inside_one_file_is_idempotent(self) -> None:
        content = line("PIC-1") + "\n" + line("PIC-1") + "\n"
        report = self.service.import_chip_records(self.token, content)
        self.assertEqual(report["inserted_count"], 1)
        self.assertEqual(report["duplicate_count"], 1)
        self.assertEqual(report["duplicate_lines"], [2])

    def test_equivalent_numeric_spelling_is_duplicate_not_conflict(self) -> None:
        self.service.import_chip_records(self.token, line("PIC-1", wavelength_nm=1310) + "\n")
        replay = self.service.import_chip_records(
            self.token, line("PIC-1", wavelength_nm=1310.0, responsivity_a_w=0.850) + "\n"
        )
        self.assertEqual(replay["duplicate_count"], 1)

    # ---- 非法行与原子性 ----------------------------------------------

    def test_any_invalid_line_rejects_whole_batch_without_half_data(self) -> None:
        content = "\n".join([
            line("PIC-1"),
            line("PIC-2"),
            '{"chip_id":"PIC-BAD","wavelength_nm":1550,"responsivity_a_w":0.9,'
            '"dark_current_a":"oops","instrument_id":"ils-07"}',
            line("PIC-3"),
        ]) + "\n"
        with self.assertRaises(BatchRejected) as caught:
            self.service.import_chip_records(self.token, content)
        report = caught.exception.report
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["failed_lines"], [3])
        self.assertEqual(report["inserted_count"], 0)
        self.assertEqual(report["inserted_lines"], [])
        self.assertIn("dark_current_a", report["failures"][0]["error"])
        count = self.service.db.execute("SELECT count(*) FROM chip_test_records").fetchone()[0]
        self.assertEqual(count, 0)

    def test_rejected_batch_still_writes_traceable_audit_event(self) -> None:
        content = line("PIC-1") + "\n{not json}\n"
        with self.assertRaises(BatchRejected):
            self.service.import_chip_records(self.token, content, "broken")
        row = self.service.db.execute(
            "SELECT * FROM imports WHERE note='broken'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(json.loads(row["failed_lines"]), [2])
        failures = json.loads(row["failures_json"])
        self.assertEqual(failures[0]["line"], 2)
        self.assertEqual(len(row["source_sha256"]), 64)

    def test_malformed_json_line_is_failure_with_physical_line_number(self) -> None:
        content = "\n\n" + line("PIC-1") + "\nnot-json\n"
        with self.assertRaises(BatchRejected) as caught:
            self.service.import_chip_records(self.token, content)
        self.assertEqual(caught.exception.report["failed_lines"], [4])

    def test_unknown_field_is_invalid_guarding_units(self) -> None:
        content = line("PIC-1", wavelength_um=1.31) + "\n"
        with self.assertRaises(BatchRejected) as caught:
            self.service.import_chip_records(self.token, content)
        self.assertEqual(caught.exception.report["failed_lines"], [1])

    def test_same_chip_with_different_content_is_conflict_failure(self) -> None:
        self.service.import_chip_records(self.token, line("PIC-1", wavelength_nm=1310.0) + "\n")
        with self.assertRaises(BatchRejected) as caught:
            self.service.import_chip_records(
                self.token, line("PIC-1", wavelength_nm=1550.0) + "\n" + line("PIC-2") + "\n"
            )
        report = caught.exception.report
        self.assertEqual(report["failed_lines"], [1])
        # 冲突行导致整批回滚：PIC-2 也没有被写入。
        self.assertEqual(
            self.service.db.execute("SELECT count(*) FROM chip_test_records").fetchone()[0], 1
        )

    def test_in_file_conflict_is_failure(self) -> None:
        content = line("PIC-1", wavelength_nm=1310.0) + "\n" + line("PIC-1", wavelength_nm=1550.0) + "\n"
        with self.assertRaises(BatchRejected) as caught:
            self.service.import_chip_records(self.token, content)
        self.assertEqual(caught.exception.report["failed_lines"], [2])
        self.assertEqual(
            self.service.db.execute("SELECT count(*) FROM chip_test_records").fetchone()[0], 0
        )

    def test_multiple_failures_are_all_reported(self) -> None:
        content = "\n".join([
            line("PIC-1"),
            '{"chip_id":"PIC-X"}',
            line("PIC-2", dark_current_a=-5),
        ]) + "\n"
        with self.assertRaises(BatchRejected) as caught:
            self.service.import_chip_records(self.token, content)
        self.assertEqual(caught.exception.report["failed_lines"], [2, 3])

    # ---- 边界 --------------------------------------------------------

    def test_empty_file_is_rejected_before_any_audit(self) -> None:
        with self.assertRaises(ValueError):
            self.service.import_chip_records(self.token, "   \n\n")
        self.assertEqual(
            self.service.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0
        )

    def test_unknown_import_id_raises_keyerror(self) -> None:
        with self.assertRaises(KeyError):
            self.service.get_import(self.token, "nope")

    def test_requires_authentication(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.import_chip_records("not-a-token", line() + "\n")


if __name__ == "__main__":
    unittest.main()
