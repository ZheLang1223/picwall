"""媒体下载只接受平台 HTTPS 地址，每次重定向重验并限制响应体积。"""
from urllib.parse import urljoin, urlsplit

import httpx

from . import config


def permitted(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and (host == "sinaimg.cn" or host.endswith(".sinaimg.cn")
                     or host in ("pbs.twimg.com", "abs.twimg.com", "video.twimg.com")))
    except (ValueError, TypeError):
        return False


async def fetch(client: httpx.AsyncClient, url: str, headers: dict,
                max_bytes: int | None = None) -> bytes | None:
    """流式下载白名单媒体；体积预算默认 MAX_IMAGE_BYTES，视频走 MAX_VIDEO_BYTES。
    每一跳重定向都重验白名单；content-type 带 html/json/text 的响应一律拒收。"""
    limit = max_bytes if max_bytes is not None else config.MAX_IMAGE_BYTES
    try:
        for _hop in range(5):
            if not permitted(url):
                return None
            async with client.stream("GET", url, headers=headers, follow_redirects=False) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return None
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                if any(part in content_type for part in ("html", "json", "text/")):
                    return None
                declared = response.headers.get("content-length", "0")
                if int(declared) > limit:
                    return None
                chunks, size = [], 0
                async for chunk in response.aiter_bytes(64 * 1024):
                    size += len(chunk)
                    if size > limit:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks) if size >= 100 else None
    except (httpx.HTTPError, ValueError):
        return None
    return None
