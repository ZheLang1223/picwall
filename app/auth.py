"""本机签发的一次性设备配对码；数据库只保存码与会话的哈希。"""
import hashlib
import secrets
import time

from starlette.responses import JSONResponse

from . import config, db

COOKIE = "pwall_device"
SESSION_SECONDS = 30 * 86400
CODE_SECONDS = 600
ROLES = {"admin", "editor", "viewer", "collector"}
MAX_REQUEST_BYTES = 2 * 1024 * 1024


class RequestBudget:
    """声明长度和分块传输均限制体积；不依赖客户端声明真实长度。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin", b"").decode("latin-1")
        expected = {"/api/ingest/harvest":"https://weibo.com", "/api/ingest/x":"https://x.com"}.get(scope["path"])
        cors = {"Access-Control-Allow-Origin":origin, "Vary":"Origin"} if origin == expected else {}
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_REQUEST_BYTES:
                return await JSONResponse({"detail": "单批请求超过 2 MiB，请拆分发送"}, status_code=413,
                                          headers=cors)(scope, receive, send)
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
        c.execute("INSERT INTO device_codes VALUES(?,?,?)", (hashed(raw), role, now + CODE_SECONDS))
    return "-".join(raw[i:i+4] for i in range(0, 12, 4))


def exchange(code: str, name: str, client: str) -> dict:
    raw = code.strip().upper().replace("-", "")
    now = time.time()
    with db._write_lock, db.conn() as c:
        c.execute("DELETE FROM pairing_attempts WHERE window<?", (now - 600,))
        total = c.execute("SELECT COALESCE(SUM(failures),0) FROM pairing_attempts").fetchone()[0]
        attempt = c.execute("SELECT failures FROM pairing_attempts WHERE client=?", (client,)).fetchone()
        if total >= 100 or (attempt and attempt[0] >= 10):
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
                      "DO UPDATE SET failures=failures+1", (client, now))
            return {"error": "配对码无效、已使用或已过期，请重新生成", "status": 400}
        c.execute("DELETE FROM pairing_attempts WHERE client=?", (client,))
        c.execute("DELETE FROM device_sessions WHERE expires<?", (now,))
        token, sid = secrets.token_urlsafe(32), secrets.token_hex(12)
        c.execute("INSERT INTO device_sessions VALUES(?,?,?,?,?,?)",
                  (sid, hashed(token), role, name.strip()[:60] or "已配对设备", now, now + SESSION_SECONDS))
        return {"token": token, "id": sid, "role": role}


def session(token: str) -> dict | None:
    if not token or len(token) > 256:
        return None
    with db.conn() as c:
        row = c.execute("SELECT id,role,name,expires FROM device_sessions WHERE hash=? AND expires>?",
                        (hashed(token), time.time())).fetchone()
        return dict(row) if row else None


def allowed_host(host: str | None) -> bool:
    return bool(host and host.lower() in config.ALLOWED_HOSTS)


def can_write(role: str, path: str) -> bool:
    if role == "admin":
        return True
    if role == "collector":
        return path in ("/api/ingest/harvest", "/api/ingest/x")
    if role == "editor":
        if path == "/api/posts/bulk-status":
            return True
        if path.startswith("/api/posts/"):
            tail = path[len("/api/posts/"):]
            if tail.rsplit("/", 1)[-1] in {"status", "restore", "favorite", "reaffirm"}:
                return True
            return tail.endswith("/comments") or "/comments/" in tail   # 评论增改删
        return False
    return False
