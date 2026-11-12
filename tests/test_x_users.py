"""X 用户主页目标：xu: 分组、UserTweets 解析与来源筛选。

外部平台结构易变，解析失败前移到单测；queryId 漂移属于运行时错误类（404→structure）。
"""
import json
import unittest
from unittest import mock

import httpx

from app import config, db
from app.sources import x as x_source
from app.sources import xapi
from app.sources.base import validate_post
from tests.test_api import ApiTestBase


def _tweet_result(tid):
    legacy = {"id_str": tid, "full_text": f"图 {tid}",
              "created_at": "Tue Sep 15 12:00:00 +0000 2026",
              "extended_entities": {"media": [
                  {"type": "photo",
                   "media_url_https": "https://pbs.twimg.com/media/AbC?format=jpg"}]}}
    core = {"user_results": {"result": {
        "rest_id": "123",
        "core": {"screen_name": "luoluokun_", "name": "落落君"},
        "legacy": {"profile_image_url_https":
                   "https://pbs.twimg.com/profile_images/x_normal.jpg"}}}}
    return {"legacy": legacy, "core": core}


def _user_page(tweet_ids, cursor=None):
    """合成 UserTweets 响应：data.user.result.timeline.timeline.instructions。"""
    entries = []
    for tid in tweet_ids:
        entries.append({
            "entryId": f"tweet-{tid}",
            "content": {"entryType": "TimelineTimelineItem",
                        "itemContent": {"tweet_results": {"result": _tweet_result(tid)}}},
        })
    if cursor:
        entries.append({"entryId": "cursor-bottom-0", "content": {"entryType": "TimelineCursor",
                                                                  "value": cursor}})
    return {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": [
        {"type": "TimelineAddEntries", "entries": entries}]}}}}}}


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10)


class XUserTargetTest(unittest.TestCase):
    def test_group_user_id_normalizes(self):
        self.assertEqual(x_source.group_user_id("@Kamyuelo"), "xu:kamyuelo")
        self.assertEqual(x_source.group_user_id("luoluokun_"), "xu:luoluokun_")
        with self.assertRaises(x_source.StructureError):
            x_source.group_user_id("太长" * 20)
        with self.assertRaises(x_source.StructureError):
            x_source.group_user_id("bad handle")

    def test_is_x_group_covers_user_targets(self):
        self.assertTrue(x_source.is_x_group("xu:kamyuelo"))
        self.assertTrue(x_source.is_x_group("x:123"))

    def test_walk_user_timeline(self):
        tweets, cursor = xapi._walk_tweets(_user_page(["1", "2"], cursor="NEXT"), root="user")
        self.assertEqual([t["legacy"]["id_str"] for t in tweets], ["1", "2"])
        self.assertEqual(cursor, "NEXT")
        # 结构不认识时严格报错——静默空转曾让列表抓取长期无声失败
        with self.assertRaises(xapi.XApiError):
            xapi._walk_tweets({"data": {}}, root="user")


class UserApiTest(unittest.IsolatedAsyncioTestCase):
    """XGraphQL 的用户端到端（MockTransport，不碰网络）。"""

    async def test_user_id_and_latest_paginated(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("auth_token=", request.headers["cookie"])
            if "UserByScreenName" in request.url.path:
                return httpx.Response(200, json={"data": {"user": {"result": {
                    "rest_id": "123", "legacy": {"screen_name": "luoluokun_"}}}}})
            vars_ = json.loads(request.url.params["variables"])
            if "cursor" not in vars_:            # 第一页：带下页游标
                return httpx.Response(200, json=_user_page(["101", "102"], cursor="NEXT"))
            self.assertEqual(vars_["cursor"], "NEXT")
            return httpx.Response(200, json=_user_page(["103"]))   # 第二页：到底

        api = xapi.XGraphQL("tok" * 10, "csrf")
        async with _mock_client(handler) as client:
            self.assertEqual(await api.user_id(client, "luoluokun_"), "123")
            items = [item async for item in api.user_latest(client, "123", max_pages=3)]
        self.assertEqual([i["id"] for i in items], ["101", "102", "103"])
        for i in items:
            self.assertEqual(i["author"]["handle"], "luoluokun_")
            self.assertTrue(i["images"][0].startswith("https://pbs.twimg.com/media/"))

    async def test_missing_user_maps_to_structure_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"errors": [{"message": "Not found"}]})

        api = xapi.XGraphQL("tok" * 10, "csrf")
        async with _mock_client(handler) as client:
            with self.assertRaises(xapi.XApiError) as ctx:
                await api.user_id(client, "ghost_user")
        self.assertEqual(ctx.exception.kind, "structure")


