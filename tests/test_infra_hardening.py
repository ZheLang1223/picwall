"""Infrastructure, Makefile, container, and CLI tooling hardening tests."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from app import cloud, config, db
from tools import (
    audit,
    backup_to_quark,
    benchmark,
    migrate_to_large,
    migrate_x_avatars,
    regen_thumbs,
    runtime,
    safari_x_cookie,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_jpeg_bytes(width: int = 800, height: int = 400) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 160, 200)).save(buf, "JPEG")
    return buf.getvalue()


class MakefileHardeningTest(unittest.TestCase):
    def test_makefile_wiring_and_guards(self):
        text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("export PICWALL_DATA_DIR ?= $(CURDIR)/data", text)
        self.assertIn("--noproxy 127.0.0.1,localhost", text)
        self.assertIn("--role '$(ROLE)'", text)
        self.assertIn("--synthetic", text)
        self.assertIn("$(ARGS) $(CLOUD_ARGS)", text)
        self.assertIn("rm -f app/static/extension/pwall-cookie-helper.zip", text)
        self.assertIn("tools/runtime.py needs-backup", text)
        self.assertIn("tools/runtime.py status", text)
        self.assertIn("PICWALL_BLOB_STORE", text)


class RuntimeGuardAndStatusTest(unittest.TestCase):
    def test_require_stopped_container_blocks_only_when_owned(self):
        with (
            mock.patch.object(runtime, "container_owns_data", return_value=True),
            self.assertRaises(RuntimeError),
        ):
            runtime.require_stopped_container("测试")
        with mock.patch.object(runtime, "container_owns_data", return_value=False):
            runtime.require_stopped_container("测试")

    def test_snapshot_freshness_ignores_blobs_and_reports_lag_and_disk(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            backups = data_dir / "backups"
            blobs = backups / "blobs"
            blobs.mkdir(parents=True)
            (blobs / ("deadbeef" * 8)).write_bytes(b"blob-data")
            # Fresh blobs/ directory without any picwall-*.snapshot must still need backup.
            self.assertTrue(runtime.snapshot_needs_backup(data_dir))
            lines = runtime.format_status(data_dir)
            self.assertTrue(any("暂无快照" in line for line in lines))
            self.assertTrue(any("夸克滞后：1 个本地 blob" in line for line in lines))

            snap = backups / "picwall-20261110-120000.snapshot"
            snap.mkdir()
            (snap / "manifest.json").write_text(json.dumps({"blob_store": "quark"}), encoding="utf-8")
            old_ts = time.time() - 75 * 3600
            os.utime(snap, (old_ts, old_ts))

            db_path = data_dir / "picwall.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE cloud_blobs (key TEXT PRIMARY KEY, fid TEXT, size INTEGER)")
                conn.execute(
                    "INSERT INTO cloud_blobs VALUES (?, 'fid1', 9)",
                    ("deadbeef" * 8,),
                )

            with mock.patch("tools.runtime.shutil.disk_usage", return_value=mock.Mock(free=5 * 1024**3)):
                lines = runtime.format_status(data_dir)
            joined = "\n".join(lines)
            self.assertIn("store=quark", joined)
            self.assertIn("严重过期 ≥72h", joined)
            self.assertNotIn("夸克滞后", joined)
            self.assertIn("磁盘警告：剩余 5.0 GiB", joined)


class MigrationAndThumbHardeningTest(unittest.TestCase):
    def test_migrate_x_avatars_importable_standalone(self):
        proc = subprocess.run(
            [sys.executable, "-c", "import tools.migrate_x_avatars as m; assert hasattr(m, 'main')"],
            cwd="/tmp",
            env={
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(REPO_ROOT),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        proc_direct = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import runpy, sys; sys.path.pop(0); "
                    f"g = runpy.run_path({str(REPO_ROOT / 'tools/migrate_x_avatars.py')!r}); "
                    "assert 'main' in g"
                ),
            ],
            cwd="/tmp",
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc_direct.returncode, 0, proc_direct.stderr)

    def test_migrate_x_avatars_blocks_live_container(self):
        with (
            mock.patch.object(runtime, "container_owns_data", return_value=True),
            self.assertRaises(RuntimeError),
        ):
            asyncio.run(migrate_x_avatars.main())

    def test_migrate_to_large_safety_checks(self):
        with tempfile.TemporaryDirectory() as td:
            pics_dir = Path(td) / "pics"
            pics_dir.mkdir()
            self.assertIsNone(migrate_to_large._safe_pic_path(pics_dir, ""))
            self.assertIsNone(migrate_to_large._safe_pic_path(pics_dir, "/etc/passwd"))
            self.assertIsNone(migrate_to_large._safe_pic_path(pics_dir, "../../etc/passwd"))
            self.assertIsNone(migrate_to_large._safe_pic_path(pics_dir, "."))
            ok = migrate_to_large._safe_pic_path(pics_dir, "2026/09/a.jpg")
            self.assertEqual(ok, (pics_dir / "2026/09/a.jpg").resolve())
            self.assertIsNone(
                migrate_to_large.fetch_large("http://127.0.0.1/evil.jpg", _make_jpeg_bytes(400, 300))
            )
            self.assertIsNone(
                migrate_to_large.fetch_large("https://evil.example/pic.jpg", _make_jpeg_bytes(400, 300))
            )

        with (
            mock.patch.object(runtime, "container_owns_data", return_value=True),
            self.assertRaises(RuntimeError),
        ):
            migrate_to_large.main()

    def test_regen_thumbs_path_guard_and_chunked_verify(self):
        with tempfile.TemporaryDirectory() as td:
            pics_dir = Path(td) / "pics"
            ym = pics_dir / "2026" / "09"
            ym.mkdir(parents=True)
            data = _make_jpeg_bytes(100, 100)
            stem = hashlib.sha1(data).hexdigest()[:16]
            good_file = ym / f"{stem}.jpg"
            good_file.write_bytes(data)
            bad_file = ym / f"{'0' * 16}.jpg"
            bad_file.write_bytes(b"corrupted-bytes")

            with mock.patch.object(config, "PICS_DIR", pics_dir):
                self.assertIsNone(regen_thumbs._safe_pic_path("../escape.jpg"))
                self.assertIsNone(regen_thumbs._safe_pic_path("/etc/passwd"))
                self.assertEqual(
                    regen_thumbs._safe_pic_path("root.jpg"),
                    (pics_dir / "root.jpg").resolve(),
                )
                buf = io.StringIO()
                with mock.patch("sys.stdout", buf):
                    regen_thumbs.verify_originals()
                out = buf.getvalue()
                self.assertIn("核对 2 个", out)
                self.assertIn("内容不符 1", out)
                self.assertIn("0" * 16, out)

        with (
            mock.patch.object(runtime, "container_owns_data", return_value=True),
            self.assertRaises(RuntimeError),
        ):
            regen_thumbs.main()


class QuarkPruneAndAuditTest(unittest.TestCase):
    def test_backup_to_quark_prune_all_and_symlink_safety(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            blobs = data_dir / "backups" / "blobs"
            blobs.mkdir(parents=True)
            outside = data_dir / "outside.txt"
            outside.write_text("keep", encoding="utf-8")

            known_blob = blobs / ("a" * 64)
            unknown_blob = blobs / ("b" * 64)
            link_blob = blobs / ("c" * 64)
            known_blob.write_bytes(b"1")
            unknown_blob.write_bytes(b"2")
            link_blob.symlink_to(outside)

            with mock.patch.object(config, "DATA_DIR", data_dir):
                removed_known = backup_to_quark.prune_local({"a" * 64}, prune_all=False)
                self.assertEqual(removed_known, 1)
                self.assertFalse(known_blob.exists())
                self.assertTrue(unknown_blob.exists())
                self.assertTrue(outside.exists())

                link_blob.unlink()
                removed_all = backup_to_quark.prune_local(set(), prune_all=True)
                self.assertEqual(removed_all, 1)
                self.assertFalse(unknown_blob.exists())
                self.assertFalse(blobs.exists())

    def test_audit_single_pass_ignores_dotfiles(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            pics = data_dir / "pics"
            ym = pics / "2026" / "09"
            ym.mkdir(parents=True)
            tracked = ym / "tracked.jpg"
            tracked.write_bytes(b"tracked")
            orphan = ym / "orphan.jpg"
            orphan.write_bytes(b"orphan")
            (ym / ".DS_Store").write_bytes(b"mac-dotfile")
            (pics / ".DS_Store").write_bytes(b"root-dotfile")

            db_path = data_dir / "picwall.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE posts (sid TEXT PRIMARY KEY, avatar TEXT)")
                conn.execute("CREATE TABLE pics (id INTEGER PRIMARY KEY, sid TEXT, file TEXT, thumb TEXT)")
                conn.execute("INSERT INTO posts VALUES ('s1', '')")
                conn.execute("INSERT INTO pics VALUES (1, 's1', '2026/09/tracked.jpg', '2026/09/tracked.jpg')")

            buf = io.StringIO()
            with (
                mock.patch.object(audit, "DATA_DIR", data_dir),
                mock.patch.object(audit, "DB", db_path),
                mock.patch.object(audit, "PICS", pics),
                mock.patch("sys.stdout", buf),
            ):
                audit.main()
            out = buf.getvalue()
            self.assertIn("媒体总量：2 个文件", out)
            self.assertIn("磁盘不在库 1 个，库指向但磁盘缺失 0 个", out)

    def test_cloud_db_delegates_when_container_owns_data(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            with (
                mock.patch.object(config, "DATA_DIR", data_dir),
                mock.patch.object(config, "DB_PATH", data_dir / "picwall.db"),
                mock.patch.object(config, "PICS_DIR", data_dir / "pics"),
                mock.patch.object(config, "EXPORT_DIR", data_dir / "pics" / "export"),
            ):
                self.assertTrue(runtime._handle_cloud_db({"action": "init_db"}))
                self.assertEqual(runtime._handle_cloud_db({"action": "known_keys"}), [])
                n = runtime._handle_cloud_db({
                    "action": "register",
                    "items": [{"key": "k1", "fid": "f1", "size": 12}],
                })
                self.assertEqual(n, 1)
                self.assertEqual(runtime._handle_cloud_db({"action": "known_keys"}), ["k1"])
                self.assertEqual(
                    runtime._handle_cloud_db({"action": "list_blobs"}),
                    [{"key": "k1", "fid": "f1", "size": 12}],
                )

        seen_cmds = []

        def fake_run(cmd, input=None, **kwargs):
            seen_cmds.append((cmd, json.loads(input)))
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"ok": True, "result": ["k1"]}), stderr=""
            )

        with (
            mock.patch.object(runtime, "container_owns_data", return_value=True),
            mock.patch.object(runtime.subprocess, "run", side_effect=fake_run),
        ):
            keys = runtime.cloud_db("known_keys")
        self.assertEqual(keys, ["k1"])
        self.assertEqual(
            seen_cmds[0][0],
            ["docker", "exec", "-i", "picwall", "python", "/app/tools/runtime.py", "cloud-db"],
        )
        self.assertEqual(seen_cmds[0][1], {"action": "known_keys"})

    def test_backup_to_quark_never_touches_host_sqlite_when_container_running(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            blobs = data_dir / "backups" / "blobs"
            blobs.mkdir(parents=True)
            blob_bytes = b"sample-blob-payload"
            sha = hashlib.sha256(blob_bytes).hexdigest()
            blob_file = blobs / sha
            blob_file.write_bytes(blob_bytes)
            verified_copy = data_dir / "verified.bin"
            verified_copy.write_bytes(blob_bytes)

            registered: list[dict] = []
            delegated_actions: list[str] = []

            def fake_exec(cmd, input=None, **kwargs):
                req = json.loads(input)
                act = req["action"]
                delegated_actions.append(act)
                if act == "init_db":
                    res = True
                elif act == "known_keys":
                    res = [it["key"] for it in registered]
                elif act == "register":
                    registered.extend(req["items"])
                    res = len(req["items"])
                elif act == "list_blobs":
                    res = list(registered)
                else:
                    raise AssertionError(f"unexpected action {act}")
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps({"ok": True, "result": res}), stderr=""
                )

            with (
                mock.patch.object(config, "DATA_DIR", data_dir),
                mock.patch.object(config, "DB_PATH", data_dir / "picwall.db"),
                mock.patch.object(runtime, "container_owns_data", return_value=True),
                mock.patch.object(runtime.subprocess, "run", side_effect=fake_exec),
                mock.patch("sqlite3.connect", side_effect=AssertionError("host SQLite touched")),
                mock.patch.object(cloud.QuarkBlobs, "dir_fid", return_value="dir-fid"),
                mock.patch.object(cloud, "upload_files", return_value={sha: "fid-123"}),
                mock.patch.object(cloud, "read_blob", return_value=verified_copy),
                mock.patch.object(sys, "argv", ["tools/backup_to_quark.py", "--verify-sample", "1", "--prune-local"]),
            ):
                rc = backup_to_quark.main()

            self.assertEqual(rc, 0)
            self.assertFalse((data_dir / "picwall.db").exists())
            self.assertEqual(delegated_actions, ["init_db", "known_keys", "register", "known_keys", "list_blobs"])
            self.assertEqual(len(registered), 1)
            self.assertEqual(registered[0]["fid"], "fid-123")
            self.assertFalse(blob_file.exists())


class SafariCookieHardeningTest(unittest.TestCase):
    def test_url_policy_and_allow_insecure_lan(self):
        self.assertEqual(
            safari_x_cookie.validate_picwall_url("http://localhost:8081/"),
            "http://localhost:8081",
        )
        self.assertEqual(
            safari_x_cookie.validate_picwall_url("http://127.0.0.1:8081"),
            "http://127.0.0.1:8081",
        )
        self.assertEqual(
            safari_x_cookie.validate_picwall_url("http://[::1]:8081"),
            "http://[::1]:8081",
        )
        self.assertEqual(
            safari_x_cookie.validate_picwall_url("https://picwall.lan:8443"),
            "https://picwall.lan:8443",
        )
        with self.assertRaises(ValueError):
            safari_x_cookie.validate_picwall_url("http://192.168.1.20:8081")
        with self.assertRaises(ValueError):
            safari_x_cookie.validate_picwall_url("ftp://localhost:8081")

        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            res = safari_x_cookie.validate_picwall_url("http://192.168.1.20:8081", allow_insecure_lan=True)
        self.assertEqual(res, "http://192.168.1.20:8081")
        self.assertIn("未加密 HTTP", err.getvalue())

        rc = safari_x_cookie.main(["--url", "http://192.168.1.20:8081", "--file", "/nonexistent"])
        self.assertEqual(rc, 1)

    def test_extract_x_prefers_x_com_and_latest_expiry(self):
        cookies = [
            {"domain": ".twitter.com", "name": "auth_token", "value": "tw_tok", "expires": 9999},
            {"domain": ".x.com", "name": "auth_token", "value": "x_old", "expires": 100},
            {"domain": ".x.com", "name": "auth_token", "value": "x_new", "expires": 200},
            {"domain": ".x.com", "name": "ct0", "value": "ct_1"},
            {"domain": ".x.com", "name": "ct0", "value": "ct_2"},
        ]
        self.assertEqual(
            safari_x_cookie.extract_x(cookies),
            {"auth_token": "x_new", "ct0": "ct_2"},
        )

    def test_read_size_cap_and_linear_pairs_detection(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "Cookies.binarycookies"
            f.write_bytes(b"cook" + b"\x00" * 1024)
            with (
                mock.patch.object(safari_x_cookie, "MAX_COOKIE_FILE_BYTES", 256),
                self.assertRaises(ValueError),
            ):
                safari_x_cookie.read_safari_cookies(f)

        # Craft 10,000 pairs directory slots (~200 KiB buffer) — linear detector finishes fast.
        n_pages = 5000
        dir_end = 8 + 8 * n_pages
        buf = bytearray(b"cook" + struct.pack(">I", n_pages))
        for k in range(n_pages):
            buf += struct.pack(">II", 12, dir_end + k * 12)
        buf += b"\x00" * (12 * n_pages)
        t0 = time.monotonic()
        cands = safari_x_cookie._detect_candidates(bytes(buf))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5)
        self.assertEqual(cands["pairs"][0], n_pages)


class MigrateHeaderAndBenchmarkTest(unittest.TestCase):
    def test_migrate_header_import_has_no_side_effects(self):
        import tools.migrate_header as mh

        self.assertTrue(callable(mh.main))

    def test_benchmark_synthetic_seeds_mixed_statuses_safely(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            with (
                mock.patch.object(config, "DATA_DIR", data_dir),
                mock.patch.object(config, "DB_PATH", data_dir / "picwall.db"),
                mock.patch.object(config, "PICS_DIR", data_dir / "pics"),
                mock.patch.object(config, "EXPORT_DIR", data_dir / "pics" / "export"),
            ):
                config.ensure_dirs()
                db.init_db()
                buf = io.StringIO()
                with (
                    mock.patch.object(sys, "argv", ["tools/benchmark.py", "--synthetic"]),
                    mock.patch("sys.stdout", buf),
                ):
                    benchmark.main()
                payload = json.loads(buf.getvalue())
                self.assertEqual(payload["posts"], 20000)
                self.assertEqual(payload["state"], "pending")
                self.assertTrue(payload["synthetic"])
                self.assertIn("download_plan", payload["after"])
                self.assertIn("search_fts", payload["after"])
                self.assertNotIn("idx_posts_uid_status", "\n".join(benchmark.INDEXES))
                self.assertTrue(
                    any("posts_fts" in step for step in payload["query_plans"]["search_fts"])
                )
                self.assertTrue(
                    any("idx_posts_status (" in step for step in payload["query_plans"]["search"])
                )


class ContainerConfigHardeningTest(unittest.TestCase):
    def test_container_files_hardened(self):
        dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pat in (".venv/", ".pytest_cache/", ".ruff_cache/", ".zcode/", ".DS_Store", "tests/", "docs/verification/"):
            self.assertIn(pat, dockerignore)
        self.assertNotIn("*.zip", dockerignore)

        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("COPY tests/", dockerfile)
        self.assertIn("COPY docs/project_atlas.html", dockerfile)

        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("cap_drop:", compose)
        self.assertIn("- ALL", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("mem_limit: 2g", compose)
        self.assertIn("pids_limit: 256", compose)


class ProxyEnvIsolationTest(unittest.TestCase):
    def test_shell_proxy_vars_cleared_in_pytest(self):
        for var in ("all_proxy", "ALL_PROXY", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
            self.assertNotIn(var, os.environ)
