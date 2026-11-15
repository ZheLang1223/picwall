"""可重建媒体修复与完整内容盘点，不改变帖子决定或删除历史记录。"""
import hashlib
import os
import tempfile
from pathlib import Path

from . import config, db, purge, storage
from .gate import gate


def _stage_thumb(src: Path, dst: Path, wide: bool = False, max_side: int = 96) -> Path | None:
    fd, tmp_name = tempfile.mkstemp(prefix=".repair-", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    ok = storage._render_wide_thumb(src, tmp) if wide else storage._render_thumb(src, tmp, max_side=max_side)
    if ok:
        return tmp
    tmp.unlink(missing_ok=True)
    return None


def repair_local(deep: bool = False) -> dict:
    """修复缺失缩略图/头像并登记媒体内容；哈希与缩略图生成在写锁外完成，减少锁竞争。"""
    rebuilt = avatar_relinked = registered = 0
    with db.conn() as c:
        pics = c.execute("SELECT * FROM pics").fetchall()
        posts = c.execute("SELECT sid,source,uid,avatar FROM posts ORDER BY created_at DESC,sid").fetchall()
        known_media = set() if deep else {r[0] for r in c.execute("SELECT file FROM media_contents")}

    new_media: list[tuple[str, str, int]] = []
    thumb_updates: list[tuple[str, int]] = []
    staged_thumbs: list[tuple[Path, Path, str, int]] = []
    staged_avatars: list[tuple[Path, Path]] = []
    avatar_updates: list[tuple[str, str]] = []

    try:
        for p in pics:
            rel_file = p["file"] or ""
            try:
                src = storage.resolve_pic(rel_file)
            except ValueError:
                continue
            if not src.is_file():
                continue
            if deep or rel_file not in known_media:
                new_media.append((storage.file_hash(src), rel_file, src.stat().st_size))
                registered += 1
            thumb_rel = p["thumb"] or ""
            thumb_ok = False
            if thumb_rel:
                try:
                    thumb_ok = storage.resolve_pic(thumb_rel).is_file()
                except ValueError:
                    thumb_ok = False
            if thumb_ok:
                continue
            if p["w"] > storage.CARD_THUMB_W:
                dst = src.parent / f"t_{src.stem}.jpg"
                tmp = _stage_thumb(src, dst, wide=True)
                if tmp:
                    staged_thumbs.append((tmp, dst, dst.relative_to(config.PICS_DIR).as_posix(), p["id"]))
            else:
                thumb_updates.append((src.relative_to(config.PICS_DIR).as_posix(), p["id"]))
                rebuilt += 1

        available = {}
        for p in posts:
            avatar = p["avatar"] or ""
            key = (p["source"], p["uid"])
            if avatar.startswith("/media/"):
                try:
                    if storage.resolve_pic(avatar.removeprefix("/media/")).is_file():
                        available.setdefault(key, avatar)
                except ValueError:
                    pass

        for p in posts:
            avatar = p["avatar"] or ""
            key = (p["source"], p["uid"])
            if not avatar.startswith("/media/"):
                continue
            av_rel = avatar.removeprefix("/media/")
            try:
                av_path = storage.resolve_pic(av_rel)
            except ValueError:
                continue
            if av_path.is_file():
                continue
            rel = Path(av_rel)
            candidates = list((config.PICS_DIR / rel.parent).glob(
                rel.name.removeprefix("t_").removesuffix(".jpg") + ".*"))
            tmp = _stage_thumb(candidates[0], av_path, wide=False, max_side=96) if candidates else None
            if tmp:
                staged_avatars.append((tmp, av_path))
            elif key in available:
                avatar_updates.append((available[key], p["sid"]))
                avatar_relinked += 1

        if new_media or thumb_updates or staged_thumbs or staged_avatars or avatar_updates:
            with gate:
                for tmp, dst, rel_thumb, pid in staged_thumbs:
                    os.replace(tmp, dst)
                    thumb_updates.append((rel_thumb, pid))
                    rebuilt += 1
                for tmp, dst in staged_avatars:
                    os.replace(tmp, dst)
                    rebuilt += 1
                with db._write_lock, db.conn() as c:
                    if new_media:
                        c.executemany("INSERT OR IGNORE INTO media_contents VALUES(?,?,?)", new_media)
                    if thumb_updates:
                        c.executemany("UPDATE pics SET thumb=? WHERE id=?", thumb_updates)
                    if avatar_updates:
                        c.executemany("UPDATE posts SET avatar=? WHERE sid=?", avatar_updates)
    finally:
        for tmp, *_ in staged_thumbs:
            tmp.unlink(missing_ok=True)
        for tmp, _ in staged_avatars:
            tmp.unlink(missing_ok=True)

    archive = storage.retry_archive()
    # 已无原图的既有归档仍可核对历史内容寻址文件名，建立以后校验的基准。
    with db.conn() as c:
        legacy = c.execute("SELECT file,day FROM archive_entries WHERE sha256=''").fetchall()
    legacy_records: list[tuple[str, str, str, int, str, str]] = []
    for r in legacy:
        dst = config.EXPORT_DIR / r["day"] / Path(r["file"]).name
        if dst.is_file():
            with dst.open("rb") as stream:
                old_hash = hashlib.sha1()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    old_hash.update(chunk)
            if old_hash.hexdigest()[:16] == Path(r["file"]).stem:
                legacy_records.append((r["file"], r["day"], storage.file_hash(dst),
                                       dst.stat().st_size, "content_name_verified", ""))
    if legacy_records:
        storage._archive_records_batch(legacy_records)
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
