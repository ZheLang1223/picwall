"""恢复完整备份到空目录，或在临时目录演练后自动清理。"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import backup, config

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path)
    parser.add_argument("--batch", type=Path)
    args = parser.parse_args()
    batch = args.batch or backup.latest(config.DATA_DIR / "backups")
    if args.target:
        result = backup.restore(batch, args.target)
    else:
        with tempfile.TemporaryDirectory(prefix="picwall-restore-") as tmp:
            result = backup.restore(batch, Path(tmp))
    print(json.dumps(result, ensure_ascii=False))
