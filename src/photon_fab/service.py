"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Sequence

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .importer import ImportRejected, parse_jsonl
from .storage import connect, event, transaction, utcnow


class PhotonService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "photon-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, wafer_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if wafer_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute("INSERT INTO chip_lots VALUES(?,?,?,?,?,?,?,?)", (lot_id, product, process_rev, wafer_count, "engineering", actor.user_id, now, now))
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(self, token: str, lot_id: str, wavelength_nm: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM chip_lots WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(wavelength_nm), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "wavelength_nm": wavelength_nm})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT wavelength_nm,response FROM measurements WHERE lot_id=? ORDER BY wavelength_nm", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_spectrum([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["wafer_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "spectrum": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(self, token: str, lot_id: str, decision: str, reason: str) -> dict:
        actor = self.auth.require(token, "approve")
        if decision not in {"release", "hold", "reject"} or not reason.strip():
            raise ValueError("decision and reason are required")
        with transaction(self.db):
            self.db.execute("INSERT OR REPLACE INTO approvals VALUES(?,?,?,?,?)", (lot_id, actor.user_id, decision, reason, utcnow()))
            status = {"release": "released", "hold": "hold", "reject": "rejected"}[decision]
            self.db.execute("UPDATE chip_lots SET status=?,updated_at=? WHERE lot_id=?", (status, utcnow(), lot_id))
            event(self.db, lot_id, "approval", actor.user_id, {"decision": decision, "reason": reason})
        return self.get_lot(token, lot_id)

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]

    def import_chip_tests(self, token: str, source_name: str, content: str) -> dict:
        """离线批量导入封测线 JSONL。

        逐行校验字段与单位：存在任何非法行时整批拒绝，chip_tests 不留下
        任何数据；全部合法时在一个事务内完成查重与写入，重复行返回原记录。
        无论完成或拒绝都恰好写入一次导入审计事件。
        """
        actor = self.auth.require(token, "measure")
        if not isinstance(source_name, str) or not source_name.strip():
            raise ValueError("source_name is required")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty JSONL string")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

        entries, failures = parse_jsonl(content)
        total_lines = len(entries) + len(failures)
        if total_lines == 0:
            raise ValueError("no records found in file")

        def _report(status: str) -> dict:
            return {
                "status": status,
                "source_name": source_name,
                "input_sha256": digest,
                "total_lines": total_lines,
                "succeeded": {"count": 0, "lines": []},
                "duplicates": {"count": 0, "lines": [], "records": []},
                "failed": {
                    "count": len(failures),
                    "lines": [item.line for item in failures],
                    "reasons": [item.as_dict() for item in failures],
                },
            }

        if failures:
            report = _report("rejected")
            with transaction(self.db):
                audit_id = self._write_import_audit(actor.user_id, report, line_numbers={
                    "succeeded": [],
                    "duplicate": [],
                    "failed": report["failed"]["lines"],
                }, detail={"failures": report["failed"]["reasons"]})
            report["import_audit_id"] = audit_id
            raise ImportRejected(report)

        report = _report("completed")
        report["failed"] = {"count": 0, "lines": [], "reasons": []}
        with transaction(self.db):
            # 先建审计事件，chip_tests 通过外键指向它；最终计数在提交前回填。
            audit_id = self._write_import_audit(
                actor.user_id, report,
                line_numbers={"succeeded": [], "duplicate": [], "failed": []},
                detail={"duplicates": []},
            )
            now = utcnow()
            for line, record in entries:
                row = self.db.execute(
                    "SELECT * FROM chip_tests WHERE chip_id=?", (record.chip_id,)
                ).fetchone()
                if row:
                    report["duplicates"]["lines"].append(line)
                    report["duplicates"]["records"].append({"line": line, "record": dict(row)})
                    continue
                self.db.execute(
                    "INSERT INTO chip_tests VALUES(?,?,?,?,?,?,?,?)",
                    (record.chip_id, record.wavelength_nm, record.responsivity_aw,
                     record.dark_current_a, record.instrument_id, actor.user_id,
                     audit_id, now),
                )
                report["succeeded"]["lines"].append(line)
            report["succeeded"]["count"] = len(report["succeeded"]["lines"])
            report["duplicates"]["count"] = len(report["duplicates"]["lines"])
            self.db.execute(
                "UPDATE import_audits SET total_lines=?,succeeded_count=?,"
                "duplicate_count=?,failed_count=?,line_numbers=?,detail=? WHERE import_audit_id=?",
                (total_lines, report["succeeded"]["count"], report["duplicates"]["count"], 0,
                 json.dumps({
                     "succeeded": report["succeeded"]["lines"],
                     "duplicate": report["duplicates"]["lines"],
                     "failed": [],
                 }, sort_keys=True),
                 json.dumps({
                     "duplicates": report["duplicates"]["records"],
                 }, sort_keys=True),
                 audit_id),
            )
        report["import_audit_id"] = audit_id
        return report

    def _write_import_audit(self, actor: str, report: dict, line_numbers: dict, detail: dict) -> int:
        cursor = self.db.execute(
            "INSERT INTO import_audits(source_name,actor,status,total_lines,"
            "succeeded_count,duplicate_count,failed_count,line_numbers,input_sha256,"
            "detail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (report["source_name"], actor, report["status"], report["total_lines"],
             report["succeeded"]["count"], report["duplicates"]["count"],
             report["failed"]["count"], json.dumps(line_numbers, sort_keys=True),
             report["input_sha256"], json.dumps(detail, sort_keys=True), utcnow()),
        )
        return int(cursor.lastrowid)

    def get_import_audit(self, token: str, import_audit_id: int) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute(
            "SELECT * FROM import_audits WHERE import_audit_id=?", (import_audit_id,)
        ).fetchone()
        if not row:
            raise KeyError(import_audit_id)
        result = dict(row)
        result["line_numbers"] = json.loads(result["line_numbers"])
        result["detail"] = json.loads(result["detail"])
        return result
