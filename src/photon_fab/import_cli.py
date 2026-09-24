"""封测 JSONL 离线批量导入命令行入口。

不依赖 HTTP 服务或任何外部进程，直接在本地 SQLite 数据库上完成
逐行校验、单事务写入和导入审计事件登记：

    PYTHONPATH=src python3 -m photon_fab.import_cli \
        --database photon.sqlite3 --file fixtures/chip_test_demo.jsonl \
        --user admin --password photon-admin --note "2026-09-24 封测日报"

退出码：0 全部合法（可能含重复行），2 存在非法行（整批拒绝）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .service import BatchRejected, PhotonService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="封测线 JSONL 离线批量导入")
    parser.add_argument("--database", default=":memory:", help="SQLite 数据库路径")
    parser.add_argument("--file", required=True, type=Path, help="JSONL 文件路径，- 表示标准输入")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="photon-admin")
    parser.add_argument("--note", default=None)
    parser.add_argument("--bootstrap-admin", action="store_true",
                        help="数据库全新时先建立默认管理员（admin/photon-admin）")
    args = parser.parse_args(argv)

    service = PhotonService(args.database)
    if args.bootstrap_admin:
        service.bootstrap_admin()
    token = service.auth.login(args.user, args.password)

    if str(args.file) == "-":
        content = sys.stdin.read()
    else:
        content = args.file.read_text(encoding="utf-8")

    try:
        report = service.import_chip_records(token, content, args.note)
    except BatchRejected as rejected:
        print(json.dumps(rejected.report, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
