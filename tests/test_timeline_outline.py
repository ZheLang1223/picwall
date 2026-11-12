"""编年时间线 outline 接口：按日聚合与筛选口径一致性。

跑法与 test_api 相同：项目根目录 `python -m unittest discover -s tests -t .`。
造数走 db.insert_post 直插（固定东八区时间戳，不依赖 demo.seed 的相对时间），
并用 group 参数与演示数据隔离。
"""
import httpx

from app import db
from tests.test_api import ApiTestBase

DAY1, DAY2, DAY3 = "2026-10-01", "2026-10-02", "2026-10-03"
GROUP = "gtest"


def _mkpost(sid, day, status="pending", favorite=0, uid="u1", via=""):
    db.insert_post({"sid": sid, "uid": uid, "author": f"作者{sid}", "avatar": "",
                    "text": f"正文{sid}", "created_at": f"{day}T10:30:00+08:00",
                    "fetched_at": f"{day}T11:00:00+08:00",
                    "weibo_url": "https://weibo.com", "via": via, "pics": []}, GROUP)
    if status != "pending":
        assert db.mark_snapshot(sid, status) is not None
    if favorite:
        with db.conn() as c:
            c.execute("UPDATE posts SET favorite=1 WHERE sid=?", (sid,))


class TimelineOutlineTest(ApiTestBase):
    async def test_groups_by_local_day_with_counts(self):
        _mkpost("a1", DAY1)
        _mkpost("a2", DAY1, status="kept")
        _mkpost("a3", DAY2, status="kept", favorite=1)
        _mkpost("a4", DAY2, status="discarded")
        _mkpost("a5", DAY3)
        db.add_comment("a2", "记一笔")
        db.add_comment("a3", "再记一笔")
        r = await self.client().get(f"/api/timeline/outline?group={GROUP}")
        self.assertEqual(r.status_code, 200)
        days = r.json()["days"]
        self.assertEqual([d["d"] for d in days], [DAY3, DAY2, DAY1])   # 降序，最新在前
        by = {d["d"]: d for d in days}
        self.assertEqual(by[DAY1], {"d": DAY1, "total": 2, "kept": 1,
                                    "discarded": 0, "fav": 0, "cmts": 1})
        self.assertEqual(by[DAY2]["total"], 2)
        self.assertEqual(by[DAY2]["kept"], 1)
        self.assertEqual(by[DAY2]["discarded"], 1)
        self.assertEqual(by[DAY2]["fav"], 1)
        self.assertEqual(by[DAY2]["cmts"], 1)
        self.assertEqual(by[DAY3]["cmts"], 0)

    async def test_status_filter_matches_posts_scope(self):
        _mkpost("b1", DAY1, status="kept")
        _mkpost("b2", DAY1, status="discarded")
        _mkpost("b3", DAY2, status="kept", favorite=1)
        client = self.client()
        for status, total in (("kept", 2), ("discarded", 1), ("favorites", 1)):
            outline = (await client.get(f"/api/timeline/outline?group={GROUP}&status={status}")).json()
            posts = (await client.get(f"/api/posts?group={GROUP}&status={status}&limit=50")).json()
            self.assertEqual(sum(d["total"] for d in outline["days"]), total)
            self.assertEqual(sum(d["total"] for d in outline["days"]), posts["total"],
                             "outline 与 /api/posts 必须同一筛选口径")

    async def test_search_and_author_scoping(self):
        _mkpost("c1", DAY1, uid="u1")
        _mkpost("c2", DAY2, uid="u2")
        _mkpost("c3", DAY2, uid="u1")
        db.add_comment("c3", "独特的批注词")
        client = self.client()
        by_author = (await client.get(f"/api/timeline/outline?group={GROUP}&author=u1")).json()
        self.assertEqual([d["d"] for d in by_author["days"]], [DAY2, DAY1])
        self.assertEqual(sum(d["total"] for d in by_author["days"]), 2)
        by_q = (await client.get(f"/api/timeline/outline?group={GROUP}&q=独特的批注词")).json()
        self.assertEqual([d["d"] for d in by_q["days"]], [DAY2])
        self.assertEqual(by_q["days"][0]["total"], 1)

    async def test_requires_pairing(self):
        anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                 base_url="http://test")
        try:
            r = await anon.get("/api/timeline/outline")
            self.assertIn(r.status_code, (401, 403))
        finally:
            await anon.aclose()
