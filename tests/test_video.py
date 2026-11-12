"""X 视频链路回归：解析 → 契约 → 白名单 → 落盘 → 入库 → 补抓 → 打包。

钉死的边界：微博视频仍整帖跳过（test_parse 钉过，此处不重复）；
视频行 pics.file=.mp4、thumb=海报缩略图、w/h=宽高比、poster_url 记海报原址；
批量打包即使用户勾「缩略图」也必须给原视频文件。
"""
import json
import unittest
from unittest.mock import patch

from app import config, crawler, db, downloads, storage
from app.sources import x as x_source
from app.sources import xapi
from app.sources.base import validate_post
from tests.test_api import ApiTestBase, _jpeg_bytes

_MP4 = b"\x00\x00\x00\x18ftypmp42\x00" + b"\x01" * 2048   # 头部带 ftyp box 的最小 mp4


def _video_result(vid="123", bitrate_high=2176000, with_hls=True, gif=False):
    """原始 GraphQL tweet result 构造器（test_x_users._tweet_result 的视频版）。"""
    variants = [
        {"bitrate": 632000, "content_type": "video/mp4", "url": "https://video.twimg.com/low.mp4"},
        {"bitrate": bitrate_high, "content_type": "video/mp4", "url": "https://video.twimg.com/high.mp4"},
    ]
    if with_hls:
        variants.insert(0, {"bitrate": 288000, "content_type": "application/x-mpegURL",
                            "url": "https://video.twimg.com/a.m3u8"})
    return {
        "legacy": {"id_str": vid, "full_text": f"视频帖 {vid}",
                   "created_at": "Wed Oct 11 12:00:00 +0000 2026",
                   "extended_entities": {"media": [{
                       "type": "animated_gif" if gif else "video",
                       "media_url_https": "https://pbs.twimg.com/media/ABC.jpg",
                       "video_info": {"aspect_ratio": [16, 9], "duration_millis": 52000,
                                      "variants": variants}}]}},
        "core": {"user_results": {"result": {
            "rest_id": "999",
            "core": {"screen_name": "tester", "name": "测试者"},
            "legacy": {},
            "avatar": {"image_url": "https://pbs.twimg.com/profile_images/x.jpg"}}}},
    }


