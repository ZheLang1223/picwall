"""抓取编排：每日定时 + 手动触发，逐分组拉时间线、解析、落图入库。

首次抓取一个分组时自动深度回补 CRAWL_BACKFILL_DAYS（默认 30 天），逐页往回翻到
窗口边界为止（页数有硬上限）；之后的日常抓取只翻 CRAWL_MAX_PAGES 页。
礼貌性节流：翻页间隔 1.5s，分组间隔 1s，每天一次的量级对微博毫无压力。
"""
import asyncio
import io
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image

from . import capture_result, config, connections, db, media_fetch, storage, weibo
from .sources import xapi

logger = logging.getLogger("picwall.weibo")   # 日志含来源与阶段，绝不含 Cookie
_HTTP_LIMITS = httpx.Limits(max_connections=16, max_keepalive_connections=8)
_STORE_CTX: ContextVar[dict | None] = ContextVar("_store_ctx", default=None)

def _blank_progress(error: str = "") -> dict:
    """进度初值；error 非空表示这轮没能开抓（如无 Cookie），弹窗要能看见原因。"""
    return {"running": False, "current": "", "page": 0, "done": 0,
            "total": 0, "new_posts": 0, "new_pics": 0, "error": error,
            "groups": []}


state = {"running": False, "progress": _blank_progress()}


def _now() -> str:
    return datetime.now(config.NOW_TZ).isoformat(timespec="seconds")


def deep_stop(parsed: list[dict], fresh_count: int, cutoff: str) -> bool:
    """深度回补的翻页终止判断：整页都是旧帖（无新帖）且已越过窗口边界才收。

    ISO 串同为 +08:00 偏移，直接字典序比较即时间比较。
    """
    if not parsed:
        return True
    if fresh_count:
        return False
    return max(p["created_at"] for p in parsed) < cutoff


def _scrub_error(err: BaseException | str) -> str:
    """抹除异常文本里的代理账密（http://user:pass@host → http://***@host）。"""
    text = str(err)
    text = re.sub(r"(https?|socks5h?)://[^:/@\s]+:[^@\s]+@", r"\1://***:***@", text, flags=re.IGNORECASE)
    return re.sub(r"(https?|socks5h?)://[^/@\s]+@", r"\1://***@", text, flags=re.IGNORECASE)


def _is_safe_pic_rel(rel: str, root: Path | None = None) -> bool:
    if not rel or "\\" in rel or rel.startswith("/"):
        return False
    base = root if root is not None else config.PICS_DIR.resolve()
    return (base / rel).resolve().is_relative_to(base)


def _pic_exists(rel: str, root: Path | None = None) -> bool:
    base = root if root is not None else config.PICS_DIR.resolve()
    if not _is_safe_pic_rel(rel, base):
        return False
    return (base / rel).resolve().is_file()


def _looks_like_mp4(data: bytes) -> bool:
    return isinstance(data, (bytes, bytearray)) and len(data) >= 12 and data[4:8] == b"ftyp"


def _validate_image_payload(data: bytes) -> None:
    if len(data) > config.MAX_IMAGE_BYTES:
        raise ValueError("图片超过下载体积上限")
    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        if not w or not h:
            raise ValueError("下载内容无法解码为图片")
        if w * h > config.MAX_IMAGE_PIXELS:
            raise ValueError("图片超过像素上限，保留补抓任务")
        im.load()


async def _download(client: httpx.AsyncClient, url: str, max_bytes: int | None = None) -> bytes | None:
    host = (urlsplit(url).hostname or "").lower()
    req_headers = {"User-Agent": weibo.UA}
    if host == "sinaimg.cn" or host.endswith(".sinaimg.cn"):
        req_headers["Referer"] = weibo.BASE + "/"
    return await media_fetch.fetch(client, url, req_headers, max_bytes=max_bytes)


async def _download_limited(dl_sem: asyncio.Semaphore | None, client: httpx.AsyncClient,
                            url: str, max_bytes: int | None = None) -> bytes | None:
    args = (client, url) if max_bytes is None else (client, url, max_bytes)
    if dl_sem is None:
        return await _download(*args)
    async with dl_sem:
        return await _download(*args)


def _queue_one(c, p, list_id):
    if c.execute("SELECT 1 FROM tombstones WHERE sid=?", (p["sid"],)).fetchone():
        return False
    c.execute("INSERT INTO image_jobs(sid,list_id,payload,created_at,updated_at) VALUES(?,?,?,?,?) "
              "ON CONFLICT(sid) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
              (p["sid"], list_id, json.dumps(p, ensure_ascii=False), _now(), _now()))
    return True


def queue_posts(posts, list_id):
    if not posts:
        return
    now = _now()
    # 一页/一个浏览器批次先登记全部待处理内容，首条下载时中断也不丢后续记录。
    with db._write_lock, db.conn() as c:
        sids = list(dict.fromkeys(p["sid"] for p in posts))
        dead: set[str] = set()
        for i in range(0, len(sids), 400):
            chunk = sids[i:i + 400]
            qmarks = ",".join("?" * len(chunk))
            dead.update(r[0] for r in c.execute(f"SELECT sid FROM tombstones WHERE sid IN ({qmarks})", chunk))
        rows = [
            (p["sid"], list_id, json.dumps(p, ensure_ascii=False), now, now)
            for p in posts if p["sid"] not in dead
        ]
        if rows:
            c.executemany(
                "INSERT INTO image_jobs(sid,list_id,payload,created_at,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(sid) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                rows,
            )


