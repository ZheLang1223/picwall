"""随机档（/api/posts?shuffle=seed）的回归钉子：种子稳定性、keyed 游标翻页、
筛选/搜索同口径、锚点帖中途消失不漂移。

跑法与 test_api 相同：项目根目录 `python -m unittest discover -s tests -t .`。
用独立分组 shuffle-t 隔离（setUp 的 demo.seed() 也灌了 pending 帖，不能混进口径）。
"""
import unittest
from datetime import datetime
from typing import ClassVar

from app import config, db
from tests.test_api import ApiTestBase


def make_shuffled_post(sid, *, created_at, status="pending"):
    """造一帖落库到 shuffle-t 分组（created_at 控制时间序，status 控制筛选面）。"""
    p = {"sid": sid, "uid": f"u-{sid}", "author": f"作者{sid}", "avatar": "",
         "text": f"文本{sid}", "created_at": created_at, "fetched_at": created_at,
         "weibo_url": "https://weibo.com", "via": "", "source": "weibo"}
    db.insert_post(p, "shuffle-t")
    if status != "pending":
        # 决定时间戳触发器要求：kept 带 kept_at、discarded 带 discarded_at，二者互斥
        stamp = created_at
        with db._write_lock, db.conn() as c:
            c.execute("UPDATE posts SET status=?, kept_at=?, discarded_at=? WHERE sid=?",
                      (status, stamp if status == "kept" else None,
                       stamp if status == "discarded" else None, sid))


def seed_sids(n=12):
    sids = []
    for i in range(n):
        sid = f"s{i:02d}"
        make_shuffled_post(sid, created_at=f"2026-09-{i + 1:02d}T10:00:00+08:00")
        sids.append(sid)
    return sids


class ShuffleTest(ApiTestBase):
    GROUP: ClassVar[dict] = {"status": "all", "group": "shuffle-t"}

    async def _order(self, client, **params):
        r = await client.get("/api/posts", params={**self.GROUP, **params})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        return body, [p["sid"] for p in body["posts"]]

    async def test_seed_stable_and_seed_sensitive(self):
        """同 seed 两次全序一致（含日期形状的 daily 种子）；换 seed 换一套排法；
        结果集恒等——随机只重排，不增删。"""
        sids = seed_sids()
        async with self.client() as c:
            _, order_a = await self._order(c, shuffle="seed-a", limit=200)
            _, order_a2 = await self._order(c, shuffle="seed-a", limit=200)
            _, order_d = await self._order(c, shuffle="d2026-11-07", limit=200)
            _, order_d2 = await self._order(c, shuffle="d2026-11-07", limit=200)
            _, order_b = await self._order(c, shuffle="seed-b", limit=200)
        self.assertEqual(order_a, order_a2)
        self.assertEqual(order_d, order_d2)
        self.assertNotEqual(order_a, order_b)
        self.assertEqual(sorted(order_a), sorted(sids))

    async def test_cursor_pagination_no_dup_no_gap(self):
        """keyed 游标逐页走完 = 单发全序；不重不漏，total 与 has_more 收口一致。"""
        seed_sids()
        async with self.client() as c:
            body, full = await self._order(c, shuffle="p", limit=200)
            seen, cursor = [], None
            while True:
                params = {"shuffle": "p", "limit": 5}
                if cursor:
                    params["cursor"] = cursor
                body, order = await self._order(c, **params)
                seen += order
                if not body["has_more"]:
                    break
                cursor = body["next_cursor"]
                self.assertIsNotNone(cursor)
        self.assertEqual(seen, full)
        self.assertEqual(len(seen), len(set(seen)))

    async def test_cursor_survives_tail_vanishing(self):
        """翻页中途首屏整页（含游标锚点帖）被转出筛选面：游标按 key 二分照常落位，
        已看的帖不重发，没看的帖不跳过。"""
        seed_sids()
        async with self.client() as c:
            params = {"group": "shuffle-t", "status": "pending", "shuffle": "m", "limit": 5}
            body, first = await self._order(c, **params)
            for sid in first:
                with db._write_lock, db.conn() as c2:
                    c2.execute("UPDATE posts SET status='discarded', kept_at=NULL, "
                               "discarded_at=? WHERE sid=?",
                               (datetime.now(config.NOW_TZ).isoformat(timespec="seconds"), sid))
            seen = list(first)
            while body["has_more"]:
                body, order = await self._order(
                    c, **{**params, "cursor": body["next_cursor"]})
                seen += order
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 12)   # 首屏 5（已转出也在账）+ 后续 7，一张不缺
        self.assertEqual(set(seen), {f"s{i:02d}" for i in range(12)})

    async def test_filter_and_search_carry_through(self):
        """随机档与筛选/搜索同口径：status/q 都进虚拟全序，total = 命中数；
        空命中返回空页不报错。"""
        seed_sids(6)
        make_shuffled_post("k1", created_at="2026-09-20T10:00:00+08:00", status="kept")
        make_shuffled_post("k2", created_at="2026-09-21T10:00:00+08:00", status="kept")
        async with self.client() as c:
            body, order = await self._order(c, status="kept", shuffle="f", limit=200)
            self.assertEqual(sorted(order), ["k1", "k2"])
            self.assertEqual(body["total"], 2)
            body2, order2 = await self._order(c, q="文本s01", shuffle="f", limit=200)
            self.assertEqual(order2, ["s01"])
            self.assertEqual(body2["total"], 1)
            body3, _ = await self._order(c, q="不存在的词", shuffle="f", limit=200)
            self.assertEqual(body3["posts"], [])
            self.assertEqual(body3["total"], 0)
            self.assertFalse(body3["has_more"])

    async def test_seed_ignores_whitespace_and_caps_length(self):
        """seed 去首尾空白；超长截断后仍按截断值稳定（防御性，不参与语义）。"""
        seed_sids(4)
        async with self.client() as c:
            _, trimmed = await self._order(c, shuffle="  ab  ", limit=200)
            _, plain = await self._order(c, shuffle="ab", limit=200)
            _, long1 = await self._order(c, shuffle="x" * 200, limit=200)
            _, long2 = await self._order(c, shuffle="x" * 64, limit=200)
        self.assertEqual(trimmed, plain)
        self.assertEqual(long1, long2)


if __name__ == "__main__":
    unittest.main()
