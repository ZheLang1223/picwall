"""下载预览和 ZIP 共用同一份文件计划；去重、缺失与上限均可见。"""

import asyncio
import os
import tempfile
import time
import zipfile
from collections import defaultdict
from pathlib import Path

from fastapi import HTTPException

from . import config, db, jobs, storage
from .queries import post_filter

MAX_POSTS = 500
MAX_ZIP_BYTES = 2 * 1024**3
QUALITIES = ("full", "mid", "unknown", "thumbnail")


def _max_zip_bytes() -> int:
    val = getattr(config, "MAX_ZIP_BYTES", None)
    return int(val) if val else int(MAX_ZIP_BYTES)


def _temp_dir() -> Path:
    d = config.DATA_DIR / ".tmp"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        if (d.stat().st_mode & 0o777) != 0o700:
            os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def tokens(value: str, limit: int = 0) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in (value or "").split(","):
        tok = part.strip()
        if not tok or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if limit and len(out) > limit:
            raise HTTPException(400, f"筛选参数过多（最多 {limit} 项），请分批选择")
    return out


def _unique_entry_name(entries: dict, name: str) -> str:
    if name not in entries:
        return name
    folder, sep, leaf = name.rpartition("/")
    stem, ext = os.path.splitext(leaf)
    ext_bytes = len(ext.encode("utf-8"))
    for idx in range(2, len(entries) + 10):
        suffix = f"_{idx}"
        budget = max(1, 255 - ext_bytes - len(suffix.encode("utf-8")))
        cut = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
        candidate_leaf = f"{cut}{suffix}{ext}"
        candidate = f"{folder}{sep}{candidate_leaf}" if sep else candidate_leaf
        if candidate not in entries:
            return candidate
    return name


