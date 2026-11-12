"""下载预览和 ZIP 共用同一份文件计划；去重、缺失与上限均可见。"""

import asyncio
import os
import tempfile
import time
import zipfile
from collections import defaultdict

from fastapi import HTTPException

from . import config, db, jobs, storage
from .queries import post_filter

MAX_POSTS = 500
QUALITIES = ("full", "mid", "unknown", "thumbnail")


def tokens(value: str) -> list[str]:
    return list(dict.fromkeys(x.strip() for x in value.split(",") if x.strip()))


def plan(
    status="kept",
    group="all",
    author="all",
    range_="all",
    authors="",
    dates="",
    select="",
    thumb=False,
    subfolder=False,
):
    if status == "selected" and not select:
        return {"posts": 0, "pics": 0, "missing": 0, "entries": {}, "rows": [], "quality": dict.fromkeys(QUALITIES, 0)}
    if select:
        sids = tokens(select)
        if len(sids) > MAX_POSTS:
            raise HTTPException(400, "单包最多 500 帖，请分批选择")
        cond, args = f"p.sid IN ({','.join('?' * len(sids))})", sids
    else:
        if status not in ("kept", "all", "pending", "discarded", "favorites"):
            raise HTTPException(400, "下载范围无效")
        cond, args = post_filter(status, group, author, range_)
    author_ids = tokens(authors)
    if author_ids:
        conditions = []
        for key in author_ids:
            source, uid = key.split(":", 1) if ":" in key else ("weibo", key)
            conditions.append("(p.uid=? AND COALESCE(NULLIF(p.source,''),'weibo')=?)")
            args += [uid, source]
        cond += " AND (" + " OR ".join(conditions) + ")"
    for column, values in (("substr(p.created_at,1,10)", tokens(dates)),):
        if values:
            cond += f" AND {column} IN ({','.join('?' * len(values))})"
            args += values
    with db.conn() as c:
        count = c.execute(f"SELECT COUNT(*) FROM posts p WHERE {cond}", args).fetchone()[0]
        if count > MAX_POSTS:
            # 只读取用于缩小范围的帖子元数据，避免关系扫描和磁盘检查。
            rows = c.execute(f"SELECT p.sid,p.uid,p.source,p.author,p.created_at,'' file,'' thumb FROM posts p WHERE {cond}", args).fetchall()
            return {"posts": count, "pics": None, "missing": None, "entries": {}, "rows": rows, "quality": None}
        rows = c.execute(
            "SELECT p.sid,p.uid,p.source,p.author,p.created_at,p.text,pc.file,pc.thumb,pc.quality,pc.kind "
            "FROM posts p LEFT JOIN pics pc ON pc.sid=p.sid "
            f"WHERE {cond} ORDER BY p.created_at DESC,p.sid,pc.position,pc.id",
            args,
        ).fetchall()
    entries, seen, posts, missing, numbered = {}, set(), set(), set(), defaultdict(int)
    quality_files = {}
    for r in rows:
        posts.add(r["sid"])
        # 视频行 thumb=海报：即使用户勾了「缩略图」也必须给原视频，否则包里是图不是片
        rel = r["file"] if (r["kind"] or "photo") == "video" else (r["thumb"] if thumb else r["file"])
        if not rel:
            continue
        src = (config.PICS_DIR / rel).resolve()
        if not src.is_relative_to(config.PICS_DIR.resolve()) or not src.is_file():
            missing.add(rel)
            continue
        quality = "thumbnail" if thumb else r["quality"] if r["quality"] in QUALITIES else "unknown"
        if rel in seen:
            if quality_files[rel] != quality:
                quality_files[rel] = "mid" if "mid" in (quality_files[rel], quality) else "unknown"
            continue
        seen.add(rel)
        quality_files[rel] = quality
        numbered[r["sid"]] += 1
        name = storage.zip_entry_name(
            r["created_at"], r["author"], r["sid"], numbered[r["sid"]], src.suffix, r["text"]
        )
        if subfolder:
            name = f"{storage.sanitize_filename(r['author'], 60)}/{name}"
        entries[name] = src
    return {
        "posts": len(posts),
        "pics": len(entries),
        "missing": len(missing),
        "entries": entries,
        "rows": rows,
        "quality": {key: sum(q == key for q in quality_files.values()) for key in QUALITIES},
    }


def notes_text(rows) -> str:
    """打包范围内的批注文本（zip 附带 评论.txt 用）；范围内无批注返回空串不落文件。"""
    sids = list(dict.fromkeys(r["sid"] for r in rows))
    meta = {}
    for r in rows:
        meta.setdefault(r["sid"], r)
    if not sids:
        return ""
    by_sid = db.comments_map(sids)
    by_sid = {s: c for s, c in by_sid.items() if c}
    if not by_sid:
        return ""
    groups = []
    for s in sids:
        if s in by_sid:
            m = meta[s]
            day = (m["created_at"] or "")[:10]
            groups.append((f"{day}_{m['author']}_{s}", by_sid[s]))
    return storage.format_notes(groups, "导出")


