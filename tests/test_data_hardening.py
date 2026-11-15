"""Phase 2 Wave 1 数据层 / 爬虫 / 媒体抓取 / 批量任务 / 来源适配安全回归测试。"""
import asyncio
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app import config, crawler, db, demo, downloads, jobs, media_fetch, queries, storage, weibo
from app.sources import x as x_source
from app.sources import xapi
from tests.test_api import ApiTestBase, _jpeg_bytes

_MP4 = b"\x00\x00\x00\x18ftypmp42\x00" + b"\x01" * 2048


class _DirectTransport(httpx.AsyncBaseTransport):
    """非 MockTransport 的直接传输桩：用于验证 media_fetch.fetch 确实执行 DNS 解析校验。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self.handler(request)


class SearchQueryHardeningTest(ApiTestBase):
    """D-S1：search_live limit 限幅 + 仅前缀空关键词阻断 LIKE '%%' 全表扫描。"""

    def test_bare_prefix_search_returns_empty_and_zero_scan_filter(self):
        for raw in ("@", "＠", "#", "＃", "//", "／／", "c:", "c：", "@   ", "c:   ", ""):
            res = db.search_live(raw)
            self.assertEqual(res["authors"], [])
            self.assertEqual(res["tags"], [])
            self.assertEqual(res["content"], [])
            self.assertEqual(res["comments"], [])
            if raw.strip():
                cond, args = queries.post_filter("all", "all", "all", "all", q=raw)
                self.assertIn("1=0", cond)
                self.assertNotIn("LIKE", cond)
                self.assertEqual(args, [])

        # 带前缀 + 空白后的有效词仍可命中
        res = db.search_live("@ 演示作者")
        self.assertTrue(res["authors"])

    def test_search_live_clamps_limit(self):
        with db._write_lock, db.conn() as c:
            for i in range(120):
                sid = f"lim{i:03d}"
                c.execute(
                    "INSERT INTO posts(sid,list_id,uid,author,avatar,text,created_at,fetched_at,weibo_url,via,is_long,source) "
                    "VALUES(?,'demo',?,'限制测试','','统一测试关键词', '2026-10-01T10:00:00+08:00','2026-10-01T10:00:00+08:00','','',0,'weibo')",
                    (sid, f"u{i}"),
                )
        res = db.search_live("统一测试关键词", q_scope="text", limit=999999)
        self.assertEqual(len(res["content"]), 100)
        res_min = db.search_live("统一测试关键词", q_scope="text", limit=-5)
        self.assertEqual(len(res_min["content"]), 1)


class MediaFetchHardeningTest(ApiTestBase):
    """D-S2：主机白名单边界、DNS 解析公网校验、MIME、总超时、流式临时文件与 MP4 魔数。"""

    def test_permitted_rejects_dot_trailing_whitespace_and_userinfo(self):
        self.assertTrue(media_fetch.permitted("https://sinaimg.cn/large/a.jpg"))
        self.assertTrue(media_fetch.permitted("https://wx1.sinaimg.cn/large/a.jpg"))
        self.assertTrue(media_fetch.permitted("https://video.twimg.com/v.mp4"))
        for bad in (
            "https://.sinaimg.cn/a.jpg",
            "https://wx1..sinaimg.cn/a.jpg",
            "https://pbs.twimg.com./a.jpg",
            "https://sinaimg.cn./a.jpg",
            "https://127.0.0.1\\@pbs.twimg.com/a.jpg",
            "https://pbs.twimg.com/a.jpg\n",
            "http://pbs.twimg.com/a.jpg",
            "https://user:pass@pbs.twimg.com/a.jpg",
            "https://pbs.twimg.com:8443/a.jpg",
        ):
            self.assertFalse(media_fetch.permitted(bad), bad)

    def test_public_ip_checks_v4_v6_and_mapped_addresses(self):
        for private in (
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.10",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "::1",
            "::ffff:127.0.0.1",
            "::ffff:169.254.169.254",
            "fe80::1",
            "fc00::1",
        ):
            self.assertFalse(media_fetch._is_public_ip(private), private)
        self.assertTrue(media_fetch._is_public_ip("1.1.1.1"))
        self.assertTrue(media_fetch._is_public_ip("2606:4700:4700::1111"))

    async def test_fetch_blocks_private_dns_and_allows_public_or_proxy(self):
        tr = _DirectTransport(lambda req: httpx.Response(200, content=_jpeg_bytes(), headers={"content-type": "image/jpeg"}))
        async with httpx.AsyncClient(transport=tr) as c:
            with patch("app.media_fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
                self.assertIsNone(await media_fetch.fetch(c, "https://pbs.twimg.com/a.jpg", {}))
            self.assertEqual(tr.calls, 0)

            with patch("app.media_fetch.socket.getaddrinfo", return_value=[(10, 1, 6, "", ("::ffff:169.254.169.254", 443))]):
                self.assertIsNone(await media_fetch.fetch(c, "https://pbs.twimg.com/a.jpg", {}))
            self.assertEqual(tr.calls, 0)

            with patch("app.media_fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("151.101.1.69", 443))]):
                got = await media_fetch.fetch(c, "https://pbs.twimg.com/a.jpg", {})
            self.assertIsNotNone(got)
            self.assertEqual(tr.calls, 1)

        # 显式代理场景由代理侧解析，本地不因 getaddrinfo 返回内网/失败而拒发请求
        async with httpx.AsyncClient(proxy="http://127.0.0.1:8899") as proxied:
            self.assertFalse(media_fetch._needs_dns_check(proxied, "https://pbs.twimg.com/a.jpg"))

    async def test_fetch_rejects_svg_and_enforces_wall_clock_timeout(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=b"<svg xmlns='http://www.w3.org/2000/svg'/>" * 5,
                                       headers={"content-type": "image/svg+xml"})
        )) as c:
            self.assertIsNone(await media_fetch.fetch(c, "https://pbs.twimg.com/bad.svg", {}))

        class SlowStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"a" * 60
                await asyncio.sleep(0.08)
                yield b"b" * 60

        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, stream=SlowStream(), headers={"content-type": "image/jpeg"})
        )) as c:
            self.assertIsNone(await media_fetch.fetch(c, "https://pbs.twimg.com/slow.jpg", {}, timeout=0.04))

    async def test_video_rejects_non_mp4_payload_before_persisting(self):
        p = x_source.normalize_tweet({
            "id": "9001",
            "author": {"id": "77", "uid": "tester", "handle": "tester", "name": "测试者"},
            "text": "假视频",
            "created_at": "2026-10-16T10:00:00+08:00",
            "videos": [{"poster": "https://pbs.twimg.com/media/ABC.jpg",
                        "url": "https://video.twimg.com/bad.mp4",
                        "variants": [{"url": "https://video.twimg.com/bad.mp4", "bitrate": 100}]}],
        })
        async def fake_download(client, url, max_bytes=None):
            return b"NOT_MP4_HTML_GARBAGE" * 20 if "video.twimg.com" in url else _jpeg_bytes()

        with patch.object(crawler, "_download", fake_download):
            saved = await crawler._store_post(httpx.AsyncClient(), p, "x:1", {})
        self.assertEqual(saved, 0)
        self.assertEqual(list(config.PICS_DIR.rglob("*.mp4")), [])


class DownloadsAndJobsHardeningTest(ApiTestBase):
    """D-S3：总字节上限、DATA_DIR/.tmp 0700 临时目录、启动清理、隐藏 result.file、参数令牌上限。"""

    def test_plan_enforces_token_cap_byte_budget_and_preview_safety(self):
        many_authors = ",".join(f"u{i}" for i in range(downloads.MAX_POSTS + 1))
        with self.assertRaises(HTTPException) as ctx:
            downloads.plan("kept", authors=many_authors)
        self.assertEqual(ctx.exception.status_code, 400)

        many_dates = ",".join(f"2026-01-{i:02d}" for i in range(downloads.MAX_POSTS + 1))
        with self.assertRaises(HTTPException) as ctx2:
            downloads.plan("kept", dates=many_dates)
        self.assertEqual(ctx2.exception.status_code, 400)

        db.set_status("demo0000", "kept")
        with patch.object(downloads, "MAX_ZIP_BYTES", 10):
            with self.assertRaises(HTTPException) as ctx3:
                downloads.plan("kept")
            self.assertEqual(ctx3.exception.status_code, 400)
            prev = downloads.preview("kept")
            self.assertTrue(prev["over_limit"])
            self.assertGreater(prev["bytes"], 10)

        # 视频帖 + thumb=True 时 preview 必须与 plan 一致计入 .mp4
        vid_rel = storage.save_video(_MP4, "https://video.twimg.com/x.mp4")
        _poster_rel, _, _, t_rel = storage.save_image(_jpeg_bytes(), "https://pbs.twimg.com/p.jpg")
        db.insert_post({
            "sid": "vid1", "uid": "vx", "author": "XVideo", "avatar": "", "text": "v",
            "created_at": "2026-10-01T10:00:00+08:00", "fetched_at": "2026-10-01T10:00:00+08:00",
            "weibo_url": "https://x.com/vx/status/1", "via": "", "source": "x",
        }, "x:1")
        db.set_status("vid1", "kept")
        db.insert_pic("vid1", vid_rel, t_rel, 16, 9, "https://video.twimg.com/x.mp4",
                      kind="video", dur_ms=1000, poster_url="https://pbs.twimg.com/p.jpg")
        # 再插一条仅含路径越界坏图的新帖，preview/plan 都必须跳过
        db.insert_post({
            "sid": "badpic1", "uid": "vb", "author": "BadPic", "avatar": "", "text": "bad",
            "created_at": "2026-10-01T10:00:00+08:00", "fetched_at": "2026-10-01T10:00:00+08:00",
            "weibo_url": "https://weibo.com/1/badpic1", "via": "", "source": "weibo",
        }, "demo")
        db.insert_pic("badpic1", "../../etc/passwd", "../../etc/passwd", 10, 10, "https://evil.test/1")
        db.set_status("badpic1", "kept")
        pv = downloads.preview(select="vid1,badpic1", thumb=True)
        self.assertEqual(pv["pics"], 1)
        planned = downloads.plan(select="vid1,badpic1", thumb=True)
        self.assertEqual(len(planned["entries"]), 1)
        arcname, src_path = next(iter(planned["entries"].items()))
        self.assertTrue(arcname.endswith(".mp4"))
        self.assertTrue(src_path.name.endswith(".mp4"))

    def test_build_zip_uses_private_datadir_tmp_and_recover_sweeps(self):
        db.set_status("demo0000", "kept")
        tmp_path, stats = downloads._build_zip({"scope": "kept"}, lambda: False, lambda d, t: None)
        self.assertGreater(stats["bytes"], 0)
        tmp_file = Path(tmp_path)
        expected_dir = (config.DATA_DIR / ".tmp").resolve()
        self.assertTrue(tmp_file.resolve().is_relative_to(expected_dir))
        self.assertEqual(stat.S_IMODE(expected_dir.stat().st_mode), 0o700)
        stale = expected_dir / "zip-stale-orphan.zip"
        stale.write_bytes(b"stale")
        downloads._RESULT_FILES["stale_job"] = {"path": str(tmp_file), "ts": 0}
        jobs.recover_interrupted()
        self.assertFalse(stale.exists())
        self.assertEqual(downloads._RESULT_FILES, {})

    def test_duplicate_truncated_entry_names_do_not_overwrite(self):
        rel1, w1, h1, t1 = storage.save_image(_jpeg_bytes(40, 40), "https://wx1.sinaimg.cn/large/a.jpg")
        rel2, w2, h2, t2 = storage.save_image(_jpeg_bytes(45, 45), "https://wx1.sinaimg.cn/large/b.jpg")
        long_text = "非常长" * 120
        sid = "s" * 260
        for idx, (s, r, t, w, h) in enumerate(((sid + "A", rel1, t1, w1, h1), (sid + "B", rel2, t2, w2, h2))):
            db.insert_post({
                "sid": s, "uid": "u1", "author": "作者" * 40, "avatar": "", "text": long_text,
                "created_at": "2026-10-01T10:00:00+08:00", "fetched_at": "2026-10-01T10:00:00+08:00",
                "weibo_url": "https://weibo.com/1/a", "via": "",
            }, "demo")
            db.set_status(s, "kept")
            db.insert_pic(s, r, t, w, h, f"https://wx1.sinaimg.cn/large/{idx}.jpg")
        planned = downloads.plan(select=f"{sid}A,{sid}B")
        self.assertEqual(len(planned["entries"]), 2)


class CrawlerHardeningTest(ApiTestBase):
    """D-S4：锁外解码校验、越界文件防护、损坏检查点容错、Referer 与凭据脱敏、墓碑清理。"""

    async def test_corrupt_image_fails_before_acquiring_write_lock(self):
        p = {
            "sid": "badimg1", "uid": "1", "author": "甲", "avatar": "", "text": "t",
            "created_at": "2026-10-01T10:00:00+08:00", "fetched_at": "2026-10-01T10:00:00+08:00",
            "weibo_url": "https://weibo.com/1/badimg1", "via": "", "source": "weibo",
            "pics": [{"pid": "p1", "full": "https://wx1.sinaimg.cn/large/a.jpg",
                      "mid": "https://wx1.sinaimg.cn/mw2000/a.jpg"}],
        }
        lock_enters = 0
        validate_lock_states = []
        save_image_calls = 0
        lock_cls = type(db._write_lock)
        orig_enter = lock_cls.__enter__
        orig_validate = crawler._validate_image_payload
        orig_save = storage.save_image

        def counted_enter(lock_self):
            nonlocal lock_enters
            lock_enters += 1
            return orig_enter(lock_self)

        def checked_validate(data: bytes):
            validate_lock_states.append((db._write_lock.locked(), lock_enters))
            return orig_validate(data)

        def spy_save(*args, **kwargs):
            nonlocal save_image_calls
            save_image_calls += 1
            return orig_save(*args, **kwargs)

        dl_lock_snapshot = []

        async def fake_dl(client, url, max_bytes=None):
            dl_lock_snapshot.append(lock_enters)
            return b"not-an-image-" * 20

        with (
            patch.object(crawler, "_download", fake_dl),
            patch.object(lock_cls, "__enter__", counted_enter),
            patch.object(crawler, "_validate_image_payload", checked_validate),
            patch.object(storage, "save_image", spy_save),
        ):
            saved = await crawler._store_post(httpx.AsyncClient(), p, "demo", {})
        self.assertEqual(saved, 0)
        self.assertEqual(save_image_calls, 0)
        self.assertGreaterEqual(len(validate_lock_states), 1)
        # 所有解码校验都在锁外执行，且下载返回到校验失败之间没有额外加锁
        for idx, (locked, count_during_validate) in enumerate(validate_lock_states):
            self.assertFalse(locked)
            self.assertEqual(count_during_validate, dl_lock_snapshot[idx])

        # queue_missing_files 忽略越界路径
        db.insert_pic("demo0000", "../../etc/passwd", "", 1, 1, "https://evil.test/x.jpg")
        crawler.queue_missing_files()
        self.assertFalse(crawler.needs_images("demo0000"))

    async def test_download_only_sends_weibo_referer_to_sinaimg(self):
        seen_headers = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen_headers.append((req.url.host, dict(req.headers)))
            return httpx.Response(200, content=_jpeg_bytes(), headers={"content-type": "image/jpeg"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await crawler._download(c, "https://wx1.sinaimg.cn/large/a.jpg")
            await crawler._download(c, "https://pbs.twimg.com/media/a.jpg")
        self.assertEqual(seen_headers[0][1].get("referer"), "https://weibo.com/")
        self.assertNotIn("referer", seen_headers[1][1])

    async def test_corrupt_checkpoint_tombstone_prune_and_proxy_scrub(self):
        db.set_meta("crawl_checkpoint", "{corrupt json")
        crawler.recover_interrupted()
        self.assertEqual(crawler.state["progress"]["running"], False)

        # queue_missing_files 忽略越界路径
        db.insert_pic("demo0000", "../../etc/passwd", "", 1, 1, "https://evil.test/x.jpg")
        crawler.queue_missing_files()

        # tombstones 会被 retry_missing 自动清出 image_jobs
        with db._write_lock, db.conn() as c:
            c.execute("INSERT OR REPLACE INTO tombstones VALUES('deadjob', '2026-10-01T00:00:00+08:00')")
            c.execute(
                "INSERT OR REPLACE INTO image_jobs(sid,list_id,payload) VALUES('deadjob','demo','{}')"
            )
        res = await crawler.retry_missing(manual=True)
        self.assertEqual(res["ok"], True)
        self.assertFalse(crawler.needs_images("deadjob"))

        # 代理账密在 x_last_error 中被脱敏
        with patch.object(crawler.xapi, "crawl_lists", new=AsyncMock(side_effect=RuntimeError("proxy http://alice:supersecret@10.0.0.1:7890 failed"))):
            db.add_group("x:12345", "XList")
            with patch.object(config, "get_x_auth", return_value="tok" * 10):
                out = await crawler.crawl_x()
        self.assertFalse(out["ok"])
        self.assertNotIn("supersecret", out["msg"])
        self.assertNotIn("supersecret", db.get_meta("x_last_error"))


class SourcesAndDemoHardeningTest(ApiTestBase):
    """D-S5：weibo/x/xapi/demo 解析、重定向、null 防护、ct0 接力与引用计数清理。"""

    async def test_weibo_validation_strip_html_redirects_and_recursion_cap(self):
        self.assertEqual(weibo.strip_html("<b>a</b><br/>b &amp; c"), "a b & c")
        self.assertEqual(weibo.strip_html("&lt;script&gt;alert(1)&lt;/script&gt;safe"), "safe")
        self.assertEqual(weibo.strip_html("&lt;img src=x onerror=alert(1)&gt;ok"), "ok")
        self.assertEqual(weibo.strip_html("2 &lt; 3 &amp;&amp; 5 &gt; 1"), "2 < 3 && 5 > 1")

        # 深层嵌套结构不触发 RecursionError
        nested: dict = {"gid": "123", "title": "deep"}
        cur = nested
        for _ in range(80):
            cur = {"child": cur}
        self.assertEqual(weibo.parse_groups(cur), [])

        with self.assertRaises(weibo.WeiboError):
            await weibo.group_timeline(httpx.AsyncClient(), "SUB=1", "123&page=99")
        with self.assertRaises(weibo.WeiboError):
            await weibo.user_timeline(httpx.AsyncClient(), "SUB=1", "../123")
        with self.assertRaises(weibo.WeiboError):
            await weibo.profile_info(httpx.AsyncClient(), "SUB=1", "bad_uid")

        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(307, headers={"location": "https://weibo.com/other"})
        )) as c:
            with self.assertRaises(weibo.CookieExpired):
                await weibo.group_timeline(c, "SUB=1", "12345")

    def test_x_normalizer_restricts_avatar_weibo_url_handle_and_video_host(self):
        p = x_source.normalize_tweet({
            "id": "1001",
            "url": "https://evil.example.com/phish",
            "author": {
                "id": "55",
                "uid": "artist1",
                "handle": "artist1",
                "name": "画师",
                "avatar": "javascript:alert(1)",
            },
            "text": "x" * 60000,
            "images": ["https://pbs.twimg.com/media/AbC?format=jpg"],
            "videos": [{
                "poster": "https://pbs.twimg.com/media/AbC.jpg",
                "url": "https://pbs.twimg.com/not-video.mp4",
                "variants": [{"url": "https://video.twimg.com/real.mp4", "bitrate": 800}],
            }],
        })
        self.assertIsNotNone(p)
        self.assertEqual(p["avatar"], "")
        self.assertEqual(p["weibo_url"], "https://x.com/artist1/status/1001")
        self.assertEqual(len(p["text"]), 50000)
        video_pic = next(pc for pc in p["pics"] if pc.get("kind") == "video")
        self.assertEqual(video_pic["full"], "https://video.twimg.com/real.mp4")

        bad_handle = x_source.normalize_tweet({
            "id": "1002",
            "author": {"id": "55", "uid": "u", "handle": "bad/../handle", "name": "N"},
            "images": ["https://pbs.twimg.com/media/AbC?format=jpg"],
        })
        self.assertIsNone(bad_handle)

    async def test_xapi_null_safety_ct0_carryover_and_no_100_truncation(self):
        payload = {
            "data": {
                "list": {
                    "tweets_timeline": {
                        "timeline": {
                            "instructions": [{
                                "entries": [{
                                    "entryId": "tweet-1",
                                    "content": {
                                        "entryType": "TimelineTimelineItem",
                                        "itemContent": {
                                            "tweet_results": {
                                                "result": {
                                                    "legacy": {
                                                        "id_str": "2001",
                                                        "full_text": "null safe",
                                                        "retweeted_status_result": None,
                                                        "extended_entities": {
                                                            "media": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/X.jpg"}]
                                                        },
                                                    },
                                                    "core": {"user_results": {"result": {
                                                        "rest_id": "9",
                                                        "core": {"screen_name": "u1", "name": "N"},
                                                        "legacy": None,
                                                        "avatar": None,
                                                    }}},
                                                }
                                            }
                                        },
                                    },
                                }]
                            }]
                        }
                    }
                }
            }
        }
        items, _ = xapi.parse_list_timeline(payload)
        self.assertEqual(len(items), 1)

        # 重定向在 _call 被拦
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(302, headers={"location": "https://x.com/i/flow/login"})
        )) as rc:
            with self.assertRaises(xapi.XApiError) as ctx:
                await xapi.XGraphQL("tok", "c0")._call(rc, "/graphql/x/Test", {})
        self.assertEqual(ctx.exception.kind, "auth")

        # ct0 在多次 list 请求之间保留刷新值
        seen_ct0 = []

        def list_handler(req: httpx.Request) -> httpx.Response:
            seen_ct0.append(req.headers.get("x-csrf-token"))
            hdrs = {"set-cookie": "ct0=rotated_ct0; Path=/"} if len(seen_ct0) == 1 else {}
            return httpx.Response(200, json={"data": {"list": {"tweets_timeline": {"timeline": {"instructions": []}}}}}, headers=hdrs)

        async with httpx.AsyncClient(transport=httpx.MockTransport(list_handler)) as lc:
            with patch.object(xapi.asyncio, "sleep", new=AsyncMock()) if hasattr(xapi, "asyncio") else patch("asyncio.sleep", new=AsyncMock()):
                await xapi.crawl_lists(
                    lc,
                    "tok",
                    [{"list_id": "x:1", "name": "L1"}, {"list_id": "x:2", "name": "L2"}],
                    ct0="initial_ct0",
                )
        self.assertEqual(seen_ct0, ["initial_ct0", "rotated_ct0"])

        # _ingest_items 不再截断前 100 条
        batch = [
            {"id": str(5000 + i), "author": {"id": "1", "uid": "u1", "handle": "u1", "name": "U"},
             "text": "t", "images": ["https://pbs.twimg.com/media/A.jpg"]}
            for i in range(105)
        ]
        async def fake_dl(client, url, max_bytes=None):
            return _jpeg_bytes()
        with patch.object(crawler, "_download", fake_dl):
            summary = {"new_posts": 0, "new_pics": 0}
            await xapi._ingest_items(httpx.AsyncClient(), batch, "x:99", "Big", summary)
        self.assertEqual(summary["new_posts"], 105)

    def test_demo_timezone_and_clear_orphans_with_refcounts(self):
        post0 = db.get_post("demo0000")
        self.assertRegex(post0["fetched_at"], r"[+-]\d{2}:\d{2}$")
        db.set_post_tags("demo0000", add=["演示标签"])
        db.mark_snapshot("demo0000", "kept")
        # 构造另一条真实帖子共享 demo0000 的同一张图（引用计数 > 1 时不能删文件和 media_contents）
        shared_rel = post0["pics"][0]["file"]
        shared_thumb = post0["pics"][0]["thumb"]
        db.add_group("real1", "真实分组")
        db.insert_post({
            "sid": "realshare1", "uid": "99", "author": "真实作者", "avatar": "", "text": "共享",
            "created_at": "2026-10-01T10:00:00+08:00", "fetched_at": "2026-10-01T10:00:00+08:00",
            "weibo_url": "https://weibo.com/99/realshare1", "via": "",
        }, "real1")
        db.insert_pic("realshare1", shared_rel, shared_thumb, 100, 100, "https://wx1.sinaimg.cn/large/s.jpg")

        demo.clear()
        self.assertTrue((config.PICS_DIR / shared_rel).is_file())
        with db.conn() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM post_comments WHERE sid LIKE 'demo%'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM post_tags WHERE sid LIKE 'demo%'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM post_groups WHERE list_id='demo' OR sid LIKE 'demo%'").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM decision_undos WHERE sid LIKE 'demo%'").fetchone()[0], 0)
            self.assertIsNotNone(c.execute("SELECT 1 FROM media_contents WHERE file=?", (shared_rel,)).fetchone())
            # 未被共享的其他 demo 图片则连文件带 media_contents 一并清掉
            demo1_pic = [r["file"] for r in c.execute("SELECT file FROM media_contents WHERE file != ?", (shared_rel,)).fetchall()]
        self.assertEqual(demo1_pic, [])
        self.assertTrue(os.path.exists(config.PICS_DIR / shared_rel))


class DataPerformanceTest(ApiTestBase):
    """Phase 2 Wave 2（D-P1..D-P4）性能与正确性回归测试。"""

    def test_fts5_trigram_search_short_fallback_quotes_cjk_and_sync_triggers(self):
        db.add_group("g1", "测试组")
        posts = [
            ("fts_cjk", "演示内容插画与摄影"),
            ("fts_ascii", 'Alpha C++ & "quoted" demo'),
            ("fts_kw", "alpha AND beta keyword"),
            ("fts_short", "鸠在窗边"),
        ]
        for sid, text in posts:
            db.insert_post({
                "sid": sid, "uid": "u_fts", "author": "作者Alpha", "avatar": "",
                "text": text, "created_at": "2026-10-02T10:00:00+08:00",
                "fetched_at": "2026-10-02T10:00:00+08:00",
                "weibo_url": f"https://weibo.com/1/{sid}", "via": "",
            }, "g1")
        cid = db.add_comment("fts_cjk", "重要评论关键字在这里")["id"]
        db.add_comment("fts_short", "短评鸠")

        # ≥3 字符走 trigram FTS5（含 CJK、标点、引号、FTS 保留字 AND）
        self.assertEqual([r["sid"] for r in db.search_live("内容插", q_scope="text")["content"]], ["fts_cjk"])
        self.assertEqual([r["sid"] for r in db.search_live("C++", q_scope="text")["content"]], ["fts_ascii"])
        self.assertEqual([r["sid"] for r in db.search_live('"quoted"', q_scope="text")["content"]], ["fts_ascii"])
        self.assertEqual([r["sid"] for r in db.search_live("AND", q_scope="text")["content"]], ["fts_kw"])
        self.assertEqual([r["sid"] for r in db.search_live("关键字", q_scope="comment")["comments"]], ["fts_cjk"])

        # <3 字符回落 LIKE
        self.assertEqual([r["sid"] for r in db.search_live("鸠", q_scope="text")["content"]], ["fts_short"])
        self.assertEqual([r["sid"] for r in db.search_live("鸠", q_scope="comment")["comments"]], ["fts_short"])

        cond, args = queries.post_filter("all", "all", "all", "all", q="内容插", q_scope="text")
        self.assertIn("posts_fts MATCH ?", cond)
        with db.conn() as c:
            rows = c.execute(f"SELECT p.sid FROM posts p WHERE {cond}", args).fetchall()
        self.assertEqual([r["sid"] for r in rows], ["fts_cjk"])

        # 更新/删除触发器保持 FTS5 同步且通过 integrity-check
        db.update_comment("fts_cjk", cid, "改写后的新关键词")
        self.assertEqual(db.search_live("关键字", q_scope="comment")["comments"], [])
        self.assertEqual([r["sid"] for r in db.search_live("新关键", q_scope="comment")["comments"]], ["fts_cjk"])
        db.delete_comment("fts_cjk", cid)
        self.assertEqual(db.search_live("新关键", q_scope="comment")["comments"], [])
        with db._write_lock, db.conn() as c:
            c.execute("UPDATE posts SET text='改写后的正文文本' WHERE sid='fts_kw'")
            c.execute("DELETE FROM posts WHERE sid='fts_ascii'")
        self.assertEqual([r["sid"] for r in db.search_live("改写后", q_scope="text")["content"]], ["fts_kw"])
        self.assertEqual(db.search_live("beta", q_scope="text")["content"], [])
        self.assertEqual(db.search_live('"quoted"', q_scope="text")["content"], [])
        with db.conn() as c:
            c.execute("INSERT INTO posts_fts(posts_fts) VALUES('integrity-check')")
            c.execute("INSERT INTO post_comments_fts(post_comments_fts) VALUES('integrity-check')")

    def test_search_live_cte_uses_position_before_id_for_thumb(self):
        db.add_group("g_pos", "顺位组")
        db.insert_post({
            "sid": "pos_post", "uid": "upos", "author": "顺位作者", "avatar": "",
            "text": "顺位检索长文本测试", "created_at": "2026-10-03T10:00:00+08:00",
            "fetched_at": "2026-10-03T10:00:00+08:00",
            "weibo_url": "https://weibo.com/1/pos_post", "via": "",
        }, "g_pos")
        # 故意先插 position=1（较小 id），再插 position=0（较大 id）
        with db._write_lock:
            db._insert_pic_locked("pos_post", "p1.jpg", "t_second.jpg", 10, 10, "https://wx1.sinaimg.cn/large/1.jpg", position=1)
            db._insert_pic_locked("pos_post", "p0.jpg", "t_first.jpg", 10, 10, "https://wx1.sinaimg.cn/large/0.jpg", position=0)
        db.add_comment("pos_post", "顺位评论长文本")

        res = db.search_live("顺位检索长", q_scope="text")
        self.assertEqual(res["content"][0]["thumb"], "t_first.jpg")
        cres = db.search_live("顺位评论长", q_scope="comment")
        self.assertEqual(cres["comments"][0]["thumb"], "t_first.jpg")

    def test_shuffled_sids_cache_hit_and_raw_sql_invalidation(self):
        db._SHUFFLE_CACHE.clear()
        s1 = db.shuffled_sids("p.status='pending'", [], "seedA")
        self.assertIn("demo0000", [sid for _, sid in s1])
        self.assertEqual(len(db._SHUFFLE_CACHE), 1)
        s2 = db.shuffled_sids("p.status='pending'", [], "seedA")
        self.assertEqual(s1, s2)
        self.assertEqual(len(db._SHUFFLE_CACHE), 1)

        # db 写入触发 epoch 失效
        db.set_status("demo0000", "kept")
        s3 = db.shuffled_sids("p.status='pending'", [], "seedA")
        self.assertNotIn("demo0000", [sid for _, sid in s3])

        # 绕过 db 辅助函数的原生 SQL 修改也通过 total_changes/data_version 自动失效
        kept_before = db.shuffled_sids("p.status='kept'", [], "seedB")
        self.assertIn("demo0000", [sid for _, sid in kept_before])
        with db._write_lock, db.conn() as c:
            c.execute(
                "UPDATE posts SET status='discarded', kept_at=NULL, discarded_at='2026-10-01T00:00:00+08:00' WHERE sid='demo0000'"
            )
        kept_after = db.shuffled_sids("p.status='kept'", [], "seedB")
        self.assertNotIn("demo0000", [sid for _, sid in kept_after])

    def test_downloads_over_limit_sql_buckets_and_throttled_zip_progress(self):
        total_over = downloads.MAX_POSTS + 3
        with db._write_lock, db.conn() as c:
            c.execute("INSERT OR IGNORE INTO groups(list_id,name) VALUES('bulk','批量组')")
            for i in range(total_over):
                sid = f"bulk_{i:04d}"
                uid = f"u{i % 7}"
                c.execute(
                    "INSERT INTO posts(sid,list_id,uid,author,avatar,text,created_at,fetched_at,weibo_url,via,status,kept_at,source) "
                    "VALUES(?,'bulk',?,?,'','t',?,'2026-10-01T00:00:00+08:00','','','kept','2026-10-01T00:00:00+08:00','weibo')",
                    (sid, uid, f"Author{i % 7}", f"2026-10-{(i % 5) + 1:02d}T12:00:00+08:00"),
                )
                c.execute("INSERT OR IGNORE INTO post_groups(sid,list_id) VALUES(?,'bulk')", (sid,))

        pv = downloads.preview("kept")
        self.assertEqual(pv["posts"], total_over)
        self.assertIsNone(pv["pics"])
        self.assertTrue(pv["over_limit"])
        self.assertEqual(len(pv["authors"]), 7)
        self.assertEqual(sum(a["n"] for a in pv["authors"]), total_over)
        self.assertTrue(all(a["pics"] is None for a in pv["authors"]))
        self.assertEqual(len(pv["dates"]), 5)
        self.assertEqual(downloads.plan("kept", group="bulk", enforce_byte_limit=False)["rows"], [])

        # _build_zip 节流 progress（35 个文件仅在 32 和 35 回调，不再逐文件写库）
        with db._write_lock, db.conn() as c:
            c.execute("INSERT OR IGNORE INTO groups(list_id,name) VALUES('zip35','节流组')")
            for i in range(35):
                sid = f"z35_{i:02d}"
                r, w, h, tr = storage.save_image(_jpeg_bytes(20 + i, 20), f"https://wx1.sinaimg.cn/large/z{i}.jpg")
                c.execute(
                    "INSERT INTO posts(sid,list_id,uid,author,avatar,text,created_at,fetched_at,weibo_url,via,status,kept_at,source) "
                    "VALUES(?,'zip35','uz','Zipper','','t','2026-10-01T00:00:00+08:00','2026-10-01T00:00:00+08:00','','','kept','2026-10-01T00:00:00+08:00','weibo')",
                    (sid,),
                )
                c.execute("INSERT OR IGNORE INTO post_groups(sid,list_id) VALUES(?,'zip35')", (sid,))
                c.execute(
                    "INSERT INTO pics(sid,file,thumb,w,h,url,kind,position,actual_url,quality) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, r, tr, w, h, f"https://wx1.sinaimg.cn/large/z{i}.jpg", "photo", 0, f"https://wx1.sinaimg.cn/large/z{i}.jpg", "full"),
                )
        prog_calls = []
        tmp_path, stats = downloads._build_zip({"scope": "kept", "group": "zip35"}, lambda: False, lambda d, t: prog_calls.append((d, t)))
        try:
            self.assertEqual(stats["entries"], 35)
            self.assertEqual(prog_calls, [(32, 35), (35, 35)])
        finally:
            os.unlink(tmp_path)

    async def test_batch_fresh_observe_queue_missing_and_concurrent_downloads(self):
        db.add_group("bg", "批处理组")
        demo_post = db.get_post("demo0000")
        p_existing = {
            "sid": "demo0000", "uid": demo_post["uid"], "author": "演示作者改名",
            "avatar": "", "text": demo_post["text"], "created_at": demo_post["created_at"],
            "fetched_at": demo_post["fetched_at"], "weibo_url": demo_post["weibo_url"], "via": "",
            "pics": [],
        }
        p_new = {
            "sid": "fresh1", "uid": "uf1", "author": "新作者", "avatar": "", "text": "t",
            "created_at": "2026-10-01T10:00:00+08:00", "fetched_at": "2026-10-01T10:00:00+08:00",
            "weibo_url": "https://weibo.com/1/fresh1", "via": "", "source": "weibo",
            "pics": [
                {"pid": f"p{i}", "full": f"https://wx1.sinaimg.cn/large/conc{i}.jpg",
                 "mid": f"https://wx1.sinaimg.cn/mw2000/conc{i}.jpg"}
                for i in range(4)
            ],
        }
        with db._write_lock, db.conn() as c:
            c.execute("INSERT OR IGNORE INTO tombstones VALUES('dead_batch', '2026-10-01T00:00:00+08:00')")
        p_dead = dict(p_new, sid="dead_batch")

        observed_n = db.observe_posts([p_existing, p_new, p_dead], "bg")
        self.assertEqual(observed_n, 1)
        with db.conn() as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM post_groups WHERE sid='demo0000' AND list_id='bg'").fetchone())
            self.assertIsNone(c.execute("SELECT 1 FROM post_groups WHERE sid='dead_batch' AND list_id='bg'").fetchone())
        fresh = crawler.fresh_posts([p_existing, p_new, p_dead, p_new])
        self.assertEqual([p["sid"] for p in fresh], ["fresh1"])

        # 多图并发抓取（in_flight > 1）且严格保持 position 0..3 顺序入库
        in_flight = 0
        max_in_flight = 0

        async def conc_dl(client, url, max_bytes=None):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.02)
            in_flight -= 1
            idx = int(url.rsplit("conc", 1)[1].split(".")[0])
            return _jpeg_bytes(30 + idx, 30)

        with patch.object(crawler, "_download", conc_dl):
            saved = await crawler._store_post(httpx.AsyncClient(), p_new, "bg", {})
        self.assertEqual(saved, 4)
        self.assertGreaterEqual(max_in_flight, 2)
        stored_pics = db.get_post("fresh1")["pics"]
        self.assertEqual([pc["position"] for pc in stored_pics], [0, 1, 2, 3])
        self.assertEqual([pc["w"] for pc in stored_pics], [30, 31, 32, 33])

        # queue_missing_files 跳过已入队与越界，仅补入真正缺图帖
        missing_rel = stored_pics[0]["file"]
        (config.PICS_DIR / missing_rel).unlink()
        db.insert_pic("demo0000", "../../etc/passwd", "", 1, 1, "https://evil.test/x.jpg")
        queued_n = crawler.queue_missing_files()
        self.assertEqual(queued_n, 1)
        self.assertTrue(crawler.needs_images("fresh1"))
        self.assertEqual(crawler.queue_missing_files(), 0)

        # xapi._ingest_items 并发摄取同一作者多推文，头像 URL 仅下载一次
        avatar_downloads = 0
        batch = [
            {
                "id": f"98{i}",
                "author": {
                    "id": "same_x", "uid": "samex", "handle": "samex", "name": "SameX",
                    "avatar": "https://pbs.twimg.com/profile_images/same_avatar.jpg",
                },
                "text": f"tweet {i}",
                "images": [f"https://pbs.twimg.com/media/XImg{i}.jpg"],
            }
            for i in range(6)
        ]

        async def avatar_counting_dl(client, url, max_bytes=None):
            nonlocal avatar_downloads
            await asyncio.sleep(0.01)
            if "same_avatar" in url:
                avatar_downloads += 1
            return _jpeg_bytes()

        with patch.object(crawler, "_download", avatar_counting_dl):
            summary = {"new_posts": 0, "new_pics": 0}
            await xapi._ingest_items(httpx.AsyncClient(), batch, "x:conc", "ConcX", summary)
        self.assertEqual(summary["new_posts"], 6)
        self.assertEqual(summary["new_pics"], 6)
        self.assertEqual(avatar_downloads, 1)

    def test_status_index_plans_for_short_like_list_and_top_posts(self):
        with db._write_lock, db.conn() as c:
            c.execute("INSERT OR IGNORE INTO groups(list_id,name) VALUES('plan_g','规划组')")
            for i in range(60):
                st = "pending" if i < 40 else "kept"
                c.execute(
                    "INSERT INTO posts(sid,list_id,uid,author,avatar,text,created_at,fetched_at,weibo_url,via,status,kept_at,source) "
                    "VALUES(?,'plan_g',?,?,'','图文测试',?,'2026-10-01T00:00:00+08:00','','',?,?,'weibo')",
                    (f"pl_{i:03d}", f"up{i % 5}", f"A{i % 5}", f"2026-10-{(i % 20) + 1:02d}T12:00:00+08:00",
                     st, "2026-10-01T00:00:00+08:00" if st == "kept" else None),
                )
        cond_list, args_list = queries.post_filter("pending", "all", "all", "all")
        cond_like, args_like = queries.post_filter("pending", "all", "all", "all", q="图文", q_scope="text")
        with db.conn() as c:
            plan_list = " ".join(r[3] for r in c.execute(
                f"EXPLAIN QUERY PLAN SELECT p.sid FROM posts p WHERE {cond_list} ORDER BY p.created_at DESC, p.sid LIMIT 50",
                args_list,
            ))
            plan_like = " ".join(r[3] for r in c.execute(
                f"EXPLAIN QUERY PLAN SELECT p.sid FROM posts p WHERE {cond_like} ORDER BY p.created_at DESC, p.sid LIMIT 50",
                args_like,
            ))
            plan_like_count = " ".join(r[3] for r in c.execute(
                f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM posts p WHERE {cond_like}",
                args_like,
            ))
            plan_top = " ".join(r[3] for r in c.execute(
                "EXPLAIN QUERY PLAN SELECT p.sid FROM posts p WHERE p.status='kept' ORDER BY p.keep_count DESC, p.created_at DESC LIMIT 9"
            ))
        self.assertIn("idx_posts_status_cursor", plan_list)
        self.assertNotIn("TEMP B-TREE", plan_list)
        self.assertIn("idx_posts_status_cursor", plan_like)
        self.assertNotIn("TEMP B-TREE", plan_like)
        self.assertIn("idx_posts_status ", plan_like_count + " ")
        self.assertNotIn("idx_posts_status_keep", plan_like_count)
        self.assertIn("idx_posts_status_keep", plan_top)
        self.assertNotIn("TEMP B-TREE", plan_top)
