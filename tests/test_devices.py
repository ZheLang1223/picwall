import time
from unittest.mock import patch

import httpx

from app import auth, db
from tests.test_api import ApiTestBase


class DeviceBoundaryTest(ApiTestBase):
    def raw_client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                 base_url="http://localhost")

    async def pair(self, client, role="editor"):
        code = auth.issue_code(role)
        response = await client.post("/api/device/pair", headers={"Origin": "http://localhost"},
                                     json={"code": code, "name": "测试设备"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("token", response.json())
        return response

    async def test_private_data_and_writes_require_pairing(self):
        thumb = db.get_post("demo0000")["pics"][0]["thumb"]
        async with self.raw_client() as c:
            for path in ("/api/posts", "/api/status", "/api/authors", "/api/download/zip", "/api/export/zip", "/media/"+thumb):
                self.assertEqual((await c.get(path)).status_code, 401, path)
            for path in ("/api/purge", "/api/crawl", "/api/settings/cookie", "/api/ingest/harvest"):
                self.assertEqual((await c.post(path, json={}, headers={"Origin":"http://localhost"})).status_code, 401, path)
            self.assertEqual((await c.get("/api/health")).status_code, 200)
            self.assertIn("连接 PWALL", (await c.get("/")).text)

    async def test_cookie_is_private_one_use_and_logout_revokes(self):
        code = auth.issue_code("editor")
        async with self.raw_client() as c:
            r = await c.post("/api/device/pair", headers={"Origin":"http://localhost"}, json={"code":code})
            self.assertIn("HttpOnly", r.headers["set-cookie"])
            self.assertIn("SameSite=strict", r.headers["set-cookie"])
            self.assertEqual((await c.get("/api/posts")).status_code, 200)
            self.assertEqual((await c.post("/api/device/pair", headers={"Origin":"http://localhost"}, json={"code":code})).status_code, 400)
            self.assertEqual((await c.delete("/api/device/session", headers={"Origin":"http://localhost"})).status_code, 200)
            self.assertEqual((await c.get("/api/posts")).status_code, 401)

    async def test_roles_enforced_for_every_management_path(self):
        async with self.raw_client() as c:
            await self.pair(c, "editor")
            for method, path in (("POST","/api/purge"),("POST","/api/settings/cookie"),
                                 ("DELETE","/api/settings/x-auth"),("POST","/api/groups"),
                                 ("DELETE","/api/groups/demo"),("POST","/api/crawl"),
                                 ("POST","/api/demo"),("POST","/api/archive/retry"),
                                 ("DELETE","/api/devices/any")):
                r = await c.request(method,path,headers={"Origin":"http://localhost"},json={})
                self.assertEqual(r.status_code, 403, path)
            self.assertEqual((await c.post("/api/posts/demo0000/status", headers={"Origin":"http://localhost"},json={"status":"kept"})).status_code, 200)
        async with self.raw_client() as c:
            await self.pair(c, "viewer")
            self.assertEqual((await c.get("/api/posts")).status_code, 200)
            self.assertEqual((await c.post("/api/posts/demo0000/favorite", headers={"Origin":"http://localhost"},json={"favorite":True})).status_code, 403)
            self.assertEqual((await c.get("/api/devices")).status_code, 403)

    async def test_origin_host_and_cross_source_ingest(self):
        async with self.raw_client() as c:
            await self.pair(c, "admin")
            for headers in ({},{"Origin":"https://evil.test"},{"Origin":"null"},{"Host":"evil.test","Origin":"http://evil.test"}):
                self.assertEqual((await c.post("/api/posts/demo0000/favorite",headers=headers,json={"favorite":True})).status_code, 403)
            self.assertEqual((await c.get("/api/posts",headers={"Host":"evil.test"})).status_code, 403)
            self.assertFalse(db.get_post("demo0000")["favorite"])
        token = auth.exchange(auth.issue_code("collector"), "采集", "test")["token"]
        async with self.raw_client() as c:
            headers={"Origin":"https://weibo.com","Authorization":"Bearer "+token}
            pre = await c.options("/api/ingest/harvest",headers={"Origin":"https://weibo.com"})
            self.assertIn("authorization",pre.headers["access-control-allow-headers"])
            self.assertNotIn("access-control-allow-credentials", pre.headers)
            self.assertEqual((await c.post("/api/ingest/harvest",headers=headers,json={"list_id":"123","statuses":[]})).status_code, 200)
            self.assertEqual((await c.get("/api/posts",headers={"Authorization":"Bearer "+token})).status_code, 403)
            self.assertEqual((await c.post("/api/settings/cookie",headers=headers,json={})).status_code, 403)
            self.assertEqual((await c.options("/api/ingest/harvest",headers={"Origin":"https://evil.test"})).status_code, 403)
            unauth=await c.post("/api/ingest/harvest",headers={"Origin":"https://weibo.com","Authorization":"Bearer bad"},json={})
            self.assertEqual(unauth.status_code,401)
            self.assertEqual(unauth.headers["access-control-allow-origin"],"https://weibo.com")

    async def test_admin_revokes_existing_device_and_hashes_are_not_tokens(self):
        async with self.raw_client() as admin, self.raw_client() as viewer:
            await self.pair(admin,"admin")
            response=await self.pair(viewer,"viewer")
            token=viewer.cookies.get(auth.COOKIE)
            with db.conn() as c:
                self.assertIsNone(c.execute("SELECT id FROM device_sessions WHERE hash=?",(token,)).fetchone())
            listing=(await admin.get("/api/devices")).json()["devices"]
            self.assertEqual(len(listing),2)
            self.assertTrue(all("hash" not in d for d in listing))
            self.assertEqual((await admin.delete("/api/devices/"+response.json()["id"],headers={"Origin":"http://localhost"})).status_code,200)
            self.assertEqual((await viewer.get("/api/posts")).status_code,401)

    async def test_expiry_and_attempt_budget(self):
        code=auth.issue_code()
        with patch.object(auth.time,"time",return_value=time.time()+601):
            self.assertEqual(auth.exchange(code,"test","host")["status"],400)
        for _ in range(10):
            self.assertEqual(auth.exchange("wrong","test","another")["status"],400)
        self.assertEqual(auth.exchange(auth.issue_code(),"test","another")["status"],429)

    async def test_chunked_ingest_has_a_body_budget(self):
        tok = auth.exchange(auth.issue_code("collector"), "ext", "collector-test")["token"]

        async def chunks():
            for _ in range(3):
                yield b"x" * (1024 * 1024)
        async with self.raw_client() as c:
            response = await c.post("/api/ingest/harvest", content=chunks(),
                                    headers={"Origin":"https://weibo.com","Content-Type":"application/json",
                                             "Authorization":f"Bearer {tok}"})
            self.assertEqual(response.status_code,413)
            self.assertIn("拆分",response.json()["detail"])
            self.assertEqual(response.headers["access-control-allow-origin"],"https://weibo.com")


class LocalOwnerTest(ApiTestBase):
    def owner_client(self):
        from app.serve import LocalOwner
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=LocalOwner(self.server.app)),
                                 base_url="http://localhost:8081")

    async def test_local_listener_enters_without_pairing_and_can_manage(self):
        async with self.owner_client() as c:
            page = await c.get("/")
            self.assertIn('data-local-owner="true"', page.text)
            self.assertNotIn("连接 PWALL", page.text)
            self.assertNotIn('id="device-logout"', page.text)
            self.assertEqual((await c.get("/api/posts")).status_code, 200)
            device = (await c.get("/api/device/session")).json()
            self.assertEqual(device["role"], "admin")
            self.assertTrue(device["local_owner"])
            self.assertEqual((await c.get("/api/devices")).json()["devices"], [])
            headers = {"Origin": "http://localhost:8081"}
            r = await c.post("/api/posts/demo0000/favorite", headers=headers, json={"favorite": True})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(db.get_post("demo0000")["favorite"])
            self.assertEqual((await c.delete("/api/device/session", headers=headers)).status_code, 409)
            self.assertEqual((await c.get("/api/posts")).status_code, 200)

    async def test_local_listener_does_not_trust_other_hosts_or_origins(self):
        from app import config
        async with self.owner_client() as c:
            for origin in ("https://evil.test", "null", "chrome-extension://unpaired"):
                r = await c.post("/api/posts/demo0000/favorite", json={"favorite": True}, headers={"Origin": origin})
                self.assertEqual(r.status_code, 403, origin)
            # 就算 LAN 主机在总白名单里，本机监听器也不能接受它。
            with patch.object(config, "ALLOWED_HOSTS", config.ALLOWED_HOSTS | {"192.168.1.23"}):
                r = await c.get("/api/posts", headers={"Host": "192.168.1.23:8081"})
                self.assertEqual(r.status_code, 403)
            self.assertFalse(db.get_post("demo0000")["favorite"])

    async def test_cross_site_subresource_gets_no_local_owner(self):
        """浏览器跨站子资源（img 等）无 Origin 头但有 Sec-Fetch-Site: cross-site：
        读不到响应却会以 local_owner 身份触发读类 GET 副作用，必须挡下。"""
        async with self.owner_client() as c:
            r = await c.get("/api/posts", headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status_code, 401)
            r = await c.get("/api/status", headers={"Sec-Fetch-Site": "cross-site"})
            self.assertEqual(r.status_code, 401)
            # 同源与无该头的客户端（curl/扩展）行为不变
            self.assertEqual((await c.get("/api/posts")).status_code, 200)
            self.assertEqual((await c.get("/api/posts",
                                          headers={"Sec-Fetch-Site": "same-origin"})).status_code, 200)
            status = (await c.get("/api/status")).json()
            self.assertIn("pairing", status)
            self.assertIn("fixed_code_weak", status["pairing"])

    async def test_lan_listener_cannot_claim_local_owner_with_headers(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                     base_url="http://localhost:8082") as c:
            r = await c.get("/api/posts", headers={"X-Pwall-Local-Owner": "true",
                                                  "X-Forwarded-For": "127.0.0.1", "Host": "localhost:8081"})
            self.assertEqual(r.status_code, 401)
            self.assertIn("连接 PWALL", (await c.get("/")).text)


