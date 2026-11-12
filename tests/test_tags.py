"""标签（post_tags）回归测试。

钉死的产品决策：标签与决定状态/收藏正交（不改 status/decision_version）；
标签筛选 = 任一命中即入选（OR，/api/posts 与 /api/timeline/outline 同口径）；
单帖 ≤10 个、单名 ≤24 字；批量打标超上限的帖跳过而不是整批失败；purge 级联清理。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import httpx

from app import config, db, demo, purge
from tests.auth_support import admin_headers


def _post(sid, uid="u1", author="作者甲", status="pending", created="2026-09-15T10:00:00+08:00"):
    return {
        "sid": sid, "uid": uid, "author": author, "avatar": "", "text": f"正文{sid}",
        "created_at": created, "fetched_at": created, "weibo_url": f"https://weibo.com/u/{sid}",
        "via": "", "is_long": 0,
    }


class TagsTestBase(unittest.IsolatedAsyncioTestCase):
    """与 test_api 同款隔离：临时数据目录 + demo 种子 + ASGI 直打。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-tags-"))
        self._old = (config.DATA_DIR, config.PICS_DIR, config.EXPORT_DIR,
                     config.DB_PATH, config.COOKIE_FILE)
        config.DATA_DIR = self.tmp
        config.PICS_DIR = self.tmp / "pics"
        config.EXPORT_DIR = self.tmp / "export"
        config.DB_PATH = self.tmp / "picwall.db"
        config.COOKIE_FILE = self.tmp / "cookie.txt"
        db.init_db()
        demo.seed()
        from app import server
        self.server = server

    def tearDown(self):
        (config.DATA_DIR, config.PICS_DIR, config.EXPORT_DIR,
         config.DB_PATH, config.COOKIE_FILE) = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                 base_url="http://test", headers=admin_headers())

    def _mk(self, sid, status="pending"):
        db.insert_post(_post(sid), "demo")
        if status != "pending":
            db.set_status(sid, status)