def fresh_posts(parsed):
    unique = {}
    for p in parsed:
        unique.setdefault(p["sid"], p)
    sids = list(unique)
    if not sids:
        return []
    tombstones: set[str] = set()
    existing: set[str] = set()
    need_imgs: set[str] = set()
    with db.conn() as c:
        for i in range(0, len(sids), 400):
            chunk = sids[i:i + 400]
            qmarks = ",".join("?" * len(chunk))
            tombstones.update(r[0] for r in c.execute(f"SELECT sid FROM tombstones WHERE sid IN ({qmarks})", chunk))
            existing.update(r[0] for r in c.execute(f"SELECT sid FROM posts WHERE sid IN ({qmarks})", chunk))
            need_imgs.update(r[0] for r in c.execute(f"SELECT sid FROM image_jobs WHERE sid IN ({qmarks})", chunk))
    return [
        p for sid, p in unique.items()
        if sid not in tombstones and (sid not in existing or sid in need_imgs)
    ]


def _checkpoint():
    p = state["progress"]
    if p.get("attempt_id"):
        record = dict(p, state="running" if p["running"] else "interrupted" if p.get("outcome") == "interrupted" else "complete")
        db.set_meta("crawl_checkpoint", json.dumps(record, ensure_ascii=False))


def recover_interrupted():
    saved = db.get_meta("crawl_checkpoint")
    if not saved:
        return
    try:
        progress = json.loads(saved)
    except (ValueError, TypeError):
        logger.warning("crawl_checkpoint 损坏，重置抓取进度")
        state["progress"] = _blank_progress()
        return
    if not isinstance(progress, dict):
        state["progress"] = _blank_progress()
        return
    state["progress"] = progress
    if progress.get("state") != "running":
        return
    message = "服务重启，采集已中断；已收到的待处理记录保留在补抓队列，可重试"
    progress.update(state="interrupted", running=False, outcome="interrupted", error=message)
    state["progress"] = progress
    groups = progress.get("groups") if isinstance(progress.get("groups"), list) else []
    summary = {"trigger": "recovery", "outcome": "interrupted", "groups": groups,
               **capture_result.aggregate(groups), "coverage_complete": False}
    db.set_meta("last_result", json.dumps(summary, ensure_ascii=False))
    db.set_meta("last_error", message)
    db.set_meta("crawl_checkpoint", json.dumps(progress, ensure_ascii=False))


def _interrupted_summary(summary, message, kind):
    progress = state["progress"]
    index = progress.get("current_index", 0)
    current = progress.get("groups", [])[index] if index < len(progress.get("groups", [])) else {}
    record = {"group": progress.get("current", ""), **{key: current.get(key, 0) for key in capture_result.COUNTERS},
              "coverage": "interrupted", "error": message, "error_kind": kind}
    if len(summary["groups"]) > index:
        summary["groups"][index].update(coverage="interrupted", error=message, error_kind=kind)
    else:
        summary["groups"].append(record)
    summary.update(capture_result.aggregate(summary["groups"]), coverage_complete=False)
    summary["outcome"] = "interrupted" if kind == "interrupted" else capture_result.outcome(summary["groups"])
    progress.update({key: value for key, value in summary.items() if key != "groups"}, error=message)
    db.set_meta("last_result", json.dumps(summary, ensure_ascii=False))
    db.set_meta("last_error", message)
    return summary


