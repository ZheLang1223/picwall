"""优化验收：用临时数据验证用户操作结果与故障恢复。"""

import io
import zipfile
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app import config, crawler, db, purge, storage, weibo
from tests.test_api import ApiTestBase, _jpeg_bytes


class OptimizationTest(ApiTestBase):
    async def test_download_scope_preview_matches_zip(self):
        db.set_status("demo0000", "kept")
        async with self.client() as c:
            preview = (await c.get("/api/download/options?scope=kept")).json()
            response = await c.get("/api/download/zip?scope=kept")
            names = [n for n in zipfile.ZipFile(io.BytesIO(response.content)).namelist()
                     if n != "评论.txt"]   # 批注附件不计入图片数
            self.assertEqual(preview["pics"], len(names))
            self.assertEqual(len(names), 1)
            self.assertIn("demo0000", names[0])
            self.assertEqual(preview["authors"][0]["pics"], 1)
            db.set_favorite("demo0001", True)
            fav = await c.get("/api/download/zip?scope=favorites")
            self.assertTrue(
                all("demo0001" in n for n in zipfile.ZipFile(io.BytesIO(fav.content)).namelist())
            )

    async def test_download_over_limit_never_silently_truncates(self):
        with db.conn() as conn:
            conn.executemany(
                "INSERT INTO posts(sid,status,created_at,kept_at) VALUES(?,'kept',?, '2026-10-01T10:00:00+08:00')",
                [(f"cap{i}", "2026-10-01T10:00:00+08:00") for i in range(501)],
            )
        async with self.client() as c:
            preview = (await c.get("/api/download/options?scope=kept")).json()
            self.assertTrue(preview["over_limit"])
            self.assertEqual((await c.get("/api/download/zip?scope=kept")).status_code, 400)

    async def test_download_deduplicates_preview_and_selection(self):
        p = db.get_post("demo0000")["pics"][0]
        db.insert_pic("demo0001", p["file"], p["thumb"], p["w"], p["h"], "shared")
        async with self.client() as c:
            preview = (
                await c.get("/api/download/options?scope=selected&select=demo0000,demo0001")
            ).json()
            zipped = await c.get("/api/download/zip?scope=selected&select=demo0000,demo0001")
            self.assertEqual(
                preview["pics"],
                sum(n != "评论.txt" for n in zipfile.ZipFile(io.BytesIO(zipped.content)).namelist())
            )

    def test_favorites_survive_purge_and_unfavorite_restarts_clock(self):
        db.set_status("demo0000", "discarded")
        db.set_favorite("demo0000", True)
        old = (datetime.now(config.NOW_TZ) - timedelta(days=8)).isoformat()
        with db.conn() as c:
            c.execute("UPDATE posts SET discarded_at=? WHERE sid=?", (old, "demo0000"))
        self.assertEqual(purge.purge_expired(7)["purged_posts"], 0)
        self.assertTrue(db.has_post("demo0000"))
        db.set_favorite("demo0000", False)
        self.assertGreater(db.get_post("demo0000")["discarded_at"], old)
        self.assertEqual(purge.purge_expired(7)["purged_posts"], 0)

    async def test_undo_restores_original_timestamps_and_refuses_newer_change(self):
        db.set_status("demo0000", "kept")
        before = db.get_post("demo0000")
        async with self.client() as c:
            r = (await c.post("/api/posts/demo0000/status", json={"status": "discarded"})).json()
            self.assertEqual(
                (
                    await c.post(
                        "/api/posts/demo0000/restore",
                        json={"previous": r["previous"], "expected": r["decision"]},
                    )
                ).status_code,
                200,
            )
            restored = db.get_post("demo0000")
            self.assertEqual(restored["status"], "kept")
            self.assertEqual(restored["kept_at"], before["kept_at"])
            db.set_status("demo0000", "pending")
            self.assertEqual(
                (
                    await c.post(
                        "/api/posts/demo0000/restore",
                        json={"previous": r["previous"], "expected": r["decision"]},
                    )
                ).status_code,
                409,
            )

    async def test_cursor_survives_new_posts_deletion_and_timestamp_ties(self):
        with db.conn() as c:
            c.execute("UPDATE posts SET created_at='2026-10-01T00:00:00+08:00'")
        async with self.client() as c:
            first = (await c.get("/api/posts?limit=3")).json()
            self.assertTrue(all("uid" in p and "discarded_at" in p for p in first["posts"]))
            with db.conn() as conn:
                conn.execute(
                    "INSERT INTO posts(sid,status,created_at) VALUES('new','pending','2026-10-02T00:00:00+08:00')"
                )
                conn.execute("DELETE FROM posts WHERE sid='demo0002'")
            second = (
                await c.get("/api/posts", params={"limit": 50, "cursor": first["next_cursor"]})
            ).json()
            self.assertEqual(
                [p["sid"] for p in second["posts"]],
                ["demo0003", "demo0004", "demo0005", "demo0006", "demo0007"],
            )
            self.assertFalse(second["has_more"])

    async def test_expired_cookie_is_failed_without_overwriting_last_success(self):
        db.set_meta("last_crawl", "old-success")
        with (
            patch.object(config, "get_cookie", return_value="fake"),
            patch.object(db, "active_groups", return_value=[{"list_id": "1", "name": "test"}]),
            patch.object(crawler, "_crawl_group", side_effect=weibo.CookieExpired("expired")),
        ):
            r = await crawler.crawl()
        self.assertFalse(r["ok"])
        self.assertEqual(db.get_meta("cookie_state"), "expired")
        self.assertEqual(db.get_meta("last_crawl"), "old-success")
        self.assertTrue(db.get_meta("last_attempt"))

    async def test_partial_failure_keeps_error_and_success_timestamp(self):
        db.set_meta("last_crawl", "old-success")
        with (
            patch.object(config, "get_cookie", return_value="fake"),
            patch.object(
                db,
                "active_groups",
                return_value=[{"list_id": "1", "name": "a"}, {"list_id": "2", "name": "b"}],
            ),
            patch.object(
                crawler,
                "_crawl_group",
                side_effect=[
                    weibo.WeiboError("broken"),
                    {"group": "b", "new_posts": 1, "new_pics": 2},
                ],
            ),
            patch.object(crawler.asyncio, "sleep", new_callable=AsyncMock),
        ):
            r = await crawler.crawl()
        self.assertEqual(r["outcome"], "partial")
        self.assertTrue(db.get_meta("last_error"))
        self.assertEqual(db.get_meta("last_crawl"), "old-success")

    async def test_archive_failure_visible_then_retry_repairs_deleted_copy(self):
        async with self.client() as c:
            with patch.object(storage.shutil, "copy2", side_effect=OSError("disk full")):
                r = (await c.post("/api/posts/demo0000/status", json={"status": "kept"})).json()
            self.assertFalse(r["archive_complete"])
            self.assertEqual(r["failed_count"], 1)
            self.assertEqual(db.get_post("demo0000")["status"], "kept")
            self.assertEqual((await c.get("/api/status")).json()["archive"]["missing"], 1)
            self.assertTrue((await self.archive_retry(c))["archive_complete"])
            src = config.PICS_DIR / db.get_post("demo0000")["pics"][0]["file"]
            day = db.get_meta("exported:" + db.get_post("demo0000")["pics"][0]["file"])
            (config.EXPORT_DIR / day / src.name).unlink()
            self.assertEqual(storage.archive_health()["missing"], 1)
            self.assertTrue((await self.archive_retry(c))["archive_complete"])
            self.assertEqual(storage.archive_health()["missing"], 0)

    async def archive_retry(self, client):
        import asyncio
        response = await client.post("/api/archive/retry")
        self.assertEqual(response.status_code, 202)
        for _ in range(200):
            job = (await client.get("/api/jobs/archive")).json()["job"]
            if job["state"] not in ("queued", "running"):
                self.assertEqual(job["state"], "complete")
                return job["result"]
            await asyncio.sleep(0.01)
        self.fail("隔离归档重试未结束")

    async def test_partial_images_retry_only_missing_urls(self):
        p = {
            "sid": "partial",
            "uid": "u",
            "author": "a",
            "avatar": "",
            "text": "text",
            "created_at": "2026-10-01T00:00:00+08:00",
            "fetched_at": "2026-10-01T00:00:00+08:00",
            "weibo_url": "https://weibo.com/1/partial",
            "via": "",
            "pics": [
                {"full": "https://test/1.jpg", "mid": "https://test/1.jpg"},
                {"full": "https://test/2.jpg", "mid": "https://test/2.jpg"},
            ],
        }

        async def download(client, url):
            return _jpeg_bytes() if url.endswith("1.jpg") else None

        with patch.object(crawler, "_download", side_effect=download):
            self.assertEqual(await crawler._store_post(None, p, "1", {}), 1)
        self.assertTrue(crawler.needs_images("partial"))
        db.set_status("partial", "kept")
        with patch.object(crawler, "_download", return_value=_jpeg_bytes(70, 50)) as dl:
            r = await crawler.retry_missing(manual=True)
            self.assertEqual(r["saved"], 1)
            self.assertEqual([call.args[1] for call in dl.call_args_list], ["https://test/2.jpg"])
        self.assertFalse(crawler.needs_images("partial"))
        self.assertEqual(len(db.get_post("partial")["pics"]), 2)
        self.assertEqual(db.get_post("partial")["status"], "kept")

    def test_connection_is_thread_local_and_exception_rolls_back(self):
        # 连接改为线程本地长连接（2026-11-07）：建连/末连接关闭的 sqlite 内部锁
        # 竞争会楔死全部请求（两次实测），改为每线程复用一条，不再用完即关。
        with db.conn() as c:
            c.execute("INSERT INTO meta VALUES('closed','yes')")
        self.assertIs(db.conn(), c, '同线程复用同一条连接')
        c.execute("SELECT 1")   # 退出事务后连接仍可用（不再关闭）
        with self.assertRaises(ValueError), db.conn() as conn:
            conn.execute("INSERT INTO meta VALUES('rolled-back','bad')")
            raise ValueError()
        self.assertEqual(db.get_meta("rolled-back"), "")

    async def test_zero_images_remain_retryable_and_auto_retry_is_bounded(self):
        p = {
            "sid": "offline",
            "uid": "u",
            "author": "a",
            "avatar": "",
            "text": "text",
            "created_at": "2026-10-01T00:00:00+08:00",
            "fetched_at": "2026-10-01T00:00:00+08:00",
            "weibo_url": "https://weibo.com/1/offline",
            "via": "",
            "pics": [{"full": "https://test/a.jpg", "mid": "https://test/a.jpg"}],
        }
        with patch.object(crawler, "_download", return_value=None) as dl:
            self.assertEqual(await crawler._store_post(None, p, "1", {}), 0)
            self.assertFalse(db.has_post("offline"))
            await crawler.retry_missing()
            await crawler.retry_missing()
            calls = dl.call_count
            await crawler.retry_missing()
            self.assertEqual(dl.call_count, calls)
        self.assertTrue(crawler.needs_images("offline"))
        with patch.object(crawler, "_download", return_value=_jpeg_bytes()):
            self.assertEqual((await crawler.retry_missing(manual=True))["saved"], 1)
        self.assertTrue(db.has_post("offline"))
        self.assertFalse(crawler.needs_images("offline"))

    def test_purge_rechecks_restored_post_inside_lock(self):
        db.set_status("demo0000", "discarded")
        # 演示评论会让帖子豁免清理（评论豁免是产品规则）；本测试练的是锁内重验，先移除
        with db.conn() as c:
            c.execute("DELETE FROM post_comments WHERE sid='demo0000'")
        old = (datetime.now(config.NOW_TZ) - timedelta(days=8)).isoformat()
        with db.conn() as c:
            c.execute("UPDATE posts SET discarded_at=? WHERE sid=?", (old, "demo0000"))
        original = purge._purge_post

        def restored_after_preview(sid, cutoff):
            db.set_status(sid, "kept")
            return original(sid, cutoff)

        with patch.object(purge, "_purge_post", side_effect=restored_after_preview):
            self.assertEqual(purge.purge_expired(7)["purged_posts"], 0)
        self.assertEqual(db.get_post("demo0000")["status"], "kept")

    async def test_missing_originals_recover_without_losing_status(self):
        db.set_status("demo0000", "kept")
        original = db.get_post("demo0000")["pics"][0]
        (config.PICS_DIR / original["file"]).unlink()
        with patch.object(crawler, "_download", return_value=_jpeg_bytes(70, 50)):
            result = await crawler.retry_missing(manual=True)
        self.assertEqual(result["saved"], 1)
        self.assertEqual(db.get_post("demo0000")["status"], "kept")
        self.assertEqual(storage.archive_health()["missing"], 0)

    async def test_thumbnail_preview_matches_actual_download(self):
        db.set_status("demo0000", "kept")
        async with self.client() as c:
            preview = (await c.get("/api/download/options?scope=kept&thumb=1")).json()
            zipped = await c.get("/api/download/zip?scope=kept&thumb=1&subfolder=1")
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
            img_names = [n for n in archive.namelist() if n != "评论.txt"]
            self.assertEqual(len(img_names), preview["pics"])
            self.assertTrue(all("/" in name for name in img_names))
            self.assertEqual(
                archive.read(archive.namelist()[0]),
                (config.PICS_DIR / db.get_post("demo0000")["pics"][0]["thumb"]).read_bytes(),
            )