def _safe_pic_src(rel: str) -> Path | None:
    if not rel or "\\" in rel or rel.startswith("/"):
        return None
    root = config.PICS_DIR.resolve()
    raw = config.PICS_DIR / rel
    if raw.is_symlink():
        return None
    resolved = raw.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    return resolved


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
    *,
    enforce_byte_limit: bool = True,
):
    if status == "selected" and not select:
        return {"posts": 0, "pics": 0, "missing": 0, "bytes": 0,
                "entries": {}, "rows": [], "quality": dict.fromkeys(QUALITIES, 0),
                "valid_rels": set()}
    if select:
        sids = tokens(select, MAX_POSTS)
        cond, args = f"p.sid IN ({','.join('?' * len(sids))})", sids
    else:
        if status not in ("kept", "all", "pending", "discarded", "favorites"):
            raise HTTPException(400, "下载范围无效")
        cond, args = post_filter(status, group, author, range_)
    author_ids = tokens(authors, MAX_POSTS)
    if author_ids:
        conditions = []
        for key in author_ids:
            source, uid = key.split(":", 1) if ":" in key else ("weibo", key)
            conditions.append("(p.uid=? AND COALESCE(NULLIF(p.source,''),'weibo')=?)")
            args += [uid, source]
        cond += " AND (" + " OR ".join(conditions) + ")"
    for column, values in (("substr(p.created_at,1,10)", tokens(dates, MAX_POSTS)),):
        if values:
            cond += f" AND {column} IN ({','.join('?' * len(values))})"
            args += values
    with db.conn() as c:
        count = c.execute(f"SELECT COUNT(*) FROM posts p WHERE {cond}", args).fetchone()[0]
        if count > MAX_POSTS:
            # 超过上限时在 SQL 端聚合博主/日期桶元数据，避免全量行传输与磁盘检查。
            author_rows = c.execute(
                "SELECT COALESCE(NULLIF(p.source,''),'weibo') AS source, p.uid, "
                "MAX(p.author) AS author, COUNT(*) AS n "
                f"FROM posts p WHERE {cond} "
                "GROUP BY COALESCE(NULLIF(p.source,''),'weibo'), p.uid",
                args,
            ).fetchall()
            date_rows = c.execute(
                "SELECT substr(p.created_at,1,10) AS day, COUNT(*) AS n "
                f"FROM posts p WHERE ({cond}) AND substr(p.created_at,1,10) != '' "
                "GROUP BY day",
                args,
            ).fetchall()
            author_buckets = {}
            for r in author_rows:
                key = db.author_key(r["source"], r["uid"])
                if key:
                    author_buckets[key] = {"author": r["author"] or "", "n": r["n"]}
            date_buckets = {r["day"]: r["n"] for r in date_rows if r["day"]}
            return {"posts": count, "pics": None, "missing": None, "bytes": 0,
                    "entries": {}, "rows": [], "quality": None,
                    "valid_rels": set(),
                    "author_buckets": author_buckets,
                    "date_buckets": date_buckets}
        rows = c.execute(
            "SELECT p.sid,p.uid,p.source,p.author,p.created_at,p.text,pc.file,pc.thumb,pc.quality,pc.kind "
            "FROM posts p LEFT JOIN pics pc ON pc.sid=p.sid "
            f"WHERE {cond} ORDER BY p.created_at DESC,p.sid,pc.position,pc.id",
            args,
        ).fetchall()
    entries, seen, posts, missing, numbered = {}, set(), set(), set(), defaultdict(int)
    valid_srcs = {}
    quality_files = {}
    total_bytes = 0
    for r in rows:
        posts.add(r["sid"])
        # 视频行 thumb=海报：即使用户勾了「缩略图」也必须给原视频，否则包里是图不是片
        rel = r["file"] if (r["kind"] or "photo") == "video" else (r["thumb"] if thumb else r["file"])
        if not rel or rel in missing:
            continue
        src = valid_srcs.get(rel)
        if src is None:
            src = _safe_pic_src(rel)
            if src is None:
                missing.add(rel)
                continue
            valid_srcs[rel] = src
        quality = "thumbnail" if thumb else r["quality"] if r["quality"] in QUALITIES else "unknown"
        if rel in seen:
            if quality_files[rel] != quality:
                quality_files[rel] = "mid" if "mid" in (quality_files[rel], quality) else "unknown"
            continue
        seen.add(rel)
        total_bytes += src.stat().st_size
        quality_files[rel] = quality
        numbered[r["sid"]] += 1
        name = storage.zip_entry_name(
            r["created_at"], r["author"], r["sid"], numbered[r["sid"]], src.suffix, r["text"]
        )
        if subfolder:
            name = f"{storage.sanitize_filename(r['author'], 60)}/{name}"
        entries[_unique_entry_name(entries, name)] = src
    if enforce_byte_limit and total_bytes > _max_zip_bytes():
        raise HTTPException(400, "打包总体积超过上限，请按博主或日期分批选择")
    return {
        "posts": len(posts),
        "pics": len(entries),
        "missing": len(missing),
        "bytes": total_bytes,
        "entries": entries,
        "rows": rows,
        "quality": {key: sum(q == key for q in quality_files.values()) for key in QUALITIES},
        "valid_rels": set(valid_srcs),
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
    result = plan(scope, authors=authors, dates=dates, select=select, thumb=thumb, enforce_byte_limit=False)
    if result["pics"] is None:
        author_list = [
            {"uid": k, "author": b["author"], "n": b["n"], "pics": None}
            for k, b in (result.get("author_buckets") or {}).items()
        ]
        date_list = [
            {"date": k, "n": n, "pics": None}
            for k, n in (result.get("date_buckets") or {}).items()
        ]
    else:
        # 桶只用于选择；合计每次按同一计划重算，跨作者/日期重复图不会被多算。
        valid_rels = result.get("valid_rels")
        buckets = {"authors": {}, "dates": {}}
        for r in result["rows"]:
            for axis, key in (("authors", db.author_key(r["source"], r["uid"])), ("dates", (r["created_at"] or "")[:10])):
                if not key:
                    continue
                b = buckets[axis].setdefault(
                    key, {"sids": set(), "files": set(), "author": r["author"]}
                )
                b["sids"].add(r["sid"])
                rel = r["file"] if (r["kind"] or "photo") == "video" else (r["thumb"] if thumb else r["file"])
                if rel and (rel in valid_rels if valid_rels is not None else _safe_pic_src(rel) is not None):
                    b["files"].add(rel)
        author_list = [
            {"uid": k, "author": b["author"], "n": len(b["sids"]), "pics": len(b["files"])}
            for k, b in buckets["authors"].items()
        ]
        date_list = [
            {"date": k, "n": len(b["sids"]), "pics": len(b["files"])}
            for k, b in buckets["dates"].items()
        ]
    return {
        "ok": True,
        "posts": result["posts"],
        "pics": result["pics"],
        "missing": result["missing"],
        "bytes": result["bytes"],
        "max_bytes": _max_zip_bytes(),
        "quality": result["quality"],
        "max_posts": MAX_POSTS,
        "over_limit": result["posts"] > MAX_POSTS or result["bytes"] > _max_zip_bytes(),
        "authors": author_list,
        "dates": date_list,
    }


# ---------- 批量打包任务（job 框架版） ----------

# 打包完成的临时 zip 登记：job_id -> {"path": 文件路径, "ts": 登记时间}。
# 交付完成后由响应的 BackgroundTask 走 pop_result_file 领走删除；没被领取的靠 sweep 兜底。
_RESULT_FILES = {}
_RESULT_TTL = 2 * 3600


def result_file(job_id: str) -> str | None:
    """按 job id 查询打包产物路径（不出借所有权，避免写入公开 job.result）。"""
    item = _RESULT_FILES.get(job_id)
    return item["path"] if item else None


def pop_result_file(job_id):
    """按 job id 领走打包产物登记（含路径与时间），没有登记返回 None。"""
    return _RESULT_FILES.pop(job_id, None)


def sweep_tmp_dir(max_age: float = 0) -> None:
    """清理 DATA_DIR/.tmp 下孤立的临时打包 zip（重启恢复或定期巡检时调用）。"""
    tmp_dir = config.DATA_DIR / ".tmp"
    if not tmp_dir.is_dir():
        return
    cutoff = time.time() - max_age if max_age > 0 else None
    active = {item["path"] for item in _RESULT_FILES.values()}
    for path in tmp_dir.glob("zip-*.zip"):
        if str(path) in active or path.is_symlink() or not path.is_file():
            continue
        try:
            if cutoff is not None and path.stat().st_mtime >= cutoff:
                continue
            path.unlink(missing_ok=True)
        except OSError:
            continue


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
    sweep_tmp_dir(_RESULT_TTL)


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
                "missing": stats["missing"], "bytes": stats["bytes"]}

    return work


