"""微博博主主页目标：wbu: 分组、mymblog 解析、博主添加与抓取调度。"""
import unittest
from unittest import mock

import httpx

from app import config, crawler, db, weibo
from tests.test_api import ApiTestBase


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)


def _mymblog_page(post_ids):
    items = []
    for sid in post_ids:
        items.append({
            "mblogid": sid,
            "created_at": "刚刚",
            "text": f"博文 {sid}",
            "user": {"id": 1669879400, "screen_name": "测试博主"},
            "pic_infos": {
                f"pid_{sid}": {
                    "large": {"url": f"https://wx1.sinaimg.cn/large/{sid}.jpg"},
                    "mw2000": {"url": f"https://wx1.sinaimg.cn/mw2000/{sid}.jpg"},
                }
            },
        })
    return {"ok": 1, "data": {"list": items, "total": len(items)}}


class WeiboUserTargetTest(unittest.TestCase):
    def test_extract_statuses_handles_mymblog_envelope(self):
        page = _mymblog_page(["s1", "s2"])
        statuses = weibo.extract_statuses(page)
        self.assertEqual(len(statuses), 2)
        self.assertEqual(statuses[0]["mblogid"], "s1")
        self.assertEqual(statuses[1]["mblogid"], "s2")

    def test_extract_statuses_handles_groupstimeline_envelope(self):
        page = {"ok": 1, "statuses": [{"mblogid": "g1"}], "total_number": 1}
        statuses = weibo.extract_statuses(page)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["mblogid"], "g1")

    def test_extract_wb_uid_patterns(self):
        from app.server import _extract_wb_uid
        self.assertEqual(_extract_wb_uid("wbu:1669879400"), "1669879400")
        self.assertEqual(_extract_wb_uid("wb:1669879400"), "1669879400")
        self.assertEqual(_extract_wb_uid("https://weibo.com/u/1669879400"), "1669879400")
        self.assertEqual(_extract_wb_uid("https://weibo.com/1669879400?from=myfollow"), "1669879400")
        self.assertIsNone(_extract_wb_uid("12345"))  # 纯短数字不当作博主 URL
        self.assertIsNone(_extract_wb_uid("x:12345678"))

    def test_db_write_lock_is_exported(self):
        self.assertIs(db.write_lock, db._write_lock)


class WeiboUserApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_profile_info_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("ajax/profile/info", request.url.path)
            self.assertEqual(request.url.params.get("uid"), "1669879400")
            return httpx.Response(200, json={
                "ok": 1,
                "data": {
                    "user": {
                        "id": 1669879400,
                        "screen_name": "测试画师",
                        "description": "这是个人简介",
                        "avatar_hd": "https://wx1.sinaimg.cn/avatar.jpg",
                    }
                }
            })

        async with _mock_client(handler) as client:
            info = await weibo.profile_info(client, "SUB=fake; XSRF-TOKEN=test", "1669879400")
            self.assertEqual(info["uid"], "1669879400")
            self.assertEqual(info["screen_name"], "测试画师")
            self.assertEqual(info["description"], "这是个人简介")

    async def test_profile_info_not_found(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": 1, "data": {}})

        async with _mock_client(handler) as client:
            with self.assertRaises(weibo.WeiboError):
                await weibo.profile_info(client, "SUB=fake", "99999999")

    async def test_user_timeline_fetches(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("ajax/statuses/mymblog", request.url.path)
            self.assertEqual(request.url.params.get("uid"), "1669879400")
            self.assertEqual(request.url.params.get("page"), "2")
            return httpx.Response(200, json=_mymblog_page(["p201", "p202"]))

        async with _mock_client(handler) as client:
            data = await weibo.user_timeline(client, "SUB=fake; XSRF-TOKEN=test", "1669879400", page=2)
            items = weibo.extract_statuses(data)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]["mblogid"], "p201")