async def _store_post(
    client: httpx.AsyncClient,
    p: dict,
    list_id: str,
    avatar_cache: dict,
    *,
    already_queued: bool = False,
    dl_sem: asyncio.Semaphore | None = None,
    avatar_locks: dict[str, asyncio.Lock] | None = None,
) -> int:
    """下载头像与图片并入库，返回成功落盘的图片数；一张都没下成则不入库。

    PIL 解码/编码与写盘是重活，丢线程池跑，别把事件循环卡住拖慢状态轮询。
    """
    sid = p["sid"]
    ctx = _STORE_CTX.get(None)
    queued = already_queued or bool(ctx and sid in ctx.get("queued", ()))
    sem = dl_sem or (ctx.get("dl_sem") if ctx else None) or asyncio.Semaphore(4)
    av_locks = avatar_locks if avatar_locks is not None else (ctx.get("avatar_locks") if ctx else None)
    observed = bool(ctx and sid in ctx.get("observed", ()))
    root = config.PICS_DIR.resolve()

    if queued:
        with db.conn() as c:
            if c.execute("SELECT 1 FROM tombstones WHERE sid=?", (sid,)).fetchone():
                return 0
            known = {r["url"]: dict(r) for r in c.execute("SELECT * FROM pics WHERE sid=?", (sid,))}
    else:
        with db._write_lock, db.conn() as c:
            if not _queue_one(c, p, list_id):
                return 0
            known = {r["url"]: dict(r) for r in c.execute("SELECT * FROM pics WHERE sid=?", (sid,))}

    def _save_and_insert(pic: dict, data: bytes, position: int, actual: str) -> bool:
        _validate_image_payload(data)
        quality = "full" if actual == pic["full"] else "mid"
        with db._write_lock:
            if db.has_tombstone(sid):
                return False
            rel, w, h, t_rel = storage.save_image(data, pic["full"])
            if not w or not h:
                raise ValueError("下载内容无法解码为图片")
            db._insert_pic_locked(
                sid,
                rel,
                t_rel,
                w,
                h,
                pic["full"],
                "photo",
                0,
                "",
                position,
                pic.get("pid", ""),
                actual,
                quality,
                True,
            )
            return True

    def _save_video_and_insert(pic: dict, data: bytes | None, position: int,
                               pdata: bytes | None, old: dict | None) -> bool:
        """视频分支：save_video 落 mp4（无 PIL），海报走 save_image 复用缩略图链路。

        pics 行：file=视频、thumb=海报缩略图、w/h=宽高比整数、poster_url 记海报原址
        （补抓自愈：海报重下失败时回落到既有 thumb，内容寻址不重复占体积）。
        """
        if data is not None and not _looks_like_mp4(data):
            raise ValueError("下载内容不是有效的 MP4 视频")
        if pdata is not None:
            try:
                _validate_image_payload(pdata)
            except (OSError, ValueError):
                pdata = None
        with db._write_lock:
            if db.has_tombstone(sid):
                return False
            if data is None:   # 变体文件已在盘上（重试路径）：只补海报/重插行
                rel = (old or {}).get("file") or ""
                if not _pic_exists(rel):
                    return False
            else:
                rel = storage.save_video(data, pic["full"])
            poster_rel = ""
            if pdata:
                try:
                    _, _, _, pt = storage.save_image(pdata, pic["poster"])
                    poster_rel = pt or ""
                except (OSError, ValueError):
                    poster_rel = ""
            if not poster_rel:
                cand = (old or {}).get("thumb") or ""
                if _pic_exists(cand):
                    poster_rel = cand
            db._insert_pic_locked(
                sid,
                rel,
                poster_rel,
                int(pic.get("aspect_w") or 1),
                int(pic.get("aspect_h") or 1),
                pic["full"],
                "video",
                pic.get("dur_ms") or 0,
                pic.get("poster") or "",
                position,
                pic.get("pid", ""),
                pic["full"],
                "full",
                True,
            )
            return True

    pic_items = list(enumerate({x["full"]: x for x in p["pics"]}.values()))

    async def _fetch_one(position: int, pic: dict):
        old = known.get(pic["full"])
        existing = bool(old and _pic_exists(old["file"], root))
        if pic.get("kind") == "video":
            # 视频：变体已落盘就不再重下（只补海报/校正位次），否则先抓变体再抓海报
            data = None if existing else await _download_limited(sem, client, pic["full"], config.MAX_VIDEO_BYTES)
            if not data and not existing:
                return ("err", position, pic, old, None, None, ("视频下载失败，稍后重试",))
            pdata = await _download_limited(sem, client, pic["poster"]) if pic.get("poster") else None
            return ("video", position, pic, old, data, pdata, ())
        if existing and old["quality"] != "mid":
            return ("reposition", position, pic, old, None, None, ())
        actual = pic["full"]
        data = await _download_limited(sem, client, pic["full"])
        errs: list[str] = []
        if not data and pic.get("mid") and pic["mid"] != pic["full"]:
            actual = pic["mid"]
            data = None if existing else await _download_limited(sem, client, actual)
            errs.append("原图待补齐，当前为中图")
        if not data:
            errs.append("图片下载失败，稍后重试")
            return ("err", position, pic, old, None, None, tuple(errs))
        return ("photo", position, pic, old, data, actual, tuple(errs))

    outcomes = await asyncio.gather(*(_fetch_one(pos, pic) for pos, pic in pic_items)) if pic_items else []

    saved = 0
    errors: list[str] = []
    repositions: list[tuple[int, int]] = []
    for kind, position, pic, old, data, extra, msgs in outcomes:
        errors.extend(msgs)
        if kind == "err":
            continue
        if kind == "reposition":
            if old["position"] != position:
                repositions.append((position, old["id"]))
            continue
        if kind == "video":
            try:
                ok = await asyncio.to_thread(_save_video_and_insert, pic, data, position, extra, old)
                saved += int(ok)
            except (OSError, ValueError) as e:
                errors.append(str(e))
            continue
        try:
            saved += int(await asyncio.to_thread(_save_and_insert, pic, data, position, extra))
        except (OSError, ValueError) as e:
            errors.append(str(e))

    if repositions:
        with db._write_lock, db.conn() as c:
            c.executemany("UPDATE pics SET position=? WHERE id=?", repositions)

    # 恢复时本轮可能没有新增图片；已有有效文件同样足以完成建帖。
    with db.conn() as c:
        files = c.execute("SELECT file FROM pics WHERE sid=?", (sid,)).fetchall()
        has_post = c.execute("SELECT 1 FROM posts WHERE sid=?", (sid,)).fetchone() is not None
        is_dead = c.execute("SELECT 1 FROM tombstones WHERE sid=?", (sid,)).fetchone() is not None
    available = any(r["file"] and _pic_exists(r["file"], root) for r in files)
    just_inserted = False
    if available and not has_post:
        avatar_key = db.author_key(p.get("source", "weibo"), p["uid"])
        safe_avatar_url = p["avatar"] if str(p.get("avatar") or "").lower().startswith(("http://", "https://")) else ""
        if avatar_key not in avatar_cache and safe_avatar_url:
            async def _ensure_avatar():
                if avatar_key in avatar_cache:
                    return
                data = await _download_limited(sem, client, safe_avatar_url)
                if data:
                    try:
                        _validate_image_payload(data)
                        avatar_cache[avatar_key] = await asyncio.to_thread(storage.save_avatar, data, safe_avatar_url)
                    except (OSError, ValueError):
                        logger.warning("weibo avatar stage=decode kind=media")
            if av_locks is not None:
                lock = av_locks.setdefault(avatar_key, asyncio.Lock())
                async with lock:
                    await _ensure_avatar()
            else:
                await _ensure_avatar()
        local = dict(p)
        local["avatar_url"] = safe_avatar_url
        local["avatar"] = f"/media/{avatar_cache[avatar_key]}" if avatar_cache.get(avatar_key) else ""
        db.insert_post(local, list_id)
        just_inserted = True
        has_post = True
    if not just_inserted and not observed:
        db.observe_post(p, list_id)
    if not has_post and not is_dead:
        errors.append("帖子尚未完成提交，保留任务等待恢复")
    kept_files: list[str] | None = None
    kept_day = ""
    with db._write_lock, db.conn() as c:
        if errors:
            c.execute("UPDATE image_jobs SET attempts=attempts+1,error=?,error_kind=?,updated_at=?,next_retry_at=? WHERE sid=?",
                      ("；".join(dict.fromkeys(errors)), "media", _now(),
                       (datetime.now(config.NOW_TZ)+timedelta(hours=4)).isoformat(timespec="seconds"), sid))
        else:
            c.execute("DELETE FROM image_jobs WHERE sid=?", (sid,))
        row = c.execute("SELECT status, kept_at FROM posts WHERE sid=?", (sid,)).fetchone()
        if row and row["status"] == "kept":
            kept_day = (row["kept_at"] or _now())[:10]
            kept_files = [r["file"] for r in c.execute("SELECT file FROM pics WHERE sid=? ORDER BY position, id", (sid,))]
    if kept_files is not None:
        await asyncio.to_thread(storage.export_report, kept_files, kept_day)
    return saved


