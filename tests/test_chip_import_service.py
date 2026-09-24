from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from photon_fab.api import Handler
from photon_fab.importer import ImportRejected
from photon_fab.service import PhotonService


ROOT = Path(__file__).resolve().parents[1]

HEADER = (
    '{"chip_id":"@@CHIP@@","wavelength":{"value":1550.0,"unit":"nm"},'
    '"responsivity":{"value":0.9,"unit":"A/W"},'
    '"dark_current":{"value":2.5,"unit":"nA"},"instrument_id":"PD-1"}'
)


def row(chip: str) -> str:
    return HEADER.replace("@@CHIP@@", chip)


class ImportServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService(":memory:")
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")

    def tearDown(self) -> None:
        self.service.db.close()

    def test_import_succeeds_and_writes_audit(self) -> None:
        content = "\n".join([row("C1"), row("C2"), row("C3")]) + "\n"
        report = self.service.import_chip_tests(self.token, "day-1.jsonl", content)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["total_lines"], 3)
        self.assertEqual(report["succeeded"], {"count": 3, "lines": [1, 2, 3]})
        self.assertEqual(report["duplicates"], {"count": 0, "lines": [], "records": []})
        self.assertEqual(report["failed"], {"count": 0, "lines": [], "reasons": []})
        self.assertEqual(len(report["input_sha256"]), 64)

        count = self.service.db.execute("SELECT count(*) FROM chip_tests").fetchone()[0]
        self.assertEqual(count, 3)
        stored = self.service.db.execute(
            "SELECT wavelength_nm,responsivity_aw,dark_current_a FROM chip_tests WHERE chip_id='C1'"
        ).fetchone()
        self.assertEqual(tuple(stored), (1550.0, 0.9, 2.5e-9))

        audits = self.service.db.execute("SELECT * FROM import_audits").fetchall()
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["status"], "completed")
        self.assertEqual(json.loads(audits[0]["line_numbers"]),
                         {"succeeded": [1, 2, 3], "duplicate": [], "failed": []})
        fetched = self.service.get_import_audit(self.token, report["import_audit_id"])
        self.assertEqual(fetched["source_name"], "day-1.jsonl")

    def test_duplicate_rows_return_original_record(self) -> None:
        first = self.service.import_chip_tests(
            self.token, "day-1.jsonl", "\n".join([row("C1"), row("C0")])
        )
        self.assertEqual(first["succeeded"]["count"], 2)

        content = "\n".join([
            row("C1").replace("PD-1", "PD-9"),  # 与库中已有记录重复，且内容被篡改
            row("C2"),
            row("C0"),  # 另一条库中已有记录
        ])
        report = self.service.import_chip_tests(self.token, "day-2.jsonl", content)
        self.assertEqual(report["succeeded"], {"count": 1, "lines": [2]})
        self.assertEqual(report["duplicates"]["count"], 2)
        self.assertEqual(report["duplicates"]["lines"], [1, 3])
        originals = report["duplicates"]["records"]
        self.assertEqual({item["line"] for item in originals}, {1, 3})
        by_chip = {item["record"]["chip_id"]: item["record"] for item in originals}
        self.assertEqual(set(by_chip), {"C1", "C0"})
        # 返回的是原记录，而不是本次文件里伪造的仪器编号
        self.assertEqual(by_chip["C1"]["instrument_id"], "PD-1")
        for record in by_chip.values():
            self.assertEqual(record["import_audit_id"], first["import_audit_id"])

        count = self.service.db.execute("SELECT count(*) FROM chip_tests").fetchone()[0]
        self.assertEqual(count, 3)

    def test_any_invalid_line_rolls_back_entire_batch(self) -> None:
        content = "\n".join([
            row("C1"),
            row("C2").replace('"unit":"nm"', '"unit":"GHz"'),  # 非法单位
            row("C3"),
        ])
        with self.assertRaises(ImportRejected) as caught:
            self.service.import_chip_tests(self.token, "bad.jsonl", content)
        report = caught.exception.report
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["failed"]["count"], 1)
        self.assertEqual(report["failed"]["lines"], [2])
        self.assertIn("unit", report["failed"]["reasons"][0]["reason"])

        # 半批数据不得残留
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM chip_tests").fetchone()[0], 0)
        # 拒绝也必须留下恰好一次可追溯审计事件
        audit_rows = self.service.db.execute(
            "SELECT * FROM import_audits WHERE status='rejected'"
        ).fetchall()
        self.assertEqual(len(audit_rows), 1)
        self.assertEqual(json.loads(audit_rows[0]["line_numbers"]),
                         {"succeeded": [], "duplicate": [], "failed": [2]})
        self.assertEqual(audit_rows[0]["succeeded_count"], 0)
        self.assertEqual(audit_rows[0]["input_sha256"], report["input_sha256"])

    def test_malformed_json_line_is_rejected_batch(self) -> None:
        content = row("C1") + "\n{broken\n"
        with self.assertRaises(ImportRejected) as caught:
            self.service.import_chip_tests(self.token, "broken.jsonl", content)
        self.assertEqual(caught.exception.report["failed"]["lines"], [2])
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM chip_tests").fetchone()[0], 0)

    def test_in_file_duplicate_is_invalid_and_rolls_back(self) -> None:
        content = row("C1") + "\n" + row("C1") + "\n"
        with self.assertRaises(ImportRejected) as caught:
            self.service.import_chip_tests(self.token, "dup.jsonl", content)
        self.assertEqual(caught.exception.report["failed"]["lines"], [2])
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM chip_tests").fetchone()[0], 0)

    def test_empty_file_rejected_without_audit(self) -> None:
        with self.assertRaises(ValueError):
            self.service.import_chip_tests(self.token, "empty.jsonl", "  \n")
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM import_audits").fetchone()[0], 0)

    def test_rejected_batch_does_not_block_later_import(self) -> None:
        with self.assertRaises(ImportRejected):
            self.service.import_chip_tests(self.token, "bad.jsonl", row("C1").replace("0.9", "-1"))
        report = self.service.import_chip_tests(self.token, "good.jsonl", row("C1"))
        self.assertEqual(report["succeeded"]["count"], 1)
        statuses = [
            r[0] for r in self.service.db.execute(
                "SELECT status FROM import_audits ORDER BY import_audit_id"
            ).fetchall()
        ]
        self.assertEqual(statuses, ["rejected", "completed"])

    def test_persists_to_file_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "photon.sqlite3")
            service = PhotonService(path)
            service.bootstrap_admin()
            token = service.auth.login("admin", "photon-admin")
            service.import_chip_tests(token, "day-1.jsonl", row("C1"))
            service.db.close()

            reopened = PhotonService(path)
            count = reopened.db.execute("SELECT count(*) FROM chip_tests").fetchone()[0]
            audits = reopened.db.execute("SELECT count(*) FROM import_audits").fetchone()[0]
            reopened.db.close()
            self.assertEqual((count, audits), (1, 1))

    def test_operator_can_import_invalid_token_cannot(self) -> None:
        self.service.auth.create_user("op", "operator-pass", "operator")
        token = self.service.auth.login("op", "operator-pass")
        report = self.service.import_chip_tests(token, "day-1.jsonl", row("C1"))
        self.assertEqual(report["status"], "completed")
        with self.assertRaises(PermissionError):
            self.service.import_chip_tests("not-a-token", "day-2.jsonl", row("C2"))

    def test_fixture_file_imports(self) -> None:
        content = (ROOT / "fixtures" / "demo_chip_tests.jsonl").read_text(encoding="utf-8")
        report = self.service.import_chip_tests(self.token, "demo_chip_tests.jsonl", content)
        self.assertEqual(report["succeeded"]["count"], 3)


