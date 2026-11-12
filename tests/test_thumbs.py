"""缩略图宽度封顶策略：窄图 thumb 指原图（长条漫画不再糊）、宽图压到 720。"""
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from app import config, storage


def _jpeg(w, h):
    im = Image.new("RGB", (w, h), (90, 110, 130))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class ThumbStrategyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-thumb-"))
        self._old = (config.DATA_DIR, config.PICS_DIR)
        config.DATA_DIR = self.tmp
        config.PICS_DIR = self.tmp / "pics"

    def tearDown(self):
        (config.DATA_DIR, config.PICS_DIR) = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_narrow_image_thumb_is_original(self):
        """宽 ≤720（长条漫画等）：thumb 直接指原图，不生成 t_ 文件。"""
        rel, w, h, t = storage.save_image(_jpeg(544, 10000), "x.jpg")
        self.assertEqual(w, 544)
        self.assertEqual(h, 10000)
        self.assertEqual(t, rel)                                   # thumb = 原图
        self.assertEqual(list((config.PICS_DIR / rel).parent.glob("t_*.jpg")), [])

    def test_wide_image_thumb_capped_at_720(self):
        """宽 >720：等比压到 720 宽。"""
        rel, _w, _h, t = storage.save_image(_jpeg(2000, 500), "x.jpg")
        self.assertNotEqual(t, rel)
        with Image.open(config.PICS_DIR / t) as im:
            self.assertEqual(im.size, (720, round(500 * 720 / 2000)))

    def test_tall_wide_image_height_uncapped(self):
        """宽超限的长条（1231×3000）：宽度封顶、高度等比不设限。"""
        rel, _w, _h, t = storage.save_image(_jpeg(1231, 3000), "x.jpg")
        self.assertNotEqual(t, rel)
        with Image.open(config.PICS_DIR / t) as im:
            self.assertEqual(im.size, (720, round(3000 * 720 / 1231)))


if __name__ == "__main__":
    unittest.main()