class VideoParseTest(unittest.TestCase):
    def test_best_mp4_variant_and_hls_excluded(self):
        item = xapi._tweet_to_item(_video_result())
        self.assertTrue(item["images"] == [] and len(item["videos"]) == 1)
        v = item["videos"][0]
        self.assertEqual(v["url"], "https://video.twimg.com/high.mp4")   # 最高码率
        # variants 只留 mp4（HLS 浏览器原生播不了且下载无用，解析层即弃）
        self.assertEqual([x["url"] for x in v["variants"]],
                         ["https://video.twimg.com/low.mp4", "https://video.twimg.com/high.mp4"])
        self.assertEqual(v["poster"], "https://pbs.twimg.com/media/ABC.jpg")
        self.assertEqual((v["aspect_ratio"], v["duration_millis"]), ([16, 9], 52000))

    def test_gif_and_video_only_tweet_not_dropped(self):
        item = xapi._tweet_to_item(_video_result(vid="456", gif=True))
        self.assertEqual(len(item["videos"]), 1)
        item = xapi._tweet_to_item(_video_result(vid="789", bitrate_high=0))
        self.assertTrue(item and item["videos"])

    def test_text_only_tweet_still_dropped(self):
        raw = _video_result()
        raw["legacy"]["extended_entities"]["media"] = []
        self.assertIsNone(xapi._tweet_to_item(raw))

    def test_normalize_contract(self):
        p = x_source.normalize_tweet(xapi._tweet_to_item(_video_result()))
        validate_post(p)
        vids = [pc for pc in p["pics"] if pc.get("kind") == "video"]
        self.assertEqual(len(vids), 1)
        pc = vids[0]
        self.assertEqual((pc["full"], pc["poster"]), ("https://video.twimg.com/high.mp4",
                                                      "https://pbs.twimg.com/media/ABC.jpg"))
        self.assertEqual((pc["aspect_w"], pc["aspect_h"], pc["dur_ms"]), (16, 9, 52000))

    def test_normalize_drops_video_with_offwhitelist_urls(self):
        item = xapi._tweet_to_item(_video_result())
        item["videos"][0]["poster"] = "https://evil.test/abc.jpg"   # 海报不在白名单 → 整条视频跳过
        p = x_source.normalize_tweet(item)
        self.assertIsNone(p)   # 该帖没有其他媒体，也无图 → 整帖跳过
        # 主 url 仿冒：回落到合法 variants（韧性）；variants 全仿冒才整条跳过
        item["videos"][0]["poster"] = "https://pbs.twimg.com/media/ABC.jpg"
        item["videos"][0]["url"] = "https://video.twimg.com.evil.test/x.mp4"
        p2 = x_source.normalize_tweet(item)
        self.assertIsNotNone(p2)
        self.assertEqual(p2["pics"][0]["full"], "https://video.twimg.com/high.mp4")
        item["videos"][0]["variants"] = [{"url": "https://evil.test/x.mp4", "bitrate": 9}]
        self.assertIsNone(x_source.normalize_tweet(item))

    def test_photo_post_unchanged(self):
        item = {"id": "111", "author": {"id": "9", "uid": "tester", "handle": "tester",
                                        "name": "测试者", "avatar": ""},
                "text": "图帖", "created_at": "2026-10-11T12:00:00+08:00", "via": "",
                "images": ["https://pbs.twimg.com/media/AbC?format=jpg"]}
        p = x_source.normalize_tweet(item)
        self.assertTrue(p and all(pc.get("kind", "photo") == "photo" for pc in p["pics"]))
        self.assertEqual(p["pics"][0]["full"], "https://pbs.twimg.com/media/AbC?format=jpg&name=orig")


class VideoWhitelistTest(unittest.TestCase):
    def test_video_twimg_allowed_and_spoof_blocked(self):
        from app.media_fetch import permitted
        self.assertTrue(permitted("https://video.twimg.com/t/1/2/x.mp4?ct=video/mp4"))
        self.assertFalse(permitted("http://video.twimg.com/x.mp4"))            # 非 https
        self.assertFalse(permitted("https://video.twimg.com.evil.test/x.mp4")) # 仿冒后缀域
        self.assertFalse(permitted("https://user:pw@video.twimg.com/x.mp4"))   # userinfo
        self.assertFalse(permitted("https://video.twimg.com:8443/x.mp4"))      # 非 443 端口