class _StubHandler(Handler):
    """绕过 socket 初始化的 Handler 测试桩。"""

    def __init__(self, service: PhotonService, token: str) -> None:
        self.service = service
        self._token = token
        self.status_code: int | None = None
        self.log_message = lambda *args: None  # type: ignore[assignment]

    def send_response(self, code: int) -> None:  # type: ignore[override]
        self.status_code = code

    def send_header(self, name: str, value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def post(self, path: str, raw: bytes, content_type: str = "application/x-ndjson",
             source: str | None = None) -> tuple[int, dict]:
        self.path = path
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": content_type,
            "Content-Length": str(len(raw)),
        }
        if source:
            headers["X-Import-Source"] = source
        self.headers = headers  # type: ignore[assignment]
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.do_POST()
        assert self.status_code is not None
        return self.status_code, json.loads(self.wfile.getvalue())


class ImportHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PhotonService(":memory:")
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "photon-admin")
        self.handler = _StubHandler(self.service, self.token)

    def tearDown(self) -> None:
        self.service.db.close()

    def test_ndjson_upload(self) -> None:
        status, body = self.handler.post(
            "/chip-tests/import", (row("C1") + "\n" + row("C2")).encode(), source="line.jsonl"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["succeeded"]["count"], 2)
        self.assertEqual(body["source_name"], "line.jsonl")

    def test_rejected_upload_returns_422_with_report(self) -> None:
        status, body = self.handler.post(
            "/chip-tests/import", (row("C1") + "\n{bad json").encode()
        )
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "import_rejected")
        self.assertEqual(body["import_result"]["failed"]["lines"], [2])
        # 拒绝后没有芯片记录落库
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM chip_tests").fetchone()[0], 0)

    def test_json_envelope_upload(self) -> None:
        payload = json.dumps({
            "source_name": "env.jsonl",
            "content": [json.loads(row("C1")), json.loads(row("C2"))],
        }).encode()
        status, body = self.handler.post("/chip-tests/import", payload, content_type="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(body["succeeded"]["count"], 2)
        self.assertEqual(body["source_name"], "env.jsonl")

    def test_import_audit_get_route(self) -> None:
        status, body = self.handler.post("/chip-tests/import", row("C1").encode())
        audit_id = body["import_audit_id"]
        self.handler.path = f"/imports/{audit_id}"
        self.handler.headers = {"Authorization": f"Bearer {self.token}"}  # type: ignore[assignment]
        self.handler.wfile = io.BytesIO()
        self.handler.do_GET()
        self.assertEqual(self.handler.status_code, 200)
        result = json.loads(self.handler.wfile.getvalue())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["succeeded_count"], 1)


if __name__ == "__main__":
    unittest.main()