def preview(scope="kept", authors="", dates="", select="", thumb=False):
    result = plan(scope, authors=authors, dates=dates, select=select, thumb=thumb)
    # 桶只用于选择；合计每次按同一计划重算，跨作者/日期重复图不会被多算。
    buckets = {"authors": {}, "dates": {}}
    for r in result["rows"]:
        for axis, key in (("authors", db.author_key(r["source"], r["uid"])), ("dates", (r["created_at"] or "")[:10])):
            if not key:
                continue
            b = buckets[axis].setdefault(
                key, {"sids": set(), "files": set(), "author": r["author"]}
            )
            b["sids"].add(r["sid"])
            rel = r["thumb"] if thumb else r["file"]
            if rel and (config.PICS_DIR / rel).is_file():
                b["files"].add(rel)
    return {
        "ok": True,
        "posts": result["posts"],
        "pics": result["pics"],
        "missing": result["missing"],
        "quality": result["quality"],
        "max_posts": MAX_POSTS,
        "over_limit": result["posts"] > MAX_POSTS,
        "authors": [
            {"uid": k, "author": b["author"], "n": len(b["sids"]), "pics": len(b["files"]) if result["pics"] is not None else None}
            for k, b in buckets["authors"].items()
        ],
        "dates": [
            {"date": k, "n": len(b["sids"]), "pics": len(b["files"]) if result["pics"] is not None else None}
            for k, b in buckets["dates"].items()
        ],
    }


# ---------- 批量打包任务（job 框架版） ----------

# 打包完成的临时 zip 登记：job_id -> {"path": 文件路径, "ts": 登记时间}。
# 交付完成后由响应的 BackgroundTask 走 pop_result_file 领走删除；没被领取的靠 sweep 兜底。
_RESULT_FILES = {}
_RESULT_TTL = 2 * 3600


def pop_result_file(job_id):
    """按 job id 领走打包产物登记（含路径与时间），没有登记返回 None。"""
    return _RESULT_FILES.pop(job_id, None)


def _sweep_stale() -> None:
    """发起新打包前清掉超过 2 小时没人领取的临时 zip；容错，清理失败不挡任务。"""
    cutoff = time.time() - _RESULT_TTL
    for job_id, item in list(_RESULT_FILES.items()):
        if item["ts"] >= cutoff:
            continue
        try:
            os.unlink(item["path"])
        except FileNotFoundError:
            pass
        except OSError:
            continue   # 文件短暂被占用（杀软/索引器），保留登记下次再试
        _RESULT_FILES.pop(job_id, None)


def build_work(params: dict):
    """把批量下载查询参数打包成 runner 用的异步 work 闭包。

    plan/zip 构建是同步 CPU+IO，放 to_thread 跑不阻塞事件循环；逐文件调 progress
    回报（jobs._update 内部持 db 写锁，线程安全），范围校验与 GET 端点同语义。
    """
    _sweep_stale()

    async def work(cancelled, progress):
        # 任务运行期间独占 download 槽位，当前记录的 id 即本任务 id，产物登记按它寻址。
        try:
            job_id = jobs.status("download")["id"]
        except Exception:
            job_id = ""
        try:
            tmp, stats = await asyncio.to_thread(_build_zip, params, cancelled, progress)
        except HTTPException as e:
            return {"ok": False, "msg": e.detail}
        if job_id:
            _RESULT_FILES[job_id] = {"path": tmp, "ts": time.time()}
        return {"ok": True, "posts": stats["posts"], "entries": stats["entries"],
                "missing": stats["missing"], "file": tmp}

    return work


def _build_zip(params: dict, cancelled, progress):
    """同步构建批量 zip：与 GET /api/download/zip 同一份 plan、命名与 评论.txt 语义。

    逐文件检查中断标志，文件级失败/中断都会把半成品临时文件清掉再抛。
    """
    status = params.get("scope") or params.get("status") or "pending"
    result = plan(status, params.get("group", "all"), params.get("author", "all"),
                  params.get("range") or "all", params.get("authors", ""),
                  params.get("dates", ""), params.get("select", ""),
                  bool(params.get("thumb")), bool(params.get("subfolder")))
    if result["posts"] > MAX_POSTS:
        raise HTTPException(400, "单包最多 500 帖，请按博主或日期分批选择；没有截断下载内容")
    entries = result["entries"]
    if not entries:
        raise HTTPException(404, "没有可打包的图片")
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    notes = notes_text(result["rows"])
    total = len(entries)
    done = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for name, src in entries.items():
                if cancelled():
                    raise InterruptedError()
                z.write(src, name)
                done += 1
                progress(done, total)
            if notes:   # 范围内有批注才附带；纯文本、"> " 引用协议与详情一致
                z.writestr("评论.txt", notes)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp, {"posts": result["posts"], "entries": total, "missing": result["missing"]}
