"""凭据更新与状态回归：隔离文件、模拟平台响应，不触碰真实登录态。"""
import os
from unittest.mock import AsyncMock, patch

import httpx

from app import config, connections, db, weibo
from app.sources import xapi
from tests.test_api import ApiTestBase


class ConnectionTest(ApiTestBase):
    def setUp(self):
        super().setUp()
        self.auth_file = patch.object(config, "X_AUTH_FILE", self.tmp / "x_auth.txt")
        self.env = patch.object(config, "WB_COOKIE_ENV", "")
        self.auth_file.start()
        self.env.start()

    def tearDown(self):
        self.auth_file.stop()
        self.env.stop()
        super().tearDown()

    def test_normalization_requires_exact_nonempty_keys(self):
        for value in ("NOT_SUB=a; XSRF-TOKEN=b", "SUB=; XSRF-TOKEN=b",
                      "SUB=a; XSRF-TOKEN=b\nHeader: bad", "SUB=a; SUB=other; XSRF-TOKEN=b"):
            with self.assertRaises(ValueError):
                connections.normalize_weibo(value)
        self.assertEqual(connections.normalize_weibo("Cookie: SUB=a; SUB=a; XSRF-TOKEN=b;"),
                         "SUB=a; XSRF-TOKEN=b")

    async def test_invalid_candidate_preserves_working_cookie_and_state(self):
        old = "SUB=old; XSRF-TOKEN=old"
        config.save_cookie(old)
        connections.record("weibo", old, "ok")
        for error, code in ((weibo.CookieExpired("expired"), 400),
                            (httpx.ConnectError("offline"), 503)):
            with patch("app.weibo.all_groups", new=AsyncMock(side_effect=error)):
                async with self.client() as c:
                    r = await c.post("/api/settings/cookie", json={"cookie":"SUB=new; XSRF-TOKEN=new"})
                    self.assertEqual(r.status_code, code)
            self.assertEqual(config.get_cookie(), old)
            self.assertEqual(connections.status("weibo")["state"], "ok")

    async def test_save_checks_without_fetching_and_never_returns_secret(self):
        with patch("app.weibo.all_groups", new=AsyncMock(return_value=[])) as probe:
            async with self.client() as c:
                r = await c.post("/api/settings/cookie", json={"cookie":"SUB=secret; XSRF-TOKEN=secret"})
                self.assertEqual(r.json()["state"], "ok")
                st = (await c.get("/api/status")).json()
                self.assertEqual(st["connections"]["weibo"]["state"], "ok")
                self.assertNotIn("secret", str(st))
            probe.assert_awaited_once()
        if os.name != "nt":
            self.assertEqual(config.COOKIE_FILE.stat().st_mode & 0o777, 0o600)

    async def test_clear_reports_environment_fallback_as_unverified(self):
        config.save_cookie("SUB=web; XSRF-TOKEN=web")
        with patch.object(config, "WB_COOKIE_ENV", "SUB=env; XSRF-TOKEN=env"):
            async with self.client() as c:
                r = await c.delete("/api/settings/cookie")
                self.assertIn("环境配置", r.json()["msg"])
            self.assertEqual(connections.status("weibo")["source"], "env")
            self.assertEqual(connections.status("weibo")["state"], "saved")

    def test_old_crawl_result_cannot_expire_new_cookie(self):
        old, new = "SUB=old; XSRF-TOKEN=a", "SUB=new; XSRF-TOKEN=b"
        config.save_cookie(new)
        connections.record("weibo", new, "ok")
        connections.record("weibo", old, "expired")
        self.assertEqual(connections.status("weibo")["state"], "ok")

    async def test_network_check_does_not_mark_login_expired(self):
        config.save_cookie("SUB=a; XSRF-TOKEN=b")
        with patch("app.weibo.all_groups", new=AsyncMock(side_effect=httpx.ReadTimeout("timeout"))):
            async with self.client() as c:
                r = await c.post("/api/settings/weibo/check")
        self.assertEqual(r.json()["state"], "error")
        self.assertTrue(config.get_cookie())

    async def test_x_full_cookie_roundtrip_and_clear(self):
        token = "a" * 40
        async with self.client() as c:
            r = await c.post("/api/settings/x-auth", json={"auth_token":f"auth_token={token}; ct0=csrf123"})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["state"], "saved")
            self.assertEqual(config.get_x_auth(), token)
            self.assertEqual(config.get_x_csrf(), "csrf123")
            await c.delete("/api/settings/x-auth")
        self.assertEqual(config.get_x_auth(), "")
        self.assertEqual(connections.status("x")["state"], "unset")

    async def test_x_check_uses_existing_list_and_browser_csrf(self):
        db.add_group("x:123", "X 测试")
        token = "a" * 40
        with patch.object(xapi.XGraphQL, "_call", new=AsyncMock(return_value={"data":{"list":{}}})) as probe:
            async with self.client() as c:
                r = await c.post("/api/settings/x-auth", json={"auth_token":token, "ct0":"csrf123"})
        self.assertEqual(r.json()["state"], "ok")
        probe.assert_awaited_once()
        self.assertEqual(xapi.XGraphQL(token, config.get_x_csrf()).headers["x-csrf-token"], "csrf123")

    async def test_x_check_uses_existing_user_target(self):
        db.add_group("xu:luoluokun_", "@luoluokun_")
        token = "a" * 40
        with patch.object(xapi.XGraphQL, "user_id", new=AsyncMock(return_value="999888")) as probe:
            async with self.client() as c:
                r = await c.post("/api/settings/x-auth", json={"auth_token":token, "ct0":"csrf123"})
        self.assertEqual(r.json()["state"], "ok")
        probe.assert_awaited_once()
        self.assertEqual(db.get_meta("x_user_rest_id:luoluokun_"), "999888")

    def test_old_plain_x_auth_file_remains_readable(self):
        config.X_AUTH_FILE.write_text("a" * 40)
        self.assertEqual(config.get_x_auth(), "a" * 40)
        self.assertEqual(config.get_x_csrf(), "")

    async def test_x_403_access_and_explicit_auth_errors_are_distinct(self):
        for payload, kind in (({"errors":[{"code":326,"message":"restricted"}]}, "access"),
                              ({"errors":[{"code":89,"message":"expired"}]}, "auth")):
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request, payload=payload: httpx.Response(403, json=payload))) as c:
                with self.assertRaises(xapi.XApiError) as result:
                    await xapi.XGraphQL("a"*40)._call(c, "/test", {})
                self.assertEqual(result.exception.kind, kind)