def needs_images(sid: str) -> bool:
    with db.conn() as c:
        return c.execute("SELECT 1 FROM image_jobs WHERE sid=?", (sid,)).fetchone() is not None


def queue_missing_files() -> int:
    """存量缺原图也可恢复：URL 来自原采集记录，禁止用空 URL 伪装成功。"""
    root = config.PICS_DIR.resolve()
    with db.conn() as c:
        already_queued = {r[0] for r in c.execute("SELECT sid FROM image_jobs")}
        tombstoned = {r[0] for r in c.execute("SELECT sid FROM tombstones")}
        candidates = c.execute(
            "SELECT pc.sid,pc.file FROM pics pc JOIN posts p ON pc.sid=p.sid "
            "WHERE p.status!='discarded' OR p.favorite=1"
        ).fetchall()
    exists_cache: dict[str, bool] = {}
    missing: set[str] = set()
    for r in candidates:
        sid = r["sid"]
        rel = r["file"]
        if not rel or sid in already_queued or sid in tombstoned or sid in missing:
            continue
        ok = exists_cache.get(rel)
        if ok is None:
            # 路径越界的文件不是“缺失图”，必须直接忽略，防止被再次加入 image_jobs。
            ok = (not _is_safe_pic_rel(rel, root)) or _pic_exists(rel, root)
            exists_cache[rel] = ok
        if not ok:
            missing.add(sid)
    if not missing:
        return 0
    missing_list = sorted(missing)
    to_insert: list[tuple[str, str, str, str, str]] = []
    now = _now()
    with db.conn() as c:
        for i in range(0, len(missing_list), 400):
            chunk = missing_list[i:i + 400]
            qmarks = ",".join("?" * len(chunk))
            posts_by_sid = {
                r["sid"]: dict(r)
                for r in c.execute(f"SELECT * FROM posts WHERE sid IN ({qmarks})", chunk)
            }
            pics_by_sid: dict[str, list[dict]] = defaultdict(list)
            for pc in c.execute(
                f"SELECT * FROM pics WHERE sid IN ({qmarks}) ORDER BY sid, position, id",
                chunk,
            ):
                pics_by_sid[pc["sid"]].append(dict(pc))
            for sid in chunk:
                p = posts_by_sid.get(sid)
                if not p:
                    continue
                # 视频行把 kind/poster/宽高比/时长一并带回：重试走 video 分支而不是误当图片 PIL 解码
                p["pics"] = [
                    {"full": pc["url"], "mid": pc["url"],
                     "kind": pc["kind"] or "photo",
                     "poster": pc["poster_url"] or "",
                     "aspect_w": pc["w"] or 1, "aspect_h": pc["h"] or 1,
                     "dur_ms": pc["dur_ms"] or 0}
                    for pc in pics_by_sid.get(sid, ()) if pc["url"]
                ]
                if p["pics"]:
                    to_insert.append((sid, p["list_id"], json.dumps(p, ensure_ascii=False), now, now))
    if to_insert:
        with db._write_lock, db.conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO image_jobs(sid,list_id,payload,created_at,updated_at) VALUES(?,?,?,?,?)",
                to_insert,
            )
    return len(missing)


