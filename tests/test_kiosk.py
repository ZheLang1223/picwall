"""报刊亭：/api/authors/kiosk 聚合口径与 /kiosk 页面可达性。

聚合与 list_authors 同口径分作者（source,uid）；头像只在本地（/media/）时返回；
签名只读 meta 缓存——本测试不打任何网络。
"""
import unittest
from datetime import datetime, timedelta

import httpx

from app import config, db
from app.sources import x as x_source
from app.sources.base import validate_post
from tests.auth_support import admin_headers
from tests.test_api import ApiTestBase

NOW = datetime.now(config.NOW_TZ)


def _post(sid, uid, author, created, handle=""):
    p = x_source.normalize_tweet({
        "id": sid, "author": {"id": uid, "uid": handle or uid, "handle": handle,
                              "name": author,
                              "avatar": "https://pbs.twimg.com/profile_images/a_normal.jpg"},
        "text": f"帖 {sid}", "created_at": created,
        "images": ["https://pbs.twimg.com/media/AbC?format=jpg"]})
    validate_post(p)
    return p


class KioskApiTest(ApiTestBase):
    def _seed(self):
        recent = _post("1844000000000000001", "789", "张帕克",
                       (NOW - timedelta(days=2)).isoformat())
        older = _post("1844000000000000002", "789", "张帕克",
                      (NOW - timedelta(days=40)).isoformat())
        recent["source"] = ""                      # 微博历史行 source 为空串
        older["source"] = ""
        recent["avatar"] = "/media/avatars/aaa.jpg"
        db.insert_post(recent, "123456")
        db.insert_post(older, "123456")
        db.set_status(recent["sid"], "kept")
        db.set_status(older["sid"], "kept")
        xpost = _post("1844000000000000003", "123", "摩卡黑狗",
                      (NOW - timedelta(days=1)).isoformat(), handle="mochakuro")
        db.insert_post(xpost, "xu:mochakuro")
        db.insert_pic(recent["sid"], "2026/11/aaa_full.jpg", "2026/11/t_aaa.jpg", 800, 600, "https://x")
        db.insert_pic(xpost["sid"], "2026/11/bbb_full.jpg", "2026/11/t_bbb.jpg", 800, 600, "https://x")
        db.set_meta("author_bio:789", "日更画手。")

    async def test_shelf_aggregates(self):
        self._seed()
        async with self.client() as c:
            j = (await c.get("/api/authors/kiosk")).json()
        self.assertTrue(j["ok"])
        entries = {e["key"]: e for e in j["shelf"]}
        # demo 种子作者也在书架上（与博主轨同口径）；断言只针对本例两位
        self.assertIn("789", entries)
        self.assertIn("x:123", entries)
        wb, xk = entries["789"], entries["x:123"]
        # 微博作者：两期合订、近 30 天只算新帖、头像与签名走本地
        self.assertEqual((wb["n"], wb["kept"], wb["l30"]), (2, 2, 1))
        self.assertEqual(wb["avatar"], "/media/avatars/aaa.jpg")
        self.assertEqual(wb["bio"], "日更画手。")
        self.assertIn("/media/2026/11/t_aaa.jpg", wb["thumbs"])
        self.assertIn(str(NOW.year), wb["last_at"])
        # X 作者：远程头像过滤为空，handle 与内页图照常
        self.assertEqual((xk["handle"], xk["avatar"]), ("mochakuro", ""))
        self.assertEqual(xk["thumbs"], ["/media/2026/11/t_bbb.jpg"])
        # 按最近到货排序：昨天更的 X 作者排在两天前的微博作者之前
        self.assertLess(j["shelf"].index(xk), j["shelf"].index(wb))

    async def test_kiosk_management_metadata_and_actions(self):
        self._seed()
        db.add_group("wbu:789", "张帕克")
        db.add_group("xu:mochakuro", "摩卡黑狗")
        db.add_group("123456", "摄影清单")
        db.add_group("xg:fav1", "特选画师")
        db.add_group_member("xg:fav1", "xu:mochakuro")
        async with self.client() as c:
            j = (await c.get("/api/authors/kiosk")).json()
            self.assertEqual(j["role"], "admin")
            group_ids = {g["list_id"] for g in j["groups"]}
            # 管理大厅显示可用清单/本地分组，排除 demo 与单人关注目标 wbu:/xu:
            self.assertTrue({"123456", "xg:fav1"}.issubset(group_ids))
            self.assertFalse(any(lid.startswith(("wbu:", "xu:")) or lid == "demo" for lid in group_ids))
            entries = {e["key"]: e for e in j["shelf"]}
            wb, xk = entries["789"], entries["x:123"]
            self.assertTrue(wb["pinyin"] and len(wb["initial"]) == 1)
            self.assertEqual(wb["follow_id"], "wbu:789")
            self.assertTrue(wb["followed"])
            self.assertIn("123456", wb["groups"])
            self.assertEqual(xk["follow_id"], "xu:mochakuro")
            self.assertTrue(xk["followed"])
            self.assertEqual(xk["xg_groups"], ["xg:fav1"])

            # 在大厅取消单人关注：返回 ok，再次拉取书架 followed=False 但已抓帖保留
            r_unfollow = await c.delete("/api/groups/xu:mochakuro")
            self.assertEqual(r_unfollow.status_code, 200)
            j2 = (await c.get("/api/authors/kiosk")).json()
            xk2 = next(e for e in j2["shelf"] if e["key"] == "x:123")
            self.assertFalse(xk2["followed"])
            self.assertEqual(xk2["n"], 1)


class KioskPageTest(ApiTestBase):
    """/kiosk 页面：已配对设备可达（本机即 admin），未配对 401。"""

    async def test_kiosk_page_access(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://test") as client:
            r_admin = await client.get("/kiosk", headers=admin_headers())
            self.assertEqual(r_admin.status_code, 200)
            self.assertIn("PWALL 报刊亭", r_admin.text)
            self.assertIn("/static/kiosk.js", r_admin.text)

            r_anon = await client.get("/kiosk", headers={"Host": "localhost"})
            self.assertEqual(r_anon.status_code, 401)


if __name__ == "__main__":
    unittest.main()
