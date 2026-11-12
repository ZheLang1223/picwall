"""过期清理：discarded（不要）超过 PURGE_DAYS 天的帖子硬删除，连同落盘图片。

图片按内容寻址（sha1）可能被多个帖子引用：只有当某张图不再被任何**其他**帖引用时
才删文件，否则只删本帖的 pics 行——这是引用计数保护，防止误删别人还在用的图。
评论豁免：写过评论的帖不入清理（图片可重抓、用户敲下的字不可再生）；
删光评论即恢复清理资格，与收藏豁免同构。
"""
import json
from datetime import datetime, timedelta

from . import config, db, storage


def purge_expired(days: int | None = None) -> dict:
    days = config.PURGE_DAYS if days is None else days
    cutoff = (datetime.now(config.NOW_TZ) - timedelta(days=days)).isoformat(timespec="seconds")
    removed_posts = removed_files = 0
    with db.conn() as c:
        rows = c.execute(
            "SELECT sid FROM posts WHERE status='discarded' "
            "AND COALESCE(favorite,0)=0 AND discarded_at IS NOT NULL "
            "AND NOT EXISTS(SELECT 1 FROM post_comments pc WHERE pc.sid=posts.sid) "
            "AND discarded_at < ?", (cutoff,)).fetchall()
    for r in rows:
        sid = r["sid"]
        count = _purge_post(sid, cutoff)
        if count is not None:
            removed_files += count
            removed_posts += 1
    # 时间戳不落 meta：run() 会写同键的 JSON 结果，一键两格式坑读取方
    return {"purged_posts": removed_posts, "removed_files": removed_files,
            "days": days, "remaining": _pending_discarded()}


def _purge_post(sid: str, cutoff: str | None = None) -> int | None:
    """删一个帖子的行与无人引用的图片文件，返回删除的文件数。

    行先删、文件后删：引用计数在行删除并提交之后重查，避免与并发入库
    （同月同字节重抓 → 同一内容寻址路径）赛出「文件被删、新行还指着它」的永久坏图。
    refcheck 与 unlink 全程持 _write_lock（与 insert_pic 同一把锁）：
    「判 0 → 并发帖插入同文件行 → unlink」的锁外窗口必须封死，判据与删除才原子。
    """
    removed = 0
    with db._write_lock:
        with db.conn() as c:
            # 预览到删除期间可能已恢复/收藏，持锁重验，防误删。
            eligible = c.execute(
                "SELECT 1 FROM posts WHERE sid=? AND status='discarded' "
                "AND COALESCE(favorite,0)=0 AND discarded_at IS NOT NULL "
                "AND NOT EXISTS(SELECT 1 FROM post_comments pc WHERE pc.sid=posts.sid) "
                "AND (? IS NULL OR discarded_at < ?)", (sid, cutoff, cutoff)).fetchone()
            if not eligible:
                return None
            files = [x["file"] for x in c.execute(
                "SELECT file FROM pics WHERE sid=? AND file != ''", (sid,)).fetchall()]
            c.execute("DELETE FROM pics WHERE sid=?", (sid,))
            c.execute("DELETE FROM posts WHERE sid=?", (sid,))
            c.execute("DELETE FROM image_jobs WHERE sid=?", (sid,))
            c.execute("DELETE FROM decision_undos WHERE sid=?", (sid,))
            c.execute("DELETE FROM post_groups WHERE sid=?", (sid,))
            c.execute("DELETE FROM post_observations WHERE sid=?", (sid,))
            c.execute("DELETE FROM post_comments WHERE sid=?", (sid,))
            c.execute("DELETE FROM post_tags WHERE sid=?", (sid,))
            c.execute("DELETE FROM review_events WHERE sid=?", (sid,))
            c.executemany("INSERT OR IGNORE INTO file_jobs(file,updated_at) VALUES(?,?)",
                          [(f, datetime.now(config.NOW_TZ).isoformat()) for f in files])
            # 墓碑：硬删即立碑，入库通道见碑绕行——深翻页回补不再复活已删帖
            c.execute("INSERT OR IGNORE INTO tombstones(sid, purged_at) VALUES(?, ?)",
                      (sid, datetime.now(config.NOW_TZ).isoformat(timespec="seconds")))
        for f in files:
            removed += int(_remove_file_locked(f))
    return removed


def _remove_file_locked(file: str) -> bool:
    with db.conn() as c:
        referenced = c.execute("SELECT 1 FROM pics WHERE file=?", (file,)).fetchone()
    if referenced:
        with db.conn() as c:
            c.execute("DELETE FROM file_jobs WHERE file=?", (file,))
        return False
    try:
        removed = storage.remove_pic_files(file)
        with db.conn() as c:
            c.execute("DELETE FROM file_jobs WHERE file=?", (file,))
            c.execute("DELETE FROM media_contents WHERE file=?", (file,))
        return removed
    except (OSError, ValueError) as e:
        with db.conn() as c:
            c.execute("UPDATE file_jobs SET attempts=attempts+1,error=?,updated_at=? WHERE file=?",
                      (type(e).__name__, datetime.now(config.NOW_TZ).isoformat(), file))
        return False


def retry_files() -> dict:
    with db._write_lock:
        with db.conn() as c:
            files = [r[0] for r in c.execute("SELECT file FROM file_jobs ORDER BY attempts LIMIT 100")]
        removed = sum(_remove_file_locked(f) for f in files)
        with db.conn() as c:
            remaining = c.execute("SELECT COUNT(*) FROM file_jobs").fetchone()[0]
    return {"removed": removed, "remaining": remaining}


def _other_refs(file_rel: str, sid: str) -> int:
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) FROM pics WHERE file=? AND sid != ?",
                         (file_rel, sid)).fetchone()[0]


def _pending_discarded() -> int:
    with db.conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM posts WHERE status='discarded'").fetchone()[0]


def run() -> dict:
    """定时任务入口：跑一轮清理并把结果写进 meta。"""
    result = purge_expired()
    result["file_retry"] = retry_files()
    db.set_meta("last_purge", json.dumps(result, ensure_ascii=False))
    return result
