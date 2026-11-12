"""一次性迁移：把存量 orj960/sti960 等缩放档图重取为 /large/ 原图档。

背景：extract_pics 曾漏改写 orj960/sti960，全库存的都是 960px 缩放图
（同图实测 orj960≈152KB vs large≈903KB）。对 pics 表中 url 仍可改写为
large 的行：
  1. url 改写为 /large/ 重新下载（带 Referer，过滤规则与抓取一致）
  2. 新字节 → 内容寻址新文件 + 重生成缩略图，更新该行 file/thumb/w/h/url
  3. 同步刷新 data/export/ 下按旧文件名归档的保留副本
  4. 旧原图/旧缩略图在零引用后删除；下载失败保留原状并计入失败清单
幂等：只处理 url 仍可改写的行，跑完再跑是无操作。
用法：项目根目录  .venv/bin/python tools/migrate_to_large.py
"""
import hashlib
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from PIL import Image

from app import config, db, storage, weibo

SCALED = re.compile(r"/(orj|sti)\d+/")   # 缩放档特征（用于清单展示；改写以 _to_large 为准）
WRITE_LOCK = threading.Lock()


def fetch_large(client: httpx.Client, url: str):
    """下载 large 档并落盘+生成缩略图，返回 (file_rel, w, h, thumb_rel)；失败 None。"""
    try:
        r = client.get(url, headers={"User-Agent": weibo.UA,
                                     "Referer": weibo.BASE + "/"})
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    ct = r.headers.get("content-type", "")
    if "html" in ct or "json" in ct or "text/" in ct or len(r.content) < 100:
        return None
    data = r.content
    h = hashlib.sha1(data).hexdigest()
    ym = datetime.now().strftime("%Y/%m")
    ext = storage.ext_of(url, data)
    rel = f"{ym}/{h}{ext}"
    t_rel = f"{ym}/t_{h}.jpg"
    path = config.PICS_DIR / rel
    with WRITE_LOCK:   # 不同 URL 可能解出相同内容，同路径并发写要串行
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if not (config.PICS_DIR / t_rel).exists() and \
                not storage._make_thumb(path, config.PICS_DIR / t_rel):
            t_rel = rel
    try:
        with Image.open(path) as im:
            w, hh = im.size
    except Exception:
        w = hh = 0
    return rel, w, hh, t_rel


def refresh_export(old_rel: str, new_rel: str) -> int:
    """export/<日>/<旧文件名> 的保留副本 → 换成原图档内容并改名。"""
    old_name, new_name = Path(old_rel).name, Path(new_rel).name
    if old_name == new_name or not config.EXPORT_DIR.is_dir():
        return 0
    n = 0
    for old_copy in config.EXPORT_DIR.glob(f"*/{old_name}"):
        try:
            new_copy = old_copy.with_name(new_name)
            shutil.copy2(config.PICS_DIR / new_rel, new_copy)
            old_copy.unlink(missing_ok=True)
            n += 1
        except OSError:
            continue
    return n


def main():
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT id, file, thumb, url FROM pics")]
    limit = int(os.environ.get("MIGRATE_LIMIT", "0"))   # 冒烟测试用：只迁前 N 张
    plan = []
    for row in rows:
        large = weibo._to_large(row["url"])
        if large != row["url"]:
            plan.append((row, large))
        if limit and len(plan) >= limit:
            break
    print(f"库里 {len(rows)} 张，其中 {len(plan)} 张可改写为 large 档"
          + (f"（MIGRATE_LIMIT={limit}）" if limit else ""))
    if not plan:
        return

    unique = sorted({large for _, large in plan})
    print(f"去重后需下载 {len(unique)} 张，8 线程开跑…")
    results: dict[str, tuple | None] = {}
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as ex:
            for i, (url, res) in enumerate(
                    zip(unique, ex.map(lambda u: fetch_large(client, u), unique)), 1):
                results[url] = res
                if i % 100 == 0:
                    print(f"  下载 {i}/{len(unique)}  用时 {time.monotonic() - t0:.0f}s")

    done = fail = exported = 0
    failures: list[str] = []
    old_refs: list[tuple[str, str]] = []
    for row, large in plan:
        hit = results.get(large)
        if hit is None:
            fail += 1
            failures.append(row["url"])
            continue
        new_rel, w, hh, new_thumb = hit
        with db.conn() as c:
            c.execute("UPDATE pics SET file=?, thumb=?, w=?, h=?, url=? WHERE id=?",
                      (new_rel, new_thumb, w, hh, large, row["id"]))
        exported += refresh_export(row["file"], new_rel)
        old_refs.append((row["file"], row["thumb"]))
        done += 1

    # 旧原图/旧缩略图零引用才删（多行可能共享同一文件）
    deleted = 0
    for old_file, old_thumb in sorted(set(old_refs)):
        with db.conn() as c:
            n_file = c.execute("SELECT COUNT(*) FROM pics WHERE file=?",
                               (old_file,)).fetchone()[0]
            n_thumb = c.execute("SELECT COUNT(*) FROM pics WHERE thumb=?",
                                (old_thumb,)).fetchone()[0]
        if old_file and n_file == 0:   # file 为空 = 原图下载失败的行，不能拼进 PICS_DIR
            (config.PICS_DIR / old_file).unlink(missing_ok=True)
            deleted += 1
        if old_thumb and old_thumb != old_file and n_thumb == 0:
            (config.PICS_DIR / old_thumb).unlink(missing_ok=True)

    print(f"\n迁移完成：成功 {done}，失败 {fail}；export 副本刷新 {exported} 份，"
          f"清理旧原图 {deleted} 个")
    if failures:
        print("失败样例（最多 10 条，原状保留）：")
        for u in failures[:10]:
            print("  " + u)


if __name__ == "__main__":
    main()
