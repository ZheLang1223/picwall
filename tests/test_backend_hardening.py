"""后端安全与性能回归测试：覆盖 B-S1 至 B-S10 与 B-P1 至 B-P6。"""
import asyncio
import base64
import hashlib
import io
import json
import os
import stat
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image
from starlette.middleware.base import BaseHTTPMiddleware

from app import (
    auth,
    backup,
    cloud,
    config,
    connections,
    db,
    downloads,
    jobs,
    maintenance,
    purge,
    serve,
    server,
    storage,
)
from tests.test_api import ApiTestBase


def _make_png(width: int = 8, height: int = 8) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (20, 40, 80)).save(buf, format="PNG")
    return buf.getvalue()


class ServeHardeningTest(unittest.TestCase):
    def test_proxy_headers_disabled_on_all_listeners(self):
        for in_container in (False, True):
            for cfg in serve.server_configs(in_container):
                self.assertFalse(cfg.proxy_headers)


class PairingRateLimitTest(ApiTestBase):
    def test_window_resets_on_failure_and_decouples_global_dos(self):
        t0 = 1_700_000_000.0
        with patch.object(auth.time, "time", return_value=t0):
            for _ in range(9):
                self.assertEqual(auth.exchange("BADCODE", "d", "attacker")["status"], 400)
        # 500s later, 10th failure extends cooldown window from t0+500
        with patch.object(auth.time, "time", return_value=t0 + 500):
            self.assertEqual(auth.exchange("BADCODE", "d", "attacker")["status"], 400)
        # At t0+650 (>600s after 1st failure, <600s after 10th), attacker is STILL blocked
        with patch.object(auth.time, "time", return_value=t0 + 650):
            code = auth.issue_code("editor")
            self.assertEqual(auth.exchange(code, "d", "attacker")["status"], 429)

        # 15 other clients exhaust attempts (150 failures); clean client can still pair
        with patch.object(auth.time, "time", return_value=t0 + 660):
            for i in range(15):
                for _ in range(10):
                    auth.exchange("WRONG", "bot", f"bot-{i}")
            valid = auth.issue_code("editor")
            ok = auth.exchange(valid, "legit", "legit-client")
            self.assertIn("token", ok)
            self.assertEqual(ok["role"], "editor")

    def test_expired_sessions_pruned_on_issue_code_and_purge(self):
        now = time.time()
        with db._write_lock, db.conn() as c:
            c.execute("INSERT INTO device_sessions VALUES(?,?,?,?,?,?)",
                      ("expired-1", auth.hashed("t1"), "viewer", "old", now - 200, now - 10))
            c.execute("INSERT INTO device_sessions VALUES(?,?,?,?,?,?)",
                      ("active-1", auth.hashed("t2"), "viewer", "new", now, now + 3600))
        auth.issue_code("viewer")
        with db.conn() as c:
            ids = {r[0] for r in c.execute("SELECT id FROM device_sessions").fetchall()}
        self.assertEqual(ids, {"active-1"})

        with db._write_lock, db.conn() as c:
            c.execute("INSERT INTO device_sessions VALUES(?,?,?,?,?,?)",
                      ("expired-2", auth.hashed("t3"), "viewer", "old2", now - 200, now - 5))
        with patch.object(purge, "purge_expired", return_value={"removed_posts": 0, "removed_files": 0}), \
             patch.object(purge, "retry_files", return_value={"removed": 0, "remaining": 0}):
            purge.run()
        with db.conn() as c:
            ids = {r[0] for r in c.execute("SELECT id FROM device_sessions").fetchall()}
        self.assertEqual(ids, {"active-1"})


