"""回归测试：本轮改造修过的点都在这里钉死。

跑法与 test_parse 相同：项目根目录 `python -m unittest discover -s tests -t .`。
API 测试用 httpx ASGITransport 直打 app，数据目录指向临时文件夹，不碰真实 data/。
"""
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from app import config, crawler, db, demo
from tests.auth_support import admin_headers


def _jpeg_bytes(w=64, h=48) -> bytes:
    im = Image.new("RGB", (w, h), (120, 140, 160))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class ApiTestBase(unittest.IsolatedAsyncioTestCase):
    """每个用例一套临时数据目录，结束即删。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-test-"))
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

    async def asyncTearDown(self):
        await self.server.jobs.runner.shutdown()

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                 base_url="http://test", headers=admin_headers())


class MediaSafetyTest(ApiTestBase):
    async def test_traversal_blocked(self):
        # httpx 会把明文 .. 归一化掉，用 %2e%2e 确保打进路由本身
        async with self.client() as c:
            for path in ("/media/%2e%2e/picwall.db",
                         "/media/%2e%2e/%2e%2e/etc/passwd",
                         "/media/pics/%2e%2e/picwall.db"):
                r = await c.get(path)
                self.assertEqual(r.status_code, 404, path)

    async def test_valid_media_served(self):
        async with self.client() as c:
            posts = (await c.get("/api/posts")).json()["posts"]
            thumb = posts[0]["pics"][0]["thumb"]
            r = await c.get("/media/" + thumb)
            self.assertEqual(r.status_code, 200)


class PostsClampTest(ApiTestBase):
    async def test_limit_and_offset_clamp(self):
        async with self.client() as c:
            r = await c.get("/api/posts", params={"limit": 0})
            self.assertEqual(r.status_code, 200)
            d = r.json()
            self.assertEqual(len(d["posts"]), min(d["total"], 50))

            d = (await c.get("/api/posts", params={"limit": 999})).json()
            self.assertLessEqual(len(d["posts"]), 200)
            # 前端撤销流依赖 list_id 判断当前分组
            self.assertTrue(d["posts"] and all("list_id" in p for p in d["posts"]))

            r = await c.get("/api/posts", params={"offset": -5})
            self.assertEqual(r.status_code, 200)


class CookieValidationTest(ApiTestBase):
    async def test_requires_sub_and_xsrf(self):
        async with self.client() as c:
            r = await c.post("/api/settings/cookie", json={"cookie": "SUB=abc; other=1"})
            self.assertEqual(r.status_code, 400)
            with patch("app.weibo.all_groups", new=AsyncMock(return_value=[])):
                r = await c.post("/api/settings/cookie",
                                 json={"cookie": "SUB=abc; XSRF-TOKEN=tok"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(config.COOKIE_FILE.exists())


class KeepExportZipTest(ApiTestBase):
    async def test_keep_exports_and_zip(self):
        async with self.client() as c:
            posts = (await c.get("/api/posts")).json()["posts"]
            sid = posts[0]["sid"]
            r = await c.post(f"/api/posts/{sid}/status", json={"status": "kept"})
            self.assertEqual(r.status_code, 200)
            day = r.json()["to"].rstrip("/").split("/")[-1]
            self.assertTrue((config.EXPORT_DIR / day).is_dir())

            bad = await c.get("/api/export/zip", params={"date": "../../etc"})
            self.assertEqual(bad.status_code, 404)
            good = await c.get("/api/export/zip", params={"date": day})
            self.assertEqual(good.status_code, 200)
            zf = zipfile.ZipFile(io.BytesIO(good.content))
            self.assertTrue(zf.namelist())


class DownloadFilterTest(unittest.IsolatedAsyncioTestCase):
    """crawler._download 对错误页/风控响应的过滤。"""

    @staticmethod
    def _client(body: bytes, ct: str):
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=body,
                                       headers={"content-type": ct})))

    async def test_rejects_html(self):
        async with self._client(b"<html>403 forbidden</html>" * 10, "text/html") as c:
            self.assertIsNone(await crawler._download(c, "https://pbs.twimg.com/y.jpg"))

    async def test_rejects_json_and_tiny(self):
        async with self._client(b'{"ok":0}', "application/json") as c:
            self.assertIsNone(await crawler._download(c, "https://pbs.twimg.com/y.jpg"))
        async with self._client(b"\xff\xd8\xffsmall", "image/jpeg") as c:
            self.assertIsNone(await crawler._download(c, "https://pbs.twimg.com/y.jpg"))

    async def test_accepts_image(self):
        data = _jpeg_bytes()
        self.assertGreaterEqual(len(data), 100)
        async with self._client(data, "image/jpeg") as c:
            self.assertEqual(await crawler._download(c, "https://pbs.twimg.com/y.jpg"), data)


class QrSvgContractTest(ApiTestBase):
    async def test_qr_svg_validates_address(self):
        """扫码地址只认 http(s) 且限长：SVG 生成器的输入契约（与前端 safeUrl 同族）。"""
        async with self.client() as c:
            ok = await c.get("/qr.svg", params={"text": "http://192.168.5.10:8082"})
            self.assertEqual(ok.status_code, 200)
            self.assertIn("image/svg", ok.headers["content-type"])
            for bad in ("javascript:alert(1)", "ftp://x", "https://" + "a" * 505):
                r = await c.get("/qr.svg", params={"text": bad})
                self.assertEqual(r.status_code, 400, bad[:40])


class PicDedupeTest(ApiTestBase):
    def test_same_sid_file_inserted_once(self):
        db.insert_pic("s1", "a.jpg", "t_a.jpg", 1, 1, "u")
        db.insert_pic("s1", "a.jpg", "t_a.jpg", 1, 1, "u")   # 抓取中断重抓的去重保障
        with db.conn() as c:
            n = c.execute("SELECT COUNT(*) FROM pics WHERE sid='s1'").fetchone()[0]
        self.assertEqual(n, 1)


class ActiveGroupsTest(ApiTestBase):
    def test_demo_group_never_crawled(self):
        names = {g["list_id"] for g in db.active_groups()}
        self.assertNotIn("demo", names)   # 演示分组只用于界面预览，不进抓取队列
        db.add_group("12345", "真实分组")
        self.assertIn("12345", {g["list_id"] for g in db.active_groups()})


class AtlasPageTest(ApiTestBase):
    """设置 → 开发管理：/atlas 仅管理设备可打开（本机入口免密即 admin；局域网须管理配对）。"""

    async def test_atlas_access(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://test") as client:
            r_admin = await client.get("/atlas", headers=admin_headers())
            self.assertEqual(r_admin.status_code, 200)
            self.assertIn("atlas-data", r_admin.text)   # 图谱数据段随页面送达

            viewer = self.server.auth.exchange(self.server.auth.issue_code("viewer"), "只读", "test")
            r_viewer = await client.get("/atlas", headers={
                "Host": "localhost", "Authorization": "Bearer " + viewer["token"]})
            self.assertEqual(r_viewer.status_code, 403)

            r_anon = await client.get("/atlas", headers={"Host": "localhost"})
            self.assertEqual(r_anon.status_code, 401)


class MultiDimensionalSearchTest(ApiTestBase):
    """验证多维搜索接口 (q_scope: all/text/comment/tag/author) 及其筛选隔离性。"""

    def setUp(self):
        super().setUp()
        # 为 demo0000 打上特定标签用于测试
        db.set_post_tags("demo0000", add=["海边日落"])

    async def test_search_scopes_isolation(self):
        async with self.client() as c:
            # 1. 搜文本（内容）
            # demo0000 的 text 含有 "山间"，但 author 是 "演示作者 · A"，comment 含 "北坡观景台"，tag 含 "海边日落"
            # 当 q="山间" 且 q_scope="text" -> 命中
            r = await c.get("/api/posts?status=all&group=all&q=山间&q_scope=text")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(any(p["sid"] == "demo0000" for p in r.json()["posts"]))

            # 当 q="北坡观景台"（仅在评论中）且 q_scope="text" -> 应该查不到
            r = await c.get("/api/posts?status=all&group=all&q=北坡观景台&q_scope=text")
            self.assertEqual(len(r.json()["posts"]), 0)

            # 2. 搜评论 (comment)
            # q="北坡观景台" 且 q_scope="comment" -> 应该命中 demo0000，且注入 comment_match
            r = await c.get("/api/posts?status=all&group=all&q=北坡观景台&q_scope=comment")
            self.assertEqual(r.status_code, 200)
            posts = r.json()["posts"]
            self.assertTrue(any(p["sid"] == "demo0000" for p in posts))
            hit = next(p for p in posts if p["sid"] == "demo0000")
            self.assertIn("北坡观景台", hit.get("comment_match", ""))

            # q="山间"（正文词）且 q_scope="comment" -> 应该查不到
            r = await c.get("/api/posts?status=all&group=all&q=山间&q_scope=comment")
            self.assertEqual(len(r.json()["posts"]), 0)

            # 3. 搜标签 (tag)
            # q="海边日落" 且 q_scope="tag" -> 应该命中 demo0000
            r = await c.get("/api/posts?status=all&group=all&q=海边日落&q_scope=tag")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(any(p["sid"] == "demo0000" for p in r.json()["posts"]))

            # q="山间" 且 q_scope="tag" -> 应该查不到
            r = await c.get("/api/posts?status=all&group=all&q=山间&q_scope=tag")
            self.assertEqual(len(r.json()["posts"]), 0)

            # 4. 搜博主 (author)
            # q="演示作者 · A" 且 q_scope="author" -> 应该命中 demo0000
            r = await c.get("/api/posts?status=all&group=all&q=演示作者 · A&q_scope=author")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(any(p["sid"] == "demo0000" for p in r.json()["posts"]))

            # q="山间" 且 q_scope="author" -> 应该查不到
            r = await c.get("/api/posts?status=all&group=all&q=山间&q_scope=author")
            self.assertEqual(len(r.json()["posts"]), 0)

            # 5. 搜全部 (all)
            # 文本、评论、标签、博主任何维度命中，q_scope="all" 均应命中
            r_text = await c.get("/api/posts?status=all&group=all&q=山间&q_scope=all")
            self.assertTrue(any(p["sid"] == "demo0000" for p in r_text.json()["posts"]))

            r_cmt = await c.get("/api/posts?status=all&group=all&q=北坡观景台&q_scope=all")
            self.assertTrue(any(p["sid"] == "demo0000" for p in r_cmt.json()["posts"]))

            r_tag = await c.get("/api/posts?status=all&group=all&q=海边日落&q_scope=all")
            self.assertTrue(any(p["sid"] == "demo0000" for p in r_tag.json()["posts"]))

            r_auth = await c.get("/api/posts?status=all&group=all&q=演示作者 · A&q_scope=all")
            self.assertTrue(any(p["sid"] == "demo0000" for p in r_auth.json()["posts"]))

            # 6. comment_match 隔离性：仅在 all 或 comment 维度注入，text 维度不注入
            r_text_nomatch = await c.get("/api/posts?status=all&group=all&q=山间&q_scope=text")
            p_text = next(p for p in r_text_nomatch.json()["posts"] if p["sid"] == "demo0000")
            self.assertNotIn("comment_match", p_text)

            # 7. q_scope 容错与大小写归一化
            r_ci = await c.get("/api/posts?status=all&group=all&q=山间&q_scope=TEXT")
            self.assertTrue(any(p["sid"] == "demo0000" for p in r_ci.json()["posts"]))

    async def test_search_live_endpoint(self):
        """验证实时搜索接口 (/api/search/live) 快速返回多维命中结果。"""
        async with self.client() as c:
            # 1. 空查询返回空分类
            r = await c.get("/api/search/live?q=")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["authors"], [])
            self.assertEqual(data["tags"], [])
            self.assertEqual(data["content"], [])
            self.assertEqual(data["comments"], [])

            # 2. q="山间" (正文) 命中 content
            r = await c.get("/api/search/live?q=山间")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(any(item["sid"] == "demo0000" for item in data["content"]))
            content_item = next(item for item in data["content"] if item["sid"] == "demo0000")
            self.assertIn("山间", content_item["text"])
            self.assertTrue(content_item["author"])

            # 3. q="北坡观景台" (评论) 命中 comments
            r = await c.get("/api/search/live?q=北坡观景台")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(any(item["sid"] == "demo0000" for item in data["comments"]))
            comment_item = next(item for item in data["comments"] if item["sid"] == "demo0000")
            self.assertIn("北坡观景台", comment_item["body"])

            # 4. q="海边日落" (标签) 命中 tags
            r = await c.get("/api/search/live?q=海边日落")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(any(item["tag"] == "海边日落" for item in data["tags"]))

            # 5. q="演示作者" (博主) 命中 authors
            r = await c.get("/api/search/live?q=演示作者")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertTrue(any("演示作者" in item["author"] for item in data["authors"]))
            auth_item = next(item for item in data["authors"] if "演示作者" in item["author"])
            self.assertTrue(auth_item["uid"])
            self.assertTrue(auth_item["n"] >= 1)

            # 6. q_scope 维度限定隔离性
            # 当 q="山间" 且 q_scope="comment" 时，content 应该为空
            r_scope = await c.get("/api/search/live?q=山间&q_scope=comment")
            self.assertEqual(r_scope.status_code, 200)
            self.assertEqual(r_scope.json()["content"], [])
            self.assertEqual(r_scope.json()["authors"], [])
            self.assertEqual(r_scope.json()["tags"], [])

            # 7. 语法前缀容错支持：@博主、#标签、//评论（显式 scope 与默认 scope=all 前缀推断）
            r_prefix_auth = await c.get("/api/search/live", params={"q": "@演示作者", "q_scope": "author"})
            self.assertEqual(r_prefix_auth.status_code, 200)
            self.assertTrue(any("演示作者" in item["author"] for item in r_prefix_auth.json()["authors"]))

            r_prefix_tag = await c.get("/api/search/live", params={"q": "#海边日落", "q_scope": "tag"})
            self.assertEqual(r_prefix_tag.status_code, 200)
            self.assertTrue(any(item["tag"] == "海边日落" for item in r_prefix_tag.json()["tags"]))

            # scope=all 下携带 @/# 前缀智能分类推断
            r_auto_auth = await c.get("/api/search/live", params={"q": "@演示作者"})
            self.assertEqual(r_auto_auth.status_code, 200)
            self.assertTrue(any("演示作者" in item["author"] for item in r_auto_auth.json()["authors"]))

            r_auto_tag = await c.get("/api/search/live", params={"q": "#海边日落"})
            self.assertEqual(r_auto_tag.status_code, 200)
            self.assertTrue(any(item["tag"] == "海边日落" for item in r_auto_tag.json()["tags"]))

            r_auto_cmt = await c.get("/api/search/live", params={"q": "//北坡观景台"})
            self.assertEqual(r_auto_cmt.status_code, 200)
            self.assertTrue(any(item["sid"] == "demo0000" for item in r_auto_cmt.json()["comments"]))

            # 8. 内容与评论返回字段契约（缩略图、日期、正文、作者等完整性）
            r_all = await c.get("/api/search/live?q=山间")
            content_row = r_all.json()["content"][0]
            self.assertIn("created_at", content_row)
            self.assertIn("thumb", content_row)
            self.assertIn("sid", content_row)
            self.assertIn("text", content_row)

            r_cmt_all = await c.get("/api/search/live?q=北坡观景台")
            comment_row = r_cmt_all.json()["comments"][0]
            self.assertIn("created_at", comment_row)
            self.assertIn("comment_id", comment_row)
            self.assertIn("sid", comment_row)
            self.assertIn("body", comment_row)

    async def test_timeline_outline_scope(self):
        async with self.client() as c:
            # timeline/outline 按 tag 维度统计
            r = await c.get("/api/timeline/outline?status=all&group=all&q=海边日落&q_scope=tag")
            self.assertEqual(r.status_code, 200)
            days = r.json()["days"]
            self.assertTrue(len(days) >= 1)
            self.assertTrue(sum(d["total"] for d in days) >= 1)

            # timeline/outline 按 comment 维度统计
            r_cmt = await c.get("/api/timeline/outline?status=all&group=all&q=北坡观景台&q_scope=comment")
            self.assertEqual(r_cmt.status_code, 200)
            days_cmt = r_cmt.json()["days"]
            self.assertTrue(len(days_cmt) >= 1)

            # 不存在的 tag 返回空日集
            r_empty = await c.get("/api/timeline/outline?status=all&group=all&q=不存在的标签&q_scope=tag")
            self.assertEqual(len(r_empty.json()["days"]), 0)


if __name__ == "__main__":
    unittest.main()
