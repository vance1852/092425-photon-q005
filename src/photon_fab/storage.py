"""芯片批次和测量记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS chip_lots(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 wafer_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES chip_lots(lot_id),
 wavelength_nm REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
CREATE TABLE IF NOT EXISTS import_audits(
 import_audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_name TEXT NOT NULL, actor TEXT NOT NULL, status TEXT NOT NULL,
 total_lines INTEGER NOT NULL, succeeded_count INTEGER NOT NULL,
 duplicate_count INTEGER NOT NULL, failed_count INTEGER NOT NULL,
 line_numbers TEXT NOT NULL, input_sha256 TEXT NOT NULL,
 detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chip_tests(
 chip_id TEXT PRIMARY KEY,
 wavelength_nm REAL NOT NULL,
 responsivity_aw REAL NOT NULL,
 dark_current_a REAL NOT NULL,
 instrument_id TEXT NOT NULL,
 imported_by TEXT NOT NULL,
 import_audit_id INTEGER NOT NULL REFERENCES import_audits(import_audit_id),
 created_at TEXT NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ThreadingHTTPServer 会在工作线程中共享同一个连接；用进程级可重入锁
# 串行化所有写事务，避免多线程在同一连接上交错执行 BEGIN/COMMIT。
# 离线后台吞吐有限，跨连接串行写入没有实际代价。
_WRITE_LOCK = threading.RLock()


def write_lock(db: sqlite3.Connection | None = None):
    return _WRITE_LOCK


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    with _WRITE_LOCK:
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))
