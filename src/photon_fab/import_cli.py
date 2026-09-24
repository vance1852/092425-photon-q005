"""封测线 JSONL 离线批量导入的命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys

from .importer import ImportRejected
from .service import PhotonService


def main() -> None:
    parser = argparse.ArgumentParser(description="离线导入封测线 JSONL 文件")
    parser.add_argument("--database", default="photon.sqlite3")
    parser.add_argument("--file", required=True, help="JSONL 文件路径")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="photon-admin")
    parser.add_argument("--source-name", default=None)
    args = parser.parse_args()

    with open(args.file, encoding="utf-8") as handle:
        content = handle.read()
    service = PhotonService(args.database)
    service.bootstrap_admin()
    token = service.auth.login(args.user, args.password)
    try:
        report = service.import_chip_tests(token, args.source_name or args.file, content)
    except ImportRejected as exc:
        report = exc.report
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    sys.exit(0 if report["status"] == "completed" else 2)


if __name__ == "__main__":
    main()
