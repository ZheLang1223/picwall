"""成品冲刺新增能力回归：清理引用计数 / zip 命名 / 长文端点 / 作者聚合 / 导出去重。

跑法：python -m unittest discover -s tests -t . （与 test_parse、test_api 同一套临时数据目录机制）
"""
import io
import unittest
from datetime import datetime, timedelta

import httpx
from PIL import Image

from app import config, db, demo, purge, storage
from tests.auth_support import admin_headers


def _jpeg(w=64, h=48):
    im = Image.new("RGB", (w, h), (90, 110, 130))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


class SprintTestBase(unittest.IsolatedAsyncioTestCase):
    """临时数据目录 + ASGI 直打，不碰真实 data/。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = Path(tempfile.mkdtemp(prefix="picwall-sprint-"))
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
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app),
                                 base_url="http://test", headers=admin_headers())

    def _post_with_file(self, sid, uid="u1", author="作者甲", status="pending",
                        discarded_at=None):
        data = _jpeg()
        rel, w, h, t = storage.save_image(data, "x.jpg")
        with db.conn() as c:
            c.execute(
                "INSERT INTO posts(sid,list_id,uid,author,avatar,text,created_at,"
                "fetched_at,weibo_url,via,status,discarded_at,kept_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "L1", uid, author, "", f"帖{sid}", "2026-09-01T10:00:00+08:00",
                 "2026-09-01T10:00:00+08:00", f"https://weibo.com/1/{sid}", "",
                 status, discarded_at if status == 'discarded' else None,
                 "2026-09-01T10:00:00+08:00" if status == 'kept' else None))
            c.execute("INSERT INTO pics(sid,file,thumb,w,h,url) VALUES(?,?,?,?,?,?)",
                      (sid, rel, t, w, h, "u"))
        return rel


class PurgeTest(SprintTestBase):
    """引用计数保护：无人引用的文件删，仍被别人引用的文件留。"""

    def test_purge_removes_expired_discarded(self):
        rel = self._post_with_file("old1", status="discarded",
                                   discarded_at="2026-08-01T00:00:00+08:00")
        r = purge.purge_expired(7)
        self.assertEqual(r["purged_posts"], 1)
        self.assertFalse(db.has_post("old1"))
        src = config.PICS_DIR / rel
        thumb = src.parent / f"t_{src.name}"
        self.assertFalse(src.exists())
        self.assertFalse(thumb.exists())

    def test_recent_discarded_survives(self):
        # 夹具日期必须相对当前时间算：硬编码「最近」日期会随日历老化，
        # 落进 7 天清理窗后本测试就变成定时炸弹（2026-09-10 到 09-18 就炸过一次）
        recent = (datetime.now(config.NOW_TZ) - timedelta(days=1))\
            .isoformat(timespec="seconds")
        self._post_with_file("new1", status="discarded", discarded_at=recent)
        r = purge.purge_expired(7)
        self.assertEqual(r["purged_posts"], 0)
        self.assertTrue(db.has_post("new1"))

    async def test_purge_tombstone_blocks_reimport(self):
        """purge 硬删写墓碑：同 sid 再投喂任何入库通道都不复活。"""
        old = (datetime.now(config.NOW_TZ) - timedelta(days=8)).isoformat(timespec="seconds")
        self._post_with_file("Tk1", status="discarded", discarded_at=old)
        r = purge.purge_expired(1)
        self.assertEqual(r["purged_posts"], 1)
        self.assertFalse(db.has_post("Tk1"))
        self.assertTrue(db.has_tombstone("Tk1"))

        # harvest 通道投喂同 sid（伪造下载成功，排除干扰）：被墓碑拦下，不复活
        import app.crawler as crawler_mod
        orig = crawler_mod._download
        crawler_mod._download = lambda *a, **k: _jpeg(64, 48)
        try:
            status = {"mblog": {
                "id": "Tk1", "created_at": "Sun Sep 27 10:00:00 +0800 2026",
                "user": {"id": 1, "screen_name": "作者甲"},
                "pics": [{"url": "http://wx1.sinaimg.cn/large/abc.jpg"}]}}
            async with self.client() as client:
                resp = await client.post(
                    "/api/ingest/harvest",
                    json={"list_id": "4162896600502586", "name": "站住",
                          "statuses": [status]})
        finally:
            crawler_mod._download = orig
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["new_posts"], 0)
        self.assertFalse(db.has_post("Tk1"))

    def test_purge_refcheck_and_unlink_hold_write_lock(self):
        """refcheck + unlink 必须全程持 _write_lock：与并发 insert_pic 的赛窗靠同锁闭合。"""
        old = (datetime.now(config.NOW_TZ) - timedelta(days=8)).isoformat(timespec="seconds")
        rel = self._post_with_file("Lk1", status="discarded", discarded_at=old)
        orig = storage.remove_pic_files
        seen = {}
        storage.remove_pic_files = lambda path: (seen.__setitem__(
            "locked", db._write_lock.locked()), orig(path))[1]
        try:
            r = purge.purge_expired(1)
        finally:
            storage.remove_pic_files = orig
        self.assertEqual(r["purged_posts"], 1)
        self.assertFalse((config.PICS_DIR / rel).exists())
        self.assertTrue(seen.get("locked"), "unlink 发生在写锁之外——赛窗未闭合")

    async def test_list_posts_text_search(self):
        """q 参数按正文过滤。"""
        self._post_with_file("Q1", author="甲")
        self._post_with_file("Q2", author="乙")
        with db.conn() as c:
            c.execute("UPDATE posts SET text=? WHERE sid=?", ("苹果派的做法", "Q1"))
            c.execute("UPDATE posts SET text=? WHERE sid=?", ("狗狗散步中", "Q2"))
        async with self.client() as client:
            r = await client.get("/api/posts?status=all&group=all&author=all"
                                 "&range=all&offset=0&limit=50&q=%E8%8B%B9%E6%9E%9C")
            empty = await client.get("/api/posts?status=all&group=all&author=all"
                                     "&range=all&offset=0&limit=50&q=%E4%B8%8D%E5%AD%98%E5%9C%A8")
        self.assertEqual([x["sid"] for x in r.json()["posts"]], ["Q1"])
        self.assertEqual(empty.json()["total"], 0)

    async def test_list_posts_favorites_view(self):
        """status=favorites 服务端真过滤（旧链接 ?fav=1 前端已折叠成该视图）：分享的收藏视图不能失真。"""
        self._post_with_file("Fv1")
        self._post_with_file("Fv2")
        db.set_favorite("Fv1", True)
        async with self.client() as client:
            r = await client.get("/api/posts?status=favorites&group=all&author=all"
                                 "&range=all&offset=0&limit=50")
        self.assertEqual([x["sid"] for x in r.json()["posts"]], ["Fv1"])
        self.assertEqual(r.json()["total"], 1)

    async def test_status_purge_preview(self):
        """/api/status 带清理预览：days + 到期计数。"""
        old = (datetime.now(config.NOW_TZ) - timedelta(days=8)).isoformat(timespec="seconds")
        self._post_with_file("Pv1", status="discarded", discarded_at=old)
        async with self.client() as client:
            r = await client.get("/api/status")
        p = r.json()["purge"]
        self.assertEqual(p["days"], config.PURGE_DAYS)
        self.assertGreaterEqual(p["due"], 1)

    def test_shared_file_is_refcount_protected(self):
        # 同一张图被两个帖引用：删掉其中过期的，文件必须留下
        rel = self._post_with_file("dead", status="discarded",
                                   discarded_at="2026-08-01T00:00:00+08:00")
        (config.PICS_DIR / rel).read_bytes()   # 顺带验证文件可读
        with db.conn() as c:
            c.execute(
                "INSERT INTO posts(sid,list_id,uid,author,avatar,text,created_at,"
                "fetched_at,weibo_url,via) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("alive", "L1", "u1", "甲", "", "x",
                 "2026-09-01T10:00:00+08:00", "2026-09-01T10:00:00+08:00", "u", ""))
            c.execute("INSERT INTO pics(sid,file,thumb,w,h,url) VALUES('alive',?,?,1,1,'u')",
                      (rel, rel))

        purge.purge_expired(7)
        self.assertFalse(db.has_post("dead"))
        self.assertTrue(db.has_post("alive"))
        self.assertTrue((config.PICS_DIR / rel).exists())   # 引用计数保护


class StorePostLockTest(SprintTestBase):
    async def test_save_and_insert_hold_write_lock(self):
        """入库「写文件 + 插行」必须同一写锁临界区：purge 在锁内删行判 0 后 unlink，
        若 save_image 的存在性判断/写盘在锁外，会赛出指向已删文件的永久坏图行。"""
        import app.crawler as crawler_mod

        async def fake_download(*a, **k):
            return _jpeg(64, 48)

        orig_dl, orig_save = crawler_mod._download, storage.save_image
        crawler_mod._download = fake_download
        seen = {}

        def spy_save(data, url):
            seen["locked"] = db._write_lock.locked()
            return orig_save(data, url)

        storage.save_image = spy_save
        try:
            p = {"sid": "Lk9", "uid": "u9", "author": "作者甲", "avatar": "",
                 "text": "锁不变量", "created_at": "2026-09-01T10:00:00+08:00",
                 "fetched_at": "2026-09-01T10:00:00+08:00",
                 "weibo_url": "https://weibo.com/1/Lk9", "via": "",
                 "pics": [{"pid": "p1", "full": "http://wx1.sinaimg.cn/large/a.jpg",
                           "mid": "http://wx1.sinaimg.cn/mw2000/a.jpg"}]}
            saved = await crawler_mod._store_post(None, p, "L1", {})
        finally:
            crawler_mod._download = orig_dl
            storage.save_image = orig_save
        self.assertEqual(saved, 1)
        self.assertTrue(seen.get("locked"),
                        "save_image 发生在写锁之外——写盘决策与插行不同临界区")
        with db.conn() as c:
            rel = c.execute("SELECT file FROM pics WHERE sid='Lk9'").fetchone()["file"]
        self.assertTrue((config.PICS_DIR / rel).is_file())


class ZipNameTest(unittest.TestCase):
    def test_format(self):
        name = storage.zip_entry_name("2026-09-12T16:20:00+08:00", "中国气象爱好者",
                                      "RhJnY0Dxc", 3, ".jpg")
        self.assertEqual(name, "20260912_中国气象爱好者_RhJnY0Dxc_03.jpg")

    def test_sanitizes_bad_chars(self):
        name = storage.zip_entry_name("2026-09-12T16:20:00+08:00", 'a/b:c*?"<>| d',
                                      "Sid1", 1, ".png")
        self.assertEqual(name, "20260912_abcd_Sid1_01.png")

    def test_empty_author_fallback(self):
        name = storage.zip_entry_name("", "", "S", 1, ".jpg")
        self.assertTrue(name.endswith("_wb_S_01.jpg"))


class PostDetailEndpointTest(SprintTestBase):
    async def test_detail_returns_stored_text(self):
        async with self.client() as c:
            posts = (await c.get("/api/posts")).json()["posts"]
            sid = posts[0]["sid"]
            r = (await c.get(f"/api/posts/{sid}")).json()
            self.assertTrue(r["ok"])
            # 详情只展示入库正文（全文展开设计已移除）
            self.assertEqual(r["post"]["text"], posts[0]["text"])
            self.assertTrue(r["post"]["pics"])

    async def test_detail_404(self):
        async with self.client() as c:
            r = await c.get("/api/posts/NoSuchSid123")
            self.assertEqual(r.status_code, 404)


class AuthorsEndpointTest(SprintTestBase):
    async def test_aggregation(self):
        async with self.client() as c:
            d = (await c.get("/api/authors")).json()
            names = {a["author"]: a["n"] for a in d["authors"]}
            self.assertTrue(names and all(n > 0 for n in names.values()))
            # 无登录态时签名端点优雅降级，不 500
            uid = d["authors"][0]["uid"]
            r = await c.get(f"/api/authors/{uid}/bio")
            self.assertEqual(r.status_code, 200)

    def _mk(self, sid, uid, author, status, created="2026-09-01T10:00:00+08:00"):
        with db.conn() as c:
            c.execute(
                "INSERT INTO posts(sid,list_id,uid,author,avatar,text,created_at,"
                "fetched_at,weibo_url,via,status,kept_at,discarded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "L1", uid, author, "", "t", created,
                 "2026-09-01T10:00:00+08:00", "u", "", status,
                 created if status == 'kept' else None, created if status == 'discarded' else None))

    async def test_counts_follow_status_tab(self):
        # 张三 2 待处理；李四 1 保留；王五 1 已删
        self._mk("s1", "u1", "张三", "pending")
        self._mk("s2", "u1", "张三", "pending")
        self._mk("s3", "u2", "李四", "kept")
        self._mk("s4", "u3", "王五", "discarded")
        async with self.client() as c:
            pend = (await c.get("/api/authors",
                                params={"status": "pending", "group": "L1"})).json()["authors"]
            by = {a["author"]: a["n"] for a in pend}
            self.assertEqual(by["张三"], 2)
            self.assertEqual(by["李四"], 0)   # 没有待处理就显示 0，作者不消失
            self.assertEqual(by["王五"], 0)
            kept = (await c.get("/api/authors",
                                params={"status": "kept", "group": "L1"})).json()["authors"]
            by = {a["author"]: a["n"] for a in kept}
            self.assertEqual(by["李四"], 1)
            self.assertEqual(by["张三"], 0)

    async def test_sorted_by_pinyin(self):
        self._mk("p1", "a1", "张三", "pending")
        self._mk("p2", "a2", "李四", "pending")
        self._mk("p3", "a3", "阿宝", "pending")
        self._mk("p4", "a4", "Cherry", "pending")
        async with self.client() as c:
            authors = (await c.get("/api/authors",
                                   params={"status": "pending", "group": "L1"})).json()["authors"]
            order = [a["author"] for a in authors]
            # 阿宝(a) < Cherry(c) < 李四(l) < 张三(z)，按拼音首字母
            self.assertEqual(order, ["阿宝", "Cherry", "李四", "张三"])

    async def test_sorted_by_last_post_time(self):
        # 发布时间排序：按作者最新一帖；阿宝最新在 9/3，张三 9/2，李四 9/1
        self._mk("t1", "a1", "张三", "pending", created="2026-09-02T10:00:00+08:00")
        self._mk("t2", "a2", "李四", "pending", created="2026-09-01T10:00:00+08:00")
        self._mk("t3", "a3", "阿宝", "pending", created="2026-09-03T10:00:00+08:00")
        async with self.client() as c:
            desc = (await c.get("/api/authors", params={
                "status": "pending", "group": "L1", "sort": "time_desc"})).json()["authors"]
            self.assertEqual([a["author"] for a in desc], ["阿宝", "张三", "李四"])
            asc = (await c.get("/api/authors", params={
                "status": "pending", "group": "L1", "sort": "time_asc"})).json()["authors"]
            self.assertEqual([a["author"] for a in asc], ["李四", "张三", "阿宝"])
            # 旧值 name 仍接受，等价 name_asc
            legacy = (await c.get("/api/authors", params={
                "status": "pending", "group": "L1", "sort": "name"})).json()["authors"]
            self.assertEqual([a["author"] for a in legacy],
                             ["阿宝", "李四", "张三"])


class RangeFilterTest(SprintTestBase):
    def _mk(self, sid, created):
        with db.conn() as c:
            c.execute(
                "INSERT INTO posts(sid,list_id,uid,author,avatar,text,created_at,"
                "fetched_at,weibo_url,via,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, "L1", "u1", "张三", "", "t", created, created, "u", "", "pending"))

    async def test_thirty_day_window(self):
        with db.conn() as c:   # 清掉 demo 种子，窗口计数才可精确断言
            c.execute("DELETE FROM posts")
        now = datetime.now()
        day = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        self._mk("r1", day(1))    # 1 天前：7d 与 30d 都含
        self._mk("r2", day(10))   # 10 天前：只在 30d
        self._mk("r3", day(40))   # 40 天前：都不含
        async with self.client() as c:
            async def total(rng):
                r = await c.get("/api/posts", params={"range": rng})
                return r.json()["total"]
            self.assertEqual(await total("7d"), 1)
            self.assertEqual(await total("30d"), 2)
            self.assertEqual(await total("all"), 3)

    async def test_yesterday_day_window(self):
        """昨天 = 前一自然日（东八区），次日零点开区间：23:59:59 的亚秒戳不漏。"""
        with db.conn() as c:
            c.execute("DELETE FROM posts")
        now = datetime.now(config.NOW_TZ)
        y2 = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59)
        self._mk("y1", (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S+08:00"))
        self._mk("y2", y2.strftime("%Y-%m-%dT%H:%M:%S+08:00"))
        self._mk("t1", now.replace(hour=0, minute=0, second=1).strftime(
            "%Y-%m-%dT%H:%M:%S+08:00"))   # 今天零点后：不在昨天
        self._mk("d2", (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S+08:00"))
        async with self.client() as c:
            r = await c.get("/api/posts", params={"range": "yesterday", "status": "all"})
        self.assertEqual(sorted(x["sid"] for x in r.json()["posts"]), ["y1", "y2"])


class ExportDedupeTest(SprintTestBase):
    async def test_second_keep_does_not_duplicate(self):
        async with self.client() as c:
            posts = (await c.get("/api/posts")).json()["posts"]
            sid = posts[0]["sid"]
            r1 = (await c.post(f"/api/posts/{sid}/status",
                               json={"status": "kept"})).json()
            self.assertGreater(r1["exported"], 0)
            await c.post(f"/api/posts/{sid}/status", json={"status": "pending"})
            r2 = (await c.post(f"/api/posts/{sid}/status",
                               json={"status": "kept"})).json()
            self.assertEqual(r2["exported"], 0)        # 跨日期去重：不再重复拷贝
            self.assertEqual(r2["dup"], r1["exported"])


class BulkStatusTest(SprintTestBase):
    async def test_bulk_keeps_and_exports(self):
        async with self.client() as c:
            posts = (await c.get("/api/posts")).json()["posts"]
            sids = [p["sid"] for p in posts[:3]]
            r = (await c.post("/api/posts/bulk-status",
                              json={"sids": sids + ["ghost"], "status": "kept"})).json()
            self.assertEqual(r["updated"], len(sids))   # 未知 sid 静默跳过
            self.assertEqual(r["exported"], sum(len(p["pics"]) for p in posts[:3]))
            for sid in sids:
                self.assertEqual(db.get_post(sid)["status"], "kept")

    async def test_bulk_pending_no_export(self):
        async with self.client() as c:
            posts = (await c.get("/api/posts")).json()["posts"]
            sids = [p["sid"] for p in posts[:2]]
            r = (await c.post("/api/posts/bulk-status",
                              json={"sids": sids, "status": "pending"})).json()
            self.assertEqual(r["updated"], len(sids))
            self.assertEqual(r["exported"], 0)
            self.assertEqual(r["to"], "")


class DownloadZipTest(SprintTestBase):
    async def test_filter_zip_dedupes_and_names(self):
        import zipfile
        async with self.client() as c:
            r = await c.get("/api/download/zip",
                            params={"status": "pending", "group": "demo"})
            self.assertEqual(r.status_code, 200)
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            names = zf.namelist()
            names = [n for n in names if n != "评论.txt"]          # 批注附件不走图片命名
            self.assertTrue(names)
            self.assertEqual(len(names), len(set(names)))          # zip 内无重复
            for n in names:                                        # 统一命名规格
                # 日期_作者_ID_序号[_正文30字].ext
                self.assertRegex(n, r"^\d{8}_.+_[A-Za-z0-9]+_\d{2}(_.+)?\.\w+$")

    async def test_select_by_sids(self):
        import zipfile
        async with self.client() as c:
            posts = (await c.get("/api/posts")).json()["posts"]
            sids = [p["sid"] for p in posts[:2]]
            r = await c.get("/api/download/zip", params={"select": ",".join(sids)})
            self.assertEqual(r.status_code, 200)
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            self.assertTrue(zf.namelist())
            for sid in sids:
                self.assertTrue(any(sid in n for n in zf.namelist()))


class LongtextFlagTest(unittest.TestCase):
    def test_parse_flags_long(self):
        from app.weibo import parse_mblog
        item = {"mblog": {"mblogid": "Long1", "created_at": "刚刚", "isLongText": 1,
                          "user": {"id": 1, "screen_name": "a"}, "text": "t",
                          "pics": [{"pid": "p", "url": "https://wx/x.jpg"}]}}
        self.assertEqual(parse_mblog(item)["is_long"], 1)
        item["mblog"]["isLongText"] = 0
        self.assertEqual(parse_mblog(item)["is_long"], 0)


if __name__ == "__main__":
    unittest.main()


class XApiParseTest(unittest.TestCase):
    """xapi：GraphQL List 时间线 → 标准契约条目（gallery-dl 逻辑移植的解析层）。"""

    @staticmethod
    def _entry(tid, screen, name, text, created, photos):
        media = [{"type": "photo", "media_url_https": f"https://pbs.twimg.com/media/{tid}_{i}.jpg"}
                 for i in range(photos)]
        user_result = {"core": {"screen_name": screen, "name": name},
                       "legacy": {"profile_image_url_https":
                                  f"https://pbs.twimg.com/profile_images/{screen}_normal.jpg"}}
        return {
            "entryId": f"tweet-{tid}",
            "content": {"entryType": "TimelineTimelineItem", "itemContent": {
                "tweet_results": {"result": {
                    "legacy": {"id_str": tid, "full_text": text, "created_at": created,
                               "extended_entities": {"media": media}},
                    "core": {"user_results": {"result": user_result}},
                }}},
        }}

    def test_parse_and_scope(self):
        from app.sources import xapi
        data = {"data": {"list": {"tweets_timeline": {"timeline": {"instructions": [{
            "type": "TimelineAddEntries",
            "entries": [
                self._entry("100", "alice", "Alice", "hello &amp; hi",
                            "Tue Sep 16 01:00:00 +0000 2025", 2),
                {"entryId": "cursor-bottom-xxx", "content": {
                    "entryType": "TimelineTimelineCursor", "value": "CUR"}},
            ],
        }]}}}}}
        items, cursor = xapi.parse_list_timeline(data)
        self.assertEqual([i["id"] for i in items], ["100"])
        it = items[0]
        self.assertEqual(it["author"]["uid"], "alice")
        self.assertEqual(it["text"], "hello & hi")
        self.assertTrue(it["images"][0].endswith("name=orig"))
        self.assertTrue(it["created_at"].startswith("2025-09-16"))
        self.assertEqual(cursor, "CUR")

    def test_retweet_unwrap_sets_via(self):
        from app.sources import xapi
        inner = {"legacy": {"id_str": "55", "full_text": "原图推",
                            "created_at": "Tue Sep 16 01:00:00 +0000 2025",
                            "extended_entities": {"media": [
                                {"type": "photo", "media_url_https":
                                 "https://pbs.twimg.com/media/orig1.jpg"}]}},
                 "core": {"user_results": {"result": {"core": {
                     "screen_name": "originalguy", "name": "原博"}}}}}
        outer = {"legacy": {"id_str": "66", "retweeted_status_result": {"result": inner}},
                 "core": {"user_results": {"result": {"core": {
                     "screen_name": "retweeter", "name": "转推者"}}}}}
        items = xapi.parse_list_timeline({"data": {"list": {"tweets_timeline": {"timeline": {
            "instructions": [{"entries": [{"entryId": "tweet-66",
                "content": {"entryType": "TimelineTimelineItem",
                            "itemContent": {"tweet_results": {"result": outer}}}}]}]}}}}})[0]
        self.assertEqual(items[0]["id"], "55")
        self.assertEqual(items[0]["via"], "retweeter")

    def test_no_image_skipped(self):
        from app.sources import xapi
        data = {"data": {"list": {"tweets_timeline": {"timeline": {"instructions": [{
            "entries": [{"entryId": "tweet-9", "content": {"entryType": "TimelineTimelineItem",
                "itemContent": {"tweet_results": {"result": {"legacy": {
                    "id_str": "9", "full_text": "纯文字",
                    "created_at": "Tue Sep 16 01:00:00 +0000 2025"}}}}}}]}]}}}}}
        items, _cursor = xapi.parse_list_timeline(data)
        self.assertEqual(items, [])