class SourceFilterTest(ApiTestBase):
    def _seed(self):
        def post(sid, source, created):
            p = x_source.normalize_tweet({
                "id": sid, "author": {"uid": "h", "name": "作者",
                                      "avatar": "https://pbs.twimg.com/profile_images/a_normal.jpg"},
                "text": f"帖 {sid}", "created_at": created,
                "images": ["https://pbs.twimg.com/media/AbC?format=jpg"]})
            validate_post(p)
            return p

        db.insert_post(post("1844000000000000001", "x", "2026-10-16T10:00:00+08:00"),
                       "xu:kamyuelo")
        wb = post("1844000000000000002", "weibo", "2026-10-16T11:00:00+08:00")
        wb["source"] = ""                       # 微博历史行 source 为空串
        db.insert_post(wb, "123456")
        db.add_group("xu:kamyuelo", "@Kamyuelo")

    async def test_source_filter_splits_wall(self):
        self._seed()
        async with self.client() as c:
            xs = (await c.get("/api/posts", params={"status": "all", "source": "x"})).json()
            wb = (await c.get("/api/posts", params={"status": "all", "source": "weibo"})).json()
            al = (await c.get("/api/posts", params={"status": "all"})).json()
        x_sids = {p["sid"] for p in xs["posts"]}
        wb_sids = {p["sid"] for p in wb["posts"]}
        # demo 帖子按空 source 归入微博；来源筛选必须把 X 帖与微博帖切开
        self.assertEqual(x_sids, {"x:1844000000000000001"})
        self.assertIn("x:1844000000000000002", wb_sids)
        self.assertNotIn("x:1844000000000000001", wb_sids)
        self.assertEqual(len(al["posts"]), len(wb_sids) + 1)

    async def test_authors_follow_source(self):
        """作者聚合按来源切分：X 博主与微博博主各列各的（头像面板数据源）。"""
        self._seed()
        async with self.client() as c:
            xa = (await c.get("/api/authors", params={"source": "x"})).json()["authors"]
            wa = (await c.get("/api/authors", params={"source": "weibo"})).json()["authors"]
            al = (await c.get("/api/authors")).json()["authors"]
        self.assertEqual([a["uid"] for a in xa], ["x:h"])   # X 作者 uid 自带 x: 前缀
        self.assertEqual(xa[0]["source"], "x")
        self.assertIn("h", [a["uid"] for a in wa])          # 微博侧同名字符串是裸 uid
        self.assertNotIn("x:h", [a["uid"] for a in wa])
        self.assertIn("x:h", [a["uid"] for a in al])

    async def test_group_add_via_xu_prefix(self):
        self._seed()
        async with self.client() as c:
            async def fake_ensure(handle):
                return f"xu:{handle.lower()}"

            with mock.patch.object(self.server, "_ensure_x_user", side_effect=fake_ensure):
                r = await c.post("/api/groups", json={"list_id": "@Yuuko891"})
            self.assertEqual(r.status_code, 200)
            bad = await c.post("/api/groups", json={"list_id": "xu:含中文"})
            self.assertEqual(bad.status_code, 400)
            st = (await c.get("/api/status")).json()
        names = {g["list_id"]: g["name"] for g in st["groups"]}
        self.assertEqual(names.get("xu:yuuko891"), "@Yuuko891")

    async def test_crawl_x_dispatches_user_targets(self):
        """_crawl_x 把 xu: 目标派给 crawl_users，进度按合并后的组序列推进。"""
        self._seed()
        async with self.client() as c:
            async def fake_crawl_users(client, token, users, max_pages=1, on_user=None, ct0=""):
                for i, u in enumerate(users):
                    if on_user:
                        on_user(i, {"group": u["name"], "new_posts": 2, "new_pics": 3, "error": ""})
                return [{"group": u["name"], "new_posts": 2, "new_pics": 3, "error": ""}
                        for u in users]

            with mock.patch.object(config, "get_x_auth", return_value="tok" * 10), \
                 mock.patch.object(self.server.xapi, "crawl_users", side_effect=fake_crawl_users), \
                 mock.patch.object(self.server.httpx, "AsyncClient", wraps=self.server.httpx.AsyncClient) as ctor:
                r = (await c.post("/api/crawl/x")).json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["new_posts"], 2)
        # X 链路的客户端带上了代理配置
        ctor.assert_called_once()
        self.assertEqual(ctor.call_args.kwargs.get("proxy"), config.X_PROXY_URL or None)

    async def test_user_id_cached_and_reused_in_crawl_users(self):
        """crawl_users 优先复用 meta 表中的 rest_id 缓存，避免重复请求 user_id。"""
        db.set_meta("x_user_rest_id:kamyuelo", "999000")
        users = [{"handle": "kamyuelo", "name": "@Kamyuelo"}]

        async def fake_latest(client, rest_id, max_pages=1):
            self.assertEqual(rest_id, "999000")
            if False:
                yield {}

        with mock.patch.object(xapi.XGraphQL, "user_id") as mock_uid, \
             mock.patch.object(xapi.XGraphQL, "user_latest", side_effect=fake_latest):
            async with _mock_client(lambda r: httpx.Response(200, json={})) as client:
                summaries = await xapi.crawl_users(client, "tok"*10, users)
        mock_uid.assert_not_called()
        self.assertEqual(summaries[0]["group"], "@Kamyuelo")

    async def test_crawl_users_skips_subsequent_on_rate_limit(self):
        """当一个博主遇到 429 限流时，后续博主优雅跳过并标记 ratelimit。"""
        users = [{"handle": "u1", "name": "@u1"}, {"handle": "u2", "name": "@u2"}]

        with mock.patch.object(xapi.XGraphQL, "user_id", side_effect=xapi.XApiError("ratelimit", "429")):
            async with _mock_client(lambda r: httpx.Response(200, json={})) as client:
                summaries = await xapi.crawl_users(client, "tok"*10, users)
        self.assertEqual(len(summaries), 2)
        self.assertEqual(summaries[0]["error_kind"], "ratelimit")
        self.assertEqual(summaries[1]["error_kind"], "ratelimit")
        self.assertIn("跳过本轮", summaries[1]["error"])

    async def test_backfill_x_user_dispatches(self):
        """POST /api/groups/{list_id}/backfill 对 xu: 用户能正确校验并触发 X 回补。"""
        self._seed()
        async with self.client() as c:
            with mock.patch.object(config, "get_x_auth", return_value="tok"*10), \
                 mock.patch.object(self.server.crawler, "crawl_x", return_value={"ok": True}) as mock_cx:
                r = await c.post("/api/groups/xu:kamyuelo/backfill")
                self.assertEqual(r.status_code, 200)
                self.assertIn("X 回补已开始", r.json()["msg"])
                mock_cx.assert_called_once_with(only="xu:kamyuelo", max_pages=10,
                                                http_client_factory=mock.ANY, xapi_module=mock.ANY)

    async def test_trigger_crawl_with_x_only(self):
        """当只有 X 配置时，POST /api/crawl 自动触发 X 抓取而不是报错缺少微博 Cookie。"""
        self._seed()
        async with self.client() as c:
            with mock.patch.object(config, "get_cookie", return_value=""), \
                 mock.patch.object(config, "get_x_auth", return_value="tok"*10), \
                 mock.patch.object(self.server.crawler, "crawl_x", return_value={"ok": True}):
                r = await c.post("/api/crawl")
                self.assertEqual(r.status_code, 200)
                self.assertIn("抓取已开始", r.json()["msg"])


