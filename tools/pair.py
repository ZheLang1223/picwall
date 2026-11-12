"""签发设备配对码；只在操作者的终端显示，不写日志。"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import auth, db
from tools.runtime import delegate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", default="editor", choices=sorted(auth.ROLES))
    parser.add_argument("--collector-token", action="store_true")
    args = parser.parse_args()
    if delegate("pair.py", sys.argv[1:]):
        return
    db.init_db()
    if args.collector_token:
        result = auth.exchange(auth.issue_code("collector"), "浏览器采集助手", "terminal")
        print("采集令牌（30 天有效，仅可导入；可在设备管理取消配对）：")
        print(result["token"])
        return
    print(f"设备配对码（{args.role}，10 分钟内有效，仅可使用一次）：{auth.issue_code(args.role)}")


if __name__ == "__main__":
    main()
