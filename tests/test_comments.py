"""评论（单人批注时间线）回归测试。

钉死的产品决策：
- 只增不覆盖：一帖多条、旧→新（id 即顺序），每条带首发时间；
- 编辑只改正文，created_at 不漂移（updated_at 仅内部记录）；
- 限长 500 字、空文本拒绝；
- 评论豁免 7 天硬删（图片可重抓、用户敲下的字不可再生），删光评论恢复清理资格；
- 帖子硬删时评论随之清理；列表/回看 payload 带评论数据与计数。
"""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from app import config, db, demo, purge
from tests.auth_support import admin_headers


def _post(sid, status="pending", created="2026-09-15T10:00:00+08:00"):
    return {
        "sid": sid, "uid": "u1", "author": "作者甲", "avatar": "", "text": f"正文{sid}",
        "created_at": created, "fetched_at": created, "weibo_url": f"https://weibo.com/u/{sid}",
        "via": "", "is_long": 0,
    }


class CommentTestBase(unittest.IsolatedAsyncioTestCase):
    """与 test_favorite 同款隔离：临时数据目录 + demo 种子 + ASGI 直打。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-cmt-"))
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
        db.insert_post(_post(sid, status=status), "demo")
        if status != "pending":
            db.set_status(sid, status)


class CommentCrudTest(CommentTestBase):
    async def test_append_keeps_order_and_first_time(self):
        self._mk("c1")
        async with self.client() as c:
            r1 = await c.post("/api/posts/c1/comments", json={"body": "第一条"})
            r2 = await c.post("/api/posts/c1/comments", json={"body": "第二条"})
            self.assertEqual(r1.status_code, 200)
            cid1, cid2 = r1.json()["comment"]["id"], r2.json()["comment"]["id"]
            self.assertLess(cid1, cid2)
            # 单帖详情内嵌评论，旧→新
            detail = (await c.get("/api/posts/c1")).json()["post"]
            self.assertEqual([x["body"] for x in detail["comments"]], ["第一条", "第二条"])
            # 编辑不改首发时间
            put = await c.put(f"/api/posts/c1/comments/{cid1}", json={"body": "第一条（改）"})
            self.assertEqual(put.status_code, 200)
            edited = put.json()["comment"]
            self.assertEqual(edited["body"], "第一条（改）")
            self.assertEqual(edited["created_at"], r1.json()["comment"]["created_at"])
            self.assertTrue(edited["updated_at"])
            # 删除单条不影响其余
            self.assertEqual((await c.delete(f"/api/posts/c1/comments/{cid1}")).status_code, 200)
            detail = (await c.get("/api/posts/c1")).json()["post"]
            self.assertEqual([x["body"] for x in detail["comments"]], ["第二条"])

    async def test_validation_and_cross_post(self):
        self._mk("c2")
        self._mk("c3")
        async with self.client() as c:
            self.assertEqual((await c.post("/api/posts/c2/comments", json={"body": "   "})
                              ).status_code, 400)
            self.assertEqual((await c.post("/api/posts/c2/comments", json={"body": "字" * 501})
                              ).status_code, 400)
            self.assertEqual((await c.post("/api/posts/nope/comments", json={"body": "x"})
                              ).status_code, 404)
            made = (await c.post("/api/posts/c2/comments", json={"body": "A 帖的评论"})).json()
            # 帖子不匹配的评论 id 视为不存在
            self.assertEqual((await c.put(f"/api/posts/c3/comments/{made['comment']['id']}",
                                          json={"body": "跨帖改写"})).status_code, 404)
            self.assertEqual((await c.delete(f"/api/posts/c3/comments/{made['comment']['id']}")
                              ).status_code, 404)

    async def test_list_payload_carries_comment_count(self):
        self._mk("c4")
        db.add_comment("c4", "一条")
        db.add_comment("c4", "两条")
        async with self.client() as c:
            body = (await c.get("/api/posts?status=pending")).json()
            mine = [p for p in body["posts"] if p["sid"] == "c4"]
            self.assertEqual(mine[0]["comment_count"], 2)


class CommentPurgeExemptionTest(CommentTestBase):
    def _mark_old_discarded(self, sid):
        self._mk(sid, status="discarded")
        old = (datetime.now() - timedelta(days=30)).isoformat()
        with db.conn() as c:
            c.execute("UPDATE posts SET discarded_at=? WHERE sid=?", (old, sid))

    async def test_commented_post_survives_purge_then_purges_after_removal(self):
        self._mark_old_discarded("d1")   # 无评论：照常清理
        self._mark_old_discarded("d2")   # 有评论：豁免
        db.add_comment("d2", "回头查这个摄影师")
        result = purge.purge_expired(days=7)
        self.assertEqual(result["purged_posts"], 1)
        self.assertFalse(db.has_post("d1"))
        self.assertTrue(db.has_post("d2"))
        # 删光评论 = 恢复清理资格
        for cm in db.list_comments("d2"):
            db.delete_comment("d2", cm["id"])
        result = purge.purge_expired(days=7)
        self.assertEqual(result["purged_posts"], 1)
        self.assertFalse(db.has_post("d2"))

    async def test_purge_recheck_refuses_commented_post_at_lock_time(self):
        """候选查询豁免之外，_purge_post 锁内重验也必须挡住有评论的帖（防预览-删除窗口竞态）。"""
        self._mark_old_discarded("d3")
        db.add_comment("d3", "锁内也要保护")
        self.assertIsNone(purge._purge_post("d3"))
        self.assertTrue(db.has_post("d3"))
        self.assertEqual([c["body"] for c in db.list_comments("d3")], ["锁内也要保护"])


class CommentPermissionTest(CommentTestBase):
    """editor 日常权限应覆盖评论增改删；viewer 只读被中间件挡下。"""

    def role_headers(self, role):
        from app import auth
        result = auth.exchange(auth.issue_code(role), f"{role}-机", "test")
        return {"Host": "localhost", "Origin": "http://localhost",
                "Authorization": "Bearer " + result["token"]}

    async def test_editor_can_write_comments(self):
        self._mk("e1")
        h = self.role_headers("editor")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://test", headers=h) as c:
            r = await c.post("/api/posts/e1/comments", json={"body": "editor 写的评论"})
            self.assertEqual(r.status_code, 200)
            cid = r.json()["comment"]["id"]
            self.assertEqual((await c.put(f"/api/posts/e1/comments/{cid}",
                                          json={"body": "editor 改的评论"})).status_code, 200)
            self.assertEqual((await c.delete(f"/api/posts/e1/comments/{cid}")).status_code, 200)
            # editor 已有的决定权限不受影响
            self.assertEqual((await c.get("/api/posts")).status_code, 200)

    async def test_viewer_cannot_write_comments(self):
        self._mk("e2")
        h = self.role_headers("viewer")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://test", headers=h) as c:
            self.assertEqual((await c.post("/api/posts/e2/comments", json={"body": "x"})
                              ).status_code, 403)
            made = db.add_comment("e2", "占位")
            self.assertEqual((await c.put(f"/api/posts/e2/comments/{made['id']}",
                                          json={"body": "x"})).status_code, 403)
            self.assertEqual((await c.delete(f"/api/posts/e2/comments/{made['id']}")
                              ).status_code, 403)
            # 读不受限：列表带计数，详情带评论
            self.assertEqual((await c.get("/api/posts")).status_code, 200)
            self.assertEqual((await c.get("/api/posts/e2")).status_code, 200)


class CommentSearchAndFilterTest(CommentTestBase):
    """钉死的检索面：正文搜索覆盖评论文本；「有评论」筛选；payload 带预览与可选内嵌。"""

    async def test_search_covers_comment_bodies(self):
        self._mk("s1")
        db.add_comment("s1", "青苔石阶尽头的小院")
        db.add_comment("s1", "另一条不相关的批注")
        async with self.client() as c:
            r = await c.get("/api/posts", params={"q": "青苔石阶"})
            posts = r.json()["posts"]
            self.assertEqual([p["sid"] for p in posts], ["s1"])
            self.assertEqual(posts[0]["comment_match"], "青苔石阶尽头的小院")   # 指认命中的那条
            r = await c.get("/api/posts", params={"q": " DefinitelyNotThere "})
            self.assertEqual(r.json()["total"], 0)

    async def test_has_cmts_filter(self):
        self._mk("s2")
        self._mk("s3")
        db.add_comment("s3", "有批注的帖")
        async with self.client() as c:
            r = await c.get("/api/posts", params={"has_cmts": "1"})
            sids = {p["sid"] for p in r.json()["posts"]}
            self.assertIn("s3", sids)
            self.assertNotIn("s2", sids)

    async def test_payload_carries_preview_and_optional_embed(self):
        self._mk("s4")
        db.add_comment("s4", "第一条")
        db.add_comment("s4", "最新一条")
        async with self.client() as c:
            body = (await c.get("/api/posts?status=pending")).json()["posts"]
            mine = next(p for p in body if p["sid"] == "s4")
            self.assertEqual(mine["comment_preview"], "最新一条")
            self.assertNotIn("comments", mine)   # 默认不内嵌（列表瘦身）
            r = (await c.get("/api/posts", params={"status": "pending", "cmts": "1"})).json()
            mine = next(p for p in r["posts"] if p["sid"] == "s4")
            self.assertEqual([x["body"] for x in mine["comments"]], ["第一条", "最新一条"])


class NotesFeedTest(CommentTestBase):
    """「最近批注」流：全库评论倒排 + 游标分页，带帖作者/缩略图。"""

    async def test_newest_first_with_cursor(self):
        self._mk("n1")
        self._mk("n2")
        db.add_comment("n1", "旧的一条")
        db.add_comment("n2", "新的一条")
        async with self.client() as c:
            r = (await c.get("/api/comments", params={"limit": 2})).json()
            self.assertEqual(r["total"], r["total"])   # demo 评论也在库里，只验相对序
            bodies = [x["body"] for x in r["items"]]
            self.assertIn("新的一条", bodies[:1])      # 全库最新的一条排最前
            self.assertTrue(all("author" in x and "thumb" in x for x in r["items"]))
            # 游标翻页不重复、不遗漏（本页取余下的）
            r2 = (await c.get("/api/comments", params={"limit": 100, "cursor": r["next_cursor"]})).json()
            ids1 = {x["id"] for x in r["items"]}
            ids2 = {x["id"] for x in r2["items"]}
            self.assertFalse(ids1 & ids2)


class NotesExportTest(CommentTestBase):
    """导出带批注：归档文件夹 评论.txt 随批注增改删全量重建；zip 文本格式。"""

    async def test_archive_notes_rebuild_and_removal(self):
        self._mk("x1", "kept")
        async with self.client() as c:
            day = db.get_post("x1")["kept_at"][:10]
            target = config.EXPORT_DIR / day / "评论.txt"
            self.assertFalse(target.exists())
            await c.post("/api/posts/x1/comments", json={"body": "归档里的批注"})
            self.assertTrue(target.exists())
            text = target.read_text(encoding="utf-8")
            self.assertIn("归档里的批注", text)
            self.assertIn(f"{day}_作者甲_x1", text)
            # 编辑反映进归档
            cid = db.list_comments("x1")[0]["id"]
            await c.put(f"/api/posts/x1/comments/{cid}", json={"body": "改过的批注"})
            self.assertIn("改过的批注", target.read_text(encoding="utf-8"))
            # 删光 = 文件移除
            await c.delete(f"/api/posts/x1/comments/{cid}")
            self.assertFalse(target.exists())

    async def test_kept_after_comment_creates_archive_notes(self):
        self._mk("x2")
        db.add_comment("x2", "先写的批注")
        async with self.client() as c:
            await c.post("/api/posts/x2/status", json={"status": "kept"})
            day = db.get_post("x2")["kept_at"][:10]
            target = config.EXPORT_DIR / day / "评论.txt"
            self.assertTrue(target.exists())
            self.assertIn("先写的批注", target.read_text(encoding="utf-8"))

    def test_zip_notes_text_format(self):
        from app import downloads
        class R(dict):
            def __init__(self, **kw): super().__init__(**kw)
        rows = [R(sid="z1", author="作者乙", created_at="2026-10-01T10:00:00+08:00")]
        db.add_comment("demo0000", "占位，不在范围内")
        self.assertEqual(downloads.notes_text(rows), "")   # 范围内无批注不落文件
        db.add_comment("z1", "> 引用行\n批注正文")
        text = downloads.notes_text(rows)
        self.assertIn("2026-10-01_作者乙_z1", text)
        self.assertIn("> 引用行", text)
        self.assertIn("批注正文", text)
        self.assertNotIn("占位", text)


if __name__ == "__main__":
    unittest.main()
