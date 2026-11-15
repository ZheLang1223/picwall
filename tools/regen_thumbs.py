"""存量缩略图重生成 + 引用规范化：宽度封顶策略（宽 >720 压到 720；宽 ≤720 thumb 指原图）。

背景：旧策略 thumbnail((640,640)) 方形盒适配会把长条漫画压成几十像素宽，
卡片放大十几倍必然糊。另有个别 Era 的行把 thumb 存成了绝对路径，本工具一并
归一为相对路径。幂等可重复跑。
用法：项目根目录  PICWALL_DATA_DIR=$PWD/data .venv/bin/python tools/regen_thumbs.py
（建议先停容器再跑，避开 WAL 多进程写；跑完再起。--clean-orphans 才做孤儿清理；
--verify 只做内容寻址自检，不写库不动文件）
"""
import glob as _glob
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from app import config, db, storage
from tools import runtime


def _safe_pic_path(rel: str) -> Path | None:
    if not rel:
        return None
    p = Path(rel)
    if p.is_absolute():
        return None
    root = config.PICS_DIR.resolve()
    resolved = (root / p).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        return None
    return resolved


def _sha1_prefix(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def main() -> None:
    runtime.require_stopped_container("缩略图重建")
    with db.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT rowid AS rid, file, thumb FROM pics WHERE file != ''")]
    updates = []   # (thumb, rid)
    regenerated = switched = missing = unread = 0
    unread_names = []
    seen_files = {}   # file → 原图宽（同一内容寻址文件可能被多行共享，只处理一次）
    for r in rows:
        rel = r["file"]
        src = _safe_pic_path(rel)
        if src is None:
            unread += 1
            unread_names.append(rel)
            continue
        if rel in seen_files:
            w = seen_files[rel]
        else:
            if not src.is_file():
                missing += 1
                seen_files[rel] = -1
                continue
            try:
                with Image.open(src) as im:
                    w = im.size[0]
            except Exception:
                unread += 1
                unread_names.append(rel)
                seen_files[rel] = -1
                continue
            seen_files[rel] = w
        if w < 0:
            continue
        if w <= storage.CARD_THUMB_W:
            # 窄图（长条漫画等）：thumb 指原图——零重编码，绝对路径等旧格式一并归一
            if r["thumb"] != rel:
                updates.append((rel, r["rid"]))
                switched += 1
            continue
        # 宽图：规范 thumb 路径 = 原图同目录 t_<stem>.jpg，宽 720
        parent = Path(rel).parent.as_posix()
        t_rel = f"t_{src.stem}.jpg" if parent in ("", ".") else f"{parent}/t_{src.stem}.jpg"
        t_path = _safe_pic_path(t_rel)
        if t_path is None:
            continue
        t_ok = False
        if t_path.is_file():   # 幂等：已是宽度封顶的新缩略图就跳过重编码
            try:
                with Image.open(t_path) as tim:
                    t_ok = 0 < tim.size[0] <= storage.CARD_THUMB_W
            except Exception:
                t_ok = False
        if not t_ok and storage._make_wide_thumb(src, t_path):
            t_ok = True
            regenerated += 1
        # thumb 一律归一为相对路径；生成失败（超大图触发解码保护等）退回原图
        if r["thumb"] != (t_rel if t_ok else rel):
            updates.append((t_rel if t_ok else rel, r["rid"]))
    if updates:
        with db._write_lock, db.conn() as c:
            c.executemany("UPDATE pics SET thumb=? WHERE rowid=?", updates)
    # 头像缩略图修复：posts.avatar 是 web 路径（/media/avatars/t_<x>.jpg），
    # 原图在 avatars/<x>.<ext>（去 t_ 前缀同 stem）——t_ 缺失就从原图重生成
    av_fixed = av_skip = 0
    seen_av = set()
    avatars_root = (config.PICS_DIR / "avatars").resolve()
    with db.conn() as c:
        avatar_rows = [r[0] for r in c.execute(
            "SELECT DISTINCT avatar FROM posts WHERE avatar LIKE '%/avatars/t_%'")]
    for a in avatar_rows:
        if not a or a in seen_av:
            continue
        seen_av.add(a)
        rel = a.split("/media/", 1)[-1]              # avatars/t_<x>.jpg
        av_path = _safe_pic_path(rel)
        if av_path is None or av_path.is_file():
            continue
        stem = Path(rel).stem[2:]                    # 去 t_ 前缀 → 原图 stem
        if not stem or "/" in stem or "\\" in stem or stem in (".", ".."):
            av_skip += 1
            continue
        cands = [
            p for p in _glob.glob(str(config.PICS_DIR / "avatars" / f"{stem}.*"))
            if not Path(p).name.startswith("t_")
            and Path(p).is_file()
            and not Path(p).is_symlink()
            and Path(p).resolve().is_relative_to(avatars_root)
        ]
        if not cands or Path(cands[0]).stat().st_size > config.MAX_IMAGE_BYTES:
            av_skip += 1
            continue
        storage.save_avatar(Path(cands[0]).read_bytes(), cands[0])
        av_fixed += 1
    # 清孤儿 t_：默认关闭（曾误删过被引用的缩略图）。确要清理加 --clean-orphans：
    # 只扫 YYYY/MM 目录、只删不被任何行引用的文件——avatars/ 永不碰。
    orphans = 0
    if "--clean-orphans" in sys.argv:
        pics_root = config.PICS_DIR.resolve()
        with db.conn() as c:
            referenced = {x[0] for x in c.execute("SELECT DISTINCT thumb FROM pics")}
        for p in config.PICS_DIR.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/t_*.jpg"):
            if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(pics_root):
                continue
            rel = p.relative_to(config.PICS_DIR).as_posix()
            if rel not in referenced:
                p.unlink(missing_ok=True)
                orphans += 1
    print(f"重生成完成：宽图重生成 {regenerated}，窄图改指原图 {switched}，"
          f"缺原图跳过 {missing}，无法读取 {unread}，孤儿清理 {orphans}（默认关），"
          f"头像缩略图修复 {av_fixed}（缺原图跳过 {av_skip}）")
    for name in unread_names[:10]:
        print(f"  无法读取: {name}")


def verify_originals() -> None:
    """--verify：内容寻址自检（只读）。原图/头像/归档文件名字段 = sha1 前 16 位，
    字节算出的 sha1 不一致即历史坏文件（写入被截断等）。缩略图（t_ 前缀）是
    重编码产物，跳过。配合 save_image 的「存在即不重写」守卫：坏文件不会自愈，
    只能靠这里现形后单点重抓。"""
    pics_root = config.PICS_DIR.resolve()
    bad = checked = 0
    for pattern in ("[0-9][0-9][0-9][0-9]/[0-9][0-9]/*.*",
                    "avatars/*.*", "export/*/*.*"):
        for p in config.PICS_DIR.glob(pattern):
            if p.name.startswith("t_") or len(p.stem) != 16:
                continue
            if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(pics_root):
                continue
            checked += 1
            h = _sha1_prefix(p)
            if h != p.stem:
                bad += 1
                print(f"  内容不符: {p.relative_to(config.PICS_DIR).as_posix()} "
                      f"(实际 sha1={h})")
    print(f"内容寻址自检：核对 {checked} 个原图/头像/归档文件，内容不符 {bad}。")


if __name__ == "__main__":
    if "--verify" in sys.argv:
        verify_originals()
    else:
        main()
