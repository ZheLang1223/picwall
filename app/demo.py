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
    if db.has_post("demo0000"):
        return {"ok": False, "msg": "演示数据已存在"}
    db.add_group(*_GROUP)
    now = datetime.now()
    total = 0
    for i, (text, n_pics) in enumerate(_POSTS):
        created = (now - timedelta(days=i % 3, hours=3 * i, minutes=17 * i))\
            .isoformat(timespec="seconds")
        sid = f"demo{i:04d}"
        p = {"sid": sid, "uid": f"demo{i}", "author": f"演示作者 · {chr(65 + i)}",
             "avatar": "", "text": text, "created_at": created,
             "fetched_at": now.isoformat(timespec="seconds"),
             "weibo_url": "https://weibo.com", "via": "", "pics": []}
        for j in range(n_pics):
            idx = i * 4 + j + 1
            data = _gen_image(idx)
            with db._write_lock:   # 写文件+插行同临界区，与 clear 的锁内删文件不赛窗
                rel, w, h, t_rel = storage.save_image(data, "demo.jpg")
                db._insert_pic_locked(sid, rel, t_rel, w, h, f"demo://{idx:02d}")
            total += 1
        db.insert_post(p, _GROUP[0])
    # 演示评论：展示批注时间线（旧→新、每条带首发时间；编辑不漂移）
    _COMMENTS = {
        "demo0000": ["光线抓得真好，这个点位秋天再来一次。", "已查到机位：北坡观景台，日出前半小时到。"],
        "demo0002": ["这只猫叫煤球，常驻这家咖啡店门口。"],
        "demo0003": ["构图学一下：前景留白，主体压右下。", "同款思路拿竖构图再试一张。", "存进「构图参考」了。"],
        "demo0006": ["雨后一小时的光最难等，收藏当样本。"],
    }
    for sid, bodies in _COMMENTS.items():
        for k, text in enumerate(bodies):
            at = (now - timedelta(minutes=40 * (len(bodies) - k))).isoformat(timespec="seconds")
            db.add_comment(sid, text, created_at=at)
    db.set_meta("demo", "1")
    return {"ok": True, "posts": len(_POSTS), "pics": total}


def clear() -> dict:
    with db._write_lock, db.conn() as c:
        rows = c.execute(
            "SELECT p.sid, pc.file, pc.thumb FROM posts p "
            "LEFT JOIN pics pc ON pc.sid = p.sid WHERE p.list_id = ?", (_GROUP[0],)
        ).fetchall()
        files = {(r["file"], r["thumb"]) for r in rows if r["file"]}
        c.execute("DELETE FROM pics WHERE sid IN (SELECT sid FROM posts WHERE list_id=?)",
                  (_GROUP[0],))
        c.execute("DELETE FROM posts WHERE list_id=?", (_GROUP[0],))
        c.execute("DELETE FROM groups WHERE list_id=?", (_GROUP[0],))
    for f, t in files:
        for rel in {f, t}:
            if rel:
                path = (config.PICS_DIR / rel).resolve()
                # is_relative_to 而非前缀字符串比较（/media 同款写法）
                if path.is_file() and path.is_relative_to(config.PICS_DIR.resolve()):
                    path.unlink(missing_ok=True)
    db.set_meta("demo", "0")
    return {"ok": True}


def is_on() -> bool:
    return db.get_meta("demo") == "1"