class WeiboUserEndpointTest(ApiTestBase):
    async def test_add_wb_user_by_wbu_prefix_and_url(self):
        async def fake_profile_info(client, cookie, uid):
            if uid == "1669879400":
                return {"uid": uid, "screen_name": "画师小明", "description": "", "avatar": ""}
            raise weibo.WeiboError(f"未找到微博博主 {uid}")

        with mock.patch.object(config, "get_cookie", return_value="SUB=dummy; XSRF-TOKEN=1"), \
             mock.patch.object(self.server.weibo, "profile_info", side_effect=fake_profile_info):
            async with self.client() as c:
                # 1. 用 wbu: 前缀添加
                r1 = await c.post("/api/groups", json={"list_id": "wbu:1669879400"})
                self.assertEqual(r1.status_code, 200)
                data1 = r1.json()
                self.assertTrue(data1["ok"])
                self.assertIn("@画师小明", data1.get("msg", ""))

                # 2. 用微博主页 URL 添加
                r2 = await c.post("/api/groups", json={"list_id": "https://weibo.com/u/1669879400"})
                self.assertEqual(r2.status_code, 200)
                self.assertTrue(r2.json()["ok"])

                # 3. 检查 groups 表
                st = (await c.get("/api/status")).json()
                wb_groups = {g["list_id"]: g["name"] for g in st["groups"]}
                self.assertEqual(wb_groups.get("wbu:1669879400"), "@画师小明")

    async def test_add_wb_user_without_cookie_fails(self):
        with mock.patch.object(config, "get_cookie", return_value=""):
            async with self.client() as c:
                r = await c.post("/api/groups", json={"list_id": "wbu:1669879400"})
                self.assertEqual(r.status_code, 400)
                self.assertIn("还没有微博 Cookie", r.json()["detail"])

    async def test_crawl_group_dispatches_wbu_to_user_timeline(self):
        """_crawl_group 在遇到 wbu: 目标时调用 user_timeline 而非 group_timeline。"""
        fake_data = _mymblog_page(["wbpost1"])

        async def fake_user_timeline(client, cookie, uid, page=1):
            self.assertEqual(uid, "1669879400")
            if page > 1:
                return {"ok": 1, "data": {"list": []}}
            return fake_data

        with mock.patch.object(weibo, "user_timeline", side_effect=fake_user_timeline) as mock_user_tl, \
             mock.patch.object(weibo, "group_timeline") as mock_grp_tl, \
             mock.patch.object(crawler.asyncio, "sleep", return_value=None), \
             mock.patch.object(crawler, "_store_post", return_value=1):
            async with httpx.AsyncClient() as client:
                g = {"list_id": "wbu:1669879400", "name": "@测试画师"}
                res = await crawler._crawl_group(client, "SUB=test; XSRF-TOKEN=t", g, backfill_days=0)
                self.assertGreaterEqual(mock_user_tl.call_count, 1)
                mock_grp_tl.assert_not_called()
                self.assertEqual(res["group"], "@测试画师")

    async def test_ingest_harvest_pipeline_via_crawler(self):
        # 1. 模拟运行中冲突
        with mock.patch.dict(crawler.state, {"running": True}):
            code, r = await crawler.ingest_harvest("123", "分组123", [])
            self.assertEqual(code, 409)
            self.assertFalse(r["ok"])

        # 2. 模拟正常批次
        raw_items = [{
            "mblogid": "harvest1", "created_at": "刚刚", "text": "代抓图",
            "user": {"id": 1, "screen_name": "代抓博主"},
            "pics": [{"pid": "p1", "url": "https://wx1.sinaimg.cn/large/h1.jpg"}]
        }]
        async def fake_store(client, p, lid, cache):
            db.insert_post(p, lid)
            with db.write_lock, db.conn() as c:
                c.execute("DELETE FROM image_jobs WHERE sid=?", (p["sid"],))
            return 1

        with mock.patch.object(crawler, "_store_post", side_effect=fake_store):
            code, r = await crawler.ingest_harvest("9999", "代抓测试组", raw_items)
            self.assertEqual(code, 200)
            self.assertTrue(r["ok"])
            self.assertEqual(r["received"], 1)


if __name__ == "__main__":
    unittest.main()