class TagToggleTest(TagsTestBase):
    async def test_add_remove_persist_orthogonal_to_decision(self):
        self._mk("t1", "kept")
        before = db.get_post("t1")
        async with self.client() as c:
            r = await c.post("/api/posts/t1/tags", json={"add": ["风景", " 日出 "]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(set(r.json()["tags"]), {"风景", "日出"})   # trim 落库
            after = db.get_post("t1")
            self.assertEqual(set(after["tags"]), {"风景", "日出"})
            # 正交性：状态与决定版本都不动（标签不是决定，不参与撤销竞态）
            self.assertEqual(after["status"], before["status"])
            self.assertEqual(after["decision_version"], before["decision_version"])
            self.assertEqual(after["keep_count"], before["keep_count"])
            # 去重：同一标签幂等
            r = await c.post("/api/posts/t1/tags", json={"add": ["日出"]})
            self.assertEqual(set(r.json()["tags"]), {"风景", "日出"})
            # 移除
            r = await c.post("/api/posts/t1/tags", json={"remove": ["风景"]})
            self.assertEqual(r.json()["tags"], ["日出"])
            self.assertEqual(db.get_post("t1")["tags"], ["日出"])

    async def test_validation_rejects_bad_names(self):
        self._mk("t2")
        async with self.client() as c:
            r = await c.post("/api/posts/t2/tags", json={"add": ["   "]})
            self.assertEqual(r.status_code, 400)
            r = await c.post("/api/posts/t2/tags", json={"add": ["x" * 25]})
            self.assertEqual(r.status_code, 400)
            self.assertEqual(db.get_post("t2")["tags"], [])
        # 直调层同样拒绝
        with self.assertRaises(ValueError):
            db.set_post_tags("t2", add=[""])

    async def test_per_post_limit(self):
        self._mk("t3")
        tags = [f"标签{i}" for i in range(10)]
        r = db.set_post_tags("t3", add=tags)
        self.assertEqual(len(r), 10)
        with self.assertRaises(ValueError):
            db.set_post_tags("t3", add=["再来一个"])
        # 超限拒绝是整体回滚，不产生半截修改
        self.assertEqual(db.get_post("t3")["tags"], sorted(tags))   # 同秒打标按 (created_at,tag) 定序

    async def test_404_for_unknown_sid(self):
        async with self.client() as c:
            r = await c.post("/api/posts/nope/tags", json={"add": ["x"]})
            self.assertEqual(r.status_code, 404)


class BulkTagsTest(TagsTestBase):
    async def test_bulk_add_and_skip_overflow(self):
        for sid in ("b1", "b2", "b3"):
            self._mk(sid)
        # b2 先塞满 10 个：批量再加会超上限，应被跳过而不是整批失败
        db.set_post_tags("b2", add=[f"满{i}" for i in range(10)])
        async with self.client() as c:
            r = await c.post("/api/posts/bulk-tags",
                             json={"sids": ["b1", "b2", "b3", "b1"], "add": ["装机"]})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["updated"], 2)
            self.assertEqual(r.json()["skipped"], 1)
        self.assertEqual(db.get_post("b1")["tags"], ["装机"])
        self.assertEqual(db.get_post("b2")["tags"], sorted(f"满{i}" for i in range(10)))
        self.assertEqual(db.get_post("b3")["tags"], ["装机"])

    async def test_bulk_empty_payload_rejected(self):
        self._mk("b4")
        async with self.client() as c:
            r = await c.post("/api/posts/bulk-tags", json={"sids": ["b4"], "add": []})
            self.assertEqual(r.status_code, 400)
            r = await c.post("/api/posts/bulk-tags",
                             json={"sids": [f"s{i}" for i in range(501)], "add": ["x"]})
            self.assertEqual(r.status_code, 400)


class TagFilterTest(TagsTestBase):
    async def test_filter_any_match_and_payload_rides_along(self):
        self._mk("f1")
        self._mk("f2")
        self._mk("f3")
        db.set_post_tags("f1", add=["风景"])
        db.set_post_tags("f2", add=["装机"])
        async with self.client() as c:
            r = await c.get("/api/posts", params={"tags": "风景,装机"})
            body = r.json()
            self.assertEqual(body["total"], 2)
            self.assertEqual({p["sid"] for p in body["posts"]}, {"f1", "f2"})
            # 列表 payload 带 tags（前端 matchesView 依赖）
            sample = next(p for p in body["posts"] if p["sid"] == "f1")
            self.assertEqual(sample["tags"], ["风景"])
            # 单标签
            r = await c.get("/api/posts", params={"tags": "装机"})
            self.assertEqual({p["sid"] for p in r.json()["posts"]}, {"f2"})
            # 移除后离开筛选结果
            db.set_post_tags("f2", remove=["装机"])
            r = await c.get("/api/posts", params={"tags": "装机"})
            self.assertEqual(r.json()["posts"], [])
            # 编年聚合共用同一筛选口径
            r = await c.get("/api/timeline/outline", params={"tags": "风景"})
            days = r.json()["days"]
            self.assertEqual(sum(d["total"] for d in days), 1)

    async def test_all_means_no_filter(self):
        self._mk("f4")
        self._mk("f5")
        db.set_post_tags("f4", add=["风景"])
        async with self.client() as c:
            # tags=all 等于不筛：按作者隔离出本用例的两帖（demo 种子不掺和）
            r = await c.get("/api/posts", params={"tags": "all", "author": "u1"})
            self.assertEqual(r.json()["total"], 2)

    async def test_tags_listing_counts(self):
        self._mk("g1")
        self._mk("g2")
        db.set_post_tags("g1", add=["风景", "猫"])
        db.set_post_tags("g2", add=["风景"])
        async with self.client() as c:
            r = await c.get("/api/tags")
            tags = {t["tag"]: t["n"] for t in r.json()["tags"]}
            self.assertEqual(tags["风景"], 2)
            self.assertEqual(tags["猫"], 1)

    async def test_detail_payload_includes_tags(self):
        self._mk("g3")
        db.set_post_tags("g3", add=["风景"])
        async with self.client() as c:
            r = await c.get("/api/posts/g3")
            self.assertEqual(r.json()["post"]["tags"], ["风景"])


class TagPurgeTest(TagsTestBase):
    async def test_purge_cascades_tag_rows(self):
        self._mk("p1", "discarded")
        db.set_post_tags("p1", add=["风景"])
        db.set_meta("purge_days_probe", "")
        # 把 discarded_at 拨到 PURGE_DAYS 之前，硬删并验证 post_tags 一并清掉
        with db.conn() as c:
            c.execute("UPDATE posts SET discarded_at=? WHERE sid=?",
                      ("2020-01-01T00:00:00+08:00", "p1"))
        result = purge.purge_expired(days=config.PURGE_DAYS)
        self.assertEqual(result["purged_posts"], 1)
        with db.conn() as c:
            left = c.execute("SELECT COUNT(*) FROM post_tags WHERE sid='p1'").fetchone()[0]
        self.assertEqual(left, 0)
        # 标签清单不再统计被硬删的帖
        self.assertEqual([t for t in db.list_tags() if t["tag"] == "风景"], [])


if __name__ == "__main__":
    unittest.main()