class XProfileTest(unittest.IsolatedAsyncioTestCase):
    """user_profile 解析：签名 t.co 展开、缺 core 兜底、用户不存在报结构错。"""

    async def test_user_profile_expands_bio_links(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIn("UserByScreenName", request.url.path)
            return httpx.Response(200, json={"data": {"user": {"result": {
                "rest_id": "123",
                "core": {"screen_name": "mochakuro", "name": "摩卡黑狗"},
                "legacy": {"description": "兽人插画 约稿常开 https://t.co/Shop",
                           "entities": {"description": {"urls": [
                               {"url": "https://t.co/Shop",
                                "expanded_url": "https://skeb.jp/@mochakuro"}]}},
                           "profile_image_url_https":
                               "https://pbs.twimg.com/profile_images/x_normal.jpg"}}}}})

        api = xapi.XGraphQL("tok" * 10, "csrf")
        async with _mock_client(handler) as client:
            p = await api.user_profile(client, "mochakuro")
        self.assertEqual(p["rest_id"], "123")
        self.assertEqual(p["handle"], "mochakuro")
        self.assertEqual(p["name"], "摩卡黑狗")
        self.assertEqual(p["description"], "兽人插画 约稿常开 https://skeb.jp/@mochakuro")

    async def test_user_profile_without_core_falls_back_to_legacy(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"user": {"result": {
                "rest_id": "456",
                "legacy": {"screen_name": "old_school", "name": "老用户",
                           "description": "bio only"}}}}})

        api = xapi.XGraphQL("tok" * 10, "csrf")
        async with _mock_client(handler) as client:
            p = await api.user_profile(client, "old_school")
        self.assertEqual(p["handle"], "old_school")
        self.assertEqual(p["description"], "bio only")

    async def test_user_profile_missing_user_is_structure_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {}})

        api = xapi.XGraphQL("tok" * 10, "csrf")
        async with _mock_client(handler) as client:
            with self.assertRaises(xapi.XApiError) as ctx:
                await api.user_profile(client, "ghost_user")
        self.assertEqual(ctx.exception.kind, "structure")