async def retry_missing(manual: bool = False, cancelled=None, progress=None) -> dict:
    if state["running"]:
        return {"ok": False, "msg": "已有抓取在进行中，请完成后重试"}
    state["running"] = True
    try:
        await asyncio.to_thread(queue_missing_files)
        with db._write_lock, db.conn() as c:
            c.execute("DELETE FROM image_jobs WHERE sid IN (SELECT sid FROM tombstones)")
            rows = c.execute("SELECT * FROM image_jobs " +
                             ("" if manual else "WHERE attempts < 3 AND (next_retry_at='' OR next_retry_at<=?) ") +
                             "ORDER BY attempts,sid LIMIT 100", [] if manual else [_now()]).fetchall()
            tombstoned = {r[0] for r in c.execute("SELECT sid FROM tombstones")}
        valid_items: list[tuple[object, dict]] = []
        for r in rows:
            if cancelled and cancelled():
                raise InterruptedError()
            if r["sid"] in tombstoned:
                continue
            try:
                p = json.loads(r["payload"])
            except (ValueError, TypeError):
                logger.warning("image_jobs payload 损坏 sid=%s", r["sid"])
                continue
            if not isinstance(p, dict):
                continue
            valid_items.append((r, p))
        saved = 0
        proxy = config.X_PROXY_URL or None
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, limits=_HTTP_LIMITS) as direct:
            via = (httpx.AsyncClient(timeout=30, follow_redirects=False, proxy=proxy, limits=_HTTP_LIMITS)
                   if proxy else None)
            post_sem = asyncio.Semaphore(4)
            dl_sem = asyncio.Semaphore(6)
            avatar_locks: dict[str, asyncio.Lock] = {}
            avatar_cache: dict[str, str] = {}
            done_count = 0
            token = _STORE_CTX.set({
                "queued": {r["sid"] for r, _ in valid_items},
                "dl_sem": dl_sem,
                "avatar_locks": avatar_locks,
            })
            try:
                async def _run_retry(r, p) -> int:
                    nonlocal done_count
                    async with post_sem:
                        if cancelled and cancelled():
                            raise InterruptedError()
                        # X 帖的图在 pbs.twimg.com（多被墙），走 X 专用代理；微博照旧直连
                        client = via if (via and p.get("source") == "x") else direct
                        res = await _store_post(client, p, r["list_id"], avatar_cache)
                        done_count += 1
                        if progress:
                            await asyncio.to_thread(progress, done_count, len(rows))
                        return res

                if valid_items:
                    saved = sum(await asyncio.gather(*(_run_retry(r, p) for r, p in valid_items)))
            finally:
                _STORE_CTX.reset(token)
                if via:
                    await via.aclose()
        with db.conn() as c:
            remaining = c.execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0]
        return {"ok": True, "saved": saved, "remaining": remaining}
    finally:
        state["running"] = False


async def _crawl_group(client: httpx.AsyncClient, cookie: str, g: dict,
                       backfill_days: int = 0, progress: dict | None = None) -> dict:
    list_id, name = g["list_id"], g["name"]
    first_time = not db.get_meta(f"group_seen:{list_id}")
    deep = first_time or backfill_days > 0
    if deep:
        days = backfill_days if backfill_days > 0 else config.CRAWL_BACKFILL_DAYS
        cutoff = (datetime.now(weibo.TZ8) - timedelta(days=days)).isoformat(timespec="seconds")
        max_pages = max(1, config.CRAWL_BACKFILL_MAX_PAGES)
    else:
        cutoff = None
        max_pages = max(1, config.CRAWL_MAX_PAGES)
    result = {"group": name, **capture_result.counts(), "coverage": "page_limit",
              "pages": 0, "backfill": deep, "window_days": days if deep else None,
              "page_limit": max_pages}
    avatar_cache: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        result["pages"] = page
        try:
            if list_id.startswith("wbu:"):
                uid = list_id.removeprefix("wbu:")
                data = await weibo.user_timeline(client, cookie, uid, page)
            else:
                data = await weibo.group_timeline(client, cookie, list_id, page)
        except (weibo.WeiboError, httpx.HTTPError) as e:
            result.update(coverage="interrupted", error=_scrub_error(e),
                          error_kind="auth" if isinstance(e, weibo.CookieExpired) else "network" if isinstance(e, httpx.HTTPError) else "structure")
            break
        items = weibo.extract_statuses(data)
        result["received"] += len(items)
        parsed = [p for p in (weibo.parse_mblog(it) for it in items) if p]
        result["normalized"] += len(parsed)
        db.observe_posts(parsed, list_id)
        fresh = fresh_posts(parsed)
        result["skipped"] += len(items) - len(fresh)
        queue_posts(fresh, list_id)
        existed_sids: set[str] = set()
        if fresh:
            fresh_sids = [p["sid"] for p in fresh]
            with db.conn() as c:
                for i in range(0, len(fresh_sids), 400):
                    chunk = fresh_sids[i:i + 400]
                    qmarks = ",".join("?" * len(chunk))
                    existed_sids.update(r[0] for r in c.execute(f"SELECT sid FROM posts WHERE sid IN ({qmarks})", chunk))
        result["unprocessed"] = result["received"] - result["skipped"] - result["accepted"] - result["failed"]
        if progress is not None:
            progress.update(result, page=page)
            _checkpoint()
        # 持久记录已收到但尚未处理的批次；有效内容已在 image_jobs 中。
        db.set_meta(f"group_coverage:{list_id}", json.dumps(result, ensure_ascii=False))
        dl_sem = asyncio.Semaphore(6)
        avatar_locks: dict[str, asyncio.Lock] = {}
        token = _STORE_CTX.set({
            "queued": {p["sid"] for p in fresh},
            "observed": {p["sid"] for p in parsed},
            "dl_sem": dl_sem,
            "avatar_locks": avatar_locks,
        })
        try:
            for idx, p in enumerate(fresh):
                existed = p["sid"] in existed_sids
                saved = await _store_post(client, p, list_id, avatar_cache)
                capture_result.stored(result, p["sid"], existed, saved)
                result["unprocessed"] -= 1
                if progress is not None:
                    progress.update(result, page=page)
                    if (idx + 1) % 5 == 0 or idx + 1 == len(fresh):
                        _checkpoint()
        except Exception as e:
            result.update(coverage="interrupted", error="本页处理未结束，剩余记录已保留，可补抓",
                          error_kind=type(e).__name__)
            break
        finally:
            _STORE_CTX.reset(token)
        if progress is not None:
            progress.update(result, page=page)
        db.set_meta(f"group_coverage:{list_id}", json.dumps(result, ensure_ascii=False))
        if not items:
            result["coverage"] = "end"
            break
        if cutoff is not None:
            # 分组分页按外层发布时间排序；转发原帖的旧时间不能当作分页边界。
            dates = [{"created_at": weibo.parse_time(it["created_at"])} for it in items
                     if isinstance(it, dict) and it.get("created_at")]
            if (dates or parsed) and deep_stop(dates or parsed, len(fresh), cutoff):
                result["coverage"] = "date_boundary"
                break
        elif parsed and not fresh:
            result["coverage"] = "known_page"
            break
        await asyncio.sleep(1.5)
    if first_time and result["coverage"] in ("end", "date_boundary"):
        db.set_meta(f"group_seen:{list_id}", _now())
    result["outcome"] = capture_result.outcome([result])
    db.set_meta(f"group_coverage:{list_id}", json.dumps(result, ensure_ascii=False))
    return result