def _build_zip(params: dict, cancelled, progress):
    """同步构建批量 zip：与 GET /api/download/zip 同一份 plan、命名与 评论.txt 语义。

    逐文件检查中断标志，文件级失败/中断都会把半成品临时文件清掉再抛。
    """
    status = params.get("scope") or params.get("status") or "pending"
    result = plan(status, params.get("group", "all"), params.get("author", "all"),
                  params.get("range") or "all", params.get("authors", ""),
                  params.get("dates", ""), params.get("select", ""),
                  bool(params.get("thumb")), bool(params.get("subfolder")),
                  enforce_byte_limit=False)
    if result["posts"] > MAX_POSTS:
        raise HTTPException(400, "单包最多 500 帖，请按博主或日期分批选择；没有截断下载内容")
    if result["bytes"] > _max_zip_bytes():
        raise HTTPException(400, "打包总体积超过上限，请按博主或日期分批选择")
    entries = result["entries"]
    if not entries:
        raise HTTPException(404, "没有可打包的图片")
    fd, tmp = tempfile.mkstemp(prefix="zip-", suffix=".zip", dir=_temp_dir())
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
                if done % 32 == 0 or done == total:
                    progress(done, total)
            if notes:   # 范围内有批注才附带；纯文本、"> " 引用协议与详情一致
                z.writestr("评论.txt", notes)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return tmp, {"posts": result["posts"], "entries": total,
                 "missing": result["missing"], "bytes": result["bytes"]}
