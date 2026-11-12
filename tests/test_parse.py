"""解析层单测：跑法 = 容器内 python -m unittest discover -s tests -t .

微博接口字段若变，先改 fixture 再跑这里，parse 挂了就知道哪断了。
"""
import json
import unittest
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from app.crawler import deep_stop
from app.weibo import (
    extract_statuses,
    parse_groups,
    parse_mblog,
    parse_time,
    strip_html,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixture_groupstimeline.json").read_text(encoding="utf-8"))


class ParseMblogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.posts = [p for p in (parse_mblog(i) for i in FIXTURE["data"]) if p]

    def test_skips_video_only(self):
        self.assertEqual(len(self.posts), 2)
        self.assertFalse(any(p["sid"] == "VideoOnly" for p in self.posts))

    def test_normal_post_pics(self):
        p = self.posts[0]
        self.assertEqual(p["sid"], "PkAfBcDe")
        self.assertEqual(len(p["pics"]), 2)
        self.assertEqual(p["pics"][0]["full"], "https://wx1.sinaimg.cn/large/abc1.jpg")
        self.assertEqual(p["pics"][1]["full"], "https://wx1.sinaimg.cn/large/abc2.png")
        self.assertTrue(p["pics"][0]["mid"].startswith("https://wx1.sinaimg.cn/"))

    def test_text_stripped(self):
        text = self.posts[0]["text"]
        self.assertIn("西湖的黄昏", text)
        self.assertIn("光线绝了 推荐", text)
        self.assertNotIn("<", text)

    def test_retweet_uses_original(self):
        p = self.posts[1]
        self.assertEqual(p["sid"], "Orig1234")
        self.assertEqual(p["author"], "原博主王五")
        self.assertEqual(p["via"], "爱转发的李四")
        self.assertEqual(p["weibo_url"], "https://weibo.com/333/Orig1234")
        self.assertEqual(p["pics"][0]["full"], "https://wx2.sinaimg.cn/large/def.jpg")
        self.assertIn("转发", p["text"])

    def test_size_fallback_derives_large(self):
        # 只有 orj480 小图时也要能推出 large 地址
        item = {"mblog": {
            "mblogid": "NoLarge1", "created_at": "刚刚",
            "user": {"id": 1, "screen_name": "a"},
            "text": "t",
            "pics": [{"pid": "x", "url": "https://wx3.sinaimg.cn/orj480/z.jpg"}],
        }}
        p = parse_mblog(item)
        self.assertEqual(p["pics"][0]["full"], "https://wx3.sinaimg.cn/large/z.jpg")

    def test_orj960_sti960_derive_large(self):
        # feed 常用的 orj960/sti960 缩放档同样要推出 large（曾漏掉致全库存小图）
        item = {"mblog": {
            "mblogid": "Orj960x1", "created_at": "刚刚",
            "user": {"id": 1, "screen_name": "a"}, "text": "t",
            "pics": [
                {"pid": "p1", "url": "https://wx3.sinaimg.cn/orj960/z.jpg"},
                {"pid": "p2", "url": "https://wx3.sinaimg.cn/sti960/g.gif"},
            ],
        }}
        p = parse_mblog(item)
        self.assertEqual(p["pics"][0]["full"], "https://wx3.sinaimg.cn/large/z.jpg")
        self.assertEqual(p["pics"][1]["full"], "https://wx3.sinaimg.cn/large/g.gif")
        # mid 保持中档作 large 拉不到时的回退位
        self.assertEqual(p["pics"][0]["mid"], "https://wx3.sinaimg.cn/orj960/z.jpg")

    def test_pic_infos_prefers_largest(self):
        # pic_infos 里 largest 是最高质量档，优先于 large
        item = {"mblog": {
            "mblogid": "Largest1", "created_at": "刚刚",
            "user": {"id": 1, "screen_name": "a"}, "text": "t",
            "pic_infos": {"pid1": {
                "largest": {"url": "https://wx1.sinaimg.cn/large/big.jpg"},
                "large": {"url": "https://wx1.sinaimg.cn/mw2000/mid.jpg"},
            }},
        }}
        p = parse_mblog(item)
        self.assertEqual(p["pics"][0]["full"], "https://wx1.sinaimg.cn/large/big.jpg")
        # 没有 mw2000/mw690 时 mid 回退为 full
        self.assertEqual(p["pics"][0]["mid"], "https://wx1.sinaimg.cn/large/big.jpg")


class TimeTest(unittest.TestCase):
    def test_standard_format(self):
        iso = parse_time("Wed Sep 09 21:03:14 +0800 2026")
        self.assertEqual(datetime.fromisoformat(iso).strftime("%Y-%m-%dT%H:%M:%S"),
                         "2026-09-09T21:03:14")

    def test_relative_minutes(self):
        t = datetime.fromisoformat(parse_time("30分钟前"))
        self.assertLess((datetime.now(t.tzinfo) - t).total_seconds(), 31 * 60)

    def test_today_and_yesterday(self):
        self.assertIn("T08:10:00", parse_time("今天 08:10"))
        self.assertEqual(parse_time("昨天 23:05")[11:16], "23:05")

    def test_garbage_falls_back_to_now(self):
        self.assertTrue(parse_time("未知格式").startswith(datetime.now().strftime("%Y-%m-%d")))


class StripHtmlTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(strip_html("<b>a</b><br/>b &amp; c"), "a b & c")


class GroupsEnvelopeTest(unittest.TestCase):
    """用 2026-09-12 实测的 allGroups 信封结构做回归。"""

    DATA: ClassVar[dict] = {
        "ok": 1,
        "groups": [
            {"title": "默认分组", "group": [
                {"gid": "100016385850138", "title": "全部关注", "sysgroup": 2},
                {"gid": "4161416150276989", "title": "特别关注", "sysgroup": 1},
            ]},
            {"title": "我的分组", "group": [
                {"gid": "4202393614228265", "title": "科学", "sysgroup": 0},
                {"gid": "5029356869783529", "title": "绘画", "sysgroup": 0},
            ]},
        ],
    }

    def test_parse_groups_filters_system(self):
        groups = parse_groups(self.DATA)
        ids = [g["list_id"] for g in groups]
        self.assertIn("4202393614228265", ids)          # 自建分组保留
        self.assertIn("4161416150276989", ids)          # 特别关注保留
        self.assertNotIn("100016385850138", ids)        # 系统分组过滤
        names = {g["list_id"]: g["name"] for g in groups}
        self.assertEqual(names["4202393614228265"], "科学")

    def test_extract_statuses_envelope(self):
        # 真实 groupstimeline 信封：数组在 statuses 键下
        self.assertEqual(len(extract_statuses(
            {"ok": 1, "statuses": [{"mblog": {}}], "total_number": 296,
             "max_id": "5340561263561102"})), 1)
        self.assertEqual(extract_statuses({"ok": 1, "statuses": []}), [])
        self.assertEqual(len(extract_statuses({"ok": 1, "data": [{"mblog": {}}]})), 1)
        self.assertEqual(extract_statuses(None), [])


class DeepStopTest(unittest.TestCase):
    """回补翻页终止逻辑：整页旧帖且越过窗口边界才收。"""
    CUTOFF = "2026-08-13T00:00:00+08:00"

    @staticmethod
    def page(*times):
        return [{"sid": f"s{i}", "created_at": t} for i, t in enumerate(times)]

    def test_empty_page_stops(self):
        self.assertTrue(deep_stop([], 0, self.CUTOFF))

    def test_fresh_content_continues_even_if_page_touches_past(self):
        page = self.page("2026-09-01T10:00:00+08:00", "2026-08-01T10:00:00+08:00")
        self.assertFalse(deep_stop(page, 2, self.CUTOFF))

    def test_all_seen_within_window_continues(self):
        page = self.page("2026-09-01T10:00:00+08:00")
        self.assertFalse(deep_stop(page, 0, self.CUTOFF))

    def test_all_old_beyond_cutoff_stops(self):
        page = self.page("2026-08-01T10:00:00+08:00", "2026-07-20T10:00:00+08:00")
        self.assertTrue(deep_stop(page, 0, self.CUTOFF))


if __name__ == "__main__":
    unittest.main()