async def crawl(trigger: str = "manual", only: str | None = None,
                backfill_days: int = 0) -> dict:
    """抓一轮分组；only=只抓这个分组，backfill_days>0=强制深度回补指定天数。

    返回摘要 dict（ok=False 表示没跑起来，msg 说明原因）。
    """
    if state["running"]:
        return {"ok": False, "msg": "已有抓取在进行中"}
    db.set_meta("last_attempt", _now())
    cookie = config.get_cookie()
    if not cookie:
        db.set_meta("cookie_state", "unset")
        state["progress"] = _blank_progress("还没有填 Cookie")
        return {"ok": False, "msg": "还没有填 Cookie"}
    groups = [g for g in db.active_groups(source="weibo")]   # 微博抓取不碰 x: 前缀分组
    if only:
        groups = [g for g in groups if g["list_id"] == only]
        if not groups:
            return {"ok": False, "msg": "分组不存在或已移除"}
    if not groups:
        state["progress"] = _blank_progress("还没有配置分组，先到「管理分组」拉取")
        return {"ok": False, "msg": "还没有配置分组"}

    state["running"] = True
    db.set_meta("last_attempt", _now())
    started = time.monotonic()
    logger.info("weibo crawl start trigger=%s groups=%d deep=%s",
                trigger, len(groups), bool(backfill_days))
    state["progress"] = _blank_progress()
    state["progress"].update(
        running=True, total=len(groups), attempt_id=uuid.uuid4().hex, started_at=_now(),
        groups=[{"name": g["name"], "status": "pending",
                 "page": 0, "new_posts": 0, "new_pics": 0} for g in groups])
    _checkpoint()
    try:
        summary: dict = {"trigger": trigger, "groups": [],
                         "new_posts": 0, "new_pics": 0}
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, limits=_HTTP_LIMITS) as client:
            for i, g in enumerate(groups):
                gp = state["progress"]["groups"][i]
                gp["status"] = "doing"
                state["progress"]["current"] = g["name"]
                state["progress"]["current_index"] = i
                try:
                    r = await _crawl_group(client, cookie, g,
                                           backfill_days=backfill_days, progress=gp)
                except weibo.CookieExpired:
                    gp["status"] = "error"
                    raise
                except (weibo.WeiboError, httpx.HTTPError) as e:
                    # 单组失败不拖垮整轮：进度里记错，其余分组照常
                    msg = _scrub_error(e)
                    gp["status"] = "error"
                    state["progress"]["error"] = f"{g['name']}：{msg}"
                    summary["groups"].append({"group": g["name"], "new_posts": 0,
                                              "new_pics": 0, "error": msg})
                    logger.warning("weibo crawl group failed group=%s kind=structure err=%s",
                                   g["name"], msg)
                    state["progress"]["done"] += 1
                    continue
                gp.update(r, status="error" if r.get("error") else "done")
                state["progress"]["done"] += 1
                summary["groups"].append(r)
                summary.update(capture_result.aggregate(summary["groups"]))
                state["progress"].update(capture_result.aggregate(summary["groups"]))
                if r.get("error_kind") == "auth":
                    break
                if i < len(groups) - 1:
                    await asyncio.sleep(1)
        summary.update(capture_result.aggregate(summary["groups"]))
        errors = [g["error"] for g in summary["groups"] if g.get("error")]
        outcome = capture_result.outcome(summary["groups"])
        summary["outcome"] = outcome
        summary["coverage_complete"] = all(g.get("coverage") != "page_limit" and not g.get("error") for g in summary["groups"])
        notes = list(errors)
        if summary["failed"] or summary["unprocessed"]:
            notes.append("部分记录图片未齐或尚未处理，已保留补抓任务")
        if not summary["coverage_complete"] and any(g.get("coverage") == "page_limit" for g in summary["groups"]):
            notes.append("达到翻页上限，本轮尚未覆盖完整日期范围")
        message = "；".join(notes)
        db.set_meta("last_result", json.dumps(summary, ensure_ascii=False))
        db.set_meta("last_error", message)
        auth_failed = any(g.get("error_kind") == "auth" for g in summary["groups"])
        connections.record("weibo", cookie, "expired" if auth_failed else "error" if errors else "ok",
                           "微博登录已失效，请重新连接" if auth_failed else "部分分组暂不可访问，请到自动抓取查看" if errors else "")
        if outcome == "success":
            db.set_meta("last_crawl", _now())
        state["progress"].update(outcome=outcome, error=message, coverage_complete=summary["coverage_complete"])
        logger.info("weibo crawl done outcome=%s groups=%d new_posts=%d new_pics=%d cost=%.1fs",
                    outcome, len(summary["groups"]), summary["new_posts"],
                    summary["new_pics"], time.monotonic() - started)
        return {"ok": outcome == "success", "outcome": outcome, **summary}
    except asyncio.CancelledError:
        _interrupted_summary(summary, "服务停止，采集中断；已收到的记录保留，可补抓", "interrupted")
        raise
    except weibo.CookieExpired as e:
        state["progress"]["error"] = f"Cookie 过期或无效：{e}"
        connections.record("weibo", cookie, "expired", "微博登录已失效，请重新连接")
        db.set_meta("last_error", str(e))
        logger.warning("weibo crawl failed trigger=%s stage=crawl kind=auth err=%s", trigger, e)
        _interrupted_summary(summary, "微博登录过期，请重新连接 Cookie", "auth")
        return {"ok": False, **summary, "msg": "微博登录过期，请重新连接 Cookie"}
    except weibo.WeiboError as e:
        state["progress"]["error"] = f"微博接口异常：{e}"
        db.set_meta("last_error", str(e))
        # 失败的一轮不盖 last_crawl：否则 catch_up 把它当新鲜，20h 内不再补抓
        logger.warning("weibo crawl failed trigger=%s stage=crawl kind=structure err=%s",
                       trigger, e)
        _interrupted_summary(summary, f"微博接口异常：{e}", "structure")
        return {"ok": False, **summary, "msg": f"微博接口异常：{e}"}
    except Exception as e:  # 网络断连等，保留现场给状态页
        msg = _scrub_error(e)
        state["progress"]["error"] = f"抓取失败：{msg}"
        db.set_meta("last_error", f"{type(e).__name__}: {msg}")
        logger.warning("weibo crawl failed trigger=%s stage=crawl kind=%s err=%s",
                       trigger, type(e).__name__, msg)
        _interrupted_summary(summary, f"抓取失败：{msg}", type(e).__name__)
        return {"ok": False, **summary, "msg": f"抓取失败：{msg}"}
    finally:
        state["running"] = False
        if state["progress"].get("error") and not state["progress"].get("outcome"):
            state["progress"]["outcome"] = "failed"
            db.set_meta("last_result", json.dumps(
                {"outcome": "failed", "new_posts": state["progress"]["new_posts"],
                 "new_pics": state["progress"]["new_pics"]}, ensure_ascii=False))
        state["progress"]["running"] = False   # 结果保留在 progress 里给弹窗展示
        _checkpoint()