class RequestBudgetAndBoundaryTest(ApiTestBase):
    def raw_client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                 base_url="http://localhost")

    def pair_headers(self, role: str) -> dict:
        res = auth.exchange(auth.issue_code(role), f"{role}-dev", f"test-{role}")
        return {"Authorization": f"Bearer {res['token']}", "Origin": "http://localhost"}

    async def test_pre_auth_rejects_unauth_write_before_reading_body(self):
        chunks_pulled = 0

        async def huge_chunks():
            nonlocal chunks_pulled
            for _ in range(5):
                chunks_pulled += 1
                yield b"x" * (1024 * 1024)

        async with self.raw_client() as c:
            r = await c.post("/api/ingest/harvest", content=huge_chunks(),
                             headers={"Origin": "https://weibo.com", "Content-Type": "application/json"})
        self.assertEqual(r.status_code, 403)
        self.assertLessEqual(chunks_pulled, 1)

    async def test_disallowed_host_and_oversized_content_length_rejected_early(self):
        chunks_pulled = 0

        async def chunks():
            nonlocal chunks_pulled
            chunks_pulled += 1
            yield b"{}"

        async with self.raw_client() as c:
            bad_host = await c.post("/api/device/pair", content=chunks(),
                                    headers={"Host": "evil.example", "Origin": "http://evil.example"})
            self.assertEqual(bad_host.status_code, 403)
            self.assertEqual(chunks_pulled, 0)

            admin_h = self.pair_headers("admin")
            too_big = await c.post("/api/posts/bulk-tags", content=b"{}",
                                   headers=admin_h | {"Content-Length": str(auth.MAX_REQUEST_BYTES + 1)})
            self.assertEqual(too_big.status_code, 413)

    async def test_duplicate_security_headers_rejected_with_400(self):
        admin_h = self.pair_headers("admin")
        seen = []

        async def fake_receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def capture_send(msg):
            seen.append(msg)

        scope = {
            "type": "http", "method": "POST", "scheme": "http", "path": "/api/posts/bulk-status",
            "query_string": b"", "root_path": "", "server": ("localhost", 80),
            "headers": [
                (b"host", b"localhost"),
                (b"origin", b"http://localhost"),
                (b"origin", b"https://weibo.com"),
                (b"authorization", admin_h["Authorization"].encode()),
            ],
        }
        await self.server.app(scope, fake_receive, capture_send)
        self.assertEqual(seen[0]["status"], 400)

        # Also check GET duplicate Host/Origin on device_boundary directly
        seen.clear()
        get_scope = {
            "type": "http", "method": "GET", "scheme": "http", "path": "/api/health",
            "query_string": b"", "root_path": "", "server": ("localhost", 80),
            "headers": [(b"host", b"localhost"), (b"host", b"evil.test")],
        }
        await self.server.app(get_scope, fake_receive, capture_send)
        self.assertEqual(seen[0]["status"], 400)

    async def test_slow_body_triggers_408_timeout(self):
        admin_h = self.pair_headers("admin")

        async def slow_chunks():
            yield b'{"a":'
            await asyncio.sleep(0.2)
            yield b"1}"

        with patch.object(auth, "REQUEST_BODY_TIMEOUT", 0.05):
            async with self.raw_client() as c:
                r = await c.post("/api/posts/bulk-tags", content=slow_chunks(), headers=admin_h)
        self.assertEqual(r.status_code, 408)


