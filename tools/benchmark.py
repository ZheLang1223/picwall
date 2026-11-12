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

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_posts_status_cursor ON posts(status,created_at DESC,sid)",
    "CREATE INDEX IF NOT EXISTS idx_posts_group_cursor ON posts(list_id,created_at DESC,sid)",
    "CREATE INDEX IF NOT EXISTS idx_posts_favorite_cursor ON posts(created_at DESC,sid) WHERE favorite=1",
    "CREATE INDEX IF NOT EXISTS idx_posts_uid_status ON posts(uid,status)",
]


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
        source = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        source.backup(c)
        source.close()
        if "--synthetic" in sys.argv:
            c.execute("DELETE FROM pics")
            c.execute("DELETE FROM posts")
            c.executemany(
                "INSERT INTO posts(sid,uid,author,avatar,text,created_at,status,favorite) "
                "VALUES(?,?,?,'',?,?,'pending',?)",
                [
                    (
                        f"bench{i:06}",
                        f"u{i % 200}",
                        f"作者{i % 200}",
                        "图片与文字" * 20,
                        f"2026-09-{1 + i % 28:02}T10:00:{i % 60:02}+08:00",
                        i % 7 == 0,
                    )
                    for i in range(20000)
                ],
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
                "SELECT uid,MAX(author),MAX(avatar),MAX(created_at),SUM(status='pending') FROM posts GROUP BY uid",
                (),
            ),
            "search": (
                "SELECT COUNT(*) FROM posts WHERE status=? AND text LIKE '%图片%'",
                (state,),
            ),
            "download_plan": (
                "SELECT p.sid,p.uid,p.author,p.created_at,p.text,pc.file,pc.thumb FROM posts p LEFT JOIN pics pc ON pc.sid=p.sid WHERE p.status='kept' ORDER BY p.created_at DESC,p.sid,pc.id",
                (),
            ),
        }
        if cursor:
            queries["list_cursor_500"] = (
                (
                    "SELECT * FROM posts WHERE status=? AND "
                    "(created_at < ? OR (created_at=? AND sid>?)) ORDER BY created_at DESC,sid LIMIT 50"
                ),
                (state, cursor[0], cursor[0], cursor[1]),
            )
        before = {k: measure(c, *v) for k, v in queries.items()}
        for ddl in INDEXES:
            c.execute(ddl)
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