async def discover_groups() -> list[dict]:
    cookie = config.get_cookie()
    if not cookie:
        raise weibo.CookieExpired("还没有填 Cookie")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, limits=_HTTP_LIMITS) as client:
        return await weibo.all_groups(client, cookie)


async def catch_up_if_stale() -> None:
    """启动后补一次：距上次成功抓取超过 20 小时才补，避免重启就抓。"""
    last = db.get_meta("last_crawl")
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:   # 旧格式（无时区）按本时区补齐再比
                last_dt = last_dt.replace(tzinfo=config.NOW_TZ)
            age = datetime.now(config.NOW_TZ) - last_dt
            if age.total_seconds() < 20 * 3600:
                return
        except (ValueError, TypeError):
            pass
    await asyncio.sleep(20)  # 等端口先就绪
    await crawl(trigger="catch-up")


async def ingest_harvest(list_id: str, name: str, statuses: list[dict]) -> tuple[int, dict]:
    """浏览器代抓导入流水线：校验通过后占坑、解析、去重入库与落盘。"""
    if state["running"]:
        return 409, {"ok": False, "msg": "已有抓取在进行中"}
    state["running"] = True
    try:
        result = {**capture_result.counts(len(statuses)), "group": name,
                  "coverage": "browser_batch", "backfill": False, "name": name, "status": "doing"}
        state["progress"] = dict(_blank_progress(), running=True,
            attempt_id=uuid.uuid4().hex, started_at=_now(), trigger="harvest", total=1,
            current=name, current_index=0, groups=[result])
        parsed = [p for p in (weibo.parse_mblog(it) for it in statuses) if p]
        result["normalized"] = len(parsed)
        db.observe_posts(parsed, list_id)
        fresh = fresh_posts(parsed)
        result["skipped"] = len(statuses) - len(fresh)
        result["unprocessed"] = len(fresh)
        queue_posts(fresh, list_id)
        existed_sids: set[str] = set()
        if fresh:
            fresh_sids = [p["sid"] for p in fresh]
            with db.conn() as c:
                for i in range(0, len(fresh_sids), 400):
                    chunk = fresh_sids[i:i + 400]
                    qmarks = ",".join("?" * len(chunk))
                    existed_sids.update(r[0] for r in c.execute(f"SELECT sid FROM posts WHERE sid IN ({qmarks})", chunk))
        state["progress"].update(capture_result.aggregate([result]))
        _checkpoint()
        avatar_cache: dict[str, str] = {}
        dl_sem = asyncio.Semaphore(6)
        avatar_locks: dict[str, asyncio.Lock] = {}
        token = _STORE_CTX.set({
            "queued": {p["sid"] for p in fresh},
            "observed": {p["sid"] for p in parsed},
            "dl_sem": dl_sem,
            "avatar_locks": avatar_locks,
        })
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, limits=_HTTP_LIMITS) as client:
            try:
                for idx, p in enumerate(fresh):
                    existed = p["sid"] in existed_sids
                    saved = await _store_post(client, p, list_id, avatar_cache)
                    capture_result.stored(result, p["sid"], existed, saved)
                    result["unprocessed"] -= 1
                    state["progress"].update(capture_result.aggregate([result]))
                    if (idx + 1) % 5 == 0 or idx + 1 == len(fresh):
                        _checkpoint()
            except Exception as e:
                result.update(error="本批处理未结束，剩余记录已保留，可补抓", error_kind=type(e).__name__)
            finally:
                _STORE_CTX.reset(token)
        db.add_group(list_id, name)
        outcome = capture_result.outcome([result])
        result["status"] = "error" if result.get("error") else "done"
        summary = {"trigger": "harvest", "outcome": outcome, "groups": [result],
                   **capture_result.aggregate([result]), "coverage_complete": False}
        db.set_meta("last_result", json.dumps(summary, ensure_ascii=False))
        db.set_meta("last_ingest", datetime.now(config.NOW_TZ).isoformat(timespec="seconds"))
        db.set_meta("last_error", result.get("error", "") or ("部分图片待补抓" if result["failed"] else ""))
        state["progress"].update(capture_result.aggregate([result]), outcome=outcome,
            error=result.get("error", "") or ("部分图片待补抓" if result["failed"] else ""), done=1)
        return 200, {"ok": outcome == "success", "outcome": outcome,
                     **result, "coverage_complete": False, "truncated": 0}
    except asyncio.CancelledError:
        _interrupted_summary({"trigger": "harvest", "groups": []},
                             "浏览器导入中断，已收到的记录保留，可补抓", "interrupted")
        raise
    finally:
        state["running"] = False
        state["progress"]["running"] = False
        _checkpoint()


