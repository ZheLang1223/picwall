"""回看：选组算法、出场记账、点赞计数、权限与点赞排序的回归钉子。

跑法与 test_api 相同：项目根目录 `python -m unittest discover -s tests -t .`。
复用 ApiTestBase 的临时数据目录与 admin 客户端；demo 数据全是 pending，不进回看候选。
"""
import unittest
from datetime import datetime, timedelta

from app import auth, config, db
from tests.test_api import ApiTestBase


def recent_iso(**kw):
    """相对当前东八区时间的 ISO 串：夹具禁止写死「今天」日期（跨零点即哑弹）。"""
    return (datetime.now(config.NOW_TZ) + timedelta(**kw)).isoformat(timespec="seconds")


def viewer_headers():
    result = auth.exchange(auth.issue_code("viewer"), "隔离只读", "test")
    return {"Host": "localhost", "Origin": "http://localhost",
            "Authorization": "Bearer " + result["token"]}


def make_post(sid, *, kept_at, keep_count=1, last_shown_at=None, status="kept"):
    """造一帖并直接落库到指定决定状态（kept_at 为 ISO 串，控制排序）。"""
    p = {"sid": sid, "uid": f"u-{sid}", "author": f"作者{sid}", "avatar": "",
         "text": f"文本{sid}", "created_at": kept_at, "fetched_at": kept_at,
         "weibo_url": "https://weibo.com", "via": "", "source": "weibo"}
    db.insert_post(p, "demo")
    if status != "pending":
        with db._write_lock, db.conn() as c:
            c.execute("UPDATE posts SET status=?, kept_at=?, keep_count=?, last_shown_at=? "
                      "WHERE sid=?", (status, kept_at, keep_count, last_shown_at, sid))


def kept_rows():
    with db.conn() as c:
        return {r["sid"]: dict(r) for r in c.execute(
            "SELECT sid,keep_count,shown_count,last_shown_at,last_confirm_at FROM posts "
            "WHERE status='kept'")}


class ReviewSelectionTest(ApiTestBase):
    async def test_grouping_order_and_quota(self):
        """10 张 = 4（≥2 次数降序）+ 6（=1 保留时间最早优先），组间不越名额。"""
        base = "2026-09-0{}T10:00:00+08:00"
        make_post("hi5", kept_at=base.format(1), keep_count=5)
        make_post("hi3", kept_at=base.format(2), keep_count=3)
        make_post("hi2old", kept_at=base.format(3), keep_count=2,
                  last_shown_at="2026-09-01T00:00:00+08:00")
        make_post("hi2new", kept_at=base.format(4), keep_count=2)   # 从未出场：同次数排最前
        for i in range(8):   # =1 组 8 张，名额只要 6，验证按 kept_at 最早优先且不越界
            make_post(f"one{i}", kept_at=f"2026-08-{i + 1:02d}T00:00:00+08:00")
        async with self.client() as c:
            r = await c.get("/api/review/next", params={"limit": 10})
        self.assertEqual(r.status_code, 200)
        posts = r.json()["posts"]
        self.assertEqual([p["sid"] for p in posts[:4]],
                         ["hi5", "hi3", "hi2new", "hi2old"])
        self.assertEqual([p["sid"] for p in posts[4:]],
                         [f"one{i}" for i in range(6)])
        self.assertTrue(all(p["keep_count"] >= 1 for p in posts))

    async def test_short_group_redistributes(self):
        """≥2 组只有 1 张时：名额让给 =1 组；=1 组见底后 ≥2 组越过名额补齐。"""
        make_post("only2", kept_at="2026-09-01T00:00:00+08:00", keep_count=2)
        for i in range(3):
            make_post(f"one{i}", kept_at=f"2026-08-{i + 1:02d}T00:00:00+08:00")
        async with self.client() as c:
            r = await c.get("/api/review/next", params={"limit": 10})
        posts = r.json()["posts"]
        self.assertEqual([p["sid"] for p in posts],
                         ["only2", "one0", "one1", "one2"])   # 池子全量，顺序仍 ≥2 在前

    async def test_empty_top_group_fills_bottom(self):
        """≥2 组为空（冷启动）时：全部名额给 =1 组，不缩水。"""
        for i in range(9):
            make_post(f"one{i}", kept_at=f"2026-08-{i + 1:02d}T00:00:00+08:00")
        async with self.client() as c:
            r = await c.get("/api/review/next", params={"limit": 5})
        posts = r.json()["posts"]
        self.assertEqual(len(posts), 5)   # 曾在此缩成 3：2/5 名额落空未回补
        self.assertEqual([p["sid"] for p in posts],
                         [f"one{i}" for i in range(5)])

    async def test_seven_day_exclusion(self):
        """上次出场在 7 天内的不重复入选；其余组照常补位。"""
        make_post("fresh2", kept_at="2026-09-01T00:00:00+08:00", keep_count=2,
                  last_shown_at=recent_iso(days=-1))    # 1 天前出场，7 天窗内必被排除
        make_post("stale2", kept_at="2026-09-02T00:00:00+08:00", keep_count=2,
                  last_shown_at=recent_iso(days=-30))   # 很久前，可再入
        make_post("one0", kept_at="2026-08-01T00:00:00+08:00")
        async with self.client() as c:
            r = await c.get("/api/review/next", params={"limit": 10})
        sids = [p["sid"] for p in r.json()["posts"]]
        self.assertIn("stale2", sids)
        self.assertNotIn("fresh2", sids)
        self.assertIn("one0", sids)

    async def test_shown_accounting_and_rerun_empty(self):
        """被选中即出场记账（shown_count/last_shown_at/事件）；池子耗尽后再次取组为空。"""
        for i in range(3):
            make_post(f"one{i}", kept_at=f"2026-08-{i + 1:02d}T00:00:00+08:00")
        async with self.client() as c:
            first = await c.get("/api/review/next", params={"limit": 10})
            self.assertEqual(len(first.json()["posts"]), 3)
            rows = kept_rows()
            self.assertTrue(all(v["shown_count"] == 1 and v["last_shown_at"] for
                                v in rows.values()))
            with db.conn() as conn:
                events = conn.execute(
                    "SELECT action, COUNT(*) n FROM review_events GROUP BY action").fetchall()
            self.assertEqual({r["action"]: r["n"] for r in events}.get("shown"), 3)
            second = await c.get("/api/review/next", params={"limit": 10})
            self.assertEqual(second.json()["posts"], [])   # 全部在 7 天排除窗内


