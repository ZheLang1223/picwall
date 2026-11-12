"""本机媒体修复与深度健康检查入口。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, maintenance
from tools.runtime import delegate

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    if delegate("maintenance.py", sys.argv[1:]):
        sys.exit(0)
    db.init_db()
    if args.repair:
        print(json.dumps(maintenance.repair_local(), ensure_ascii=False))
    else:
        report = maintenance.health()
        report["archive"].pop("files", None)
        print(json.dumps(report, ensure_ascii=False))