class VideoStorageTest(ApiTestBase):
    """save_video 落盘语义（沿用 ApiTestBase 的临时数据目录）。"""

    def test_ext_of_mp4_by_url_and_magic(self):
        self.assertEqual(storage.ext_of("https://video.twimg.com/x.mp4?ct=v", _MP4), ".mp4")
        self.assertEqual(storage.ext_of("https://video.twimg.com/x", _MP4), ".mp4")   # 魔数兜底
        self.assertEqual(storage.ext_of("https://pbs.twimg.com/a.jpg", _jpeg_bytes()), ".jpg")

    def test_save_video_content_addressed_and_registered(self):
        rel1 = storage.save_video(_MP4, "https://video.twimg.com/x.mp4")
        rel2 = storage.save_video(_MP4, "https://video.twimg.com/x.mp4")   # 同内容 → 同路径
        self.assertEqual(rel1, rel2)
        self.assertTrue(rel1.endswith(".mp4"))
        self.assertTrue((config.PICS_DIR / rel1).is_file())
        with db.conn() as c:
            row = c.execute("SELECT size FROM media_contents WHERE file=?", (rel1,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["size"], len(_MP4))

    def test_save_video_rejects_oversize(self):
        storage.save_video(bytes(1024), "https://video.twimg.com/x.mp4")   # 小文件应成功
        orig = config.MAX_VIDEO_BYTES
        config.MAX_VIDEO_BYTES = 4
        try:
            with self.assertRaises(ValueError):
                storage.save_video(_MP4, "https://video.twimg.com/x.mp4")
        finally:
            config.MAX_VIDEO_BYTES = orig


class VideoIngestTest(ApiTestBase):
    async def _ingest_one(self, vid="123"):
        p = x_source.normalize_tweet(xapi._tweet_to_item(_video_result(vid)))
        validate_post(p)
        poster = _jpeg_bytes(1200, 675)

        async def fake_download(client, url, max_bytes=None):
            return _MP4 if "video.twimg.com" in url else poster

        import httpx as _httpx
        with patch("app.crawler._download", fake_download):
            saved = await crawler._store_post(_httpx.AsyncClient(), p, "x:123", {})
        return p, saved

    async def test_store_post_video_end_to_end(self):
        p, saved = await self._ingest_one()
        self.assertEqual(saved, 1)
        post = db.get_post(p["sid"])
        self.assertIsNotNone(post)
        pc = post["pics"][0]
        self.assertEqual(pc["kind"], "video")
        self.assertTrue(pc["file"].endswith(".mp4"))
        self.assertTrue((config.PICS_DIR / pc["file"]).is_file())
        # 海报缩略图落盘且 pics.thumb 指向它
        self.assertTrue(pc["thumb"] and (config.PICS_DIR / pc["thumb"]).is_file())
        self.assertEqual((pc["w"], pc["h"], pc["dur_ms"]), (16, 9, 52000))
        self.assertEqual(pc["poster_url"], "https://pbs.twimg.com/media/ABC.jpg")
        self.assertEqual(pc["quality"], "full")
        # 列表 payload 带出 kind/dur_ms（前端渲染依赖）
        async with self.client() as c:
            posts = (await c.get("/api/posts")).json()["posts"]
            sample = next(x for x in posts if x["sid"] == p["sid"])
            self.assertEqual(sample["pics"][0]["kind"], "video")
            self.assertEqual(sample["pics"][0]["dur_ms"], 52000)

    async def test_missing_retry_keeps_video_kind(self):
        p, saved = await self._ingest_one()
        self.assertEqual(saved, 1)
        pc = db.get_post(p["sid"])["pics"][0]
        # 模拟丢文件：删掉视频文件后走补抓登记
        (config.PICS_DIR / pc["file"]).unlink()
        n = crawler.queue_missing_files()
        self.assertGreaterEqual(n, 1)
        with db.conn() as c:
            row = c.execute("SELECT payload FROM image_jobs WHERE sid=?", (p["sid"],)).fetchone()
        payload = json.loads(row["payload"])
        self.assertEqual(payload["pics"][0]["kind"], "video")
        self.assertEqual(payload["pics"][0]["poster"], "https://pbs.twimg.com/media/ABC.jpg")
        # 重试落回 video 分支：文件恢复、行保持 kind=video
        poster = _jpeg_bytes(1200, 675)

        async def fake_download(client, url, max_bytes=None):
            return _MP4 if "video.twimg.com" in url else poster

        import httpx as _httpx
        with patch("app.crawler._download", fake_download):
            saved2 = await crawler._store_post(_httpx.AsyncClient(), p, "x:123", {})
        self.assertEqual(saved2, 1)
        pc2 = db.get_post(p["sid"])["pics"][0]
        self.assertEqual(pc2["kind"], "video")
        self.assertTrue((config.PICS_DIR / pc2["file"]).is_file())

    async def test_download_plan_video_always_full_file(self):
        p, _saved = await self._ingest_one()
        db.set_status(p["sid"], "kept")
        plan = downloads.plan("kept", thumb=True)
        names = list(plan["entries"])
        self.assertTrue(names)
        self.assertTrue(all(name.endswith(".mp4") for name in names))   # 缩略图档也不给海报
        plan2 = downloads.plan("kept", thumb=False)
        self.assertTrue(all(name.endswith(".mp4") for name in plan2["entries"]))


if __name__ == "__main__":
    unittest.main()
