"""关系、来源、画质、图序与历史归档的行为验收。"""
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app import config, crawler, db, downloads, purge, storage
from app.sources import x
from tests.test_api import ApiTestBase, _jpeg_bytes


class MediaModelTest(ApiTestBase):
    def post(self, sid="ordered"):
        return {**db.get_post("demo0000"), "sid": sid, "avatar": "",
                "pics": [{"pid": "a", "full": "https://test/a.jpg", "mid": "https://test/a.jpg"},
                         {"pid": "b", "full": "https://test/b.jpg", "mid": "https://test/b.jpg"}]}

    async def test_retry_preserves_source_image_order(self):
        p = self.post()
        with patch.object(crawler, "_download", side_effect=[None, _jpeg_bytes(70, 50)]):
            await crawler._store_post(None, p, "g1", {})
        with patch.object(crawler, "_download", return_value=_jpeg_bytes(80, 60)):
            await crawler.retry_missing(manual=True)
        self.assertEqual([r["url"] for r in db.get_post(p["sid"])["pics"]], [r["full"] for r in p["pics"]])

    async def test_medium_fallback_stays_retryable_and_records_actual_quality(self):
        p = self.post()
        p["pics"] = [{"full": "https://test/full.jpg", "mid": "https://test/mid.jpg"}]
        with patch.object(crawler, "_download", side_effect=[None, _jpeg_bytes()]):
            await crawler._store_post(None, p, "g1", {})
        self.assertTrue(crawler.needs_images(p["sid"]))
        first = db.get_post(p["sid"])["pics"][0]
        self.assertEqual(first["quality"], "mid")
        self.assertEqual(first["actual_url"], "https://test/mid.jpg")
        with patch.object(crawler, "_download", return_value=_jpeg_bytes(100, 80)):
            await crawler.retry_missing(manual=True)
        self.assertFalse(crawler.needs_images(p["sid"]))
        self.assertEqual(db.get_post(p["sid"])["pics"][0]["quality"], "full")

    async def test_same_post_is_available_in_both_groups_with_contexts(self):
        p = self.post()
        with patch.object(crawler, "_download", return_value=_jpeg_bytes()):
            await crawler._store_post(None, p, "g1", {})
        db.observe_post({**p, "via": "relay", "text": "different context"}, "g2")
        async with self.client() as c:
            first = (await c.get("/api/posts?group=g1")).json()
            second = (await c.get("/api/posts?group=g2")).json()
        self.assertEqual(first["posts"][0]["sid"], second["posts"][0]["sid"])
        self.assertEqual(set(second["posts"][0]["groups"]), {"g1", "g2"})
        self.assertEqual(len(db.get_post(p["sid"])["observations"]), 2)

    async def test_author_identity_is_source_scoped_and_x_id_survives_handle_change(self):
        p = self.post("wb")
        p["uid"] = "123"
        db.insert_post(p, "g1")
        tweet = {"id": "456", "author": {"id": "123", "uid": "old", "name": "X name"}, "images": ["https://pbs.twimg.com/media/a.jpg"]}
        one = x.normalize_tweet(tweet)
        two = x.normalize_tweet({**tweet, "id": "457", "author": {**tweet["author"], "uid": "new"}})
        db.insert_post(one, "x:1")
        db.insert_post(two, "x:1")
        authors = {a["uid"]: a for a in db.list_authors()}
        self.assertIn("123", authors)
        self.assertEqual(authors["x:123"]["n"], 2)
        async with self.client() as c:
            self.assertEqual((await c.get("/api/posts?author=x:123")).json()["total"], 2)
            with patch("app.weibo.profile_bio", side_effect=AssertionError("X 作者不能请求微博")):
                self.assertEqual((await c.get("/api/authors/x:123/bio")).json()["source"], "x")

    def test_same_size_archive_corruption_is_repaired_after_post_is_not_kept(self):
        rel = db.get_post("demo0000")["pics"][0]["file"]
        storage.export_report([rel], "2026-10-01")
        dst = config.EXPORT_DIR / "2026-10-01" / rel.rsplit("/", 1)[-1]
        original = dst.read_bytes()
        dst.write_bytes(b"x" * len(original))
        self.assertEqual(storage.archive_health(deep=True)["historical_corrupt"], 1)
        storage.retry_archive()
        self.assertEqual(dst.read_bytes(), original)

    def test_failed_file_delete_is_persistent_and_retry_does_not_lie(self):
        db.set_status("demo0000", "discarded")
        # 演示评论会让帖子豁免清理（评论豁免是产品规则）；本测试练的是文件删除重试，先移除
        with db.conn() as c:
            c.execute("DELETE FROM post_comments WHERE sid='demo0000'")
        with db.conn() as c:
            c.execute("UPDATE posts SET discarded_at=? WHERE sid='demo0000'", ((datetime.now(config.NOW_TZ)-timedelta(days=8)).isoformat(),))
        with patch.object(storage, "remove_pic_files", side_effect=OSError("busy")):
            result = purge.purge_expired()
            self.assertEqual(result["removed_files"], 0)
        self.assertFalse(db.has_post("demo0000"))
        self.assertEqual(purge.retry_files()["remaining"], 0)

    def test_historical_archive_missing_is_not_reported_complete(self):
        storage._archive_record("2026/01/missing.jpg", "2026-01-01", "", 0, "missing")
        result = storage.retry_archive()
        self.assertTrue(result["current_complete"])
        self.assertFalse(result["historical_complete"])
        self.assertFalse(result["archive_complete"])

    def test_absent_file_retry_does_not_count_as_deleted(self):
        with db.conn() as c:
            c.execute("INSERT INTO file_jobs(file,updated_at) VALUES('2026/01/absent.jpg','now')")
        self.assertEqual(purge.retry_files(), {"removed":0, "remaining":0})

    def test_corrupt_cleanup_path_does_not_delete_outside_media(self):
        outside = self.tmp / "private.txt"
        outside.write_text("safe")
        with db.conn() as c:
            c.execute("INSERT INTO file_jobs(file,updated_at) VALUES('../private.txt','now')")
        self.assertEqual(purge.retry_files(), {"removed":0, "remaining":1})
        self.assertTrue(outside.exists())

    async def test_ingest_over_limit_explicitly_rejects_without_creating_group(self):
        async with self.client() as c:
            r = await c.post("/api/ingest/harvest", json={"list_id": "12345", "statuses": [{}]*101})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(any(g["list_id"] == "12345" for g in db.active_groups()))

    def test_over_limit_download_does_not_scan_files(self):
        with db.conn() as c:
            c.executemany("INSERT INTO posts(sid,status,kept_at) VALUES(?,'kept','2026-10-01T10:00:00+08:00')", [(f"large{i}",) for i in range(501)])
        with patch("pathlib.Path.is_file", side_effect=AssertionError("超限不应扫描文件")):
            self.assertGreater(downloads.preview()["posts"], 500)

    async def test_backfill_crosses_no_image_page_and_reports_limit(self):
        p = self.post()
        with patch.object(config, "CRAWL_BACKFILL_MAX_PAGES", 2), patch.object(crawler.weibo, "group_timeline", new=AsyncMock(return_value={})), patch.object(crawler.weibo, "extract_statuses", side_effect=[[{}], [{}]]), patch.object(crawler.weibo, "parse_mblog", side_effect=[None, p]), patch.object(crawler, "_download", return_value=_jpeg_bytes()), patch.object(crawler.asyncio, "sleep", new=AsyncMock()):
            result = await crawler._crawl_group(None, "fake", {"list_id": "g1", "name": "test"})
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["coverage"], "page_limit")
        self.assertEqual(json.loads(db.get_meta("group_coverage:g1"))["received"], 2)
