"""用于离线验收的无依赖 JSON HTTP API。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from .service import BatchRejected, PhotonService


# 单线程服务器：所有请求复用同一个 SQLite 连接，且连接始终在本线程使用，
# 规避跨线程连接限制；离线批量后台没有慢速外部调用，串行处理即足够。
Server = HTTPServer


class Handler(BaseHTTPRequestHandler):
    service = PhotonService()

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"status": "ok", "service": "photon-fab"})
        if self.path.startswith("/chip-imports/"):
            import_id = self.path.split("/", 2)[2]
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            try:
                return self._json(200, self.service.get_import(token, import_id))
            except PermissionError as exc:
                return self._json(403, {"error": str(exc)})
            except KeyError:
                return self._json(404, {"error": "import not found"})
            except Exception as exc:
                return self._json(400, {"error": str(exc)})
        if self.path.startswith("/lots/"):
            try:
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                return self._json(200, self.service.get_lot(token, self.path.split("/", 2)[2]))
            except Exception as exc:
                return self._json(400, {"error": str(exc)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path == "/chip-imports":
                # 请求体是原始 JSONL（Content-Type: application/x-ndjson）。
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                note = self.headers.get("X-Import-Note")
                try:
                    report = self.service.import_chip_records(token, raw.decode("utf-8"), note)
                except BatchRejected as rejected:
                    return self._json(422, rejected.report)
                return self._json(200, report)
            body = json.loads(raw)
            if self.path == "/login":
                return self._json(200, {"token": self.service.auth.login(body["user_id"], body["password"])})
            token = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if self.path == "/lots":
                return self._json(201, self.service.create_lot(token, body["lot_id"], body["product"], body["process_rev"], body["wafer_count"]))
            if self.path.startswith("/lots/") and self.path.endswith("/measurements"):
                lot_id = self.path.split("/")[2]
                return self._json(201, self.service.add_measurement(token, lot_id, body["wavelength_nm"], body["response"], body.get("noise", 0.0), body["instrument"]))
            if self.path.startswith("/lots/") and self.path.endswith("/analysis"):
                return self._json(200, self.service.analyze(token, self.path.split("/")[2]))
            return self._json(404, {"error": "not found"})
        except PermissionError as exc:
            return self._json(403, {"error": str(exc)})
        except Exception as exc:
            return self._json(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=":memory:")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    Handler.service = PhotonService(args.database)
    Handler.service.bootstrap_admin()
    # serve_forever 与上面的连接创建都在主线程，请求被串行分发，连接永不跨线程。
    Server((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
