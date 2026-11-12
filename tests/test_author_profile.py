"""博主视图（作者全量档案）测试。

钉死的产品决策：GET /api/authors/{key}/profile 是纯本地聚合（无网络请求，
签名的网络获取留给 /api/authors/{uid}/bio）；统计跨状态、跨分组
（n/kept/favorites/comments/时间跨度），身份（author/avatar/handle）取最新一帖；
key = author_key 口径（微博裸 uid、X 为 x:<id>），未知作者 404。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import httpx

from app import config, db, demo
from tests.auth_support import admin_headers


def _post(sid, uid="u1", author="作者甲", created="2026-09-15T10:00:00+08:00",
          source="weibo", handle="", avatar=""):
    return {
        "sid": sid, "uid": uid, "author": author, "avatar": avatar,
        "text": f"正文{sid}", "created_at": created, "fetched_at": created,
        "weibo_url": f"https://weibo.com/u/{sid}", "via": "", "is_long": 0,
        "source": source, "handle": handle,
    }


class AuthorProfileTest(unittest.IsolatedAsyncioTestCase):
    """与 test_api 同款隔离：临时数据目录 + demo 种子 + ASGI 直打。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-avprofile-"))
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

    async def test_profile_aggregates_across_statuses_and_groups(self):
        db.insert_post(_post("p1", created="2026-09-15T10:00:00+08:00"), "demo")
        db.insert_post(_post("p2", created="2026-09-16T10:00:00+08:00"), "demo")
        db.insert_post(_post("p3", created="2026-09-17T10:00:00+08:00"), "demo")
        db.insert_post(_post("p4", created="2026-09-10T10:00:00+08:00", source=""), "demo")
        db.set_status("p2", "kept")
        db.set_status("p3", "kept")
        db.set_favorite("p3", True)
        db.set_status("p4", "discarded")
        db.add_comment("p2", "第一条批注", created_at="2026-09-16T11:00:00+08:00")
        db.add_comment("p2", "第二条批注", created_at="2026-09-16T12:00:00+08:00")
        async with self.client() as c:
            r = (await c.get("/api/authors/u1/profile")).json()
        self.assertEqual(r["n"], 4)
        self.assertEqual(r["kept"], 2)
        self.assertEqual(r["favorites"], 1)
        self.assertEqual(r["comments"], 2)
        self.assertEqual(r["first_at"], "2026-09-10T10:00:00+08:00")
        self.assertEqual(r["last_at"], "2026-09-17T10:00:00+08:00")
        self.assertEqual(r["source"], "weibo")

    async def test_identity_follows_latest_post(self):
        db.insert_post(_post("o1", author="旧名", avatar="avatars/old.jpg",
                             created="2026-09-01T10:00:00+08:00"), "demo")
        db.insert_post(_post("n1", author="新名", avatar="avatars/new.jpg",
                             created="2026-09-02T10:00:00+08:00"), "demo")
        async with self.client() as c:
            r = (await c.get("/api/authors/u1/profile")).json()
        self.assertEqual(r["author"], "新名")
        self.assertEqual(r["avatar"], "avatars/new.jpg")

    async def test_x_key_parses_source_and_handle(self):
        db.insert_post(_post("x1", uid="luoluokun_", source="x",
                             handle="luoluokun_"), "demo")
        async with self.client() as c:
            r = (await c.get("/api/authors/x:luoluokun_/profile")).json()
        self.assertEqual(r["source"], "x")
        self.assertEqual(r["uid"], "luoluokun_")
        self.assertEqual(r["handle"], "luoluokun_")
        self.assertEqual(r["n"], 1)

    async def test_unknown_author_404(self):
        async with self.client() as c:
            self.assertEqual((await c.get("/api/authors/nobody/profile")).status_code, 404)
            self.assertEqual((await c.get("/api/authors/x:none/profile")).status_code, 404)
            self.assertEqual((await c.get("/api/authors/x:/profile")).status_code, 404)