class PairCodeIssueTest(ApiTestBase):
    """「手机扫码连接」弹窗的配对码签发：admin 专属、码可真实兑换、一次性。"""

    def role_headers(self, role):
        result = auth.exchange(auth.issue_code(role), f"{role}-机", "test")
        return {"Host": "localhost", "Origin": "http://localhost",
                "Authorization": "Bearer " + result["token"]}

    async def test_issue_code_exchanges_once_with_right_role(self):
        async with self.client() as c:
            r = await c.post("/api/device/code", json={"role": "editor"})
            self.assertEqual(r.status_code, 200)
            code = r.json()["code"]
            self.assertEqual(len(code.replace("-", "")), 12)
            self.assertEqual(r.json()["ttl"], 600)
            pair = await c.post("/api/device/pair",
                                json={"code": code, "name": "码签发机", "bearer": True})
            self.assertEqual(pair.status_code, 200)
            token = pair.json()["token"]
            sess = await c.get("/api/device/session",
                               headers={"Authorization": "Bearer " + token})
            self.assertEqual(sess.json()["role"], "editor")
            # 同一码不可复用
            again = await c.post("/api/device/pair",
                                 json={"code": code, "name": "重放机", "bearer": True})
            self.assertNotEqual(again.status_code, 200)

    async def test_issue_code_admin_only_and_validates_role(self):
        async with self.client() as c:
            self.assertEqual((await c.post("/api/device/code",
                                           json={"role": "nope"})).status_code, 400)
        for role in ("editor", "viewer"):
            async with self.client() as c:
                r = await c.post("/api/device/code", json={"role": "editor"},
                                 headers=self.role_headers(role))
                self.assertEqual(r.status_code, 403, role)


