"""图片落盘：原图按内容寻址（sha1）去重，生成浏览用缩略图，「保留」时复制进导出区。

目录文法沿用本项目的媒体规范：
  pics/YYYY/MM/<sha1前16>.<ext>        原图
  pics/YYYY/MM/t_<sha1前16>.jpg        缩略图（宽 >720 的图才有；宽度压到 720、等比不设限）
  （宽 ≤720 的图不另生成缩略图：thumb 列直接指原图，窄长条不再被压瘪）
  pics/avatars/<sha1前16>.<ext>        作者头像
  export/<保留日期>/<sha1前16>.<ext>    保留归档（同步到设备的文件夹）
"""
import hashlib
import io
import os
import re
import shutil
import tempfile
from pathlib import Path

from PIL import Image

from . import config
from .gate import guarded

CARD_THUMB_W = 720   # 卡片缩略图宽度上限：墙列 ~380px 的 2 倍分辨率


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def ext_of(url: str, data: bytes) -> str:
    tail = url.rsplit(".", 1)[-1].lower() if "." in url.rsplit("/", 1)[-1] else ""
    if tail in ("jpg", "jpeg", "png", "gif", "webp", "mp4"):
        return ".jpg" if tail == "jpeg" else f".{tail}"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    # MP4 魔数：box 类型 'ftyp' 在文件头 offset 4（video.twimg.com 的变体 URL 未必带 .mp4 后缀）
    if len(data) > 8 and data[4:8] == b"ftyp":
        return ".mp4"
    return ".jpg"


def _write_thumb(im, dst: Path, quality: int):
    fd, tmp = tempfile.mkstemp(prefix=".thumb-", dir=dst.parent)
    os.close(fd)
    try:
        im.save(tmp, "JPEG", quality=quality)
        os.replace(tmp, dst)
    finally:
        Path(tmp).unlink(missing_ok=True)


@guarded
def _make_thumb(src: Path, dst: Path, max_side: int = 640) -> bool:
    try:
        with Image.open(src) as im:
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            _write_thumb(im, dst, 82)
        return True
    except Exception:
        return False


@guarded
def _make_wide_thumb(src: Path, dst: Path, target_w: int = CARD_THUMB_W) -> bool:
    """宽图缩略图：宽度压到 target_w、等比（高度不限），长条图不塌缩。"""
    try:
        with Image.open(src) as im:
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            if im.size[0] > target_w:
                im = im.resize((target_w, round(im.size[1] * target_w / im.size[0])),
                               Image.LANCZOS)
            _write_thumb(im, dst, 85)
        return True
    except Exception:
        return False


