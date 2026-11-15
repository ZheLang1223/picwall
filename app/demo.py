"""演示数据：不碰微博、不需要 Cookie，本地生成几张占位图灌进库，
用来在填 Cookie 之前预览相册交互。作者名带「演示」前缀，随时一键清除。
"""
import random
from datetime import datetime, timedelta

from PIL import Image, ImageDraw

from . import config, db, storage

_GROUP = ("demo", "演示分组")

_POSTS = [
    ("山间清晨的雾还没散，光线正好。", 1),
    ("傍晚的天色，随手一拍就是壁纸。", 4),
    ("街角的猫，蹲了它十分钟。", 2),
    ("这组光影构图绝了，存下来慢慢学。", 3),
    ("海边的风很大，但值得。", 2),
    ("老城区的午后，时间仿佛慢了半拍。", 1),
    ("雨后的叶子，绿得发亮。", 3),
    ("夜市的人间烟火气。", 2),
]

_PALETTES = [((240, 244, 248), (168, 192, 214)), ((250, 246, 238), (222, 196, 160)),
             ((242, 240, 246), (188, 172, 210)), ((240, 246, 242), (164, 200, 180)),
             ((248, 242, 242), (214, 168, 160))]


def _gen_image(idx: int) -> bytes:
    rnd = random.Random(idx)
    (c1, c2) = _PALETTES[idx % len(_PALETTES)]
    w, h = rnd.choice([(900, 1200), (1200, 900), (1000, 1000)])
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)],
               fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)))
    label = f"DEMO {idx:02d}"
    d.rectangle([40, h - 140, 40 + 46 + 9 * len(label), h - 70], fill=(255, 255, 255))
    d.text((60, h - 120), label, fill=(60, 60, 66))
    import io
    out = io.BytesIO()
    im.save(out, "JPEG", quality=88)
    return out.getvalue()


def seed() -> dict:
    config.ensure_dirs()
    now = datetime.now(config.NOW_TZ)
    generated = {
        i * 4 + j + 1: _gen_image(i * 4 + j + 1)
        for i, (_, n_pics) in enumerate(_POSTS)
        for j in range(n_pics)
    }
    _COMMENTS = {
        "demo0000": ["光线抓得真好，这个点位秋天再来一次。", "已查到机位：北坡观景台，日出前半小时到。"],
        "demo0002": ["这只猫叫煤球，常驻这家咖啡店门口。"],
        "demo0003": ["构图学一下：前景留白，主体压右下。", "同款思路拿竖构图再试一张。", "存进「构图参考」了。"],
        "demo0006": ["雨后一小时的光最难等，收藏当样本。"],
    }
    with db._write_lock:
        with db.conn() as c:
            if c.execute("SELECT 1 FROM posts WHERE sid='demo0000'").fetchone():
                return {"ok": False, "msg": "演示数据已存在"}
        saved_imgs: dict[int, tuple[str, int, int, str]] = {}
        for idx, data in generated.items():
            saved_imgs[idx] = storage.save_image(data, "demo.jpg")
        now_iso = now.isoformat(timespec="seconds")
        total = 0
        with db.conn() as c:
            c.execute(
                "INSERT INTO groups(list_id,name,added_at,active) VALUES(?,?,?,1) "
                "ON CONFLICT(list_id) DO UPDATE SET name=excluded.name, active=1",
                (_GROUP[0], _GROUP[1], now_iso),
            )
            for i, (text, n_pics) in enumerate(_POSTS):
                created = (now - timedelta(days=i % 3, hours=3 * i, minutes=17 * i))\
                    .isoformat(timespec="seconds")
                sid = f"demo{i:04d}"
                uid = f"demo{i}"
                author = f"演示作者 · {chr(65 + i)}"
                c.execute(
                    "INSERT OR IGNORE INTO posts(sid,list_id,uid,author,avatar,text,created_at,"
                    "fetched_at,weibo_url,via,is_long,source) VALUES(?,?,?,?,?,?,?,?,?,?,0,'weibo')",
                    (sid, _GROUP[0], uid, author, "", text, created, now_iso, "https://weibo.com", ""),
                )
                c.execute("INSERT OR IGNORE INTO post_groups VALUES(?,?)", (sid, _GROUP[0]))
                c.execute(
                    "INSERT INTO post_observations VALUES(?,?,?,?,?) "
                    "ON CONFLICT(sid,list_id,via) DO UPDATE SET text=excluded.text,observed_at=excluded.observed_at",
                    (sid, _GROUP[0], "", text, now_iso),
                )
                for j in range(n_pics):
                    idx = i * 4 + j + 1
                    rel, w, h, t_rel = saved_imgs[idx]
                    c.execute(
                        "INSERT OR IGNORE INTO pics(sid,file,thumb,w,h,url,kind,dur_ms,poster_url) "
                        "VALUES(?,?,?,?,?,?,'photo',0,'')",
                        (sid, rel, t_rel, w, h, f"demo://{idx:02d}"),
                    )
                    total += 1
            for sid, bodies in _COMMENTS.items():
                for k, text in enumerate(bodies):
                    at = (now - timedelta(minutes=40 * (len(bodies) - k))).isoformat(timespec="seconds")
                    c.execute("INSERT INTO post_comments(sid,body,created_at) VALUES(?,?,?)", (sid, text, at))
            c.execute(
                "INSERT INTO meta(k,v) VALUES('demo','1') "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v"
            )
    return {"ok": True, "posts": len(_POSTS), "pics": total}


def clear() -> dict:
    pics_root = config.PICS_DIR.resolve()
    with db._write_lock:
        to_unlink: set[str] = set()
        with db.conn() as c:
            sids = [r[0] for r in c.execute("SELECT sid FROM posts WHERE list_id=?", (_GROUP[0],)).fetchall()]
            files: set[str] = set()
            if sids:
                ph = ",".join("?" * len(sids))
                rows = c.execute(f"SELECT file, thumb FROM pics WHERE sid IN ({ph})", sids).fetchall()
                for r in rows:
                    if r["file"]:
                        files.add(r["file"])
                    if r["thumb"]:
                        files.add(r["thumb"])
                for tbl in ("pics", "posts", "image_jobs", "decision_undos",
                            "post_groups", "post_observations", "post_comments",
                            "post_tags", "review_events"):
                    c.execute(f"DELETE FROM {tbl} WHERE sid IN ({ph})", sids)
            c.execute("DELETE FROM post_groups WHERE list_id=?", (_GROUP[0],))
            c.execute("DELETE FROM groups WHERE list_id=?", (_GROUP[0],))
            c.execute(
                "INSERT INTO meta(k,v) VALUES('demo','0') "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v"
            )
            for rel in files:
                if c.execute("SELECT 1 FROM pics WHERE file=? OR thumb=?", (rel, rel)).fetchone():
                    continue
                c.execute("DELETE FROM media_contents WHERE file=?", (rel,))
                to_unlink.add(rel)
        for rel in to_unlink:
            raw = config.PICS_DIR / rel
            path = raw.resolve()
            if not raw.is_symlink() and path.is_file() and path.is_relative_to(pics_root):
                path.unlink(missing_ok=True)
    return {"ok": True}


def is_on() -> bool:
    return db.get_meta("demo") == "1"