class AuthzAndEndpointHardeningTest(ApiTestBase):
    def role_client(self, role: str):
        res = auth.exchange(auth.issue_code(role), f"{role}-dev", f"role-{role}")
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.server.app),
            base_url="http://localhost",
            headers={"Authorization": f"Bearer {res['token']}", "Origin": "http://localhost"},
        )

    async def test_archive_downloads_require_admin_role(self):
        for role in ("editor", "viewer", "collector"):
            async with self.role_client(role) as c:
                for url in ("/api/download/zip?scope=kept",
                            "/api/download/zip-file?job=any",
                            "/api/export/zip?date=2026-09-01"):
                    r = await c.get(url)
                    self.assertEqual(r.status_code, 403, f"{role} on {url}")

    async def test_editor_can_tag_but_viewer_and_collector_cannot(self):
        async with self.role_client("editor") as c:
            r1 = await c.post("/api/posts/demo0000/tags", json={"add": ["风景"]})
            self.assertEqual(r1.status_code, 200)
            r2 = await c.post("/api/posts/bulk-tags", json={"sids": ["demo0000"], "add": ["风景"]})
            self.assertEqual(r2.status_code, 200)
        for role in ("viewer", "collector"):
            async with self.role_client(role) as c:
                self.assertEqual((await c.post("/api/posts/demo0000/tags", json={"add": ["x"]})).status_code, 403)
                self.assertEqual((await c.post("/api/posts/bulk-tags", json={"sids": ["demo0000"], "add": ["x"]})).status_code, 403)

    async def test_viewer_review_next_does_not_mutate_shown_state(self):
        async with self.client() as c:
            self.assertEqual((await c.post("/api/posts/demo0000/status", json={"status": "kept"})).status_code, 200)
        async with self.role_client("viewer") as c:
            r = await c.get("/api/review/next?limit=5")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["posts"])
        with db.conn() as c:
            total_shown = c.execute("SELECT COALESCE(SUM(shown_count),0) FROM posts").fetchone()[0]
            events = c.execute("SELECT COUNT(*) FROM review_events").fetchone()[0]
        self.assertEqual(total_shown, 0)
        self.assertEqual(events, 0)

    async def test_author_bio_validation_viewer_isolation_and_error_handling(self):
        db.set_meta("author_bio:12345", "已缓存简介")
        async with self.role_client("viewer") as c:
            # Invalid uids rejected
            for bad in ("123&extra=1", "x:bad$path", "x:bad..evil", "foo:bar:baz"):
                self.assertEqual((await c.get(f"/api/authors/{bad}/bio")).status_code, 400, bad)
            # Cached hit succeeds for viewer
            cached = await c.get("/api/authors/12345/bio")
            self.assertEqual(cached.status_code, 200)
            self.assertEqual(cached.json()["bio"], "已缓存简介")
            # Cache miss blocked for viewer without hitting external network
            with patch.object(server.weibo, "profile_bio", side_effect=AssertionError("must not fetch")):
                miss = await c.get("/api/authors/999999/bio")
                self.assertEqual(miss.status_code, 403)
                miss_x = await c.get("/api/authors/x:999999/bio")
                self.assertEqual(miss_x.status_code, 403)

        # Network errors from XGraphQL/httpx return 502 and scrub proxy credentials
        with patch.object(db, "x_handle_for_uid", return_value="handle555"), \
             patch.object(config, "get_x_auth", return_value="auth_tok"), \
             patch.object(config, "X_PROXY_URL", "http://user:secretpass@proxy.local:7897"), \
             patch.object(server.xapi.XGraphQL, "user_profile",
                          side_effect=httpx.ConnectError("failed via http://user:secretpass@proxy.local:7897")):
            async with self.client() as c:
                r = await c.get("/api/authors/x:555/bio")
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("secretpass", r.text)

    async def test_posts_cursor_and_error_redaction(self):
        async with self.client() as c:
            bad_cursors = [
                base64.urlsafe_b64encode(b"123").decode(),
                base64.urlsafe_b64encode(b'["onlyone"]').decode(),
                base64.urlsafe_b64encode(b'["a","b","c"]').decode(),
                base64.urlsafe_b64encode(b'["a","b","c"]').decode(),
                "A" * 600,
                "not-base64!!!",
            ]
            for cur in bad_cursors:
                self.assertEqual((await c.get("/api/posts", params={"cursor": cur})).status_code, 400)
            for cur in (base64.urlsafe_b64encode(b'[true,"2026-01-01","s1"]').decode(),
                        base64.urlsafe_b64encode(json.dumps([2**70, "2026-01-01", "s1"]).encode()).decode()):
                self.assertEqual((await c.get("/api/posts", params={"sort": "count", "cursor": cur})).status_code, 400)

            with patch.object(server.crawler, "discover_groups",
                              side_effect=RuntimeError("boom http://u:mysecret@proxy.local:9999")):
                d = await c.post("/api/groups/discover")
                self.assertEqual(d.status_code, 502)
                self.assertNotIn("mysecret", d.text)

            with patch.object(server.x_source, "normalize_tweets",
                              side_effect=RuntimeError("db error at /secret/internal/path http://a:topsecret@h")):
                x = await c.post("/api/ingest/x",
                                 json={"list_id": "x:1", "name": "L", "tweets": [{"id": "1"}]})
                self.assertEqual(x.status_code, 502)
                self.assertNotIn("topsecret", x.text)
                self.assertNotIn("/secret/internal/path", x.text)
        err_meta = db.get_meta("x_last_error")
        self.assertNotIn("topsecret", err_meta)
        self.assertNotIn("/secret/internal/path", err_meta)

    async def test_security_headers_csp_and_concurrent_zip_download_claim(self):
        thumb = db.get_post("demo0000")["pics"][0]["thumb"]
        async with self.client() as c:
            home = await c.get("/")
            self.assertEqual(home.headers.get("referrer-policy"), "no-referrer")
            csp = home.headers.get("content-security-policy", "")
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertIn("script-src 'self'", csp)
            med = await c.get(f"/media/{thumb}")
            self.assertEqual(med.headers.get("referrer-policy"), "no-referrer")
            self.assertEqual(med.headers.get("content-security-policy"), "default-src 'none'; sandbox")

        # Concurrent zip-file claim cannot double-stream or race cleanup
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        Path(tmp).write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        job_id = "racejob1"
        downloads._RESULT_FILES[job_id] = {"path": tmp, "ts": time.monotonic()}
        try:
            with patch.object(jobs, "status", return_value={"id": job_id, "state": "complete", "result": {"file": tmp}}):
                server._ZIP_CLAIMED.add(job_id)
                try:
                    async with self.client() as c:
                        busy = await c.get("/api/download/zip-file", params={"job": job_id})
                        self.assertEqual(busy.status_code, 409)
                        self.assertTrue(os.path.exists(tmp))
                finally:
                    server._ZIP_CLAIMED.discard(job_id)

                async with self.client() as c:
                    ok = await c.get("/api/download/zip-file", params={"job": job_id})
                    self.assertEqual(ok.status_code, 200)
                    self.assertFalse(os.path.exists(tmp))
                    after = await c.get("/api/download/zip-file", params={"job": job_id})
                    self.assertEqual(after.status_code, 404)
        finally:
            downloads.pop_result_file(job_id)
            server._ZIP_CLAIMED.discard(job_id)
            Path(tmp).unlink(missing_ok=True)