@guarded
def save_image(data: bytes, url: str) -> tuple[str, int, int, str]:
    """写原图 + 缩略图，返回 (相对路径, 宽, 高, 缩略图相对路径)。

    缩略图按「宽度封顶 CARD_THUMB_W」生成，高度等比不设限——方形盒适配会把
    长条漫画压成几十像素宽（卡片放大十几倍必然糊）。宽 ≤720 的图不另生成
    缩略图，thumb 直接指原图：零重编码零额外体积，GIF 在卡片里还保留动图。
    缩略图生成失败时退回原图本身，宁可卡片加载慢也不缺图。
    """
    from datetime import datetime
    # 先验证再落盘，平台错误页/截断图片不会留下永久坏文件。
    if len(data) > config.MAX_IMAGE_BYTES:
        raise ValueError("图片超过下载体积上限")
    with Image.open(io.BytesIO(data)) as im:
        w, hh = im.size
        if w * hh > config.MAX_IMAGE_PIXELS:
            raise ValueError("图片超过像素上限，保留补抓任务")
        im.verify()
    from . import db
    content_hash = hashlib.sha256(data).hexdigest()
    catalog = config.DB_PATH.parent == config.DATA_DIR and config.DB_PATH.is_file()
    existing = None
    if catalog:
        with db.conn() as c:
            existing = c.execute("SELECT file FROM media_contents WHERE sha256=?", (content_hash,)).fetchone()
    ym = datetime.now(config.NOW_TZ).strftime("%Y/%m")
    h = sha1(data)
    ext = ext_of(url, data)
    rel = f"{ym}/{h}{ext}"
    if existing:
        candidate = config.PICS_DIR / existing["file"]
        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == content_hash:
            rel = existing["file"]
    path = config.PICS_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd, tmp = tempfile.mkstemp(prefix=".picwall-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    t_rel = rel
    if w > CARD_THUMB_W:
        t_path = path.parent / f"t_{h}.jpg"
        if t_path.is_file() or _make_wide_thumb(path, t_path):
            t_rel = str(t_path.relative_to(config.PICS_DIR))

    if catalog:
        with db.conn() as c:
            c.execute("INSERT INTO media_contents VALUES(?,?,?) ON CONFLICT(sha256) "
                      "DO UPDATE SET file=excluded.file,size=excluded.size", (content_hash, rel, len(data)))

    return rel, w, hh, t_rel


@guarded
def save_avatar(data: bytes, url: str) -> str:
    """头像落盘：优先 96px 小图（界面只画 22-34px，原图仅供回退），返回相对路径。"""
    if len(data) > config.MAX_IMAGE_BYTES:
        raise ValueError("头像超过体积上限")
    with Image.open(io.BytesIO(data)) as im:
        if im.width * im.height > config.MAX_IMAGE_PIXELS:
            raise ValueError("头像超过像素上限")
        im.verify()
    rel = f"avatars/{sha1(data)}{ext_of(url, data)}"
    path = config.PICS_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(data)
    t_rel = f"avatars/t_{sha1(data)}.jpg"
    if _make_thumb(path, config.PICS_DIR / t_rel, max_side=96):
        return t_rel
    return rel


@guarded
def save_video(data: bytes, url: str) -> str:
    """视频落盘（X 的 mp4 变体）：sha1 内容寻址 + media_contents 去重，无 PIL 解码。

    不生成本地缩略图/封面帧（容器不引 ffmpeg）——封面用推文自带的海报图，
    由调用方走 save_image 落盘后把 pics.thumb 指向它。返回相对路径。
    """
    from datetime import datetime
    if len(data) > config.MAX_VIDEO_BYTES:
        raise ValueError("视频超过下载体积上限")
    from . import db
    content_hash = hashlib.sha256(data).hexdigest()
    catalog = config.DB_PATH.parent == config.DATA_DIR and config.DB_PATH.is_file()
    existing = None
    if catalog:
        with db.conn() as c:
            existing = c.execute("SELECT file FROM media_contents WHERE sha256=?",
                                 (content_hash,)).fetchone()
    ym = datetime.now(config.NOW_TZ).strftime("%Y/%m")
    h = sha1(data)
    rel = f"{ym}/{h}{ext_of(url, data)}"
    if existing:
        candidate = config.PICS_DIR / existing["file"]
        if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == content_hash:
            rel = existing["file"]
    path = config.PICS_DIR / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd, tmp = tempfile.mkstemp(prefix=".picwall-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    if catalog:
        with db.conn() as c:
            c.execute("INSERT INTO media_contents VALUES(?,?,?) ON CONFLICT(sha256) "
                      "DO UPDATE SET file=excluded.file,size=excluded.size",
                      (content_hash, rel, len(data)))
    return rel


@guarded
def _export_one(rel: str, kept_day: str) -> str:
    """一张图的文件和记录共用锁；整批检查不占住其他决定写入。"""
    from . import db
    src = config.PICS_DIR / rel
    if not src.resolve().is_relative_to(config.PICS_DIR.resolve()):
        raise ValueError("归档原图路径越界")
    with db.conn() as c:
        entry = c.execute("SELECT day,sha256 FROM archive_entries WHERE file=?", (rel,)).fetchone()
    old_day = entry["day"] if entry else db.get_meta(f"exported:{rel}")
    day = old_day or kept_day
    dst = config.EXPORT_DIR / day / src.name
    if not dst.resolve().is_relative_to(config.EXPORT_DIR.resolve()):
        raise ValueError("归档目标路径越界")
    tmp = None
    content_hash = ""
    try:
        content_hash = file_hash(src) if src.is_file() else ""
        expected = entry["sha256"] if entry else ""
        if expected and content_hash != expected:
            # 已知基准下的坏原图不能覆盖独立归档；有效副本可反向恢复原图。
            if dst.is_file() and file_hash(dst) == expected:
                src.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".picwall-", dir=src.parent)
                os.close(fd)
                shutil.copy2(dst, tmp)
                if file_hash(Path(tmp)) != expected:
                    raise OSError("原图恢复内容校验失败")
                os.replace(tmp, src)
                _archive_record(rel, day, expected, src.stat().st_size, "complete")
                return "restored"
            _archive_record(rel, day, "", 0, "failed", "original_checksum")
            return "failed"
        if not src.is_file():
            raise FileNotFoundError("原图缺失，请先补抓图片")
        size = src.stat().st_size
        if dst.is_file() and file_hash(dst) == content_hash:
            _archive_record(rel, day, content_hash, size, "complete")
            return "dup"
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".picwall-", dir=dst.parent)
        os.close(fd)
        shutil.copy2(src, tmp)
        if file_hash(Path(tmp)) != content_hash:
            raise OSError("归档复制内容校验失败")
        os.replace(tmp, dst)
        _archive_record(rel, day, content_hash, size, "complete")
        return "exported"
    except OSError as e:
        _archive_record(rel, day, "", 0, "failed", type(e).__name__)
        return "failed"
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def export_report(file_rels, kept_day: str) -> dict:
    """归档逐图报告：校验既有副本，临时文件写完再原子替换；失败可重试。"""
    result = {"exported": 0, "dup": 0, "restored_originals": 0, "failed": [], "to": f"data/export/{kept_day}/"}
    for rel in dict.fromkeys(f for f in file_rels if f):
        state = _export_one(rel, kept_day)
        if state == "failed":
            result["failed"].append(rel)
        elif state == "restored":
            result["restored_originals"] += 1
            result["dup"] += 1
        else:
            result[state] += 1
    result["archive_complete"] = not result["failed"]
    result["failed_count"] = len(result["failed"])
    return result


