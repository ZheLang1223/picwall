"""数据安全批次：真实交错状态、模拟进程中断与完全离线恢复。"""
import json
import shutil
from pathlib import Path
from unittest.mock import patch

from app import auth, backup, config, crawler, db, storage
from tests.test_api import ApiTestBase, _jpeg_bytes


class RecoveryTest(ApiTestBase):
    def test_restore_does_not_revive_previously_revoked_devices(self):
        paired = auth.exchange(auth.issue_code("admin"), "旧设备", "test")
        result = backup.create()
        with db._write_lock, db.conn() as c:
            c.execute("DELETE FROM device_sessions WHERE id=?", (paired["id"],))
        self.assertIsNone(auth.session(paired["token"]))
        destination = self.tmp / "offline-restore"
        restored = backup.restore(Path(result["batch"]), destination)
        self.assertEqual(restored["devices_reset"], 1)
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(destination / "picwall.db")) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM device_sessions").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM device_codes").fetchone()[0], 0)

    async def test_aba_old_undo_cannot_overwrite_other_device_pending(self):
        db.set_status("demo0000", "kept")
        async with self.client() as pc, self.client() as phone:
            old = (await pc.post("/api/posts/demo0000/status", json={"status": "pending"})).json()
            await phone.post("/api/posts/demo0000/status", json={"status": "discarded"})
            await phone.post("/api/posts/demo0000/status", json={"status": "pending"})
            r = await pc.post("/api/posts/demo0000/restore", json={"previous": old["previous"], "expected": old["decision"]})
            self.assertEqual(r.status_code, 409)
            self.assertEqual(db.get_post("demo0000")["status"], "pending")

    async def test_undo_refuses_client_forged_history_and_replay(self):
        async with self.client() as c:
            marked = (await c.post("/api/posts/demo0000/status", json={"status": "discarded"})).json()
            fake = {**marked["previous"], "kept_at": "bad"}
            self.assertEqual((await c.post("/api/posts/demo0000/restore", json={"previous": fake, "expected": marked["decision"]})).status_code, 409)
            restored = await c.post("/api/posts/demo0000/restore", json={"previous": marked["previous"], "expected": marked["decision"]})
            self.assertEqual(restored.status_code, 200)
            self.assertGreater(restored.json()["decision"]["decision_version"], marked["decision"]["decision_version"])
            self.assertEqual((await c.post("/api/posts/demo0000/restore", json={"previous": marked["previous"], "expected": marked["decision"]})).status_code, 409)

    async def test_interrupt_after_image_commit_restores_post_without_new_download(self):
        p = {**db.get_post("demo0000"), "sid": "interrupted", "avatar": "",
             "pics": [{"full": "https://test/one.jpg", "mid": "https://test/one.jpg"}]}
        original = db._insert_pic_locked

        def killed(*args):
            original(*args)
            raise RuntimeError("模拟进程退出")

        with patch.object(crawler, "_download", return_value=_jpeg_bytes()), patch.object(db, "_insert_pic_locked", side_effect=killed), self.assertRaises(RuntimeError):
            await crawler._store_post(None, p, "demo", {})
        self.assertFalse(db.has_post(p["sid"]))
        self.assertTrue(crawler.needs_images(p["sid"]))
        with patch.object(crawler, "_download", side_effect=AssertionError("已有图不应重抓")):
            self.assertEqual(await crawler._store_post(None, p, "demo", {}), 0)
        self.assertTrue(db.has_post(p["sid"]))
        self.assertFalse(crawler.needs_images(p["sid"]))

    def test_full_restore_without_live_media_preserves_historical_archive(self):
        p = db.get_post("demo0000")
        storage.export_report([p["pics"][0]["file"]], "2026-10-01")
        db.set_status(p["sid"], "discarded")
        result = backup.create()
        batch = Path(result["batch"])
        shutil.rmtree(config.PICS_DIR)
        shutil.rmtree(config.EXPORT_DIR)
        restored = backup.restore(batch, self.tmp / "empty-restore")
        self.assertEqual(restored["posts"], 8)
        self.assertEqual(restored["preexisting_missing"], 0)
        self.assertTrue((self.tmp / "empty-restore" / "export" / "2026-10-01" / Path(p["pics"][0]["file"]).name).is_file())

    def test_backup_independent_of_in_place_live_corruption_and_deduplicated(self):
        one = backup.create()
        blobs = config.DATA_DIR / "backups" / "blobs"
        media = config.PICS_DIR / db.get_post("demo0000")["pics"][0]["file"]
        sha = backup.digest(media)
        before = len(list(blobs.iterdir()))
        backup.create()
        self.assertEqual(len(list(blobs.iterdir())), before)
        media.write_bytes(b"corrupted")
        self.assertEqual(backup.digest(blobs / sha), sha)
        self.assertEqual(backup.restore(Path(one["batch"]), self.tmp / "restored")["integrity"], "ok")

    def test_restore_refuses_corrupt_blob_and_nonempty_target(self):
        result = backup.create()
        batch = Path(result["batch"])
        with self.assertRaises(ValueError):
            backup.restore(batch, config.DATA_DIR)
        manifest = json.loads((batch / "manifest.json").read_text())
        entry = next(iter(manifest["files"].values()))
        (batch.parent / "blobs" / entry["sha256"]).write_bytes(b"bad")
        with self.assertRaises(ValueError):
            backup.restore(batch, self.tmp / "restored")
        self.assertFalse((self.tmp / "restored").exists())