class ReaffirmTest(ApiTestBase):
    async def test_reaffirm_count_and_undo_floor(self):
        """+1 计数与撤销下限照旧；同日第二次 +1 被每日一次拦下，撤销不影响其他帖。"""
        make_post("k1", kept_at="2026-09-01T00:00:00+08:00")
        make_post("k2", kept_at="2026-09-01T00:00:00+08:00")
        async with self.client() as c:
            r = await c.post("/api/posts/k1/reaffirm")
            self.assertEqual(r.json()["keep_count"], 2)
            r = await c.post("/api/posts/k1/reaffirm")   # 同日第二次：+1 被限流
            self.assertEqual(r.status_code, 409)
            r = await c.delete("/api/posts/k1/reaffirm")
            self.assertEqual(r.json()["keep_count"], 1)
            r = await c.delete("/api/posts/k1/reaffirm")   # 建账的 1 不可撤
            self.assertEqual(r.status_code, 409)
            r = await c.post("/api/posts/k2/reaffirm")   # 限流按帖隔离，不影响别的帖
            self.assertEqual(r.json()["keep_count"], 2)
            self.assertEqual(kept_rows()["k1"]["keep_count"], 1)
            with db.conn() as c:
                actions = [x[0] for x in c.execute(
                    "SELECT action FROM review_events WHERE sid='k1' ORDER BY id")]
            self.assertEqual(actions, ["reaffirm", "undo"])

    async def test_reaffirm_daily_limit(self):
        """每日一次：昨天赞过今天可再赞；今天赞过（含撤销后）同日不可再赞。"""
        make_post("d1", kept_at="2026-09-01T00:00:00+08:00")
        with db._write_lock, db.conn() as c:   # 上次点赞在昨天：今天可再赞
            c.execute("UPDATE posts SET last_confirm_at=? WHERE sid='d1'",
                      (recent_iso(days=-1),))
        async with self.client() as c:
            r = await c.post("/api/posts/d1/reaffirm")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["keep_count"], 2)
        make_post("d2", kept_at="2026-09-01T00:00:00+08:00")
        async with self.client() as c:
            self.assertEqual((await c.post("/api/posts/d2/reaffirm")).status_code, 200)
            self.assertEqual((await c.post("/api/posts/d2/reaffirm")).status_code, 409)
            # −1 不清当日账：撤销后同日仍不可再赞（防刷）
            self.assertEqual((await c.delete("/api/posts/d2/reaffirm")).json()["keep_count"], 1)
            self.assertEqual((await c.post("/api/posts/d2/reaffirm")).status_code, 409)
            self.assertEqual(kept_rows()["d2"]["keep_count"], 1)

    async def test_keep_stamps_confirm_today(self):
        """点赞建账：转入 kept（含批量保留）即盖当日账，当日 +1 随即被拦。"""
        make_post("s1", kept_at="2026-09-01T00:00:00+08:00", status="pending")
        make_post("s2", kept_at="2026-09-01T00:00:00+08:00", status="pending")
        async with self.client() as c:
            r = await c.post("/api/posts/s1/status", json={"status": "kept"})
            self.assertEqual(r.status_code, 200)
            r = await c.post("/api/posts/bulk-status",
                             json={"sids": ["s2"], "status": "kept"})
            self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(kept_rows()["s1"]["last_confirm_at"])
        self.assertIsNotNone(kept_rows()["s2"]["last_confirm_at"])
        async with self.client() as c:
            self.assertEqual((await c.post("/api/posts/s1/reaffirm")).status_code, 409)
            self.assertEqual((await c.post("/api/posts/s2/reaffirm")).status_code, 409)

    async def test_reaffirm_requires_kept(self):
        make_post("p1", kept_at="2026-09-01T00:00:00+08:00", status="pending")
        async with self.client() as c:
            r = await c.post("/api/posts/p1/reaffirm")
            self.assertEqual(r.status_code, 409)
            r = await c.post("/api/posts/nope/reaffirm")
            self.assertEqual(r.status_code, 404)

    async def test_viewer_cannot_reaffirm(self):
        make_post("k1", kept_at="2026-09-01T00:00:00+08:00")
        async with self.client() as c:
            self.assertEqual((await c.get("/api/review/next")).status_code, 200)   # 读允许
            r = await c.post("/api/posts/k1/reaffirm", headers=viewer_headers())
            self.assertEqual(r.status_code, 403)
            r = await c.delete("/api/posts/k1/reaffirm", headers=viewer_headers())
            self.assertEqual(r.status_code, 403)
        self.assertEqual(kept_rows()["k1"]["keep_count"], 1)   # 计数未被改动