class CloudStorageBackupHardeningTest(ApiTestBase):
    def test_quark_upload_avoids_filename_collisions_and_isolates_env(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            d1, d2 = root / "2026-01-01", root / "2026-01-02"
            d1.mkdir()
            d2.mkdir()
            f1 = d1 / "评论.txt"
            f2 = d2 / "评论.txt"
            f1.write_text("day one", encoding="utf-8")
            f2.write_text("day two", encoding="utf-8")
            s1 = hashlib.sha256(f1.read_bytes()).hexdigest()
            s2 = hashlib.sha256(f2.read_bytes()).hexdigest()

            calls = []

            def fake_upload(paths, parent_fid):
                names = [p.name for p in paths]
                calls.append(names)
                return {name: f"fid-{name[:8]}" for name in names}

            with patch.object(cloud, "upload_files", side_effect=fake_upload):
                store = cloud.QuarkBlobs(dir_fid="FDIR", batch=10)
                store.ensure_many([(s1, f1), (s2, f2), (s1, f1)])

            self.assertEqual(len(calls), 1)
            self.assertEqual(set(calls[0]), {s1, s2})
            self.assertEqual(cloud.known_keys(), {s1, s2})

    def test_cli_events_does_not_leak_secrets_and_read_blob_blocks_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            cli = Path(d) / "cli.js"
            cli.write_text("console.log('{}')", encoding="utf-8")
            seen_env = {}

            def fake_run(cmd, **kw):
                seen_env.update(kw["env"])
                return type("P", (), {"stdout": '{"type":"result","code":0,"data":{}}\n'})()

            with patch.object(config, "QUARK_CLI", cli), \
                 patch.dict(os.environ, {"WB_COOKIE": "secret_wb", "PWALL_PAIR_CODE": "secret_code", "PATH": "/usr/bin"}), \
                 patch.object(cloud.subprocess, "run", side_effect=fake_run), \
                 patch.object(cloud.shutil, "which", return_value="/usr/local/bin/node"):
                cloud._cli_events(["ls"], timeout=10)
            self.assertNotIn("WB_COOKIE", seen_env)
            self.assertNotIn("PWALL_PAIR_CODE", seen_env)
            self.assertEqual(seen_env.get("CLAUDECODE"), "1")

            outdir = Path(d) / "work" / "dl"
            outdir.mkdir(parents=True)
            outside = Path(d) / "secret.txt"
            outside.write_text("nope")
            link = outdir / "evil.txt"
            link.symlink_to(outside)
            with (patch.object(cloud, "_cli_events",
                               return_value=[{"type": "result", "code": 0, "data": {"filePath": str(link)}}]),
                  self.assertRaises(cloud.BlobError)):
                cloud.read_blob("FID1", Path(d) / "work", retries=1)

    def test_storage_and_backup_path_symlink_and_bomb_checks(self):
        with tempfile.TemporaryDirectory() as d:
            outside = Path(d) / "outside.jpg"
            outside.write_bytes(b"secret")
            sym = config.PICS_DIR / "2026" / "sym.jpg"
            sym.parent.mkdir(parents=True, exist_ok=True)
            sym.symlink_to(outside)
            for bad in ("../outside.jpg", "/etc/passwd", "2026\\sym.jpg", "2026/sym.jpg", ""):
                with self.assertRaises(ValueError, msg=bad):
                    storage.resolve_pic(bad)

        with (patch.object(Image, "open", side_effect=Image.DecompressionBombError("bomb")),
              self.assertRaises(ValueError)):
            storage.save_image(_make_png(), "https://example.com/a.png")

        with self.assertRaises(ValueError):
            storage.rebuild_day_notes("../escape")

        # Backup skips symlinked credentials and locks .db to 0o600
        config.COOKIE_FILE.write_text("WB=1")
        link_x = config.DATA_DIR / "x_auth.txt"
        with patch.object(config, "X_AUTH_FILE", link_x), tempfile.TemporaryDirectory() as d:
            target = Path(d) / "stolen"
            target.write_text("xsecret")
            link_x.symlink_to(target)
            snap = backup.create(keep=2, store=cloud.LocalBlobs(Path(d) / "blobs"))
            snap_dir = Path(snap["batch"])
            self.assertNotIn("x_auth.txt", snap["credentials_local"])
            db_file = Path(snap["batch"].removesuffix(".snapshot") + ".db")
            self.assertEqual(stat.S_IMODE(db_file.stat().st_mode), 0o600)

            # Restore rejects backslash/empty rel
            manifest_path = snap_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"]["pics\\evil"] = {"sha256": "0" * 64, "size": 1}
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                backup.restore(snap_dir, Path(d) / "restored", store=cloud.LocalBlobs(Path(d) / "blobs"))

    async def test_export_zip_skips_symlinks(self):
        day = "2026-09-01"
        day_dir = config.EXPORT_DIR / day
        day_dir.mkdir(parents=True, exist_ok=True)
        good = day_dir / "good.txt"
        good.write_text("ok")
        secret = config.DATA_DIR / "host_secret.txt"
        secret.write_text("secret-data")
        (day_dir / "leak.txt").symlink_to(secret)
        async with self.client() as c:
            r = await c.get("/api/export/zip", params={"date": day})
        self.assertEqual(r.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            self.assertEqual(z.namelist(), ["good.txt"])


class BackendPerformanceWave2Test(ApiTestBase):
    def raw_client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                 base_url="http://localhost")

    def pair_headers(self, role: str) -> tuple[str, dict]:
        res = auth.exchange(auth.issue_code(role), f"{role}-dev", f"test-{role}")
        return res["id"], {"Authorization": f"Bearer {res['token']}", "Origin": "http://localhost"}

    def test_maintenance_repair_local_skips_known_hashes_and_double_health_scan(self):
        first = maintenance.repair_local()
        self.assertGreaterEqual(first["registered"], 0)
        with patch.object(hashlib, "sha256", wraps=hashlib.sha256) as sha_spy, \
             patch.object(storage, "archive_health", wraps=storage.archive_health) as health_spy:
            second = maintenance.repair_local()
        self.assertEqual(second["registered"], 0)
        self.assertEqual(sha_spy.call_count, 0)
        self.assertEqual(health_spy.call_count, 1)

        with patch.object(hashlib, "sha256", wraps=hashlib.sha256) as deep_sha:
            maintenance.repair_local(deep=True)
        self.assertGreater(deep_sha.call_count, 0)

    def test_local_blobs_ensure_many_fast_skip_and_verify_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "sample.bin"
            src.write_bytes(b"AAAA-sample-payload")
            digest = hashlib.sha256(src.read_bytes()).hexdigest()
            store = cloud.LocalBlobs(root / "blobs")
            store.ensure_many([(digest, src)])

            blob_path = root / "blobs" / digest
            blob_path.write_bytes(b"BBBB-same-size-data")
            self.assertEqual(blob_path.stat().st_size, src.stat().st_size)

            # Default verify=False skips re-hashing when blob exists with identical byte size.
            store.ensure_many([(digest, src)])
            self.assertEqual(blob_path.read_bytes(), b"BBBB-same-size-data")

            # verify=True hashes and repairs same-size silent corruption.
            store.ensure_many([(digest, src)], verify=True)
            self.assertEqual(blob_path.read_bytes(), b"AAAA-sample-payload")

    async def test_connections_probe_proxy_and_batched_record(self):
        captured: dict = {}

        class _FakeClient:
            def __init__(self, **kw):
                captured.update(kw)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url, headers=None):
                return httpx.Response(200, json={"data": {"viewer": {}}})

        # X_AUTH_FILE 在 import 时按真实 DATA_DIR 算出，只重定向 DATA_DIR 挡不住
        # save_x_auth 落盘：凭据文件必须一并指进临时目录，免得覆盖宿主登录态。
        with patch.object(config, "X_AUTH_FILE", self.tmp / "x_auth.txt"):
            with patch.object(config, "X_PROXY_URL", "http://127.0.0.1:18888"), \
                 patch.object(connections.httpx, "AsyncClient", _FakeClient):
                res = await connections.probe("x", "tok" * 10, "csrf")
            self.assertEqual(captured.get("proxy"), "http://127.0.0.1:18888")
            self.assertIn(res["state"], ("ok", "saved", "expired", "error"))

            config.save_x_auth("tok" * 10, "csrf")
            connections.record("x", "tok" * 10, "ok", updated=True)
            st = connections.status("x")
        self.assertEqual(st["state"], "ok")
        self.assertTrue(st["checked_at"])
        self.assertTrue(st["updated_at"])

    async def test_session_cache_hits_and_invalidates_on_logout_revoke_and_purge(self):
        auth.invalidate_session_cache()
        res1 = auth.exchange(auth.issue_code("editor"), "dev-1", "10.0.0.1")
        tok1 = res1["token"]
        self.assertIsNotNone(auth.session(tok1))
        with patch.object(db, "conn", wraps=db.conn) as conn_spy:
            cached = auth.session(tok1)
        self.assertIsNotNone(cached)
        self.assertEqual(conn_spy.call_count, 0)

        # Self logout invalidates cache immediately
        async with self.raw_client() as c:
            h1 = {"Authorization": f"Bearer {tok1}", "Origin": "http://localhost"}
            self.assertEqual((await c.delete("/api/device/session", headers=h1)).status_code, 200)
            self.assertEqual((await c.get("/api/device/session", headers=h1)).status_code, 401)

        # Admin revoke invalidates target session immediately
        _, admin_h = self.pair_headers("admin")
        dev2_id, dev2_h = self.pair_headers("viewer")
        async with self.raw_client() as c:
            self.assertEqual((await c.get("/api/device/session", headers=dev2_h)).status_code, 200)
            self.assertEqual((await c.delete(f"/api/devices/{dev2_id}", headers=admin_h)).status_code, 200)
            self.assertEqual((await c.get("/api/device/session", headers=dev2_h)).status_code, 401)

        # purge.run() clears expired sessions and invalidates session cache
        dev3_id, dev3_h = self.pair_headers("viewer")
        async with self.raw_client() as c:
            self.assertEqual((await c.get("/api/device/session", headers=dev3_h)).status_code, 200)
        with db._write_lock, db.conn() as c:
            c.execute("UPDATE device_sessions SET expires=? WHERE id=?", (time.time() - 5, dev3_id))
        with patch.object(purge, "purge_expired", return_value={"removed_posts": 0, "removed_files": 0}), \
             patch.object(purge, "retry_files", return_value={"removed": 0, "remaining": 0}):
            purge.run()
        self.assertIsNone(auth.session(dev3_h["Authorization"][7:]))

    async def test_pure_asgi_device_boundary_headers_and_cors(self):
        self.assertFalse(any(m.cls is BaseHTTPMiddleware for m in server.app.user_middleware))
        self.assertEqual(server.app.user_middleware[0].cls, auth.RequestBudget)
        self.assertEqual(server.app.user_middleware[1].cls, server.DeviceBoundary)

        thumb = db.get_post("demo0000")["pics"][0]["thumb"]
        _, viewer_h = self.pair_headers("viewer")
        async with self.raw_client() as c:
            unauth = await c.get("/api/status")
            self.assertEqual(unauth.status_code, 401)

            dup = await c.get("/api/health", headers=[("host", "localhost"), ("host", "localhost")])
            self.assertEqual(dup.status_code, 400)

            ro = await c.post("/api/posts/demo0000/status", json={"status": "kept"}, headers=viewer_h)
            self.assertEqual(ro.status_code, 403)

            opts = await c.options("/api/ingest/x", headers={"Origin": "https://x.com"})
            self.assertEqual(opts.status_code, 204)
            self.assertEqual(opts.headers.get("access-control-allow-origin"), "https://x.com")

            med = await c.get(f"/media/{thumb}", headers=viewer_h)
            self.assertEqual(med.status_code, 200)
            self.assertEqual(med.headers.get("cache-control"), "no-store")
            self.assertEqual(med.headers.get("referrer-policy"), "no-referrer")
            self.assertEqual(med.headers.get("x-content-type-options"), "nosniff")
            self.assertEqual(med.headers.get("x-frame-options"), "DENY")
            self.assertEqual(med.headers.get("content-security-policy"), "default-src 'none'; sandbox")

    async def test_status_post_groups_counts_and_cloud_manifest_cache(self):
        with db._write_lock, db.conn() as c:
            c.execute("INSERT OR IGNORE INTO groups(list_id, name, active, added_at) VALUES(?,?,?,?)",
                      ("xg:custom1", "自定义组", 1, "2026-11-15T00:00:00"))
            c.execute("INSERT OR IGNORE INTO post_groups(sid, list_id) VALUES(?,?)",
                      ("demo0000", "xg:custom1"))
            c.execute(
                "INSERT INTO image_jobs(sid, list_id, payload) VALUES(?,?,?)",
                ("demo0001", "demo", "{}"),
            )

        async with self.client() as c:
            st = (await c.get("/api/status")).json()
            by_id = {g["list_id"]: g for g in st["groups"]}
            self.assertEqual(by_id["xg:custom1"]["total"], 1)
            self.assertEqual(by_id["xg:custom1"]["pending"], 1)

            posts_res = (await c.get("/api/posts", params={"status": "pending", "limit": 50})).json()
            complete_by_sid = {p["sid"]: p["image_complete"] for p in posts_res["posts"]}
            self.assertTrue(complete_by_sid["demo0000"])
            self.assertFalse(complete_by_sid["demo0001"])

        # Cloud manifest parse cache: reuses manifest across DB blob updates
        snap_dir = config.DATA_DIR / "backups" / "picwall-20261115.snapshot"
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / "manifest.json").write_text(
            json.dumps({"blob_store": "local", "files": {"pics/a.jpg": {"sha256": "abc123"}}})
        )
        server._CLOUD_MANIFEST_CACHE.clear()
        with patch.object(server.json, "loads", wraps=server.json.loads) as loads_spy:
            first_cloud = server._cloud_state()
            second_cloud = server._cloud_state()
            self.assertFalse(first_cloud["covered"])
            self.assertFalse(second_cloud["covered"])
            self.assertEqual(loads_spy.call_count, 1)

            with db._write_lock, db.conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO cloud_blobs(key, fid, size, uploaded_at) VALUES(?,?,?,?)",
                    ("abc123", "fid-1", 1, "2026-11-15T00:00:00"),
                )
            covered_cloud = server._cloud_state()
            self.assertTrue(covered_cloud["covered"])
            self.assertEqual(loads_spy.call_count, 1)

    async def test_shuffle_cache_reuse_and_mutation_invalidation(self):
        server._invalidate_read_caches()
        async with self.client() as c:
            with patch.object(db, "shuffled_sids", wraps=db.shuffled_sids) as shuf_spy:
                p1 = (await c.get("/api/posts", params={"status": "pending", "shuffle": "seed42", "limit": 1})).json()
                self.assertEqual(len(p1["posts"]), 1)
                self.assertTrue(p1["next_cursor"])
                p2 = (await c.get("/api/posts", params={
                    "status": "pending", "shuffle": "seed42", "limit": 1, "cursor": p1["next_cursor"]
                })).json()
                self.assertEqual(len(p2["posts"]), 1)
                self.assertEqual(shuf_spy.call_count, 1)

                sid = p1["posts"][0]["sid"]
                self.assertEqual((await c.post(f"/api/posts/{sid}/status", json={"status": "kept"})).status_code, 200)
                await c.get("/api/posts", params={"status": "pending", "shuffle": "seed42", "limit": 1})
                self.assertEqual(shuf_spy.call_count, 2)

    async def test_app_tmp_dir_sweep_and_export_zip_staging(self):
        tmp_dir = server._app_tmp_dir()
        self.assertEqual(tmp_dir, config.DATA_DIR / ".tmp")
        self.assertEqual(stat.S_IMODE(tmp_dir.stat().st_mode), 0o700)
        leftover = tmp_dir / "stale-leftover.zip"
        leftover.write_bytes(b"stale")

        async def _noop_async(*args, **kwargs):
            return None

        with patch.object(server.sched, "start", return_value=None), \
             patch.object(server.sched, "add_job", return_value=None), \
             patch.object(server.sched, "shutdown", return_value=None), \
             patch.object(jobs.runner, "shutdown", side_effect=_noop_async):
            async with server.lifespan(server.app):
                self.assertFalse(leftover.exists())

        day = "2026-09-02"
        day_dir = config.EXPORT_DIR / day
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "pic.txt").write_text("ok")
        seen_dirs: list = []
        orig_mkstemp = tempfile.mkstemp

        def spy_mkstemp(*args, **kwargs):
            seen_dirs.append(kwargs.get("dir"))
            return orig_mkstemp(*args, **kwargs)

        async with self.client() as c:
            with patch.object(server.tempfile, "mkstemp", side_effect=spy_mkstemp):
                r = await c.get("/api/export/zip", params={"date": day})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen_dirs, [tmp_dir])
        self.assertEqual(list(tmp_dir.glob("*.zip")), [])

    async def test_request_budget_security_headers_and_bg_cache_invalidation(self):
        _, viewer_h = self.pair_headers("viewer")
        _, admin_h = self.pair_headers("admin")
        async with self.raw_client() as c:
            r401 = await c.post("/api/posts/demo0000/status", json={"status": "kept"},
                                headers={"Origin": "http://localhost"})
            r403 = await c.post("/api/posts/demo0000/status", json={"status": "kept"}, headers=viewer_h)
            r413 = await c.post("/api/posts/demo0000/status",
                                headers={**admin_h, "Content-Length": str(auth.MAX_REQUEST_BYTES + 1)})
            for resp, code in ((r401, 401), (r403, 403), (r413, 413)):
                self.assertEqual(resp.status_code, code)
                self.assertEqual(resp.headers.get("cache-control"), "no-store")
                self.assertEqual(resp.headers.get("x-content-type-options"), "nosniff")
                self.assertEqual(resp.headers.get("referrer-policy"), "no-referrer")

        async def _ok_async(*args, **kwargs):
            return {"ok": True}

        epoch0 = server._read_epoch()
        with patch.object(server.crawler, "crawl", side_effect=_ok_async), \
             patch.object(server.crawler, "crawl_x", side_effect=_ok_async), \
             patch.object(server.crawler, "retry_missing", side_effect=_ok_async), \
             patch.object(server.crawler, "catch_up_if_stale", side_effect=_ok_async), \
             patch.object(server.purge, "run", return_value={}), \
             patch.object(server.maintenance, "repair_local", return_value={}):
            await server._run_crawl(trigger="manual")
            await server._crawl_x()
            await server._run_media_retry_job()
            await server._run_catch_up()
            server._run_purge_job()
            server._run_repair_job()
        self.assertGreaterEqual(server._read_epoch(), epoch0 + 6)

        with patch.object(server.crawler, "crawl", side_effect=RuntimeError("boom")), \
             self.assertRaises(RuntimeError):
            await server._run_crawl(trigger="manual")
        self.assertGreaterEqual(server._read_epoch(), epoch0 + 7)


if __name__ == "__main__":
    unittest.main()
