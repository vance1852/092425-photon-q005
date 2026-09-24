from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from photon_fab.api import Handler, Server
from photon_fab.service import PhotonService


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "chip_test_demo.jsonl"


class ChipImportHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.database = str(Path(self._directory.name) / "photon.sqlite3")
        # 主线程预置管理员后立即关闭连接；服务器线程再打开独立连接。
        seeder = PhotonService(self.database)
        seeder.bootstrap_admin()
        seeder.db.close()

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.ready.wait(timeout=2)

        _, login = self._request("POST", "/login", {"user_id": "admin", "password": "photon-admin"})
        self.token = login["token"]

    def _serve(self) -> None:
        # 连接在服务器线程内创建，请求也在该单线程服务器上处理。
        Handler.service = PhotonService(self.database)
        self.ready.set()
        self.server.serve_forever()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self._directory.cleanup()

    def _request(self, method: str, path: str, body=None, raw: bytes | None = None,
                 note: str | None = None):
        headers = {}
        if hasattr(self, "token"):
            headers["Authorization"] = f"Bearer {self.token}"
        if raw is not None:
            data = raw
            headers["Content-Type"] = "application/x-ndjson"
            if note:
                headers["X-Import-Note"] = note
        else:
            data = json.dumps(body).encode() if body is not None else None
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_import_accept_replay_and_trace(self) -> None:
        content = FIXTURE.read_text(encoding="utf-8").encode()
        status, report = self._request("POST", "/chip-imports", raw=content, note="nightly")
        self.assertEqual(status, 200)
        self.assertEqual(report["inserted_count"], 4)
        import_id = report["import_id"]

        status, replay = self._request("POST", "/chip-imports", raw=content)
        self.assertEqual(status, 200)
        self.assertEqual(replay["duplicate_count"], 4)
        self.assertEqual(replay["duplicate_lines"], [1, 2, 3, 4])
        self.assertEqual(replay["duplicates"][0]["record"]["chip_id"], "PIC-A-0001")

        status, trace = self._request("GET", f"/chip-imports/{import_id}")
        self.assertEqual(status, 200)
        self.assertEqual(trace["status"], "accepted")
        self.assertEqual(trace["note"], "nightly")
        self.assertEqual(trace["inserted_lines"], [1, 2, 3, 4])

    def test_invalid_batch_returns_422_with_line_report(self) -> None:
        bad = FIXTURE.read_text(encoding="utf-8") + "{not json}\n"
        status, report = self._request("POST", "/chip-imports", raw=bad.encode())
        self.assertEqual(status, 422)
        self.assertEqual(report["status"], "rejected")
        self.assertEqual(report["failed_lines"], [5])
        self.assertEqual(report["inserted_lines"], [])

    def test_trace_unknown_import_returns_404(self) -> None:
        status, body = self._request("GET", "/chip-imports/does-not-exist")
        self.assertEqual(status, 404)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
