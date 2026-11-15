"""本机签发的一次性设备配对码；数据库只保存码与会话的哈希。"""
import asyncio
import hashlib
import secrets
import threading
import time

from starlette.datastructures import URL, Headers
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse

from . import config, db

COOKIE = "pwall_device"
SESSION_SECONDS = 30 * 86400
CODE_SECONDS = 600
ROLES = {"admin", "editor", "viewer", "collector"}
MAX_REQUEST_BYTES = 2 * 1024 * 1024
REQUEST_BODY_TIMEOUT = 15.0
SESSION_CACHE_TTL = 5.0
_SESSION_CACHE_MAX = 256
_SESSION_CACHE_LOCK = threading.Lock()
_SESSION_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}


def invalidate_session_cache(device_id: str | None = None, token_hash: str | None = None) -> None:
    """按设备 ID 或令牌哈希清理短 TTL 会话缓存（注销/吊销/清理后立即失效）。"""
    with _SESSION_CACHE_LOCK:
        if device_id is None and token_hash is None:
            _SESSION_CACHE.clear()
            return
        drop = [
            k for k, (_, entry) in _SESSION_CACHE.items()
            if (device_id is not None and entry.get("id") == device_id)
            or (token_hash is not None and k[1] == token_hash)
        ]
        for k in drop:
            _SESSION_CACHE.pop(k, None)


def _cors_for(path: str, origin: str) -> dict:
    expected = {"/api/ingest/harvest": "https://weibo.com", "/api/ingest/x": "https://x.com"}.get(path)
    return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"} if origin == expected else {}


_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _reject(detail: str, status_code: int, cors: dict | None = None) -> JSONResponse:
    headers = {**_SECURITY_HEADERS, **(cors or {})}
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


async def _preflight(scope) -> tuple[JSONResponse | None, dict]:
    """在读取任何请求体字节前校验 Host、Origin、写权限和声明长度。"""
    headers = Headers(scope=scope)
    if any(len(headers.getlist(h)) > 1 for h in ("host", "origin", "authorization", "content-length")):
        return _reject("请求头格式无效", 400), {}
    url = URL(scope=scope)
    try:
        host = url.hostname
    except ValueError:
        return _reject("地址无效", 400), {}
    if not allowed_host(host):
        return _reject("此地址未被允许，请使用 localhost 或已配置的局域网 IP", 403), {}
    path, method = scope["path"], scope["method"]
    local_listener = scope.get("pwall_local_owner", False)
    if local_listener and host not in ("localhost", "127.0.0.1", "::1"):
        return _reject("本机入口请使用 localhost；其他设备请使用局域网入口", 403), {}
    origin = headers.get("origin", "")
    cors = _cors_for(path, origin)
    base = (f"{url.scheme}://{url.netloc}" + scope.get("root_path", "")).rstrip("/")
    same_origin = origin == base
    extension = origin.startswith(("chrome-extension://", "moz-extension://"))
    bearer = headers.get("authorization", "")
    if origin and not same_origin and not (bearer and (cors or extension)) and not (path == "/api/device/pair" and extension):
        return _reject("跨来源请求未被允许", 403, cors), cors
    if not bearer and not same_origin and not (path == "/api/device/pair" and extension):
        return _reject("请从 PWALL 页面发送请求", 403, cors), cors
    local_owner = (
        local_listener
        and (not origin or same_origin)
        and not bearer
        and headers.get("sec-fetch-site", "") != "cross-site"
    )
    if path != "/api/device/pair" and not local_owner:
        cookie_val = "; ".join(headers.getlist("cookie"))
        token = bearer[7:] if bearer.startswith("Bearer ") else cookie_parser(cookie_val).get(COOKIE, "")
        current = await asyncio.to_thread(session, token)
        if not current:
            return _reject("设备尚未配对或连接已过期，请重新配对", 401, cors), cors
        self_logout = path == "/api/device/session" and method == "DELETE"
        if not self_logout and not can_write(current["role"], path):
            return _reject("当前设备没有此操作权限，请用管理设备操作", 403, cors), cors
    raw_cl = headers.get("content-length")
    if raw_cl is not None:
        try:
            cl = int(raw_cl)
        except ValueError:
            return _reject("请求长度无效", 400, cors), cors
        if cl < 0:
            return _reject("请求长度无效", 400, cors), cors
        if cl > MAX_REQUEST_BYTES:
            return _reject("单批请求超过 2 MiB，请拆分发送", 413, cors), cors
    return None, cors


