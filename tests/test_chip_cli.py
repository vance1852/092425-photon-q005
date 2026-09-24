from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from photon_fab.import_cli import main
from photon_fab.storage import connect


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "chip_test_demo.jsonl"


class ImportCliTests(unittest.TestCase):
    def test_cli_accepts_clean_file_and_is_traceable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "photon.sqlite3"
            code = main([
                "--database", str(database), "--file", str(FIXTURE),
                "--bootstrap-admin", "--note", "nightly",
            ])
            self.assertEqual(code, 0)
            db = connect(str(database))
            try:
                self.assertEqual(db.execute("SELECT count(*) FROM chip_test_records").fetchone()[0], 4)
                event = db.execute("SELECT status, inserted_count FROM imports").fetchone()
                self.assertEqual(tuple(event), ("accepted", 4))
            finally:
                db.close()

    def test_cli_returns_2_and_writes_rejected_audit_on_bad_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "photon.sqlite3"
            source = Path(directory) / "bad.jsonl"
            source.write_text(
                FIXTURE.read_text(encoding="utf-8")
                + '{"chip_id":"PIC-BAD","wavelength_nm":1550,"responsivity_a_w":0.9,'
                  '"dark_current_a":"x","instrument_id":"i"}\n',
                encoding="utf-8",
            )
            code = main([
                "--database", str(database), "--file", str(source), "--bootstrap-admin",
            ])
            self.assertEqual(code, 2)
            db = connect(str(database))
            try:
                # 业务表零写入，但 rejected 审计事件存在。
                self.assertEqual(db.execute("SELECT count(*) FROM chip_test_records").fetchone()[0], 0)
                self.assertEqual(
                    db.execute("SELECT status FROM imports").fetchone()[0], "rejected"
                )
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
