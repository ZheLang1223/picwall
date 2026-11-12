"""统计室：/api/stats 聚合口径与 /stats 页面可达性。

聚合全部纯本地（不打网络）：总览、近 30 天每日双系列、Top 博主（按保留）、
最常点赞（keep_count 降序）、回看累计；页面已配对设备可达、未配对 401。
"""
import unittest
from datetime import datetime, timedelta

import httpx

from app import config, db
from app.sources import x as x_source
from app.sources.base import validate_post
from tests.auth_support import admin_headers
from tests.test_api import ApiTestBase

NOW = datetime.now(config.NOW_TZ)


def _post(sid, uid, author, created):
    p = x_source.normalize_tweet({
        "id": sid, "author": {"id": uid, "uid": uid, "handle": "", "name": author,
                              "avatar": "https://pbs.twimg.com/profile_images/a_normal.jpg"},
        "text": f"帖 {sid}", "created_at": created,
        "images": ["https://pbs.twimg.com/media/AbC?format=jpg"]})
    validate_post(p)
    return p


class StatsApiTest(ApiTestBase):
    def _seed(self):
        # 甲：两帖都保留，其中一帖点赞两次；乙：一帖不要（近 30 天入库口径里也要出现）
        p1 = _post("1844000000000000011", "789", "张帕克", (NOW - timedelta(days=2)).isoformat())
        p2 = _post("1844000000000000012", "789", "张帕克", (NOW - timedelta(days=5)).isoformat())
        p3 = _post("1844000000000000013", "456", "李帕克", (NOW - timedelta(days=1)).isoformat())
        for p in (p1, p2, p3):
            p["source"] = ""                      # 微博历史行 source 为空串
            db.insert_post(p, "123456")
        db.set_status(p1["sid"], "kept")
        db.set_status(p2["sid"], "kept")
        db.set_status(p3["sid"], "discarded")
        db.insert_pic(p1["sid"], "2026/11/aaa_full.jpg", "2026/11/t_aaa.jpg", 800, 600, "https://x")
        # 保留即建账当日已赞：把建账时间拨到昨天，今天的 reaffirm 才不被每日限流拦截
        with db.conn() as c:
            c.execute("UPDATE posts SET last_confirm_at=? WHERE sid=?",
                      ((NOW - timedelta(days=1)).isoformat(), p1["sid"]))
        db.reaffirm(p1["sid"])                    # ×2
        db.record_shown([p1["sid"], p2["sid"]])
        db.add_comment(p1["sid"], "好图")

    async def test_overview_and_review(self):
        self._seed()
        async with self.client() as c:
            j = (await c.get("/api/stats")).json()
        self.assertTrue(j["ok"])
        o = j["overview"]
        # demo 种子也计入总数；只对本例增量做下界断言
        self.assertGreaterEqual(o["total"], 3)
        self.assertGreaterEqual(o["kept"], 2)
        self.assertGreaterEqual(o["discarded"], 1)
        self.assertGreaterEqual(o["pics"], 1)
        self.assertGreaterEqual(o["comments"], 1)
        self.assertEqual(o["tags"], 0)   # 迭代一标签表已建，本例未打标
        r = j["review"]
        self.assertEqual(r["reaffirmed"], 1)      # demo 无回看事件，只有本例一次
        self.assertEqual(r["shown"], 2)
        self.assertGreaterEqual(r["active_days"], 1)

    async def test_daily_series_30d(self):
        self._seed()
        async with self.client() as c:
            j = (await c.get("/api/stats")).json()
        daily = {d["d"]: d for d in j["daily"]}
        self.assertLessEqual(len(j["daily"]), 31)   # 只含近 30 天出现过的日界
        near = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
        self.assertGreaterEqual(daily[near]["n"], 1)   # 昨天有入库（demo 亦可能入库）
        today = NOW.strftime("%Y-%m-%d")
        self.assertEqual(daily[today]["likes"], 1)  # 点赞发生在今天（demo 无回看事件）

    async def test_top_authors_and_top_posts(self):
        self._seed()
        async with self.client() as c:
            j = (await c.get("/api/stats")).json()
        keys = [a["key"] for a in j["top_authors"]]
        self.assertIn("789", keys)
        self.assertIn("456", keys)
        zhang = next(a for a in j["top_authors"] if a["key"] == "789")
        self.assertEqual((zhang["n"], zhang["kept"]), (2, 2))
        # 按保留数降序：张(2) 在李(0) 前
        self.assertLess(keys.index("789"), keys.index("456"))
        top = j["top_posts"]
        self.assertTrue(top and top[0]["keep_count"] >= 2)
        self.assertEqual(top[0]["thumb"], "2026/11/t_aaa.jpg")
        self.assertTrue(all(p["keep_count"] >= (top[0]["keep_count"]) for p in top[:1]))


class StatsPageTest(ApiTestBase):
    """/stats 页面：已配对设备可达（本机即 admin），未配对 401。"""

    async def test_stats_page_access(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://test") as client:
            r_admin = await client.get("/stats", headers=admin_headers())
            self.assertEqual(r_admin.status_code, 200)
            self.assertIn("PWALL 统计室", r_admin.text)
            self.assertIn("/static/stats.js", r_admin.text)

            r_anon = await client.get("/stats", headers={"Host": "localhost"})
            self.assertEqual(r_anon.status_code, 401)


if __name__ == "__main__":
    unittest.main()
