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


if __name__ == "__main__":
    unittest.main()
