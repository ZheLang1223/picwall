import asyncio
import errno
import io
import json
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import config, db, downloads, jobs, storage
from tests.test_api import ApiTestBase


class MaintenanceJobsTest(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.runner = jobs.Runner()
        self.runner_patch = patch.object(jobs, "runner", self.runner)
        self.runner_patch.start()

    def tearDown(self):
        self.runner_patch.stop()
        super().tearDown()

    async def terminal(self, kind):
        for _ in range(200):
            record = jobs.status(kind)
            if record["state"] not in jobs.ACTIVE:
                return record
            await asyncio.sleep(0.01)
        self.fail("任务未结束")

    async def test_acknowledges_once_and_finishes_after_client_disconnect(self):
        release = threading.Event()
        entered = threading.Event()
        def archive(**kwargs):
            entered.set()
            release.wait(3)
            kwargs["progress"](1, 1)
            return {"ok": True, "current_complete": True, "history": {"files": ["private"], "historical_missing": 3}}
        with patch.object(storage, "retry_archive", side_effect=archive) as work:
            try:
                async with self.client() as c:
                    start = time.monotonic()
                    response = await c.post("/api/archive/retry")
                    self.assertEqual(response.status_code, 202)
                    self.assertLess(time.monotonic() - start, 1)
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    current = (await c.get("/api/jobs/archive")).json()["job"]
                    self.assertEqual(current["state"], "running")
                    duplicate = (await c.post("/api/archive/retry")).json()["job"]
                    self.assertEqual(current["id"], duplicate["id"])
                self.assertEqual(work.call_count, 1)
            finally:
                release.set()
            final = await self.terminal("archive")
        self.assertEqual(final["state"], "complete")
        self.assertEqual(final["progress"], {"done": 1, "total": 1})
        self.assertEqual(final["result"]["history"], {"historical_missing": 3})
        self.assertTrue(final["finished_at"])

    async def test_restart_preserves_interruption_and_old_worker_cannot_replace_new(self):
        old = {"kind": "archive", "id": "old", "state": "running", "progress": {"done": 3, "total": 10}}
        db.set_meta("job:archive", json.dumps(old))
        jobs.recover_interrupted()
        self.assertEqual(jobs.status("archive")["state"], "interrupted")
        self.assertEqual(jobs.status("archive")["progress"]["done"], 3)
        async def work(cancelled, progress):
            return {"ok": True}
        new = self.runner.start("archive", work)
        jobs._update("archive", "old", state="complete", result={"bad": True})
        self.assertNotEqual(new["id"], "old")
        self.assertEqual(jobs.status("archive")["id"], new["id"])
        self.assertEqual((await self.terminal("archive"))["result"], {"ok": True})

    async def test_failure_is_durable_and_does_not_expose_exception_contents(self):
        async def work(cancelled, progress):
            raise OSError(errno.ENOSPC, "secret-cookie=private")
        self.runner.start("archive", work)
        final = await self.terminal("archive")
        self.assertEqual(final["state"], "failed")
        self.assertIn("空间不足", final["error"])
        self.assertNotIn("private", json.dumps(final))

    async def test_shutdown_stops_thread_between_files_and_records_interruption(self):
        entered, stopped = threading.Event(), threading.Event()
        def archive(cancelled, progress):
            entered.set()
            while not cancelled():
                time.sleep(0.01)
            stopped.set()
            raise InterruptedError()
        async def work(cancelled, progress):
            return await asyncio.to_thread(archive, cancelled, progress)
        self.runner.start("archive", work)
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        await self.runner.shutdown()
        self.assertTrue(stopped.is_set())
        self.assertEqual(jobs.status("archive")["state"], "interrupted")

    def test_current_and_historical_archive_are_checked_once(self):
        rel = db.get_post("demo0000")["pics"][0]["file"]
        db.set_status("demo0000", "kept")
        storage.export_report([rel], "2026-10-01")
        seen = []
        with patch.object(storage, "export_report", wraps=storage.export_report) as report:
            result = storage.retry_archive(progress=lambda done, total: seen.append((done,total)))
        self.assertEqual(report.call_count, 1)
        self.assertEqual(result["dup"], 1)
        self.assertEqual(seen, [(0,1), (1,1)])
        self.assertTrue(result["archive_complete"])

    def test_damaged_original_is_restored_from_valid_archive(self):
        rel = db.get_post("demo0000")["pics"][0]["file"]
        db.set_status("demo0000", "kept")
        storage.export_report([rel], "2026-10-01")
        src = config.PICS_DIR / rel
        original = src.read_bytes()
        src.write_bytes(b"x" * len(original))
        result = storage.retry_archive()
        self.assertEqual(result["restored_originals"], 1)
        self.assertTrue(result["archive_complete"])
        self.assertEqual(src.read_bytes(), original)

    def test_large_batch_does_not_hold_decision_lock_between_files(self):
        files = [p["file"] for post in (db.get_post("demo0000"), db.get_post("demo0001")) for p in post["pics"]]
        original = storage._export_one
        with ThreadPoolExecutor(max_workers=1) as pool:
            def export_one(rel, day):
                result = original(rel, day)
                # 同批归档继续下一张前，另一个线程仍可修改决定。
                pool.submit(db.set_favorite, "demo0000", True).result(timeout=1)
                return result
            with patch.object(storage, "_export_one", side_effect=export_one):
                result = storage.export_report(files, "2026-10-01")
        self.assertTrue(result["archive_complete"])
        self.assertTrue(db.get_post("demo0000")["favorite"])

    def test_damaged_original_cannot_replace_damaged_archive_or_hash_baseline(self):
        rel = db.get_post("demo0000")["pics"][0]["file"]
        db.set_status("demo0000", "kept")
        storage.export_report([rel], "2026-10-01")
        src = config.PICS_DIR / rel
        dst = config.EXPORT_DIR / "2026-10-01" / src.name
        baseline = storage.file_hash(src)
        src.write_bytes(b"wrong-source")
        dst.write_bytes(b"wrong-archive")
        result = storage.retry_archive()
        self.assertEqual(result["failed_count"], 1)
        self.assertFalse(result["archive_complete"])
        self.assertEqual(dst.read_bytes(), b"wrong-archive")
        self.assertEqual(result["history"]["corrupt_originals"], 1)
        with db.conn() as c:
            self.assertEqual(c.execute("SELECT sha256 FROM archive_entries WHERE file=?",(rel,)).fetchone()[0], baseline)


class DownloadZipJobTest(ApiTestBase):
    """批量打包任务化：POST 登记 job、轮询进度、zip-file 领取与交付后清理。"""

    def setUp(self):
        super().setUp()
        self.runner = jobs.Runner()
        self.runner_patch = patch.object(jobs, "runner", self.runner)
        self.runner_patch.start()

    def tearDown(self):
        self.runner_patch.stop()
        super().tearDown()

    async def terminal(self, kind):
        for _ in range(200):
            record = jobs.status(kind)
            if record["state"] not in jobs.ACTIVE:
                return record
            await asyncio.sleep(0.01)
        self.fail("任务未结束")

    async def test_download_zip_job_runs_and_delivers(self):
        db.set_status("demo0000", "kept")
        async with self.client() as c:
            r = await c.post("/api/download/zip", params={"scope": "kept"})
            self.assertEqual(r.status_code, 200)
            job = r.json()["job"]
            self.assertEqual(job["state"], "queued")
            self.assertEqual((await c.get("/api/jobs/download")).status_code, 200)
            final = await self.terminal("download")
        self.assertEqual(final["state"], "complete")
        self.assertGreater(final["result"]["entries"], 0)
        self.assertNotIn("file", final["result"])
        zip_path = downloads.result_file(final["id"])
        self.assertTrue(zip_path and os.path.exists(zip_path))
        self.assertEqual(final["progress"], {"done": final["result"]["entries"],
                                             "total": final["result"]["entries"]})
        async with self.client() as c:
            got = await c.get("/api/download/zip-file", params={"job": final["id"]})
            self.assertEqual(got.status_code, 200)
            self.assertEqual(got.headers["content-type"], "application/zip")
            names = zipfile.ZipFile(io.BytesIO(got.content)).namelist()
            self.assertTrue(any("demo0000" in n for n in names))
            # 交付完成后 BackgroundTask 已注销登记并删除临时文件
            self.assertIsNone(downloads.pop_result_file(final["id"]))
            self.assertFalse(os.path.exists(zip_path))

    async def test_zip_file_rejects_unknown_job(self):
        async with self.client() as c:
            r = await c.get("/api/download/zip-file", params={"job": "nope"})
            self.assertEqual(r.status_code, 404)

    async def test_zip_file_conflicts_while_job_incomplete(self):
        db.set_meta("job:download", json.dumps({"kind": "download", "id": "abc", "state": "running",
                                                "progress": {}, "result": None, "error": ""}))
        async with self.client() as c:
            r = await c.get("/api/download/zip-file", params={"job": "abc"})
            self.assertEqual(r.status_code, 409)

    async def test_download_zip_start_refuses_over_limit(self):
        with db.conn() as conn:
            conn.executemany(
                "INSERT INTO posts(sid,status,created_at,kept_at) VALUES(?,'kept',?, '2026-10-01T10:00:00+08:00')",
                [(f"cap{i}", "2026-10-01T10:00:00+08:00") for i in range(501)],
            )
        async with self.client() as c:
            self.assertEqual((await c.post("/api/download/zip", params={"scope": "kept"})).status_code, 400)
            # select 直传 501 个 sid：plan 里的上限校验同样拦在登记任务之前
            over = await c.post("/api/download/zip",
                                params={"select": ",".join(f"fake{i}" for i in range(501))})
            self.assertEqual(over.status_code, 400)