class KeepCountSortTest(ApiTestBase):
    async def test_sort_and_cursor_pagination(self):
        make_post("c1", kept_at="2026-09-01T00:00:00+08:00", keep_count=1)
        make_post("c3a", kept_at="2026-09-02T00:00:00+08:00", keep_count=3)
        make_post("c3b", kept_at="2026-09-03T00:00:00+08:00", keep_count=3)
        make_post("c2", kept_at="2026-09-04T00:00:00+08:00", keep_count=2)
        make_post("pending", kept_at="2026-09-05T00:00:00+08:00", status="pending")
        async with self.client() as c:
            r = await c.get("/api/posts", params={"status": "kept", "sort": "keep_count",
                                                  "limit": 2})
            data = r.json()
            self.assertEqual([p["sid"] for p in data["posts"]], ["c3b", "c3a"])
            self.assertTrue(data["has_more"])
            r2 = await c.get("/api/posts", params={"status": "kept", "sort": "keep_count",
                                                   "limit": 2, "cursor": data["next_cursor"]})
            data2 = r2.json()
            self.assertEqual([p["sid"] for p in data2["posts"]], ["c2", "c1"])
            self.assertFalse(data2["has_more"])   # 池子翻完，游标归 None
            self.assertIsNone(data2["next_cursor"])

    async def test_keep_starts_at_one(self):
        """首次保留 = ×1 建账；状态恢复不改变次数。"""
        make_post("p1", kept_at="2026-09-01T00:00:00+08:00", status="pending")
        async with self.client() as c:
            await c.post("/api/posts/p1/status", json={"status": "kept"})
            self.assertEqual(kept_rows()["p1"]["keep_count"], 1)
            await c.post("/api/posts/p1/status", json={"status": "discarded"})
            await c.post("/api/posts/p1/status", json={"status": "kept"})
            self.assertEqual(kept_rows()["p1"]["keep_count"], 1)   # 状态变化不动次数


if __name__ == "__main__":
    unittest.main()
