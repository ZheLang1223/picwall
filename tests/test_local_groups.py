"""X 本地分组（xg:）：成员映射、帖子归组、存量回填、删除语义与抓取解包。

跑法与其余用例相同：项目根目录 `python -m unittest discover -s tests -t .`。
"""
import unittest

from app import config, crawler, db
from tests.test_api import ApiTestBase


def _x_post(sid, handle="artist1"):
    return {"sid": sid, "uid": f"x_uid_{handle}", "author": f"@{handle}",
            "avatar": "", "text": f"text {sid}", "created_at": "2026-11-01T10:00:00+08:00",
            "fetched_at": "2026-11-01T10:00:00+08:00", "weibo_url": "https://x.com",
            "via": "", "source": "x"}


class LocalGroupTest(ApiTestBase):
    def setUp(self):
        super().setUp()
        db.add_group("xu:artist1", "@artist1")
        db.add_group("xu:artist2", "@artist2")
        db.add_group("xg:a1b2c3d4", "画师")

    async def test_member_end_to_end(self):
        """加成员即回填存量；新帖入库自动归组；退组保留历史；删组清关联不删帖。"""
        db.insert_post(_x_post("t1"), "xu:artist1")   # 先有帖，后建成员关系 → 应回填
        async with self.client() as c:
            r = await c.post("/api/groups/xg:a1b2c3d4/members", json={"member": "@Artist1"})
            self.assertEqual(r.status_code, 200)   # @handle 大小写归一到 xu:artist1
            r = await c.get("/api/group-members")
            by_group = r.json()["by_group"]
            self.assertIn("xu:artist1", [m["member_id"] for m in by_group["xg:a1b2c3d4"]])
        with db.conn() as c:
            linked = {r[0] for r in c.execute(
                "SELECT list_id FROM post_groups WHERE sid='t1'")}
        self.assertEqual(linked, {"xu:artist1", "xg:a1b2c3d4"})   # 存量回填生效

        db.insert_post(_x_post("t2"), "xu:artist1")   # 成员关系建立后的新帖 → 自动归组
        db.insert_post(_x_post("t3"), "xu:artist2")   # 非成员 → 不进组
        with db.conn() as c:
            in_group = {r[0] for r in c.execute(
                "SELECT sid FROM post_groups WHERE list_id='xg:a1b2c3d4'")}
        self.assertEqual(in_group, {"t1", "t2"})

        async with self.client() as c:   # 退组：映射删、历史归组保留（采集事实）
            r = await c.delete("/api/groups/xg:a1b2c3d4/members/xu:artist1")
            self.assertEqual(r.status_code, 200)
        db.insert_post(_x_post("t4"), "xu:artist1")   # 退组后新帖不再进组
        with db.conn() as c:
            in_group = {r[0] for r in c.execute(
                "SELECT sid FROM post_groups WHERE list_id='xg:a1b2c3d4'")}
        self.assertEqual(in_group, {"t1", "t2"})
        self.assertEqual(db.member_groups("xu:artist1"), [])

        async with self.client() as c:   # 删组：组、成员、归组行全清；帖子不动
            await c.delete("/api/groups/xg:a1b2c3d4")
        with db.conn() as c:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM post_groups WHERE list_id='xg:a1b2c3d4'").fetchone()[0], 0)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM group_members").fetchone()[0], 0)
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM posts WHERE sid IN ('t1','t2')").fetchone()[0], 2)

    async def test_create_group_and_membership_rules(self):
        """xg: 建组生成 slug；非 xg 组拒管成员；未添加用户拒入组。"""
        async with self.client() as c:
            r = await c.post("/api/groups", json={"list_id": "xg:", "name": "摄影"})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["list_id"].startswith("xg:"))

            r = await c.post("/api/groups/xu:artist1/members", json={"member": "@artist2"})
            self.assertEqual(r.status_code, 400)   # 只有 xg: 能管成员

            r = await c.post("/api/groups/xg:9999ffff/members", json={"member": "@nobody"})
            self.assertEqual(r.status_code, 404)   # 组不存在

            r = await c.post("/api/groups/xg:a1b2c3d4/members", json={"member": "@nobody"})
            self.assertEqual(r.status_code, 404)   # 用户目标未添加

            r = await c.post("/api/groups/xg:a1b2c3d4/members", json={"member": "artist2"})
            self.assertEqual(r.status_code, 200)   # 裸 handle 也可

    async def test_filter_and_source_scope(self):
        """xg: 组进 x 来源域；列表与作者聚合按 post_groups 命中组内成员的帖。"""
        db.add_group_member("xg:a1b2c3d4", "xu:artist1")
        db.add_group_member("xg:a1b2c3d4", "xu:artist2")
        db.insert_post(_x_post("g1", "artist1"), "xu:artist1")
        db.insert_post(_x_post("g2", "artist2"), "xu:artist2")
        db.insert_post({"sid": "w1", "uid": "u1", "author": "微博作者", "avatar": "",
                        "text": "t", "created_at": "2026-11-01T09:00:00+08:00",
                        "fetched_at": "2026-11-01T09:00:00+08:00", "weibo_url": "https://weibo.com",
                        "via": "", "source": "weibo"}, "demo")
        # xg: 归 x 域：微博抓取入口不会碰到它
        self.assertTrue(all(g["list_id"] != "xg:a1b2c3d4"
                            for g in db.active_groups(source="weibo")))
        self.assertTrue(any(g["list_id"] == "xg:a1b2c3d4"
                            for g in db.active_groups(source="x")))
        async with self.client() as c:
            r = await c.get("/api/posts", params={"status": "all", "group": "xg:a1b2c3d4"})
            sids = {p["sid"] for p in r.json()["posts"]}
            self.assertEqual(sids, {"g1", "g2"})   # 组筛选 = 全体成员的帖，微博帖不进

            r = await c.get("/api/authors", params={"group": "xg:a1b2c3d4", "status": "all"})
            names = {a["author"] for a in r.json()["authors"]}
            self.assertEqual(names, {"@artist1", "@artist2"})

    async def test_crawl_x_unpacks_local_group(self):
        """crawl_x(only=xg:) 解包成员；空组给出可操作报错。"""
        from unittest.mock import AsyncMock, patch
        with patch.object(config, "X_AUTH_FILE", self.tmp / "x_auth.txt"):
            config.save_x_auth("fake-token")
            db.remove_group("demo")
            r = await crawler.crawl_x(only="xg:a1b2c3d4")
            self.assertFalse(r["ok"])
            self.assertIn("还没有成员", r["msg"])

            db.add_group_member("xg:a1b2c3d4", "xu:artist1")
            fake = AsyncMock()
            with patch.object(crawler, "x_http_client", return_value=fake), \
                 patch.object(crawler.xapi, "crawl_users", new=AsyncMock(
                     return_value=[{"name": "@artist1", "outcome": "success",
                                    "new_posts": 0, "new_pics": 0}])) as mock_users, \
                 patch.object(crawler.xapi, "crawl_lists", new=AsyncMock(return_value=[])):
                r = await crawler.crawl_x(only="xg:a1b2c3d4")
            self.assertTrue(r["ok"])
            users_arg = mock_users.call_args.args[2]   # crawl_users(client, token, users, ...)
            self.assertEqual([u["handle"] for u in users_arg], ["artist1"])


if __name__ == "__main__":
    unittest.main()
