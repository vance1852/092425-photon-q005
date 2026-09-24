"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Sequence

from .analytics import confidence_interval, summarize_spectrum, yield_rate
from .auth import Auth
from .chip_import import (
    ChipRecord,
    ChipRecordError,
    content_digest,
    parse_jsonl,
    record_digest,
    record_view,
    stored_record_view,
    validate_record,
)
from .storage import connect, event, transaction, utcnow


class BatchRejected(RuntimeError):
    """存在非法行，整批拒绝；report 携带逐行结果供接口返回。"""

    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("batch rejected")
        self.report = report


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

    # ------------------------------------------------------------------
    # 封测线 JSONL 离线批量导入
    # ------------------------------------------------------------------

    def import_chip_records(self, token: str, content: str, note: str | None = None) -> dict:
        """逐行校验封测 JSONL 并在单个事务内写入全部合法新记录。

        - 任意非法行：整批拒绝，业务表零写入（无半批数据）；
        - 重复行（芯片编号已存在且内容指纹一致）：不写入，返回原记录；
        - 同一芯片编号内容不一致（文件内或与库内）：冲突，记为失败行；
        - 无论接受或拒绝，都落一次可追溯的导入审计事件。
        """
        actor = self.auth.require(token, "measure")
        parsed = parse_jsonl(content)
        if not parsed:
            raise ValueError("导入文件没有任何有效数据行")
        source_sha = content_digest(content)

        new_records: list[tuple[int, ChipRecord]] = []
        duplicate_lines: list[int] = []
        duplicate_records: list[dict] = []
        failed_lines: list[int] = []
        failures: list[dict] = []
        seen_in_file: dict[str, tuple[int, ChipRecord]] = {}

        for line_number, value, parse_error in parsed:
            if parse_error is not None:
                failed_lines.append(line_number)
                failures.append({"line": line_number, "error": parse_error})
                continue
            try:
                record = validate_record(value)
            except ChipRecordError as exc:
                failed_lines.append(line_number)
                failures.append({"line": line_number, "error": str(exc)})
                continue
            earlier = seen_in_file.get(record.chip_id)
            if earlier is not None:
                first_line, first_record = earlier
                if record_digest(record) == record_digest(first_record):
                    # 文件内重复发送的同一行，幂等返回首次出现的记录。
                    duplicate_lines.append(line_number)
                    duplicate_records.append({"line": line_number, "record": record_view(first_record)})
                else:
                    failed_lines.append(line_number)
                    failures.append({"line": line_number,
                                     "error": f"芯片编号在文件内重复但内容不一致，首次出现在第 {first_line} 行"})
                continue
            row = self.db.execute(
                "SELECT * FROM chip_test_records WHERE chip_id=?", (record.chip_id,)
            ).fetchone()
            if row is None:
                seen_in_file[record.chip_id] = (line_number, record)
                new_records.append((line_number, record))
            elif row["content_sha256"] == record_digest(record):
                duplicate_lines.append(line_number)
                duplicate_records.append({"line": line_number, "record": stored_record_view(row)})
            else:
                failed_lines.append(line_number)
                failures.append({"line": line_number,
                                 "error": "芯片编号已存在，但波长、响应度、暗电流或仪器编号与原记录不一致"})

        report: dict[str, Any] = {
            "total_lines": len(parsed),
            "inserted_count": len(new_records),
            "duplicate_count": len(duplicate_lines),
            "failed_count": len(failed_lines),
            "inserted_lines": [line for line, _ in new_records],
            "duplicate_lines": duplicate_lines,
            "failed_lines": failed_lines,
            "failures": failures,
            "duplicates": duplicate_records,
            "source_sha256": source_sha,
        }
        import_id = uuid.uuid4().hex
        now = utcnow()

        with transaction(self.db):
            # BEGIN IMMEDIATE 后复查，消除预检与写入之间的并发插入窗口。
            to_insert: list[ChipRecord] = []
            if not failed_lines:
                for line_number, record in new_records:
                    row = self.db.execute(
                        "SELECT * FROM chip_test_records WHERE chip_id=?", (record.chip_id,)
                    ).fetchone()
                    if row is None:
                        to_insert.append(record)
                    elif row["content_sha256"] == record_digest(record):
                        report["inserted_lines"].remove(line_number)
                        report["duplicate_lines"].append(line_number)
                        report["duplicates"].append({"line": line_number, "record": stored_record_view(row)})
                    else:
                        report["inserted_lines"].remove(line_number)
                        report["failed_lines"].append(line_number)
                        report["failures"].append({"line": line_number, "error": "芯片编号与并发导入的记录冲突"})
            rejected = bool(report["failed_lines"])
            report["duplicate_lines"].sort()
            report["failed_lines"].sort()
            if rejected:
                # 整批拒绝时预检为“新记录”的行同样不会写入，不能报告为成功。
                report["inserted_lines"] = []
            else:
                report["inserted_lines"].sort()
            report["inserted_count"] = len(report["inserted_lines"])
            report["duplicate_count"] = len(report["duplicate_lines"])
            report["failed_count"] = len(report["failed_lines"])
            if not rejected:
                # 仅当没有任何非法行时才写业务表，杜绝半批数据。
                for record in to_insert:
                    wavelength, responsivity, dark_current = record.measurement_values()
                    self.db.execute(
                        "INSERT INTO chip_test_records VALUES(?,?,?,?,?,?,?,?)",
                        (
                            record.chip_id, wavelength, responsivity, dark_current,
                            record.instrument_id, actor.user_id, now, record_digest(record),
                        ),
                    )
            self.db.execute(
                "INSERT INTO imports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    import_id, "rejected" if rejected else "accepted",
                    report["total_lines"], report["inserted_count"], report["duplicate_count"],
                    report["failed_count"], json.dumps(report["inserted_lines"]),
                    json.dumps(report["duplicate_lines"]), json.dumps(report["failed_lines"]),
                    json.dumps(report["duplicates"], ensure_ascii=False),
                    json.dumps(report["failures"], ensure_ascii=False),
                    actor.user_id, source_sha, now, note,
                ),
            )

        report["import_id"] = import_id
        report["status"] = "rejected" if rejected else "accepted"
        if rejected:
            raise BatchRejected(report)
        return report

    def get_import(self, token: str, import_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM imports WHERE import_id=?", (import_id,)).fetchone()
        if not row:
            raise KeyError(import_id)
        return {
            "import_id": row["import_id"],
            "status": row["status"],
            "total_lines": row["total_lines"],
            "inserted_count": row["inserted_count"],
            "duplicate_count": row["duplicate_count"],
            "failed_count": row["failed_count"],
            "inserted_lines": json.loads(row["inserted_lines"]),
            "duplicate_lines": json.loads(row["duplicate_lines"]),
            "failed_lines": json.loads(row["failed_lines"]),
            "duplicates": json.loads(row["duplicates_json"]),
            "failures": json.loads(row["failures_json"]),
            "actor": row["actor"],
            "source_sha256": row["source_sha256"],
            "created_at": row["created_at"],
            "note": row["note"],
        }
