"""媒体下载只接受平台 HTTPS 地址，每次重定向重验、校验解析 IP 并限制响应体积与耗时。"""
import asyncio
import hashlib
import ipaddress
import re
import socket
import tempfile
import time
from urllib.parse import urljoin, urlsplit

import httpx

from . import config

_ALLOWED_TWIMG_HOSTS = frozenset({"pbs.twimg.com", "abs.twimg.com", "video.twimg.com"})
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SPOOL_LIMIT = 8 * 1024 * 1024
_DEFAULT_IMAGE_TIMEOUT = 60.0
_DEFAULT_MEDIA_TIMEOUT = 300.0


def _valid_sinaimg_host(host: str) -> bool:
    if host == "sinaimg.cn":
        return True
    if not host.endswith(".sinaimg.cn") or host.startswith("."):
        return False
    prefix = host[: -len(".sinaimg.cn")]
    if not prefix:
        return False
    return all(_HOST_LABEL_RE.fullmatch(label) for label in prefix.split("."))


def permitted(url: str) -> bool:
    try:
        if not isinstance(url, str) or not url or any(ord(ch) <= 32 or ch == "\\" for ch in url):
            return False
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if not host or host.startswith(".") or host.endswith(".") or ".." in host:
            return False
        return (
            parsed.scheme == "https"
            and not parsed.username
            and not parsed.password
            and parsed.port in (None, 443)
            and (_valid_sinaimg_host(host) or host in _ALLOWED_TWIMG_HOSTS)
        )
    except (ValueError, TypeError):
        return False


def _is_public_ip(raw_ip: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_reserved
        and not ip.is_multicast
        and not ip.is_unspecified
    )


def _check_public_dns(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addrs = [info[4][0] for info in infos if len(info) >= 5 and info[4]]
    return bool(addrs) and all(_is_public_ip(str(addr)) for addr in addrs)


def _needs_dns_check(client: httpx.AsyncClient, url: str) -> bool:
    try:
        transport = client._transport_for_url(httpx.URL(url))
    except Exception:
        return True
    if isinstance(transport, httpx.MockTransport):
        return False
    pool = getattr(transport, "_pool", None)
    return not (pool is not None and ("Proxy" in type(pool).__name__ or getattr(pool, "_proxy_url", None) is not None))


def _permitted_content_type(raw_ct: str) -> bool:
    mime = raw_ct.split(";", 1)[0].strip().lower()
    if not mime:
        return True
    if mime.startswith("image/"):
        return mime != "image/svg+xml" and "svg" not in mime
    if mime.startswith("video/"):
        return True
    return mime == "application/octet-stream"


def _hop_headers(url: str, headers: dict) -> dict:
    host = (urlsplit(url).hostname or "").lower()
    clean = {k: v for k, v in (headers or {}).items() if k.lower() not in ("cookie", "authorization")}
    if host != "sinaimg.cn" and not host.endswith(".sinaimg.cn"):
        clean = {k: v for k, v in clean.items() if k.lower() != "referer"}
    return clean


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    max_bytes: int | None = None,
    timeout: float | None = None,
) -> bytes | None:
    """流式下载白名单媒体；逐块写入临时文件防爆内存，校验解析 IP、MIME 与总超时。"""
    limit = max_bytes if max_bytes is not None else config.MAX_IMAGE_BYTES
    budget = float(timeout if timeout is not None else (_DEFAULT_IMAGE_TIMEOUT if limit <= config.MAX_IMAGE_BYTES else _DEFAULT_MEDIA_TIMEOUT))
    deadline = time.monotonic() + max(0.01, budget)
    try:
        async with asyncio.timeout(max(0.01, budget)):
            for _hop in range(5):
                if not permitted(url):
                    return None
                host = (urlsplit(url).hostname or "").lower()
                check_dns = _needs_dns_check(client, url)
                if check_dns and not await asyncio.to_thread(_check_public_dns, host):
                    return None
                async with client.stream(
                    "GET", url, headers=_hop_headers(url, headers), follow_redirects=False
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        url = urljoin(url, location)
                        continue
                    if check_dns:
                        stream = response.extensions.get("network_stream")
                        sock = stream.get_extra_info("socket") if stream and hasattr(stream, "get_extra_info") else None
                        if sock is not None:
                            try:
                                peer = sock.getpeername()
                            except OSError:
                                peer = None
                            if peer and not _is_public_ip(str(peer[0])):
                                return None
                    response.raise_for_status()
                    if not _permitted_content_type(response.headers.get("content-type", "")):
                        return None
                    declared = (response.headers.get("content-length") or "").strip()
                    if declared:
                        content_len = int(declared)
                        if content_len < 0 or content_len > limit:
                            return None
                    tmp_dir = config.DATA_DIR / ".tmp" if config.DATA_DIR.is_dir() else None
                    if tmp_dir is not None:
                        tmp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    size = 0
                    with tempfile.SpooledTemporaryFile(max_size=_SPOOL_LIMIT, dir=tmp_dir) as tmp:
                        async for chunk in response.aiter_bytes(64 * 1024):
                            if time.monotonic() > deadline:
                                return None
                            size += len(chunk)
                            if size > limit:
                                return None
                            digest.update(chunk)
                            tmp.write(chunk)
                        if size < 100:
                            return None
                        tmp.seek(0)
                        return tmp.read()
    except (httpx.HTTPError, ValueError, TimeoutError, asyncio.TimeoutError, OSError):
        return None
    return None
