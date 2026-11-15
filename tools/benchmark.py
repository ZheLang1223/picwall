"""只读快照测量查询；所有候选索引只加在临时副本，不改活库。"""

import json
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config

# 与 app/db.py schema v13 对齐：idx_posts_status 必须建在 idx_posts_status_keep 之后
# （无 sqlite_stat1 时 SQLite 对等选择性的 status=? 索引取最后创建者），随后条件 ANALYZE。
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_posts_status_cursor ON posts(status,created_at DESC,sid)",
    "CREATE INDEX IF NOT EXISTS idx_posts_fav_cursor ON posts(created_at DESC,sid) WHERE favorite=1",
    "CREATE INDEX IF NOT EXISTS idx_posts_status_keep ON posts(status,keep_count DESC,created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_posts_src_uid_latest ON posts(source,uid,created_at DESC,fetched_at DESC,sid)",
    "CREATE INDEX IF NOT EXISTS idx_pics_sid_pos ON pics(sid,position,id,thumb,file)",
    "CREATE INDEX IF NOT EXISTS idx_post_groups_list ON post_groups(list_id,sid)",
    "DROP INDEX IF EXISTS idx_posts_status",
    "CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)",
]


def apply_candidate_indexes(c) -> None:
    """在临时副本上按 v13 顺序建索引；有数据时补 ANALYZE 生成 sqlite_stat1。"""
    for ddl in INDEXES:
        c.execute(ddl)
    if c.execute("SELECT 1 FROM posts LIMIT 1").fetchone():
        c.execute("ANALYZE")
    c.commit()


def measure(c, sql, args=()):
    samples = []
    for _ in range(12):
        start = time.perf_counter()
        c.execute(sql, args).fetchall()
        samples.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(samples), 3)


def main():
    with tempfile.TemporaryDirectory(prefix="picwall-bench-") as td:
        c = sqlite3.connect(Path(td) / "snapshot.db")
        source = sqlite3.connect(config.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
        source.backup(c)
        source.close()
        if "--synthetic" in sys.argv:
            c.execute("DELETE FROM pics")
            c.execute("DELETE FROM posts")
            statuses = ("pending", "pending", "kept", "discarded")
            rows = []
            pic_rows = []
            for i in range(20000):
                ts = f"2026-09-{1 + i % 28:02}T10:00:{i % 60:02}+08:00"
                st = statuses[i % 4]
                sid = f"bench{i:06}"
                rows.append((
                    sid,
                    f"u{i % 200}",
                    f"作者{i % 200}",
                    "图片与文字" * 20,
                    ts,
                    ts,
                    "weibo",
                    st,
                    1 if i % 7 == 0 else 0,
                    ts if st == "kept" else None,
                    ts if st == "discarded" else None,
                ))
                if st == "kept" or i % 2 == 0:
                    pic_rows.append((
                        sid,
                        f"2026/09/{i:016x}.jpg",
                        f"2026/09/t_{i:016x}.jpg",
                        1080,
                        1440,
                        f"https://wx1.sinaimg.cn/large/{i:08x}.jpg",
                        0,
                    ))
            c.executemany(
                "INSERT INTO posts(sid,uid,author,avatar,text,created_at,fetched_at,source,"
                "status,favorite,kept_at,discarded_at) VALUES(?,?,?,'',?,?,?,?,?,?,?,?)",
                rows,
            )
            c.executemany(
                "INSERT INTO pics(sid,file,thumb,w,h,url,position) VALUES(?,?,?,?,?,?,?)",
                pic_rows,
            )
        state = c.execute(
            "SELECT status FROM posts GROUP BY status ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()[0]
        count = c.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        cursor = c.execute(
            "SELECT created_at,sid FROM posts WHERE status=? "
            "ORDER BY created_at DESC,sid LIMIT 1 OFFSET 500",
            (state,),
        ).fetchone()
        has_fts = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='posts_fts'"
        ).fetchone() is not None
        queries = {
            "list_first": (
                "SELECT * FROM posts WHERE status=? ORDER BY created_at DESC,sid LIMIT 50",
                (state,),
            ),
            "list_offset_500": (
                "SELECT * FROM posts WHERE status=? ORDER BY created_at DESC,sid LIMIT 50 OFFSET 500",
                (state,),
            ),
            "authors": (
                (
                    "WITH agg AS (SELECT source,uid,MAX(created_at) last_at,"
                    "SUM(CASE WHEN status=? THEN 1 ELSE 0 END) n,"
                    "(SELECT p2.sid FROM posts p2 WHERE p2.source=posts.source AND p2.uid=posts.uid "
                    "ORDER BY p2.created_at DESC,p2.fetched_at DESC,p2.sid LIMIT 1) lsid "
                    "FROM posts WHERE uid != '' GROUP BY +source,+uid) "
                    "SELECT a.source,a.uid,p.author,p.avatar,a.last_at,a.n "
                    "FROM agg a JOIN posts p ON p.sid=a.lsid"
                ),
                (state,),
            ),
            "search": (
                "SELECT COUNT(*) FROM posts WHERE status=? AND text LIKE '%图片%'",
                (state,),
            ),
            "download_plan": (
                (
                    "SELECT p.sid,p.uid,p.author,p.created_at,p.text,pc.file,pc.thumb "
                    "FROM posts p LEFT JOIN pics pc ON pc.sid=p.sid "
                    "WHERE p.status='kept' ORDER BY p.created_at DESC,p.sid,pc.position,pc.id"
                ),
                (),
            ),
        }
        if has_fts:
            queries["search_fts"] = (
                (
                    "SELECT COUNT(*) FROM posts "
                    "WHERE rowid IN (SELECT rowid FROM posts_fts WHERE posts_fts MATCH ?)"
                ),
                ('"图片与"',),
            )
        if cursor:
            queries["list_cursor_500"] = (
                (
                    "SELECT * FROM posts WHERE status=? AND "
                    "(created_at < ? OR (created_at=? AND sid>?)) ORDER BY created_at DESC,sid LIMIT 50"
                ),
                (state, cursor[0], cursor[0], cursor[1]),
            )
        before = {k: measure(c, *v) for k, v in queries.items()}
        apply_candidate_indexes(c)
        after = {k: measure(c, *v) for k, v in queries.items()}
        plans = {
            k: [r[3] for r in c.execute("EXPLAIN QUERY PLAN " + v[0], v[1])]
            for k, v in queries.items()
        }
        c.close()
        print(
            json.dumps(
                {
                    "posts": count,
                    "state": state,
                    "synthetic": "--synthetic" in sys.argv,
                    "unit": "median_ms",
                    "before": before,
                    "after": after,
                    "query_plans": plans,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
