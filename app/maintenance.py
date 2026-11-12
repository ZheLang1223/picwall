"""可重建媒体修复与完整内容盘点，不改变帖子决定或删除历史记录。"""
import hashlib
from pathlib import Path

from . import config, db, purge, storage
from .gate import gate


def repair_local() -> dict:
    rebuilt = avatar_relinked = registered = 0
    with gate:
        with db.conn() as c:
            pics = c.execute("SELECT * FROM pics").fetchall()
            posts = c.execute("SELECT sid,source,uid,avatar FROM posts ORDER BY created_at DESC,sid").fetchall()
        for p in pics:
            src = config.PICS_DIR / (p["file"] or "")
            if not src.is_file():
                continue
            sha = storage.file_hash(src)
            with db._write_lock, db.conn() as c:
                c.execute("INSERT OR IGNORE INTO media_contents VALUES(?,?,?)", (sha, p["file"], src.stat().st_size))
            registered += 1
            dst = config.PICS_DIR / (p["thumb"] or "")
            if dst.is_file():
                continue
            if p["w"] > storage.CARD_THUMB_W:
                dst = src.parent / f"t_{src.stem}.jpg"
                success = storage._make_wide_thumb(src, dst)
            else:
                dst, success = src, True
            if success:
                with db._write_lock, db.conn() as c:
                    c.execute("UPDATE pics SET thumb=? WHERE id=?", (dst.relative_to(config.PICS_DIR).as_posix(), p["id"]))
                rebuilt += 1
        available = {}
        for p in posts:
            avatar = p["avatar"] or ""
            key = (p["source"], p["uid"])
            if avatar.startswith("/media/") and (config.PICS_DIR / avatar.removeprefix("/media/")).is_file():
                available.setdefault(key, avatar)
        for p in posts:
            avatar = p["avatar"] or ""
            key = (p["source"], p["uid"])
            if not avatar.startswith("/media/") or (config.PICS_DIR / avatar.removeprefix("/media/")).is_file():
                continue
            rel = Path(avatar.removeprefix("/media/"))
            candidates = list((config.PICS_DIR / rel.parent).glob(rel.name.removeprefix("t_").removesuffix(".jpg") + ".*"))
            if candidates and storage._make_thumb(candidates[0], config.PICS_DIR / rel, max_side=96):
                rebuilt += 1
            elif key in available:
                with db._write_lock, db.conn() as c:
                    c.execute("UPDATE posts SET avatar=? WHERE sid=?", (available[key], p["sid"]))
                avatar_relinked += 1
    archive = storage.retry_archive()
    # 已无原图的既有归档仍可核对历史内容寻址文件名，建立以后校验的基准。
    with db.conn() as c:
        legacy = c.execute("SELECT file,day FROM archive_entries WHERE sha256=''").fetchall()
    for r in legacy:
        dst = config.EXPORT_DIR / r["day"] / Path(r["file"]).name
        if dst.is_file():
            with dst.open("rb") as stream:
                old_hash = hashlib.sha1()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    old_hash.update(chunk)
            if old_hash.hexdigest()[:16] == Path(r["file"]).stem:
                storage._archive_record(r["file"], r["day"], storage.file_hash(dst),
                                        dst.stat().st_size, "content_name_verified")
    archive["history"] = storage.archive_health(deep=True)
    cleanup = purge.retry_files()
    return {"rebuilt": rebuilt, "avatar_relinked": avatar_relinked,
            "registered": registered, "archive": archive, "file_retry": cleanup}


def health() -> dict:
    with db.conn() as c:
        pics = c.execute("SELECT file,thumb FROM pics").fetchall()
        avatars = {r[0].removeprefix("/media/") for r in c.execute("SELECT avatar FROM posts WHERE avatar LIKE '/media/%'")}
        jobs = c.execute("SELECT COUNT(*) FROM file_jobs").fetchone()[0]
    return {"missing_originals": sum(not (config.PICS_DIR / p["file"]).is_file() for p in pics if p["file"]),
            "missing_thumbnails": sum(not (config.PICS_DIR / p["thumb"]).is_file() for p in pics if p["thumb"]),
            "missing_avatars": sum(not (config.PICS_DIR / p).is_file() for p in avatars),
            "pending_file_deletes": jobs, "archive": storage.archive_health(deep=True)}