def export_pics(file_rels, kept_day: str) -> tuple[int, int]:
    """旧工具调用保留二元组接口；服务端使用包含失败信息的 export_report。"""
    r = export_report(file_rels, kept_day)
    return r["exported"], r["dup"]


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _archive_record(rel, day, sha, size, state, error=""):
    from datetime import datetime

    from . import db
    with db._write_lock, db.conn() as c:
        # 成功路径一次提交归档记录与两个兼容 meta 字段，减少逐图 fsync。
        values = [(f"export_error:{rel}", error)]
        if state == "complete":
            values.append((f"exported:{rel}", day))
        c.executemany("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", values)
        c.execute("INSERT INTO archive_entries VALUES(?,?,?,?,?,?,?) ON CONFLICT(file) "
                  "DO UPDATE SET day=excluded.day,sha256=CASE WHEN excluded.sha256!='' THEN excluded.sha256 ELSE archive_entries.sha256 END,"
                  "size=CASE WHEN excluded.size>0 THEN excluded.size ELSE archive_entries.size END,"
                  "state=excluded.state,error=excluded.error,checked_at=excluded.checked_at",
                  (rel, day, sha, size, state, error, datetime.now(config.NOW_TZ).isoformat()))


def archive_health(deep: bool = False) -> dict:
    from . import db
    with db.conn() as c:
        rows = c.execute(
            "SELECT DISTINCT pc.file,m.v day,a.sha256,a.error FROM pics pc JOIN posts p ON p.sid=pc.sid "
            "LEFT JOIN meta m ON m.k='exported:' || pc.file "
            "LEFT JOIN archive_entries a ON a.file=pc.file "
            "WHERE p.status='kept' AND pc.file!=''").fetchall()
    missing = []
    original_missing = 0
    corrupt_originals = 0
    for r in rows:
        src = config.PICS_DIR / r["file"]
        dst = config.EXPORT_DIR / (r["day"] or "") / src.name
        try:
            if not src.is_file():
                original_missing += 1
            corrupt = r["error"] == "original_checksum" or (deep and src.is_file() and r["sha256"] and file_hash(src) != r["sha256"])
            corrupt_originals += int(bool(corrupt))
            if not r["day"] or not src.is_file() or not dst.is_file() \
                    or corrupt or src.stat().st_size != dst.stat().st_size:
                missing.append(r["file"])
        except OSError:
            missing.append(r["file"])
    with db.conn() as c:
        history = c.execute("SELECT * FROM archive_entries").fetchall()
    historical_missing = historical_corrupt = historical_unchecked = 0
    for r in history:
        dst = config.EXPORT_DIR / r["day"] / Path(r["file"]).name
        if not dst.is_file():
            historical_missing += 1
        elif deep and r["sha256"] and file_hash(dst) != r["sha256"]:
            historical_corrupt += 1
        elif not r["sha256"]:
            historical_unchecked += 1
    return {"missing": len(missing), "missing_originals": original_missing, "corrupt_originals": corrupt_originals, "files": missing,
            "historical_missing": historical_missing, "historical_corrupt": historical_corrupt,
            "historical_unchecked": historical_unchecked}



def retry_archive(cancelled=None, progress=None) -> dict:
    from datetime import datetime

    from . import db
    with db.conn() as c:
        rows = c.execute(
            "SELECT pc.file,MIN(substr(p.kept_at,1,10)) day FROM pics pc "
            "JOIN posts p ON p.sid=pc.sid WHERE p.status='kept' AND pc.file!='' "
            "GROUP BY pc.file").fetchall()
        history = c.execute("SELECT file,day FROM archive_entries").fetchall()
    work = {r["file"]: r["day"] for r in rows}
    # 同一个归档只检查一次；已不在保留列表里的历史副本仍独立检查和修复。
    for r in history:
        if r["file"] not in work and (config.PICS_DIR / r["file"]).is_file():
            work[r["file"]] = r["day"]
    summary = {"exported": 0, "dup": 0, "restored_originals": 0, "failed_count": 0}
    total = len(work)
    if progress:
        progress(0, total)
    for index, (rel, day) in enumerate(work.items(), 1):
        if cancelled and cancelled():
            raise InterruptedError()
        r = export_report([rel], day or datetime.now(config.NOW_TZ).strftime("%Y-%m-%d"))
        for k in summary:
            summary[k] += r[k]
        if progress and (index % 32 == 0 or index == total):
            progress(index, total)
    if cancelled and cancelled():
        raise InterruptedError()
    history = archive_health(deep=True)
    current_complete = not (history["missing"] or history["missing_originals"])
    historical_complete = not (history["historical_missing"] or history["historical_corrupt"] or history["historical_unchecked"])
    return {"ok": True, **summary, "archive_complete": current_complete and historical_complete,
            "current_complete": current_complete, "historical_complete": historical_complete,
            "history": history}


