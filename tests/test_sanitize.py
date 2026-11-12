"""文件名清洗集中化回归：storage.sanitize_filename 是唯一的名字清洗点。

export/ 是同步到设备的留存文件夹，zip 会被 Windows 资源管理器解压——
保留名、控制字符、超长名在这里挡住，就不再有散落的第二份实现。
"""
import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

import httpx
from PIL import Image

from app import config, db, demo, storage
from tests.auth_support import admin_headers


def _jpeg_bytes(w=64, h=48) -> bytes:
    im = Image.new("RGB", (w, h), (90, 110, 130))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class SanitizeTest(unittest.TestCase):
    def test_windows_reserved_names_neutered(self):
        for bad in ("CON", "con", "Nul", "AUX", "COM1", "lpt9"):
            out = storage.sanitize_filename(bad)
            self.assertFalse(out.upper() in storage._WIN_RESERVED, bad)

    def test_illegal_chars_and_control_chars_stripped(self):
        out = storage.sanitize_filename('a/b\\c:d*e?f"g<h>i|j\x00k\x1fl')
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, out)
        self.assertNotIn("\x00", out)
        self.assertNotIn("\x1f", out)
        self.assertIn("a", out)   # 内容没被整段丢掉

    def test_trailing_dots_spaces_and_empty(self):
        self.assertEqual(storage.sanitize_filename("名字. .."), "名字")
        self.assertEqual(storage.sanitize_filename("///"), "wb")   # 空兜底沿用旧约定

    def test_chinese_kept_and_length_capped(self):
        self.assertEqual(storage.sanitize_filename("博物杂志"), "博物杂志")
        self.assertLessEqual(len(storage.sanitize_filename("长" * 300, 80)), 80)

    def test_zip_entry_name_stays_under_255_and_keeps_shape(self):
        name = storage.zip_entry_name("2026-09-15T10:00:00+08:00", "超" * 200,
                                      "x:1844000000000000000", 3, ".jpg")
        self.assertLessEqual(len(name.encode("utf-8")), 255)
        self.assertRegex(name, r"^\d{8}_.+_.+_\d{2}\.jpg$")   # 日期_作者_ID_序号 结构不变
        self.assertNotIn(":", name)                            # x: 前缀的冒号被清洗


class ZipEndpointSanitizeTest(unittest.IsolatedAsyncioTestCase):
    """端到端：作者名带保留名+非法字符，zip 条目必须干净（命名规则保持）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-zip-"))
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

    async def test_zip_entries_clean_with_hostile_author(self):
        rel = storage.save_image(_jpeg_bytes(), "t.jpg")[0]
        db.insert_post({
            "sid": "zt1", "uid": "9", "author": 'CON/x*?"<>|', "avatar": "",
            "text": "t", "created_at": "2026-09-15T10:00:00+08:00",
            "fetched_at": "2026-09-15T10:00:00+08:00",
            "weibo_url": "u", "via": "", "is_long": 0,
        }, "demo")
        db.insert_pic("zt1", rel, rel, 64, 48, "https://example.com/x.jpg")
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.server.app),
                base_url="http://test", headers=admin_headers()) as c:
            r = await c.get("/api/download/zip?select=zt1")
            self.assertEqual(r.status_code, 200)
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            for info in zf.infolist():
                n = info.filename
                stem = n.split("_", 1)[1].rsplit("_", 2)[0]   # 作者片段
                self.assertFalse(stem.upper() in storage._WIN_RESERVED, n)
                for ch in '\\/:*?"<>|':
                    self.assertNotIn(ch, n)


if __name__ == "__main__":
    unittest.main()
