"""图片墙媒体只读审计：体积分布、重复文件、库-磁盘一致性。

不动任何数据——去重/清理决策由人看完报告后做。同尺寸才哈希（避免全量哈希）。
用法：项目根目录  DATA_DIR=$PWD/data .venv/bin/python tools/audit.py
（仓库入口：make picwall-audit）
"""
import hashlib
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(os.environ.get("PICWALL_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
PICS = DATA_DIR / "pics"
DB = DATA_DIR / "picwall.db"


def human(n: float) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    if not PICS.is_dir():
        print(f"目录不存在：{PICS}", file=sys.stderr)
        sys.exit(1)

    # 1) 文件系统盘点（按顶层目录 = 年份/avatars，同时构建一致性集合避免二次遍历）
    by_top = defaultdict(lambda: [0, 0])   # top -> [count, bytes]
    size_map = defaultdict(list)           # size -> [path]
    disk_files: set[str] = set()
    total_n = total_b = 0
    for p in PICS.rglob("*"):
        if p.is_symlink() or not p.is_file() or p.name.startswith("."):
            continue
        rel = p.relative_to(PICS).as_posix()
        disk_files.add(rel)
        s = p.stat().st_size
        top = p.relative_to(PICS).parts[0]
        by_top[top][0] += 1
        by_top[top][1] += s
        total_n += 1
        total_b += s
        size_map[s].append(p)

    print(f"媒体总量：{total_n} 个文件，{human(total_b)}")
    for top, (n, b) in sorted(by_top.items(), key=lambda kv: -kv[1][1]):
        print(f"  {top:<10} {n:>6} 个  {human(b)}")

    # 2) 重复检测：同尺寸分桶 → 桶内哈希
    dup_groups, dup_bytes = 0, 0
    for size, paths in size_map.items():
        if len(paths) < 2 or size < 4096:      # <4K 的缩略图不统计
            continue
        by_hash = defaultdict(list)
        for p in paths:
            by_hash[md5(p)].append(p)
        for group in by_hash.values():
            if len(group) > 1:
                dup_groups += 1
                dup_bytes += size * (len(group) - 1)
    print(f"重复文件（≥4K，内容级去重）：{dup_groups} 组，可回收 {human(dup_bytes)}")

    # 3) 库-磁盘一致性（只读）
    if DB.exists():
        with sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True) as c:
            db_files = {r[0] for r in c.execute("SELECT file FROM pics")}
            try:  # thumb 列有单独的缩略图文件，一并计入
                db_files |= {r[0] for r in c.execute("SELECT thumb FROM pics")
                             if r[0]}
            except sqlite3.Error:
                pass
            db_count = c.execute("SELECT count(*) FROM pics").fetchone()[0]
            avatar_files = {r[0].removeprefix('/media/') for r in c.execute(
                "SELECT avatar FROM posts WHERE avatar LIKE '/media/%'")}
            db_files |= avatar_files
        avatar_cache = {p for p in disk_files if p.startswith('avatars/')}
        orphan_disk = disk_files - db_files - avatar_cache
        missing_db = db_files - disk_files
        print(f"库-磁盘：pics 表 {db_count} 行；磁盘不在库 {len(orphan_disk)} 个，"
              f"库指向但磁盘缺失 {len(missing_db)} 个")
        print(f"头像缓存 {len(avatar_cache)} 个（单列，不计作可删孤儿）；头像引用缺失 {len(avatar_files-disk_files)} 个")
    print("（只读审计，未做任何修改）")


if __name__ == "__main__":
    main()
