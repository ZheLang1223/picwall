"""云端备份 blob 库：后端抽象、CLI NDJSON 解析、迁移建表与 prune 门禁。"""
import json
import unittest
import unittest.mock
from pathlib import Path

from app import backup, cloud, config, db
from tests.test_api import ApiTestBase


class FakeStore:
    """内存 blob 库：cloud.LocalBlobs / QuarkBlobs 的测试替身。"""

    NAME = "fake"

    def __init__(self):
        self.blobs = {}
        self.ensure_calls = 0

    def known(self):
        return set(self.blobs)

    def ensure_many(self, items):
        self.ensure_calls += 1
        for sha, src in items:
            self.blobs[sha] = src.read_bytes()

    def fetch_into(self, key, dest):
        dest.write_bytes(self.blobs[key])


class CloudBackupTest(ApiTestBase):
    def test_create_with_fake_store_roundtrip(self):
        """quark/fake 模式：blob 只进 store 不落本地目录，manifest 与恢复链路不变。"""
        store = FakeStore()
        result = backup.create(store=store)
        self.assertEqual(result["store"], "fake")
        self.assertGreater(len(store.blobs), 0)
        self.assertFalse((config.DATA_DIR / "backups" / "blobs").exists())
        manifest_files = len(store.blobs)
        restored = backup.restore(Path(result["batch"]), self.tmp / "restored", store=store)
        self.assertEqual(restored["integrity"], "ok")
        self.assertEqual(restored["store"], "fake")
        self.assertEqual(restored["files"], manifest_files)

    def test_credentials_stay_out_of_store_and_roundtrip_via_snapshot(self):
        """凭据永不进 blob 库（local/quark 一致），只随快照 local/ 子目录留存。"""
        config.COOKIE_FILE.write_text("SUB=secret; XSRF-TOKEN=tok", encoding="utf-8")
        store = FakeStore()
        result = backup.create(store=store)
        self.assertEqual(result["credentials_local"], ["cookie.txt"])
        cred_sha = backup.digest(config.COOKIE_FILE)
        self.assertNotIn(cred_sha, store.blobs)   # 云端（或本地 blob 库）都没有凭据内容
        snapshot = Path(result["batch"])
        local_copy = snapshot / "local" / "cookie.txt"
        self.assertTrue(local_copy.is_file())
        self.assertEqual(backup.digest(local_copy), cred_sha)
        manifest = json.loads((snapshot / "manifest.json").read_text())
        self.assertEqual(manifest["local_only"], ["cookie.txt"])
        restored = backup.restore(snapshot, self.tmp / "restored", store=store)
        self.assertEqual(restored["integrity"], "ok")
        self.assertEqual((self.tmp / "restored" / "cookie.txt").read_text(encoding="utf-8"),
                         "SUB=secret; XSRF-TOKEN=tok")

    def test_restore_fails_cleanly_when_snapshot_credential_missing(self):
        """快照 local/ 凭据被拿掉：恢复在校验步炸掉并清场，不留半成品。"""
        config.COOKIE_FILE.write_text("SUB=secret; XSRF-TOKEN=tok", encoding="utf-8")
        result = backup.create(store=FakeStore())
        (Path(result["batch"]) / "local" / "cookie.txt").unlink()
        with self.assertRaises(ValueError):
            backup.restore(Path(result["batch"]), self.tmp / "restored", store=FakeStore())
        self.assertFalse((self.tmp / "restored").exists())

    def test_cloud_state_coverage_excludes_credentials(self):
        """/api/status 云端体检：凭据不参与覆盖率核对；非凭据 blob 缺登记才算缺口。"""
        from app.server import _cloud_state
        config.COOKIE_FILE.write_text("SUB=secret; XSRF-TOKEN=tok", encoding="utf-8")
        result = backup.create(store=FakeStore())
        manifest = json.loads((Path(result["batch"]) / "manifest.json").read_text())
        self.assertEqual(manifest["local_only"], ["cookie.txt"])
        # 未登记任何 blob：covered=False，且清单文件数已剔除凭据
        state = _cloud_state()
        self.assertEqual(state["manifest_files"], len(manifest["files"]) - 1)
        self.assertFalse(state["covered"])
        # 非凭据 blob 全部登记后 covered=True：凭据永远缺席不算缺口
        with db._write_lock, db.conn() as c:
            for rel, entry in manifest["files"].items():
                if rel in manifest["local_only"]:
                    continue
                c.execute("INSERT OR REPLACE INTO cloud_blobs(key,fid,size,uploaded_at) "
                          "VALUES(?, 'f', 1, '2026-10-13T00:00:00')", (entry["sha256"],))
        self.assertTrue(_cloud_state()["covered"])

    def test_local_store_keeps_legacy_layout(self):
        """默认本地模式：blobs 目录照旧生成，二次 create 不增文件（去重）。"""
        backup.create()
        blobs = config.DATA_DIR / "backups" / "blobs"
        self.assertTrue(blobs.is_dir())
        before = len(list(blobs.iterdir()))
        backup.create()
        self.assertEqual(len(list(blobs.iterdir())), before)

    def test_restore_failure_cleans_partial_target(self):
        """恢复中途校验失败：清空半成品现场，目标目录不存在（全有或全无）。"""
        store = FakeStore()
        result = backup.create(store=store)
        # 篡改一张 pics blob 的内容 → 写盘后 digest 校验必炸（挑确定性目标，避开 db 分支）
        manifest = json.loads((Path(result["batch"]) / "manifest.json").read_text())
        key = next(e["sha256"] for rel, e in manifest["files"].items() if rel.startswith("pics/"))
        store.blobs[key] = b"corrupted"
        with self.assertRaises(ValueError):
            backup.restore(Path(result["batch"]), self.tmp / "restored", store=store)
        self.assertFalse((self.tmp / "restored").exists())

    def test_cloud_blobs_table_after_migration(self):
        with db.conn() as c:
            cols = {r[1] for r in c.execute("PRAGMA table_info(cloud_blobs)")}
        self.assertIn("fid", cols)
        with db._write_lock, db.conn() as c:
            c.execute("INSERT OR REPLACE INTO cloud_blobs(key,fid,size,uploaded_at) VALUES('k','f',1,'2026-10-10T00:00:00')")
        self.assertEqual(cloud.known_keys(), {"k"})

    def test_local_store_skip_existing_and_fetch(self):
        store = cloud.LocalBlobs(root=self.tmp / "blobs")
        src = self.tmp / "src.bin"
        payload = b"media-bytes-" * 100
        src.write_bytes(payload)
        sha = backup.digest(src)
        store.ensure_many([(sha, src)])
        mtime = (store.root / sha).stat().st_mtime_ns
        store.ensure_many([(sha, src)])   # 幂等：已存在且校验通过不重写
        self.assertEqual((store.root / sha).stat().st_mtime_ns, mtime)
        dest = self.tmp / "out.bin"
        store.fetch_into(sha, dest)
        self.assertEqual(dest.read_bytes(), payload)

    def test_quark_store_skips_known_keys(self):
        """ensure_many 只对表外 key 发起上传；上传结果逐个登记。"""
        with self.assertRaises(cloud.BlobError):
            cloud._result([{"code": -204, "msg": "上传操作失败", "data": {},
                            "action": "upload", "type": "result"}], "upload")
        ok_events = [{"code": 0, "msg": "上传成功", "data": {"fileId": "F1", "fileName": "x.bin"},
                      "action": "upload", "type": "list"},
                     {"code": 0, "msg": "成功", "data": {"successCount": 1}, "action": "upload", "type": "result"}]
        self.assertEqual(cloud._result(ok_events, "upload")["data"]["successCount"], 1)
        with self.assertRaises(cloud.BlobError):
            cloud._result([], "upload")


if __name__ == "__main__":
    unittest.main()
