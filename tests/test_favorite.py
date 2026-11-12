"""收藏（favorite）回归测试。

钉死的产品决策：favorite 与 pending/kept/deleted 正交；删除（含批量）不清除收藏；
「收藏」筛选页 = status=favorites（favorite=1 任意状态）。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import httpx

from app import config, db, demo
from tests.auth_support import admin_headers


def _post(sid, uid="u1", author="作者甲", status="pending", created="2026-09-15T10:00:00+08:00"):
    return {
        "sid": sid, "uid": uid, "author": author, "avatar": "", "text": f"正文{sid}",
        "created_at": created, "fetched_at": created, "weibo_url": f"https://weibo.com/u/{sid}",
        "via": "", "is_long": 0,
    }


class FavoriteTestBase(unittest.IsolatedAsyncioTestCase):
    """与 test_api 同款隔离：临时数据目录 + demo 种子 + ASGI 直打。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-fav-"))
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


class FavoriteToggleTest(FavoriteTestBase):
    async def test_toggle_persists_independent_of_status(self):
        self._mk("fav1")
        async with self.client() as c:
            r = await c.post("/api/posts/fav1/favorite", json={"favorite": True})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["favorite"], True)
            row = db.get_post("fav1")
            self.assertEqual(row["favorite"], 1)
            self.assertEqual(row["status"], "pending")   # 收藏不改决策
            r = await c.post("/api/posts/fav1/favorite", json={"favorite": False})
            self.assertEqual(r.json()["favorite"], False)
            self.assertEqual(db.get_post("fav1")["favorite"], 0)

    async def test_favorite_404_for_unknown_sid(self):
        async with self.client() as c:
            r = await c.post("/api/posts/nope/favorite", json={"favorite": True})
            self.assertEqual(r.status_code, 404)


class FavoriteSurvivesDecisionTest(FavoriteTestBase):
    async def test_discard_and_restore_keep_favorite(self):
        self._mk("fav2")
        db.set_favorite("fav2", True)
        async with self.client() as c:
            await c.post("/api/posts/fav2/status", json={"status": "discarded"})
            self.assertEqual(db.get_post("fav2")["favorite"], 1)
            await c.post("/api/posts/fav2/status", json={"status": "pending"})
            self.assertEqual(db.get_post("fav2")["favorite"], 1)

    async def test_bulk_discard_keeps_favorite(self):
        for sid in ("b1", "b2", "b3"):
            self._mk(sid)
        db.set_favorite("b1", True)
        db.set_favorite("b3", True)
        async with self.client() as c:
            r = await c.post("/api/posts/bulk-status",
                             json={"sids": ["b1", "b2", "b3"], "status": "discarded"})
            self.assertEqual(r.json()["updated"], 3)
        for sid in ("b1", "b2", "b3"):
            row = db.get_post(sid)
            self.assertEqual(row["status"], "discarded")
            self.assertEqual(row["favorite"], 1 if sid in ("b1", "b3") else 0)


class FavoritesFilterTest(FavoriteTestBase):
    async def test_favorites_filter_across_statuses(self):
        self._mk("p1", "pending")
        self._mk("p2", "pending")
        self._mk("k1", "kept")
        self._mk("d1", "discarded")
        for sid in ("p1", "k1", "d1"):
            db.set_favorite(sid, True)
        async with self.client() as c:
            r = await c.get("/api/posts?status=favorites")
            body = r.json()
            self.assertEqual(body["total"], 3)
            self.assertEqual({p["sid"] for p in body["posts"]}, {"p1", "k1", "d1"})
            self.assertTrue(all(p["favorite"] for p in body["posts"]))
            # 列表 payload 带 favorite/source 键（前端渲染依赖）
            sample = body["posts"][0]
            self.assertIn("favorite", sample)
            self.assertEqual(sample["source"], "weibo")
            # 取消收藏后离开筛选结果
            await c.post("/api/posts/p1/favorite", json={"favorite": False})
            r = await c.get("/api/posts?status=favorites")
            self.assertEqual({p["sid"] for p in r.json()["posts"]}, {"k1", "d1"})

    async def test_status_counts_include_favorites(self):
        self._mk("p1")
        db.set_favorite("p1", True)
        async with self.client() as c:
            r = await c.get("/api/status")
            body = r.json()
            self.assertEqual(body["counts"]["favorites"], 1)
            # 诊断面：版本/平台/schema 随状态暴露（用户反馈时够定位）
            self.assertEqual(body["version"], self.server.APP_VERSION)
            self.assertIn(body["platform"], ("darwin", "win32", "linux"))
            self.assertEqual(body["schema_version"], str(db._SCHEMA_VERSION))

    async def test_authors_count_follows_favorites_tab(self):
        # 钉死：收藏页作者条按 favorite=1 计数（曾回归为 status='favorites' 永不匹配 → 全 0）
        self._mk("fa1", "pending")
        self._mk("fa2", "pending")
        self._mk("fa3", "kept")
        db.set_favorite("fa1", True)
        db.set_favorite("fa3", True)
        async with self.client() as c:
            r = await c.get("/api/authors?status=favorites")
            mine = [a for a in r.json()["authors"] if a["uid"] == "u1"]
            self.assertEqual(len(mine), 1)
            self.assertEqual(mine[0]["n"], 2)   # fa1 + fa3，fa2 未收藏不计


if __name__ == "__main__":
    unittest.main()
