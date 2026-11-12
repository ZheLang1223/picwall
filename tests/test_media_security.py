"""平台媒体下载边界：地址、重定向、响应体积、像素上限。"""
from unittest.mock import patch

import httpx

from app import config, media_fetch, storage
from tests.test_api import ApiTestBase, _jpeg_bytes


class MediaSecurityTest(ApiTestBase):
    async def test_untrusted_addresses_never_make_a_request(self):
        def denied(request):
            raise AssertionError("不可信地址不应发出请求")
        async with httpx.AsyncClient(transport=httpx.MockTransport(denied)) as c:
            for url in ("http://pbs.twimg.com/a.jpg", "https://127.0.0.1/a.jpg",
                        "https://pbs.twimg.com.evil.test/a.jpg", "https://user:pass@pbs.twimg.com/a.jpg",
                        "https://pbs.twimg.com:8081/a.jpg", "file:///etc/passwd"):
                self.assertIsNone(await media_fetch.fetch(c, url, {}))

    async def test_redirect_to_internal_host_is_blocked(self):
        calls = []
        def response(request):
            calls.append(request.url.host)
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8080/api/status"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(response), follow_redirects=True) as c:
            self.assertIsNone(await media_fetch.fetch(c, "https://pbs.twimg.com/a.jpg", {}))
        self.assertEqual(calls, ["pbs.twimg.com"])

    async def test_streamed_body_limit_applies_without_content_length(self):
        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"a" * 150
                yield b"b" * 150
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Chunks()))) as c:
            with patch.object(config, "MAX_IMAGE_BYTES", 200):
                self.assertIsNone(await media_fetch.fetch(c, "https://pbs.twimg.com/a.jpg", {}))

    def test_pixel_limit_is_checked_before_writing(self):
        before = set(config.PICS_DIR.rglob("*.jpg"))
        with patch.object(config, "MAX_IMAGE_PIXELS", 10), self.assertRaises(ValueError):
            storage.save_image(_jpeg_bytes(10, 10), "https://pbs.twimg.com/a.jpg")
        self.assertEqual(set(config.PICS_DIR.rglob("*.jpg")), before)