class XBioEndpointTest(ApiTestBase):
    """GET /api/authors/x:<rest_id>/bio：handle 解析、缓存命中与降级。"""

    def _seed_x_post(self):
        p = x_source.normalize_tweet({
            "id": "456", "author": {"id": "123", "uid": "mochakuro", "handle": "mochakuro",
                                    "name": "摩卡黑狗",
                                    "avatar": "https://pbs.twimg.com/profile_images/a_normal.jpg"},
            "text": "帖", "created_at": "2026-10-16T10:00:00+08:00",
            "images": ["https://pbs.twimg.com/media/AbC?format=jpg"]})
        validate_post(p)
        db.insert_post(p, "xu:mochakuro")

    async def test_bio_without_token_degrades_to_need_browser(self):
        self._seed_x_post()
        async with self.client() as c:
            with mock.patch.object(config, "get_x_auth", return_value=""):
                r = (await c.get("/api/authors/x:123/bio")).json()
        self.assertEqual(r["source"], "x")
        self.assertFalse(r["ok"])
        self.assertTrue(r["need_browser"])

    async def test_bio_without_local_handle_reports_msg(self):
        async with self.client() as c:
            with mock.patch.object(config, "get_x_auth", return_value="tok" * 10):
                r = (await c.get("/api/authors/x:999/bio")).json()
        self.assertEqual(r["source"], "x")
        self.assertFalse(r["ok"])
        self.assertIn("handle", r["msg"])

    async def test_bio_fetches_via_x_and_caches(self):
        self._seed_x_post()
        calls = []

        async def fake_profile(self, client, handle):
            calls.append(handle)
            return {"rest_id": "123", "handle": handle, "name": "摩卡黑狗",
                    "description": "兽人插画 约稿常开"}

        with mock.patch.object(config, "get_x_auth", return_value="tok" * 10), \
             mock.patch.object(config, "get_x_csrf", return_value="csrf"), \
             mock.patch.object(xapi.XGraphQL, "user_profile", fake_profile):
            async with self.client() as c:
                r1 = (await c.get("/api/authors/x:123/bio")).json()
                r2 = (await c.get("/api/authors/x:123/bio")).json()
        self.assertEqual(calls, ["mochakuro"])           # 第二次命中缓存，不再出网
        self.assertTrue(r1["ok"])
        self.assertEqual(r1["bio"], "兽人插画 约稿常开")
        self.assertEqual(r2["source"], "cache")
        self.assertEqual(r2["bio"], "兽人插画 约稿常开")
        self.assertEqual(db.get_meta("author_bio:x:123"), "兽人插画 约稿常开")


if __name__ == "__main__":
    unittest.main()