def _x_list_progress(idx: int, summary: dict | None) -> None:
    """crawl_lists 的逐列表回调：开抓标 doing，收尾写 done/error 与累计计数。"""
    p = state["progress"]
    gp = p["groups"][idx]
    if summary is None:
        gp["status"] = "doing"
        p["current"] = gp["name"]
        return
    gp["status"] = "error" if summary.get("error") else "done"
    gp["new_posts"] = summary.get("new_posts", 0)
    gp["new_pics"] = summary.get("new_pics", 0)
    p["done"] = idx + 1
    p["new_posts"] = sum(g["new_posts"] for g in p["groups"])
    p["new_pics"] = sum(g["new_pics"] for g in p["groups"])


def x_http_client() -> httpx.AsyncClient:
    """X 链路专用 HTTP 客户端（抓取/代抓落图/补图共用）。"""
    return httpx.AsyncClient(timeout=30, follow_redirects=False,
                             proxy=config.X_PROXY_URL or None,
                             limits=_HTTP_LIMITS)


async def crawl_x(http_client_factory=None, xapi_module=None,
                  only: str | None = None, max_pages: int | None = None) -> dict:
    """X 服务端抓取：读扩展送来的 auth_token，抓所有启用的 X 列表与用户。"""
    if state["running"]:
        return {"ok": False, "msg": "已有抓取在进行中"}
    token = config.get_x_auth()
    if not token:
        return {"ok": False, "msg": "还没有 X Cookie（用 Cookie 助手扩展发送）"}
    groups = db.active_groups(source="x")
    if only:
        if str(only).startswith("xg:"):
            # 本地组不是抓取目标：解包成成员目标再走既有分发（一次锁 running，按成员计进度）
            member_ids = {m["member_id"] for m in db.group_members(only)}
            groups = [g for g in groups if g["list_id"] in member_ids]
            if not member_ids:
                return {"ok": False, "msg": "该分组还没有成员，先在管理分组里添加"}
        else:
            groups = [g for g in groups if g["list_id"] == only or g["list_id"].lower() == only.lower()]
            if not groups:
                return {"ok": False, "msg": "分组不存在或已移除"}
    lists = [g for g in groups if g["list_id"].startswith("x:")]
    users = [{"handle": g["list_id"].removeprefix("xu:"), "name": g["name"]}
             for g in groups if g["list_id"].startswith("xu:")]
    if not groups:
        return {"ok": False, "msg": "还没有 X 列表或用户分组（管理分组里添加）"}
    names = [g["name"] for g in lists] + [u["name"] for u in users]
    state["running"] = True
    state["progress"] = _blank_progress()
    state["progress"].update(
        running=True, total=len(names), current="X",
        groups=[{"name": n, "status": "pending",
                 "page": 0, "new_posts": 0, "new_pics": 0} for n in names])
    client_factory = http_client_factory or x_http_client
    x_mod = xapi_module or xapi
    pages = max_pages or config.X_CRAWL_MAX_PAGES
    try:
        async with client_factory() as client:
            summaries = []
            if lists:
                summaries += await x_mod.crawl_lists(
                    client, token, lists, max_pages=pages,
                    on_list=_x_list_progress, ct0=config.get_x_csrf())
            if users:
                summaries += await x_mod.crawl_users(
                    client, token, users, max_pages=pages,
                    on_user=lambda i, s: _x_list_progress(len(lists) + i, s),
                    ct0=config.get_x_csrf())
        errors = [s["error"] for s in summaries if s.get("error")]
        db.set_meta("x_last_error", errors[0] if errors else "")
        total_p = sum(s["new_posts"] for s in summaries)
        total_i = sum(s["new_pics"] for s in summaries)
        outcome = "partial" if errors and len(errors) < len(summaries) else "failed" if errors else "success"
        auth_failed = any(s.get("error_kind") == "auth" for s in summaries)
        connections.record("x", token, "expired" if auth_failed else "error" if outcome == "failed" else "ok",
                           errors[0] if errors else "")
        state["progress"].update(outcome=outcome, error="；".join(errors))
        if not errors:
            db.set_meta("x_last_crawl", datetime.now(config.NOW_TZ).isoformat(timespec="seconds"))
        return {"ok": not errors, "outcome": outcome, "new_posts": total_p, "new_pics": total_i,
                "summaries": summaries}
    except Exception as e:
        msg = _scrub_error(e)
        connections.record("x", token, "error", "X 抓取暂时失败，请稍后重新检查连接")
        db.set_meta("x_last_error", f"{type(e).__name__}: {msg}")
        state["progress"]["error"] = f"X 抓取失败：{msg}"
        return {"ok": False, "msg": f"X 抓取失败：{msg}"}
    finally:
        state["running"] = False
        state["progress"]["running"] = False
