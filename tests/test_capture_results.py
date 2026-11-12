import asyncio
import copy
import json
from unittest.mock import AsyncMock, patch

import httpx

from app import config, crawler, db, downloads, weibo
from tests.test_api import ApiTestBase, _jpeg_bytes


class CaptureResultsTest(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.old_state = copy.deepcopy(crawler.state)
        crawler.state.update(running=False, progress=crawler._blank_progress())

    def tearDown(self):
        crawler.state.clear()
        crawler.state.update(self.old_state)
        super().tearDown()

    def post(self, sid="fresh"):
        return {**db.get_post("demo0000"), "sid": sid, "avatar": "",
                "pics": [{"full": "https://test/a.jpg", "mid": "https://test/a.jpg"}]}

    def common(self):
        return (patch.object(config, "get_cookie", return_value="fake"),
                patch.object(db, "active_groups", return_value=[{"list_id":"g1","name":"测试分组"}]),
                patch.object(crawler.asyncio, "sleep", new=AsyncMock()))

    def assert_partition(self, result):
        self.assertEqual(result["received"], sum(result[k] for k in ("accepted","skipped","failed","unprocessed")))

    async def test_network_error_after_first_page_preserves_received_and_committed_counts(self):
        db.set_meta("last_crawl", "old")
        cookie, groups, sleep = self.common()
        with (cookie, groups, sleep,
              patch.object(weibo,"group_timeline",new=AsyncMock(side_effect=[{},httpx.ReadTimeout("offline")])),
              patch.object(weibo,"extract_statuses",return_value=[{}]),
              patch.object(weibo,"parse_mblog",return_value=self.post()),
              patch.object(crawler,"_download",return_value=_jpeg_bytes())):
            result = await crawler.crawl()
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual((result["received"],result["accepted"],result["new_posts"]), (1,1,1))
        self.assert_partition(result)
        self.assertEqual(db.get_meta("last_crawl"), "old")
        self.assertFalse(result["coverage_complete"])

    async def test_page_limit_does_not_claim_full_success(self):
        db.set_meta("last_crawl", "old")
        cookie, groups, sleep = self.common()
        with (cookie,groups,sleep,patch.object(config,"CRAWL_BACKFILL_MAX_PAGES",1),
              patch.object(weibo,"group_timeline",new=AsyncMock(return_value={})),
              patch.object(weibo,"extract_statuses",return_value=[{}]),
              patch.object(weibo,"parse_mblog",return_value=self.post()),
              patch.object(crawler,"_download",return_value=_jpeg_bytes())):
            result = await crawler.crawl()
        self.assertEqual(result["outcome"], "incomplete")
        self.assertFalse(result["ok"])
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(db.get_meta("last_crawl"), "old")
        self.assertEqual(db.get_meta("group_seen:g1"), "")

    async def test_total_and_partial_image_failures_have_different_outcomes(self):
        for partial in (False,True):
            post=self.post("partial" if partial else "failed")
            if partial:
                post["pics"].append({"full":"https://test/b.jpg","mid":"https://test/b.jpg"})
            cookie,groups,sleep=self.common()
            with (cookie,groups,sleep,
                  patch.object(weibo,"group_timeline",new=AsyncMock(return_value={})),
                  patch.object(weibo,"extract_statuses",side_effect=[[{}],[]]),
                  patch.object(weibo,"parse_mblog",return_value=post),
                  patch.object(crawler,"_download",side_effect=[_jpeg_bytes(),None] if partial else [None])):
                result=await crawler.crawl()
            self.assertEqual(result["outcome"], "partial" if partial else "failed")
            self.assertEqual(result["failed"],1)
            self.assertEqual(result["partial"],int(partial))
            self.assertEqual(result["new_posts"],int(partial))
            self.assertEqual(result["accepted"],0)
            self.assert_partition(result)

    async def test_page_records_are_all_durable_before_first_download_and_cancel(self):
        entered=asyncio.Event()
        async def download(*args):
            entered.set()
            await asyncio.Event().wait()
        cookie,groups,sleep=self.common()
        with (cookie,groups,sleep,
              patch.object(weibo,"group_timeline",new=AsyncMock(return_value={})),
              patch.object(weibo,"extract_statuses",return_value=[{},{}]),
              patch.object(weibo,"parse_mblog",side_effect=[self.post("one"),self.post("two")]),
              patch.object(crawler,"_download",side_effect=download)):
            task=asyncio.create_task(crawler.crawl())
            try:
                await asyncio.wait_for(entered.wait(),2)
                with db.conn() as c:
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0],2)
                checkpoint=json.loads(db.get_meta("crawl_checkpoint"))
                self.assertEqual(checkpoint["groups"][0]["unprocessed"],2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        result=json.loads(db.get_meta("last_result"))
        self.assertEqual(result["outcome"],"interrupted")
        self.assertEqual(result["unprocessed"],2)
        self.assert_partition(result)
        with patch.object(crawler,"_download",return_value=_jpeg_bytes()):
            retry=await crawler.retry_missing(manual=True)
        self.assertEqual(retry["remaining"],0)
        self.assertTrue(db.has_post("one") and db.has_post("two"))

    async def test_browser_batch_counts_skips_and_does_not_replace_full_success_time(self):
        db.set_meta("last_crawl","old")
        with (patch.object(weibo,"parse_mblog",side_effect=[None,self.post(),self.post()]),
              patch.object(crawler,"_download",return_value=_jpeg_bytes())):
            async with self.client() as c:
                r=await c.post("/api/ingest/harvest",json={"list_id":"123","statuses":[{},{},{}]})
        result=r.json()
        self.assertEqual(result["outcome"],"success")
        self.assertEqual((result["received"],result["accepted"],result["skipped"]),(3,1,2))
        self.assert_partition(result)
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(db.get_meta("last_crawl"),"old")
        self.assertTrue(db.get_meta("last_ingest"))

    async def test_browser_batch_failure_keeps_unprocessed_records_for_retry(self):
        original=crawler._store_post
        async def store(client, post, list_id, avatars):
            if post["sid"] == "two":
                raise RuntimeError("interrupted")
            return await original(client, post, list_id, avatars)
        with (patch.object(weibo,"parse_mblog",side_effect=[self.post("one"),self.post("two")]),
              patch.object(crawler,"_store_post",side_effect=store),
              patch.object(crawler,"_download",return_value=_jpeg_bytes())):
            async with self.client() as c:
                result=(await c.post("/api/ingest/harvest",json={"list_id":"123","statuses":[{},{}]})).json()
        self.assertEqual(result["outcome"],"partial")
        self.assertEqual(result["unprocessed"],1)
        self.assertEqual(result["new_posts"],1)
        self.assert_partition(result)
        self.assertTrue(crawler.needs_images("two"))

    def test_restart_restores_finished_progress_and_marks_open_run_interrupted(self):
        progress={"state":"running","running":True,"attempt_id":"old","groups":[{"name":"test","received":1,"unprocessed":1}]}
        db.set_meta("crawl_checkpoint",json.dumps(progress))
        crawler.queue_posts([self.post()],"g1")
        crawler.recover_interrupted()
        self.assertEqual(crawler.state["progress"]["outcome"],"interrupted")
        self.assertEqual(json.loads(db.get_meta("last_result"))["unprocessed"],1)
        self.assertTrue(crawler.needs_images("fresh"))
        progress.update(state="complete",running=False,outcome="success")
        db.set_meta("crawl_checkpoint",json.dumps(progress))
        crawler.state["progress"]=crawler._blank_progress()
        crawler.recover_interrupted()
        self.assertEqual(crawler.state["progress"]["outcome"],"success")

    def test_known_pinned_old_post_does_not_stop_recent_page(self):
        dates=[{"created_at":"2020-01-01T00:00:00+08:00"},{"created_at":"2026-10-01T00:00:00+08:00"}]
        self.assertFalse(crawler.deep_stop(dates,0,"2026-09-01T00:00:00+08:00"))

    def test_download_quality_counts_follow_actual_deduplicated_files(self):
        with db.conn() as c:
            c.execute("UPDATE pics SET quality='mid' WHERE sid='demo0000'")
            c.execute("UPDATE pics SET quality='full' WHERE id=(SELECT MIN(id) FROM pics WHERE sid='demo0001')")
        result=downloads.preview("all")
        self.assertEqual(result["quality"]["mid"],1)
        self.assertEqual(result["quality"]["full"],1)
        self.assertEqual(sum(result["quality"].values()),result["pics"])
        thumbs=downloads.preview("all",thumb=True)
        self.assertEqual(thumbs["quality"]["thumbnail"],thumbs["pics"])
