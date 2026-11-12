"""Source 层契约测试：微博与 X 的 normalize 输出必须覆盖统一 post 契约。

外部平台结构易变，把「缺键/坏数据」的失败前移到单测，而不是 ingest 运行时 KeyError。
"""
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from PIL import Image

from app import config, crawler, db, demo
from app.sources import x as x_source
from app.sources.base import POST_CONTRACT, StructureError, validate_post
from app.weibo import parse_mblog
from tests.auth_support import admin_headers


def _jpeg_bytes(w=64, h=48) -> bytes:
    im = Image.new("RGB", (w, h), (150, 120, 90))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _tweet(**over):
    t = {
        "id": "1844000000000000000",
        "url": "",
        "author": {"uid": "somehandler", "name": "X画师",
                   "avatar": "https://pbs.twimg.com/profile_images/abc_normal.jpg"},
        "text": "<b>带图</b>推文",
        "created_at": "2026-09-15T12:00:00+00:00",
        "via": "",
        "images": ["https://pbs.twimg.com/media/AbCdEf?format=jpg&name=large"],
    }
    t.update(over)
    return t


class XNormalizerContractTest(unittest.TestCase):
    def test_full_payload_fields(self):
        p = x_source.normalize_tweet(_tweet())
        self.assertEqual(p["sid"], "x:1844000000000000000")   # 前缀防撞
        self.assertEqual(p["source"], "x")
        self.assertEqual(p["uid"], "somehandler")
        self.assertEqual(p["author"], "X画师")
        self.assertEqual(p["weibo_url"], "https://x.com/somehandler/status/1844000000000000000")
        # 时区归一：UTC 入参 → 东八区（库内跨来源排序/筛选按字符串比较）
        self.assertEqual(p["created_at"], "2026-09-15T20:00:00+08:00")
        # 原图画质：name=large → name=orig；mid 保留采集原 URL
        self.assertTrue(p["pics"][0]["full"].endswith("name=orig"))
        self.assertIn("name=large", p["pics"][0]["mid"])

    def test_medialess_and_malformed_rejected(self):
        self.assertIsNone(x_source.normalize_tweet(_tweet(images=[])))       # 无图
        self.assertIsNone(x_source.normalize_tweet(_tweet(id="abc")))        # 非数字 id
        self.assertIsNone(x_source.normalize_tweet(_tweet(author={})))       # 缺作者
        self.assertIsNone(x_source.normalize_tweet("not a dict"))
        self.assertIsNone(x_source.normalize_tweet(None))

    def test_created_at_fallback_and_bad_url_shape(self):
        p = x_source.normalize_tweet(_tweet(created_at="垃圾串", url="",
                                            images=["https://pbs.twimg.com/media/XY:large"]))
        self.assertIn("T", p["created_at"])   # 解析不了 → 入库时刻兜底
        self.assertTrue(p["pics"][0]["full"].endswith("name=orig"))
        self.assertNotIn(":large", p["pics"][0]["full"])

    def test_pseudo_protocol_url_falls_back_to_canonical(self):
        """采集端 url 只认 http(s)：javascript: 等伪协议不落库（服务端版 safeUrl）。"""
        p = x_source.normalize_tweet(_tweet(url="javascript:fetch('/api/settings/cookie')"))
        self.assertEqual(p["weibo_url"], "https://x.com/somehandler/status/1844000000000000000")
        keep = x_source.normalize_tweet(_tweet(url="HTTPS://x.com/h/status/2"))
        self.assertEqual(keep["weibo_url"], "HTTPS://x.com/h/status/2")   # http(s) 大小写不敏感放行

    def test_all_sources_cover_post_contract(self):
        wb = parse_mblog({
            "mblogid": "NkAbCd", "text": "图", "created_at": "Tue Sep 15 10:00:00 +0800 2026",
            "user": {"id": 1, "screen_name": "甲"},
            "pic_infos": {"p1": {"large": {"url": "https://wx1.sinaimg.cn/large/p1.jpg"}}},
        })
        xp = x_source.normalize_tweet(_tweet())
        for p in (wb, xp):
            validate_post(p)                                   # 缺键即抛
            self.assertTrue(POST_CONTRACT <= set(p))


class SourceErrorsTest(unittest.TestCase):
    def test_group_list_id_requires_digits(self):
        self.assertEqual(x_source.group_list_id("123"), "x:123")
        self.assertEqual(x_source.group_list_id("x:123"), "x:123")
        with self.assertRaises(StructureError):
            x_source.group_list_id("weibo-list")


class DBBackedTestBase(unittest.IsolatedAsyncioTestCase):
    """临时数据目录隔离；setUp 建 schema + demo 种子。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-src-"))
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


class SidCollisionTest(DBBackedTestBase):
    async def test_weibo_and_x_sids_never_collide(self):
        base = {"sid": "abc123", "uid": "1", "author": "甲", "avatar": "", "text": "t",
                "created_at": "2026-09-15T10:00:00+08:00", "fetched_at": "2026-09-15T10:00:00+08:00",
                "weibo_url": "u", "via": "", "is_long": 0}
        wb = dict(base)
        xb = dict(base, sid="x:abc123", source="x")
        for p in (wb, xb):
            db.insert_post(p, "g1")
        self.assertIsNotNone(db.get_post("abc123"))
        self.assertIsNotNone(db.get_post("x:abc123"))
        self.assertEqual(db.get_post("abc123")["source"], "weibo")
        self.assertEqual(db.get_post("x:abc123")["source"], "x")


class XIngestEndpointTest(DBBackedTestBase):
    """端到端：POST /api/ingest/x → 落库归一化帖 → 分组带 x: 前缀。"""

    async def test_ingest_x_stores_posts_and_group(self):
        async def fake_download(client, url):
            return _jpeg_bytes()
        with mock.patch.object(crawler, "_download", fake_download):
            async with self.client() as c:
                r = await c.post("/api/ingest/x", json={
                    "list_id": "12345", "name": "X画师列表",
                    "tweets": [_tweet(), _tweet(id="1844000000000000001",
                                                images=[])]})
                body = r.json()
                self.assertEqual(body["ok"], True)
                self.assertEqual(body["received"], 2)
                self.assertEqual(body["normalized"], 1)   # 无图帖被过滤
                self.assertEqual(body["new_posts"], 1)
        row = db.get_post("x:1844000000000000000")
        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "x")
        self.assertEqual(row["author"], "X画师")
        groups = {g["list_id"]: g for g in db.active_groups()}
        self.assertIn("x:12345", groups)                       # 带 x: 前缀入组表
        self.assertEqual(groups["x:12345"]["source"], "x")
        # 微博抓取队列不会碰 x: 分组
        async with self.client() as c:
            st = (await c.get("/api/status")).json()
        self.assertTrue(any(g["list_id"] == "x:12345" for g in st["groups"]))

    async def test_ingest_x_rejects_bad_list_id(self):
        async with self.client() as c:
            r = await c.post("/api/ingest/x", json={"list_id": "not digits", "tweets": []})
            self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
