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
import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app import config, db, media_fetch, storage, weibo
from tools import runtime

SCALED = re.compile(r"/(orj|sti)\d+/")   # 缩放档特征（用于清单展示；改写以 _to_large 为准）


def _safe_pic_path(base: Path, rel: str) -> Path | None:
    if not rel:
        return None
    p = Path(rel)
    if p.is_absolute():
        return None
    root = base.resolve()
    resolved = (root / p).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        return None
    return resolved


async def _fetch_all(urls: list[str]) -> dict[str, bytes | None]:
    sem = asyncio.Semaphore(8)
    headers = {"User-Agent": weibo.UA, "Referer": weibo.BASE + "/"}
    out: dict[str, bytes | None] = {}
    done = 0
    t0 = time.monotonic()

    async def one(client: httpx.AsyncClient, u: str) -> None:
        nonlocal done
        async with sem:
            data = await media_fetch.fetch(client, u, headers)
        out[u] = data if data and len(data) >= 100 else None
        done += 1
        if done % 100 == 0:
            print(f"  下载 {done}/{len(urls)}  用时 {time.monotonic() - t0:.0f}s")

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        await asyncio.gather(*(one(client, u) for u in urls))
    return out


def fetch_large(url: str, data: bytes | None) -> tuple[str, int, int, str] | None:
    """校验像素/字节并按统一策略原子落盘 + 生成缩略图；失败返回 None。"""
    if not data or not media_fetch.permitted(url):
        return None
    try:
        return storage.save_image(data, url)
    except (ValueError, OSError):
        return None


def refresh_export(old_rel: str, new_rel: str) -> int:
    """export/<日>/<旧文件名> 的保留副本 → 换成原图档内容并改名。"""
    src = _safe_pic_path(config.PICS_DIR, new_rel)
    if src is None or not src.is_file():
        return 0
    old_name, new_name = Path(old_rel).name, Path(new_rel).name
    if not old_name or old_name == new_name or not config.EXPORT_DIR.is_dir():
        return 0
    export_root = config.EXPORT_DIR.resolve()
    n = 0
    for old_copy in config.EXPORT_DIR.glob(f"*/{old_name}"):
        if old_copy.is_symlink() or not old_copy.is_file():
            continue
        new_copy = old_copy.with_name(new_name)
        if not new_copy.resolve().is_relative_to(export_root):
            continue
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(prefix=".export-", dir=new_copy.parent)
            os.close(fd)
            shutil.copy2(src, tmp)
            os.replace(tmp, new_copy)
            tmp = None
            old_copy.unlink(missing_ok=True)
            n += 1
        except OSError:
            continue
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
    return n


def main():
    runtime.require_stopped_container("原图迁移")
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT id, file, thumb, url FROM pics")]
    limit = int(os.environ.get("MIGRATE_LIMIT", "0"))   # 冒烟测试用：只迁前 N 张
    plan = []
    for row in rows:
        large = weibo._to_large(row["url"])
        if large != row["url"] and media_fetch.permitted(large):
            plan.append((row, large))
        if limit and len(plan) >= limit:
            break
    print(f"库里 {len(rows)} 张，其中 {len(plan)} 张可改写为 large 档"
          + (f"（MIGRATE_LIMIT={limit}）" if limit else ""))
    if not plan:
        return

    unique = sorted({large for _, large in plan})
    print(f"去重后需下载 {len(unique)} 张，8 并发开跑…")
    raw_map = asyncio.run(_fetch_all(unique))
    results: dict[str, tuple[str, int, int, str] | None] = {
        u: fetch_large(u, raw_map.get(u)) for u in unique
    }

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
        with db._write_lock, db.conn() as c:
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
        old_file_path = _safe_pic_path(config.PICS_DIR, old_file) if n_file == 0 else None
        if old_file_path is not None:
            old_file_path.unlink(missing_ok=True)
            deleted += 1
        old_thumb_path = (
            _safe_pic_path(config.PICS_DIR, old_thumb)
            if old_thumb and old_thumb != old_file and n_thumb == 0
            else None
        )
        if old_thumb_path is not None:
            old_thumb_path.unlink(missing_ok=True)

    print(f"\n迁移完成：成功 {done}，失败 {fail}；export 副本刷新 {exported} 份，"
          f"清理旧原图 {deleted} 个")
    if failures:
        print("失败样例（最多 10 条，原状保留）：")
        for u in failures[:10]:
            print("  " + u)


if __name__ == "__main__":
    main()
