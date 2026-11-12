"""图片墙完整备份入口（仓库根目录 make picwall-backup 或直接跑）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backup import create
from tools.runtime import delegate

if __name__ == "__main__":
    if delegate("backup.py"):
        sys.exit(0)
    print(json.dumps(create(), ensure_ascii=False))