class RequestBudget:
    """写请求读取请求体前先校验 Host/Origin/权限，再限流限速缓冲请求体。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        rejected, cors = await _preflight(scope)
        if rejected is not None:
            return await rejected(scope, receive, send)
        chunks, size = [], 0
        deadline = time.monotonic() + REQUEST_BODY_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return await _reject("请求体接收超时", 408, cors)(scope, receive, send)
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except asyncio.TimeoutError:
                return await _reject("请求体接收超时", 408, cors)(scope, receive, send)
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                return await _reject("单批请求超过 2 MiB，请拆分发送", 413, cors)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, bounded_receive, send)


def hashed(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def issue_code(role: str = "editor") -> str:
    if role not in ROLES:
        raise ValueError("权限必须为 admin、editor、viewer 或 collector")
    raw = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(12))
    with db._write_lock, db.conn() as c:
        now = time.time()
        c.execute("DELETE FROM device_codes WHERE expires<?", (now,))
        c.execute("DELETE FROM device_sessions WHERE expires<?", (now,))
        c.execute("INSERT INTO device_codes VALUES(?,?,?)", (hashed(raw), role, now + CODE_SECONDS))
    return "-".join(raw[i:i+4] for i in range(0, 12, 4))


def exchange(code: str, name: str, client: str) -> dict:
    raw = code.strip().upper().replace("-", "")
    client_key = (client or "unknown").strip() or "unknown"
    now = time.time()
    with db._write_lock, db.conn() as c:
        c.execute("DELETE FROM pairing_attempts WHERE window<?", (now - 600,))
        attempt = c.execute("SELECT failures FROM pairing_attempts WHERE client=?", (client_key,)).fetchone()
        if attempt and attempt[0] >= 10:
            return {"error": "尝试过多，请 10 分钟后重试", "status": 429}
        row = c.execute("SELECT role FROM device_codes WHERE hash=? AND expires>?",
                        (hashed(raw), now)).fetchone()
        role = None
        if row:
            c.execute("DELETE FROM device_codes WHERE hash=?", (hashed(raw),))   # 临时码一次性
            role = row[0]
        elif config.PAIR_CODE and secrets.compare_digest(hashed(raw), hashed(config.PAIR_CODE)):
            role = config.PAIR_ROLE   # 固定配对码：长期有效、可重复使用，不限次数
        if not role:
            c.execute("INSERT INTO pairing_attempts VALUES(?,?,1) ON CONFLICT(client) "
                      "DO UPDATE SET window=excluded.window, failures=failures+1", (client_key, now))
            return {"error": "配对码无效、已使用或已过期，请重新生成", "status": 400}
        c.execute("DELETE FROM pairing_attempts WHERE client=?", (client_key,))
        c.execute("DELETE FROM device_sessions WHERE expires<?", (now,))
        token, sid = secrets.token_urlsafe(32), secrets.token_hex(12)
        c.execute("INSERT INTO device_sessions VALUES(?,?,?,?,?,?)",
                  (sid, hashed(token), role, name.strip()[:60] or "已配对设备", now, now + SESSION_SECONDS))
        return {"token": token, "id": sid, "role": role}


def session(token: str) -> dict | None:
    if not token or len(token) > 256:
        return None
    tok_hash = hashed(token)
    cache_key = (str(config.DB_PATH), tok_hash)
    now_mono = time.monotonic()
    with _SESSION_CACHE_LOCK:
        cached = _SESSION_CACHE.get(cache_key)
        if cached:
            if cached[0] > now_mono:
                return dict(cached[1])
            _SESSION_CACHE.pop(cache_key, None)
    now = time.time()
    with db.conn() as c:
        row = c.execute("SELECT id,role,name,expires FROM device_sessions WHERE hash=? AND expires>?",
                        (tok_hash, now)).fetchone()
    if not row:
        return None
    data = dict(row)
    ttl = min(SESSION_CACHE_TTL, max(0.0, float(data["expires"]) - now))
    if ttl > 0:
        deadline = time.monotonic() + ttl
        with _SESSION_CACHE_LOCK:
            if len(_SESSION_CACHE) >= _SESSION_CACHE_MAX:
                expired = [k for k, (exp, _) in _SESSION_CACHE.items() if exp <= now_mono]
                for k in expired:
                    _SESSION_CACHE.pop(k, None)
                if len(_SESSION_CACHE) >= _SESSION_CACHE_MAX:
                    _SESSION_CACHE.pop(next(iter(_SESSION_CACHE)), None)
            _SESSION_CACHE[cache_key] = (deadline, data)
    return dict(data)


def allowed_host(host: str | None) -> bool:
    return bool(host and host.lower() in config.ALLOWED_HOSTS)


def can_write(role: str, path: str) -> bool:
    if role == "admin":
        return True
    if role == "collector":
        return path in ("/api/ingest/harvest", "/api/ingest/x")
    if role == "editor":
        if path in ("/api/posts/bulk-status", "/api/posts/bulk-tags"):
            return True
        if path.startswith("/api/posts/"):
            tail = path[len("/api/posts/"):]
            parts = tail.split("/")
            if len(parts) == 2 and parts[0] and parts[1] in {"status", "restore", "favorite", "reaffirm", "tags", "comments"}:
                return True
            return len(parts) == 3 and bool(parts[0]) and parts[1] == "comments" and bool(parts[2])
        return False
    return False