class FixedPairCodeTest(ApiTestBase):
    """固定配对码（PWALL_PAIR_CODE）：长期有效、可重复使用、角色可配；限流与错码口径不变。"""

    def raw_client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                 base_url="http://localhost")

    def set_code(self, code, role="editor"):
        return patch.multiple(auth.config, PAIR_CODE=code, PAIR_ROLE=role)

    async def test_fixed_code_pairs_repeatedly_with_configured_role(self):
        with self.set_code("TESTFIXEDCODE", "viewer"):
            async with self.raw_client() as c:
                for name in ("手机一", "手机二"):
                    r = await c.post("/api/device/pair", headers={"Origin": "http://localhost"},
                                     json={"code": "TEST-FIXED-CODE", "name": name})
                    self.assertEqual(r.status_code, 200, name)
                    self.assertEqual(r.json()["role"], "viewer")
                # 横线与大小写不影响匹配
                r = await c.post("/api/device/pair", headers={"Origin": "http://localhost"},
                                 json={"code": "test-fixed-code", "name": "手机三"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["role"], "viewer")

    async def test_wrong_code_budget_still_applies_to_fixed_code(self):
        with self.set_code("TESTFIXEDCODE"):
            for _ in range(10):
                self.assertEqual(auth.exchange("WRONGCODE99", "test", "host")["status"], 400)
            # 错码耗尽尝试预算后，正确的固定码同样被限流挡下
            self.assertEqual(auth.exchange("TESTFIXEDCODE", "test", "host")["status"], 429)

    async def test_temp_code_still_works_alongside_fixed_code(self):
        with self.set_code("TESTFIXEDCODE", "editor"):
            async with self.raw_client() as c:
                r = await c.post("/api/device/pair", headers={"Origin": "http://localhost"},
                                 json={"code": auth.issue_code("admin"), "name": "临时机"})
                self.assertEqual(r.json()["role"], "admin")
                r = await c.post("/api/device/pair", headers={"Origin": "http://localhost"},
                                 json={"code": "TESTFIXEDCODE", "name": "固定机"})
                self.assertEqual(r.json()["role"], "editor")