@guarded
def remove_pic_files(file_rel: str) -> bool:
    """清理硬删帖子的落盘图片：原图 + 同内容的缩略图（t_ 前缀、同目录同名）。"""
    src = config.PICS_DIR / file_rel
    removed = False
    for p in {src, src.parent / f"t_{src.name}", src.parent / f"t_{src.stem}.jpg"}:
        if not p.resolve().is_relative_to(config.PICS_DIR.resolve()):
            raise ValueError("媒体清理路径越界")
        if p.is_file():
            p.unlink()
            removed = True
    return removed


# Windows 保留设备名（不分大小写；片段名撞上即使用处加前缀中和）
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)},
                 *{f"LPT{i}" for i in range(1, 10)}}


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """平台通用的文件名片段清洗（片段 = 不含扩展名的部分）。

    - 剔除 Windows 九件套非法字符与控制字符（zip 条目带控制字符在部分解压端会坏）；
    - 剔除各类空白与首尾的点/空格（Windows 不接受结尾点空格）；
    - 命中 Windows 保留设备名时加 "_" 前缀中和；
    - 限长防 MAX_PATH 连锁超限；中文保留（zip 按 UTF-8+EFS 写入）。
    """
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f\s]+', "", name or "")
    s = s.strip(". ")
    if s.upper() in _WIN_RESERVED:
        s = "_" + s
    if not s:
        s = "wb"
    return s[:max_len]


def zip_entry_name(created_at: str, author: str, sid: str, idx: int, suffix: str,
                   text: str = "") -> str:
    """批量打包的统一命名：日期_作者_原博ID_序号_正文前30字.ext。

    片段上限按字节算：中文 3 字节/字符，作者 60 字 + ID 40 字 + 固定部分
    ≤ 255 字节，深目录解压也不会撞 MAX_PATH/文件名长度限制。
    """
    from datetime import datetime
    day = (created_at or "")[:10].replace("-", "") or datetime.now().strftime("%Y%m%d")
    safe = sanitize_filename(author, 60)
    clean_sid = sanitize_filename(sid, 40)   # x: 前缀带冒号，同样要过清洗
    raw = re.sub(r"\s+", " ", (text or "").strip())
    t = sanitize_filename(raw, 30) if raw else ""
    stem = f"{day}_{safe}_{clean_sid}_{idx:02d}"
    return f"{stem}_{t}{suffix or '.jpg'}" if t else f"{stem}{suffix or '.jpg'}"


def format_notes(groups: list[tuple[str, list[dict]]], header: str) -> str:
    """批注文本格式（批量 zip 附带与保留归档共用）：帖头 + 缩进批注行。

    "> " 引用协议原样保留（纯文本可读）；多行批注保持换行并随缩进对齐。
    """
    out = [f"PWALL 批注 · {header}", ""]
    for label, comments in groups:
        out.append(label)
        for c in comments:
            at = (c["created_at"] or "")[5:16].replace("T", " ")
            out.append(f"  [{at}] {c['body']}".replace("\n", "\n  "))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def rebuild_day_notes(day: str) -> bool:
    """重建归档文件夹的 评论.txt（保留帖评论增改删、当日点赞时调用）。

    全量重写该保留日期下全部带批注的保留帖（批注与归档生命周期不同步，
    补写的批注也要进归档）；该日期不再有带批注的保留帖时移除文件。
    返回是否落了文件。临时文件写完原子替换，失败不破坏旧文件。
    """
    from . import db
    with db.conn() as c:
        rows = c.execute(
            "SELECT p.sid, p.author, c.body, c.created_at "
            "FROM posts p JOIN post_comments c ON c.sid = p.sid "
            "WHERE p.status='kept' AND substr(p.kept_at,1,10)=? ORDER BY p.sid, c.id",
            (day,)).fetchall()
    target = config.EXPORT_DIR / day / "评论.txt"
    if not rows:
        target.unlink(missing_ok=True)
        return False
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for r in rows:
        key = (r["sid"], r["author"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append({"body": r["body"], "created_at": r["created_at"]})
    text = format_notes([(f"{day}_{a}_{s}", groups[(s, a)]) for s, a in order], day)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("评论.txt.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return True
