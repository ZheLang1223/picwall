"""Picwall 服务：相册页 + JSON API + 图片/打包下载 + 每日调度。

单进程单容器；调度用 AsyncIOScheduler 挂在 uvicorn 的事件循环上。
"""
import asyncio
import base64
import bisect
import functools
import io
import json
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.datastructures import URL, Headers, MutableHeaders
from starlette.requests import Request, cookie_parser

from . import (
    auth,
    config,
    connections,
    crawler,
    db,
    demo,
    downloads,
    jobs,
    maintenance,
    purge,
    storage,
    weibo,
)
from .queries import post_filter as _post_filter
from .sources import base as source_errors
from .sources import x as x_source
from .sources import xapi

# 应用版本：发版时递增；随启动日志与 /api/status 暴露，便于用户反馈时对版本
APP_VERSION = "0.5.7"

logger = logging.getLogger("picwall")
# uvicorn 只配自己的 logger：这里给 root 兜底挂 handler，INFO 级启动/抓取日志才能落到容器日志。
# 已有 handler 的宿主环境 basicConfig 是 no-op，不会重复初始化。
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

# 模板与静态资源锚定到模块位置：桌面/快捷方式拉起时 cwd 不再影响启动
_APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))
sched = AsyncIOScheduler(timezone="Asia/Shanghai")

# 静态资源缓存版本号 = 进程启动时间：每次重建容器自动失效浏览器缓存，杜绝改完代码看到旧界面
STATIC_V = str(int(datetime.now().timestamp()))

# create_task 的引用必须有人拿着，否则任务可能被 GC 掉（官方文档警告的反模式）
_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.get_running_loop().create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


async def _run_crawl(*args, **kwargs):
    try:
        return await crawler.crawl(*args, **kwargs)
    finally:
        _invalidate_read_caches()


def _run_purge_job():
    try:
        return purge.run()
    finally:
        _invalidate_read_caches()


async def _run_media_retry_job():
    try:
        return await crawler.retry_missing()
    finally:
        _invalidate_read_caches()


def _run_repair_job():
    try:
        return maintenance.repair_local()
    finally:
        _invalidate_read_caches()


async def _run_catch_up():
    try:
        return await crawler.catch_up_if_stale()
    finally:
        _invalidate_read_caches()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    jobs.recover_interrupted()
    crawler.recover_interrupted()
    for leftover in _app_tmp_dir().glob("*.zip"):
        _unlink_quiet(str(leftover))
    # 克制的启动日志：一条说清版本/平台/数据目录/schema，用户反馈时够定位
    logger.info("picwall %s 启动 platform=%s data=%s schema=v%s",
                APP_VERSION, sys.platform, config.DATA_DIR,
                db.get_meta("schema_version") or "?")
    hh, mm = config.crawl_hm()
    # misfire_grace_time 给足 1 小时：宿主睡眠跨过抓取点，醒来仍补跑而非静默跳过
    sched.add_job(_run_crawl, "cron", hour=hh, minute=mm, id="daily",
                  misfire_grace_time=3600, kwargs={"trigger": "cron"})
    if config.PAIR_CODE_WEAK:
        logger.warning("security fixed_pair_code len<%d，限流上限内可被穷举，请换更长的码",
                       config.PAIR_CODE_MIN_LEN)
    # 「已删除」超期清理：每天凌晨 4:10 跑一轮，PURGE_DAYS 控制阈值
    sched.add_job(_run_purge_job, "cron", hour=4, minute=10, id="purge", misfire_grace_time=3600)
    # X 列表服务端抓取：独立间隔（容器时区），0 小时 = 关闭
    if config.X_CRAWL_INTERVAL_HOURS > 0:
        sched.add_job(_crawl_x, "interval", hours=config.X_CRAWL_INTERVAL_HOURS,
                      id="crawl-x", misfire_grace_time=1800)
    sched.add_job(_run_media_retry_job, "interval", hours=4, id="media-retry")
    sched.add_job(_run_repair_job, "interval", hours=24, id="local-repair")
    sched.start()
    if config.get_cookie() and db.active_groups():
        _spawn(_run_catch_up())
    try:
        yield
    finally:
        sched.shutdown(wait=False)
        await jobs.runner.shutdown()


app = FastAPI(title="picwall", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

_ZIP_DOWNLOAD_LOCK = threading.Lock()
_ZIP_CLAIMED: set[str] = set()
_CRED_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")


def _redact_err(err: Exception | str) -> str:
    return _CRED_URL_RE.sub(r"\1***@", str(err))


_HTML_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' https: http: data:; media-src 'self'; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
_STATUS_ARCHIVE_TTL = 15.0
_SHUFFLE_CACHE_TTL = 15.0
_SHUFFLE_CACHE_MAX = 64

_READ_EPOCH = 0
_READ_EPOCH_LOCK = threading.Lock()
_CLOUD_MANIFEST_LOCK = threading.Lock()
_CLOUD_MANIFEST_CACHE: dict[str, tuple[tuple[str, int, int, int, int], frozenset[str], str | None]] = {}
_ARCHIVE_CACHE_LOCK = threading.Lock()
_ARCHIVE_HEALTH_CACHE: dict[tuple[str, str, str], tuple[int, float, dict]] = {}
_SHUFFLE_CACHE_LOCK = threading.Lock()
_SHUFFLE_CACHE: OrderedDict[tuple, tuple[float, list]] = OrderedDict()


def _app_tmp_dir() -> Path:
    d = config.DATA_DIR / ".tmp"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _read_epoch() -> int:
    with _READ_EPOCH_LOCK:
        return _READ_EPOCH


def _invalidate_read_caches() -> None:
    global _READ_EPOCH
    with _READ_EPOCH_LOCK:
        _READ_EPOCH += 1
    with _ARCHIVE_CACHE_LOCK:
        _ARCHIVE_HEALTH_CACHE.clear()
    with _SHUFFLE_CACHE_LOCK:
        _SHUFFLE_CACHE.clear()


def _cors_headers(path: str, origin: str) -> dict[str, str]:
    expected = {"/api/ingest/harvest": "https://weibo.com", "/api/ingest/x": "https://x.com"}.get(path)
    if origin == expected:
        return {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "content-type, authorization",
                "Access-Control-Allow-Private-Network": "true", "Vary": "Origin"}
    return {}


class DeviceBoundary:
    """Pure ASGI 设备配对边界、CSRF/Host/Origin 防护与安全响应头中间件。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if (len(headers.getlist("host")) > 1
                or len(headers.getlist("origin")) > 1
                or len(headers.getlist("authorization")) > 1
                or len(headers.getlist("sec-fetch-site")) > 1):
            await JSONResponse({"detail": "请求头格式无效"}, status_code=400)(scope, receive, send)
            return
        url = URL(scope=scope)
        try:
            host = url.hostname
        except ValueError:
            await JSONResponse({"detail": "地址无效"}, status_code=400)(scope, receive, send)
            return
        if not auth.allowed_host(host):
            await JSONResponse(
                {"detail": "此地址未被允许，请使用 localhost 或已配置的局域网 IP"}, status_code=403
            )(scope, receive, send)
            return
        path, method = url.path, scope.get("method", "GET")
        local_listener = scope.get("pwall_local_owner", False)
        if local_listener and host not in ("localhost", "127.0.0.1", "::1"):
            await JSONResponse(
                {"detail": "本机入口请使用 localhost；其他设备请使用局域网入口"}, status_code=403
            )(scope, receive, send)
            return
        origin = headers.get("origin", "")
        cors = _cors_headers(path, origin)
        if method == "OPTIONS":
            await Response(status_code=204 if cors else 403, headers=cors)(scope, receive, send)
            return
        base_scope = dict(scope)
        app_root_path = base_scope.get("app_root_path", base_scope.get("root_path", ""))
        base_scope["path"] = app_root_path if app_root_path.endswith("/") else app_root_path + "/"
        base_scope["query_string"] = b""
        base_scope["root_path"] = app_root_path
        base_url = str(URL(scope=base_scope)).rstrip("/")
        same_origin = origin == base_url
        extension = origin.startswith(("chrome-extension://", "moz-extension://"))
        bearer = headers.get("authorization", "")
        cookie_header = headers.get("cookie")
        cookies = cookie_parser(cookie_header) if cookie_header else {}
        token = bearer[7:] if bearer.startswith("Bearer ") else cookies.get(auth.COOKIE, "")
        public = path in ("/", "/api/health", "/api/device/pair") or path.startswith("/static/")
        write = method not in ("GET", "HEAD")
        if origin and not same_origin and not (bearer and (cors or extension)) and not (path == "/api/device/pair" and extension):
            await JSONResponse({"detail": "跨来源请求未被允许"}, status_code=403)(scope, receive, send)
            return
        if write and not bearer and not same_origin and not (path == "/api/device/pair" and extension):
            await JSONResponse({"detail": "请从 PWALL 页面发送请求"}, status_code=403)(scope, receive, send)
            return
        # 仅宿主回环端口可产生此标记；浏览器扩展等跨来源客户端仍须持有自己的令牌。
        # 浏览器跨站子资源（img 等）不带 Origin 却带 Sec-Fetch-Site: cross-site——
        # 它们虽读不到响应，但会以 local_owner 身份触发读类 GET 副作用（回看出场、打包），
        # 一并挡下；curl/扩展/无该头的旧浏览器不受影响（仍受 Origin 与配对边界约束）。
        local_owner = (local_listener and (not origin or same_origin) and not bearer
                       and headers.get("sec-fetch-site", "") != "cross-site")
        current = ({"id": "local-owner", "name": "运行 PWALL 的本机", "role": "admin", "local_owner": True}
                   if local_owner else await asyncio.to_thread(auth.session, token))
        scope.setdefault("state", {})["device"] = current
        if not public:
            if not current:
                await JSONResponse(
                    {"detail": "设备尚未配对或连接已过期，请重新配对"}, status_code=401, headers=cors
                )(scope, receive, send)
                return
            self_logout = path == "/api/device/session" and method == "DELETE"
            if write and not self_logout and not auth.can_write(current["role"], path):
                await JSONResponse(
                    {"detail": "当前设备没有此操作权限，请用管理设备操作"}, status_code=403, headers=cors
                )(scope, receive, send)
                return
            if current["role"] == "collector" and method in ("GET", "HEAD") and path != "/api/device/session":
                await JSONResponse({"detail": "采集设备仅可提交内容"}, status_code=403)(scope, receive, send)
                return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                hdrs = MutableHeaders(scope=message)
                if path.startswith(("/api/", "/media/")):
                    hdrs["Cache-Control"] = "no-store"
                hdrs["X-Content-Type-Options"] = "nosniff"
                hdrs["X-Frame-Options"] = "DENY"
                hdrs["Referrer-Policy"] = "no-referrer"
                if path.startswith("/media/"):
                    hdrs["Content-Security-Policy"] = "default-src 'none'; sandbox"
                elif not path.startswith("/api/"):
                    hdrs.setdefault("Content-Security-Policy", _HTML_CSP)
            await send(message)

        await self.app(scope, receive, send_wrapper)


@app.get("/api/health")
async def health():
    if not await asyncio.to_thread(db.get_meta, "schema_version"):
        raise HTTPException(503, "数据库尚未就绪")
    return {"ok": True, "version": APP_VERSION}


class PairBody(BaseModel):
    code: str
    name: str = ""
    bearer: bool = False


@app.post("/api/device/pair")
async def pair_device(body: PairBody, request: Request):
    if len(body.code) > 64 or len(body.name) > 60:
        raise HTTPException(400, "配对码或设备名称过长")
    result = await asyncio.to_thread(auth.exchange, body.code, body.name,
                                     request.client.host if request.client else "unknown")
    if "error" in result:
        raise HTTPException(result["status"], result["error"])
    token = result.pop("token")
    response = JSONResponse(result | ({"token": token} if body.bearer else {}))
    if not body.bearer:
        response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_SECONDS,
                            httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return response


class PairCodeBody(BaseModel):
    role: str = "editor"


@app.post("/api/device/code")
async def issue_pair_code(body: PairCodeBody):
    """签发设备配对码（仅管理设备，write 中间件按角色放行）：一次性、10 分钟有效。
    明文码只进当前管理设备的页面，不落日志；新设备在配对页输入，或扫携带码的二维码自动填入。"""
    if body.role not in auth.ROLES:
        raise HTTPException(400, "未知设备角色")
    code = await asyncio.to_thread(auth.issue_code, body.role)
    return {"ok": True, "code": code, "role": body.role, "ttl": auth.CODE_SECONDS}


@app.get("/api/device/session")
async def current_device(request: Request):
    return request.state.device


@app.delete("/api/device/session")
async def logout_device(request: Request):
    if request.state.device.get("local_owner"):
        raise HTTPException(409, "本机入口无需配对；关闭页面即可离开")
    device_id = request.state.device["id"]

    def _delete():
        with db._write_lock, db.conn() as c:
            c.execute("DELETE FROM device_sessions WHERE id=?", (device_id,))

    await asyncio.to_thread(_delete)
    auth.invalidate_session_cache(device_id=device_id)
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE)
    return response


@app.get("/api/devices")
async def list_devices(request: Request):
    if request.state.device["role"] != "admin":
        raise HTTPException(403, "只有管理设备可查看设备列表")

    def _load():
        with db.conn() as c:
            return [dict(row) for row in c.execute(
                "SELECT id,name,role,created,expires FROM device_sessions WHERE expires>? ORDER BY created DESC",
                (time.time(),))]

    return {"devices": await asyncio.to_thread(_load)}


@app.delete("/api/devices/{device_id}")
async def revoke_device(device_id: str):
    def _delete():
        with db._write_lock, db.conn() as c:
            c.execute("DELETE FROM device_sessions WHERE id=?", (device_id,))

    await asyncio.to_thread(_delete)
    auth.invalidate_session_cache(device_id=device_id)
    return {"ok": True}


class RevalidatedStatic(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if path.endswith((".js", ".css")):
            response.headers["Cache-Control"] = "no-cache"
        return response


app.add_middleware(DeviceBoundary)
app.add_middleware(auth.RequestBudget)
app.mount("/static", RevalidatedStatic(directory=str(_APP_DIR / "static")), name="static")


# ---------- 相册页 ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # no-cache：HTML 永远回源，配合 ?v= 静态版本号，重建后必定看到新界面
    return templates.TemplateResponse(
        request, "index.html" if request.state.device and request.state.device["role"] != "collector" else "pair.html",
        {"v": STATIC_V, "role": request.state.device["role"] if request.state.device else "",
         "local_owner": bool(request.state.device and request.state.device.get("local_owner"))},
        headers={"Cache-Control": "no-cache"})


@app.get("/atlas", response_class=HTMLResponse)
async def atlas_page(request: Request):
    """PWALL 设置 → 开发管理：单文件交互式架构图谱（生成物，源文件 docs/project_atlas.html）。"""
    if request.state.device["role"] != "admin":
        raise HTTPException(403, "只有管理设备可打开开发管理")
    return FileResponse(_APP_DIR.parent / "docs" / "project_atlas.html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/kiosk", response_class=HTMLResponse)
async def kiosk_page(request: Request):
    """PWALL 报刊亭：博主杂志墙（每位博主一本「杂志」，点开看专栏内页）。"""
    return templates.TemplateResponse(request, "kiosk.html", {"v": STATIC_V},
                                      headers={"Cache-Control": "no-cache"})


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    """PWALL 回看统计：入库/保留/点赞的本地聚合报告页（独立深色页，模式同报刊亭）。"""
    return templates.TemplateResponse(request, "stats.html", {"v": STATIC_V},
                                      headers={"Cache-Control": "no-cache"})


# ---------- 状态与抓取 ----------

def _cookie_state() -> dict:
    return connections.status("weibo")


def _cloud_state() -> dict:
    """备份 blob 库的云端同步体检：登记数 vs 最新 manifest 覆盖率。"""
    out = {"blobs": 0, "last_upload": None, "manifest_files": None,
           "covered": None, "store": None}
    try:
        with db.conn() as c:
            has_table = c.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_blobs'"
            ).fetchone()
            registered: set[str] = set()
            if has_table:
                out["blobs"], out["last_upload"] = c.execute(
                    "SELECT COUNT(*), MAX(uploaded_at) FROM cloud_blobs"
                ).fetchone()
                if out["blobs"]:
                    registered = {r["key"] for r in c.execute("SELECT key FROM cloud_blobs")}
        snaps = sorted((config.DATA_DIR / "backups").glob("picwall-*.snapshot/manifest.json"))
        if snaps:
            snap = snaps[-1]
            st = snap.stat()
            sig = (str(snap), st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size)
            data_key = str(config.DATA_DIR)
            with _CLOUD_MANIFEST_LOCK:
                cached = _CLOUD_MANIFEST_CACHE.get(data_key)
            if cached and cached[0] == sig:
                keys, store = cached[1], cached[2]
            else:
                manifest = json.loads(snap.read_text())
                # 凭据只进本地快照（manifest.local_only），不参与云端覆盖率核对
                local_only = set(manifest.get("local_only", []))
                keys = frozenset(
                    e["sha256"] for rel, e in manifest["files"].items() if rel not in local_only
                )
                store = manifest.get("blob_store")   # 产生该 manifest 的实际后端
                with _CLOUD_MANIFEST_LOCK:
                    _CLOUD_MANIFEST_CACHE[data_key] = (sig, keys, store)
            out["manifest_files"] = len(keys)
            out["store"] = store
            out["covered"] = keys <= registered if out["blobs"] else False
    except (OSError, ValueError, KeyError):
        pass
    return out


def _status_archive_health() -> dict:
    key = (str(config.DATA_DIR), str(config.EXPORT_DIR), str(config.DB_PATH))
    epoch = _read_epoch()
    now = time.monotonic()
    with _ARCHIVE_CACHE_LOCK:
        cached = _ARCHIVE_HEALTH_CACHE.get(key)
        if cached and cached[0] == epoch and cached[1] > now:
            return cached[2]
    fresh = storage.archive_health()
    with _ARCHIVE_CACHE_LOCK:
        if _read_epoch() == epoch:
            _ARCHIVE_HEALTH_CACHE[key] = (epoch, time.monotonic() + _STATUS_ARCHIVE_TTL, fresh)
    return fresh


_STATUS_META_KEYS = (
    "schema_version", "last_result", "last_crawl", "last_ingest",
    "last_attempt", "last_error", "x_last_crawl", "x_last_error",
)


def _collect_status() -> dict:
    with db.conn() as c:
        by_status = dict(c.execute(
            "SELECT status, COUNT(*) FROM posts GROUP BY status").fetchall())
        today_pending = c.execute(
            "SELECT COUNT(*) FROM posts WHERE status='pending' AND created_at >= ?",
            (datetime.now(config.NOW_TZ).strftime("%Y-%m-%dT00:00:00+08:00"),)).fetchone()[0]
        groups = [dict(r) | {"active": bool(r["active"])} for r in c.execute(
            "WITH links AS (SELECT list_id, sid FROM posts UNION SELECT list_id, sid FROM post_groups)"
            " SELECT g.list_id, g.name, g.active,"
            " SUM(CASE WHEN p.status='pending' THEN 1 ELSE 0 END) pending,"
            " SUM(CASE WHEN p.status='kept' THEN 1 ELSE 0 END) kept,"
            " SUM(CASE WHEN p.status='discarded' THEN 1 ELSE 0 END) discarded,"
            " COUNT(p.sid) total"
            " FROM groups g LEFT JOIN links l ON l.list_id = g.list_id"
            " LEFT JOIN posts p ON p.sid = l.sid"
            " GROUP BY g.list_id ORDER BY g.active DESC, g.added_at").fetchall()]
        purge_due = c.execute(
            "SELECT COUNT(*) FROM posts WHERE status='discarded' AND COALESCE(favorite,0)=0"
            " AND discarded_at IS NOT NULL AND discarded_at < ?",
            ((datetime.now(config.NOW_TZ) - timedelta(days=config.PURGE_DAYS))
             .isoformat(timespec="seconds"),)).fetchone()[0]
        favorites = c.execute("SELECT COUNT(*) FROM posts WHERE favorite=1").fetchone()[0]
        protected = c.execute(
            "SELECT COUNT(*) FROM posts WHERE status='discarded' AND favorite=1").fetchone()[0]
        missing_posts = c.execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0]
        qmarks = ",".join("?" * len(_STATUS_META_KEYS))
        metas = {k: v for k, v in c.execute(
            f"SELECT k, v FROM meta WHERE k IN ({qmarks})", _STATUS_META_KEYS)}
    hh, mm = config.crawl_hm()
    last_result = metas.get("last_result", "")
    conns = {s: connections.status(s) for s in ("weibo", "x")}
    archive = _status_archive_health()
    return {
        "version": APP_VERSION,
        "platform": sys.platform,
        "schema_version": metas.get("schema_version") or None,
        "cookie": conns["weibo"],
        "connections": conns,
        "groups": groups,
        "counts": {"pending": by_status.get("pending", 0),
                   "kept": by_status.get("kept", 0),
                   "discarded": by_status.get("discarded", 0),
                   "today_pending": today_pending,
                   "favorites": favorites},
        "purge": {"days": config.PURGE_DAYS, "due": purge_due,
                  "discarded": by_status.get("discarded", 0), "protected": protected},
        "cloud": _cloud_state(),
        "crawl": {"running": crawler.state["running"],
                  "time": f"{hh:02d}:{mm:02d}",
                  "last": metas.get("last_crawl", ""),
                  "ingest": metas.get("last_ingest", ""),
                  "attempt": metas.get("last_attempt", ""),
                  "last_result": json.loads(last_result) if last_result else None,
                  "error": metas.get("last_error", ""),
                  "progress": crawler.state.get("progress", {}),
                  "x_auth": bool(config.get_x_auth()),
                  "x_last_crawl": metas.get("x_last_crawl", ""),
                  "x_last_error": metas.get("x_last_error", "")},
        "media": {"incomplete_posts": missing_posts},
        "pairing": {"fixed_code_weak": config.PAIR_CODE_WEAK},
        "jobs": {kind: jobs.status(kind) for kind in jobs.KINDS},
        "archive": {"missing": archive["missing"], "missing_originals": archive["missing_originals"]},
        "demo": demo.is_on(),
    }


@app.get("/api/status")
async def status():
    return await asyncio.to_thread(_collect_status)


def _x_list_progress(idx: int, summary: dict | None) -> None:
    return crawler._x_list_progress(idx, summary)


def _x_http_client() -> httpx.AsyncClient:
    return crawler.x_http_client()


async def _crawl_x(only: str | None = None, max_pages: int | None = None) -> dict:
    """X 服务端抓取：委托给 crawler.crawl_x 执行。"""
    try:
        return await crawler.crawl_x(http_client_factory=_x_http_client, xapi_module=xapi,
                                     only=only, max_pages=max_pages)
    finally:
        _invalidate_read_caches()


@app.post("/api/crawl/x")
async def crawl_x_now():
    r = await _crawl_x()
    return r if r.get("ok") else JSONResponse(r, status_code=409)


@app.post("/api/crawl")
async def trigger_crawl(source: str | None = None):
    if crawler.state["running"]:
        return JSONResponse({"ok": False, "msg": "已有抓取在进行中"}, status_code=409)

    def _preflight():
        wb_cookie = bool(config.get_cookie())
        x_auth = bool(config.get_x_auth())
        wb_groups = bool(db.active_groups(source="weibo"))
        x_groups = bool(db.active_groups(source="x"))
        return wb_cookie, x_auth, wb_groups, x_groups

    wb_cookie, x_auth, wb_groups, x_groups = await asyncio.to_thread(_preflight)
    has_wb = wb_cookie and wb_groups
    has_x = x_auth and x_groups

    if source == "x":
        if not x_auth:
            return JSONResponse({"ok": False, "msg": "还没有 X Cookie"}, status_code=400)
        if not x_groups:
            return JSONResponse({"ok": False, "msg": "还没有 X 列表或博主分组"}, status_code=400)
        _spawn(_crawl_x())
        await asyncio.sleep(0)
        return {"ok": True, "msg": "X 抓取已开始"}

    if source == "weibo":
        if not has_wb:
            r = await crawler.crawl(trigger="manual")
            return JSONResponse(r, status_code=400)
        _spawn(_run_crawl(trigger="manual"))
        await asyncio.sleep(0)
        return {"ok": True, "msg": "微博抓取已开始"}

    if not has_wb and not has_x:
        r = await crawler.crawl(trigger="manual")
        return JSONResponse(r, status_code=400)

    if has_wb and has_x:
        async def _crawl_both():
            await _run_crawl(trigger="manual")
            await _crawl_x()
        _spawn(_crawl_both())
    elif has_x:
        _spawn(_crawl_x())
    else:
        _spawn(_run_crawl(trigger="manual"))
    await asyncio.sleep(0)
    return {"ok": True, "msg": "抓取已开始"}


# ---------- 帖子列表与标记 ----------

def _post_payloads(rows) -> list[dict]:
    """帖子行 → 前端字典（pics/分组/补图状态一并取齐），列表与回看选组共用。"""
    if not rows:
        return []
    sids = [r["sid"] for r in rows]
    qmarks = ",".join("?" * len(sids))
    with db.conn() as c:
        by_sid: dict[str, list] = {}
        for r in c.execute(f"SELECT * FROM pics WHERE sid IN ({qmarks}) ORDER BY position,id",
                           sids):
            by_sid.setdefault(r["sid"], []).append(
                {"file": r["file"], "thumb": r["thumb"], "w": r["w"], "h": r["h"],
                 "position": r["position"], "quality": r["quality"],
                 "kind": r["kind"] or "photo", "dur_ms": r["dur_ms"] or 0})
        incomplete = {r[0] for r in c.execute(f"SELECT sid FROM image_jobs WHERE sid IN ({qmarks})", sids)}
        group_ids: dict[str, list] = {}
        for sid, lid in c.execute(f"SELECT sid,list_id FROM post_groups WHERE sid IN ({qmarks})",
                                  sids):
            group_ids.setdefault(sid, []).append(lid)
        comment_counts = {r["sid"]: r["n"] for r in c.execute(
            f"SELECT sid,COUNT(*) n FROM post_comments WHERE sid IN ({qmarks}) GROUP BY sid", sids)}
        # 墙卡评论预览行：每帖最新一条（有评论才在卡上出现）
        comment_preview = {r["sid"]: r["body"] for r in c.execute(
            f"SELECT pc.sid, pc.body FROM post_comments pc JOIN "
            f"(SELECT sid, MAX(id) m FROM post_comments WHERE sid IN ({qmarks}) GROUP BY sid) t "
            f"ON t.sid = pc.sid AND t.m = pc.id", sids)}
    tags_map = db.post_tags_map(sids)
    return [{
        "sid": r["sid"], "uid": db.author_key(r["source"], r["uid"]), "list_id": r["list_id"], "author": r["author"],
        "groups": group_ids.get(r["sid"], []), "text_complete": bool(r["text_complete"]),
        "decision_version": r["decision_version"],
        "avatar": r["avatar"], "text": r["text"], "created_at": r["created_at"],
        "weibo_url": r["weibo_url"], "via": r["via"], "status": r["status"],
        "group": r["group_name"], "kept_at": r["kept_at"],
        "discarded_at": r["discarded_at"],
        "favorite": bool(r["favorite"]), "source": r["source"] or "weibo",
        "keep_count": r["keep_count"] or 1,
        "last_confirm_at": r["last_confirm_at"],
        "comment_count": comment_counts.get(r["sid"], 0),
        "comment_preview": comment_preview.get(r["sid"], ""),
        "tags": tags_map.get(r["sid"], []),
        "pics": by_sid.get(r["sid"], []), "image_complete": r["sid"] not in incomplete,
    } for r in rows]


def _shuffled_sids_cached(cond: str, args, seed: str) -> list:
    epoch = _read_epoch()
    with db.conn() as c:
        rev = tuple(c.execute(
            "SELECT COUNT(*), COALESCE(SUM(decision_version), 0),"
            " COALESCE(SUM(status='pending'), 0), COALESCE(SUM(status='kept'), 0) FROM posts"
        ).fetchone())
    key = (str(config.DB_PATH), epoch, rev, cond, tuple(args), seed)
    now = time.monotonic()
    with _SHUFFLE_CACHE_LOCK:
        cached = _SHUFFLE_CACHE.get(key)
        if cached and cached[0] > now:
            _SHUFFLE_CACHE.move_to_end(key)
            return cached[1]
    fresh = db.shuffled_sids(cond, args, seed)
    with _SHUFFLE_CACHE_LOCK:
        if _read_epoch() == epoch:
            _SHUFFLE_CACHE[key] = (time.monotonic() + _SHUFFLE_CACHE_TTL, fresh)
            _SHUFFLE_CACHE.move_to_end(key)
            while len(_SHUFFLE_CACHE) > _SHUFFLE_CACHE_MAX:
                _SHUFFLE_CACHE.popitem(last=False)
    return fresh


@app.get("/api/posts")
async def list_posts(status: str = "pending", group: str = "all", author: str = "all",
                     range: str = "all", q: str = "", q_scope: str = "all",
                     has_cmts: str = "all", cmts: str = "", source: str = "all",
                     tags: str = "all",
                     offset: int = 0, limit: int = 50, cursor: str = "", sort: str = "",
                     shuffle: str = ""):
    if not 1 <= limit <= 200:
        limit = 50
    offset = max(offset, 0)
    by_count = sort == "keep_count"   # 点赞序：点赞降序 → 时间降序 → sid，游标同步三键
    seed = (shuffle or "").strip()[:64]
    if seed:
        # 随机档：db.shuffled_sids 给出 (seed, 筛选集) 的确定性虚拟全序，翻页沿用
        # 游标机制、锚升级为位次摘要 key（时间键在这里没有意义）；与 sort 互斥，
        # 前端同传时随机优先。首个 key 定序、第二个兜底同 key 撞车（sha256 可忽略）。
        decoded_shuffle: tuple[str, str] | None = None
        if cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if (not isinstance(decoded, list) or len(decoded) != 2 or
                        not all(isinstance(x, str) for x in decoded)):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError):
                raise HTTPException(400, "分页游标无效，请刷新列表")
            decoded_shuffle = (decoded[0], decoded[1])

        def _load_shuffled():
            cond, args = _post_filter(
                status, group, author, range, has_cmts, source=source, tags=tags, q=q, q_scope=q_scope
            )
            keyed = _shuffled_sids_cached(cond, args, seed)
            lo = bisect.bisect_right(keyed, decoded_shuffle) if decoded_shuffle else 0
            page = keyed[lo:lo + limit]
            has_more = lo + limit < len(keyed)
            rows = []
            if page:
                sids = [sid for _, sid in page]
                by_order = {sid: i for i, sid in enumerate(sids)}
                qmarks = ",".join("?" * len(sids))
                with db.conn() as c:
                    rows = c.execute(
                        f"SELECT p.*, COALESCE(g.name, p.list_id) group_name FROM posts p"
                        f" LEFT JOIN groups g ON g.list_id = p.list_id"
                        f" WHERE p.sid IN ({qmarks})", sids).fetchall()
                rows.sort(key=lambda r: by_order[r["sid"]])   # 虚拟全序 = 展示序
            next_cursor = None
            if has_more and page:
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps(list(page[-1])).encode()).decode()
            return {"total": len(keyed), "posts": _post_payloads(rows),
                    "next_cursor": next_cursor, "has_more": has_more}

        return await asyncio.to_thread(_load_shuffled)

    decoded_cursor: list | None = None
    if cursor:
        if len(cursor) > 512:
            raise HTTPException(400, "分页游标无效，请刷新列表")
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        except (ValueError, TypeError, UnicodeError):
            raise HTTPException(400, "分页游标无效，请刷新列表")
        if by_count:
            if (not isinstance(decoded, list) or len(decoded) != 3
                    or isinstance(decoded[0], bool) or not isinstance(decoded[0], int)
                    or not (-(2**63) <= decoded[0] < 2**63)
                    or not isinstance(decoded[1], str) or not isinstance(decoded[2], str)):
                raise HTTPException(400, "分页游标无效，请刷新列表")
        else:
            if (not isinstance(decoded, list) or len(decoded) != 2
                    or not isinstance(decoded[0], str) or not isinstance(decoded[1], str)):
                raise HTTPException(400, "分页游标无效，请刷新列表")
        decoded_cursor = decoded

    def _load_ordered():
        cond, args = _post_filter(
            status, group, author, range, has_cmts, source=source, tags=tags, q=q, q_scope=q_scope
        )
        page_cond, page_args = cond, list(args)
        if decoded_cursor is not None:
            if by_count:
                count, created, sid = decoded_cursor
                page_cond += (" AND (COALESCE(p.keep_count,1) < ? OR (COALESCE(p.keep_count,1) = ?"
                              " AND (p.created_at < ? OR (p.created_at = ? AND p.sid > ?))))")
                page_args += [count, count, created, created, sid]
            else:
                created, sid = decoded_cursor
                page_cond += " AND (p.created_at < ? OR (p.created_at = ? AND p.sid > ?))"
                page_args += [created, created, sid]
        order = ("ORDER BY COALESCE(p.keep_count,1) DESC, p.created_at DESC, p.sid" if by_count
                 else "ORDER BY p.created_at DESC, p.sid")
        with db.conn() as c:
            total = c.execute(f"SELECT COUNT(*) FROM posts p WHERE {cond}", args).fetchone()[0]
            rows = c.execute(
                f"SELECT p.*, COALESCE(g.name, p.list_id) group_name FROM posts p"
                f" LEFT JOIN groups g ON g.list_id = p.list_id"
                f" WHERE {page_cond} {order} LIMIT ? OFFSET ?",
                page_args + [limit + 1, 0 if cursor else offset]).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
        posts = _post_payloads(rows)
        sids = [r["sid"] for r in rows]
        if cmts == "1" and sids:   # 放映字幕等整卡消费场景：一次批量带齐评论流
            comments_by_sid = db.comments_map(sids)
            for p in posts:
                p["comments"] = comments_by_sid.get(p["sid"], [])
        if q.strip() and (q_scope or "all").strip().lower() in ("all", "comment") and sids:   # 搜索态：命中来自评论时，给墙卡预览行指认命中的那条（找得回来看得见为什么）
            like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            qmarks = ",".join("?" * len(sids))
            with db.conn() as c:
                hits = c.execute(
                    f"SELECT sid, body FROM post_comments WHERE sid IN ({qmarks}) "
                    f"AND body LIKE ? ESCAPE '\\' ORDER BY sid, id", [*sids, like]).fetchall()
            first_hit: dict[str, str] = {}
            for r in hits:
                first_hit.setdefault(r["sid"], r["body"])
            for p in posts:
                if p["sid"] in first_hit:
                    p["comment_match"] = first_hit[p["sid"]]
        next_cursor = None
        if has_more and rows:
            tail = ([rows[-1]["keep_count"] or 1, rows[-1]["created_at"], rows[-1]["sid"]] if by_count
                    else [rows[-1]["created_at"], rows[-1]["sid"]])
            next_cursor = base64.urlsafe_b64encode(json.dumps(tail).encode()).decode()
        return {"total": total, "posts": posts, "next_cursor": next_cursor, "has_more": has_more}

    return await asyncio.to_thread(_load_ordered)


@app.get("/api/timeline/outline")
async def timeline_outline(status: str = "all", group: str = "all", author: str = "all",
                           range: str = "all", q: str = "", q_scope: str = "all",
                           has_cmts: str = "all", source: str = "all", tags: str = "all"):
    """编年时间线的按日聚合（密度尺与日卷日头的计数源）。

    筛选口径与 /api/posts 完全一致（同一 post_filter + q 追加），
    日界取 substr(created_at,1,10)：库内 created_at 统一东八区 ISO8601，
    跨来源（微博/X）排序与「今天」筛选都按字符串比较，截前 10 位即当地日。
    days 按 d 降序（最新在前），与前端内容流方向一致。
    """
    def _load():
        cond, args = _post_filter(
            status, group, author, range, has_cmts, source=source, tags=tags, q=q, q_scope=q_scope
        )
        with db.conn() as c:
            days = {r["d"]: {"d": r["d"], "total": r["total"], "kept": r["kept"],
                             "discarded": r["discarded"], "fav": r["fav"]}
                    for r in c.execute(
                        "SELECT substr(p.created_at,1,10) d, COUNT(*) total,"
                        " COALESCE(SUM(p.status='kept'),0) kept,"
                        " COALESCE(SUM(p.status='discarded'),0) discarded,"
                        " COALESCE(SUM(p.favorite),0) fav"
                        f" FROM posts p WHERE {cond} GROUP BY d", args)}
            # 批注数单独一查：写过批注的帖可能已被硬删，但批注豁免清理，正常都在
            for r in c.execute(
                    "SELECT substr(p.created_at,1,10) d, COUNT(*) n FROM post_comments pc"
                    f" JOIN posts p ON p.sid = pc.sid WHERE {cond} GROUP BY d", args):
                days.setdefault(r["d"], {"d": r["d"], "total": 0, "kept": 0,
                                         "discarded": 0, "fav": 0})["cmts"] = r["n"]
        for d in days.values():
            d.setdefault("cmts", 0)
        return {"days": sorted(days.values(), key=lambda x: x["d"], reverse=True)}

    return await asyncio.to_thread(_load)


class StatusBody(BaseModel):
    status: str


@app.post("/api/posts/{sid}/status")
async def mark(sid: str, body: StatusBody):
    if body.status not in ("pending", "kept", "discarded"):
        raise HTTPException(400, "status 只能是 pending/kept/discarded")

    def _apply():
        decision = db.mark_snapshot(sid, body.status)
        if decision is None:
            return None, {}
        if body.status == "kept":
            day = datetime.now(config.NOW_TZ).strftime("%Y-%m-%d")
            with db.conn() as c:
                files = [r["file"] for r in c.execute(
                    "SELECT file FROM pics WHERE sid=? AND file != ''", (sid,))]
            report = storage.export_report(files, day)
            if db.list_comments(sid):   # 先批注后保留：归档日的 评论.txt 同步建账
                storage.rebuild_day_notes(day)
            return decision, report
        return decision, {}

    decision, report = await asyncio.to_thread(_apply)
    if decision is None:
        raise HTTPException(404, "帖子不存在")
    _invalidate_read_caches()
    return {"ok": True, **decision, **report}


class DecisionSnapshot(BaseModel):
    status: str
    kept_at: str | None = None
    discarded_at: str | None = None
    decision_version: int | None = None


class RestoreBody(BaseModel):
    previous: DecisionSnapshot
    expected: DecisionSnapshot


@app.post("/api/posts/{sid}/restore")
async def restore(sid: str, body: RestoreBody):
    if body.previous.status not in ("pending", "kept", "discarded"):
        raise HTTPException(400, "恢复状态无效")
    prev = body.previous.model_dump()
    exp = body.expected.model_dump()

    def _apply():
        if not db.restore_snapshot(sid, prev, exp):
            return None
        return db.get_post(sid)

    post = await asyncio.to_thread(_apply)
    if post is None:
        raise HTTPException(409, "这帖已发生其他修改，请刷新后查看；撤销未覆盖新操作")
    _invalidate_read_caches()
    return {"ok": True, "decision": {k: post[k] for k in
            ("status", "kept_at", "discarded_at", "decision_version")}}


@app.post("/api/media/retry")
async def retry_media():
    async def work(cancelled, progress):
        try:
            return await crawler.retry_missing(manual=True, cancelled=cancelled, progress=progress)
        finally:
            _invalidate_read_caches()
    return JSONResponse({"job": jobs.runner.start("media", work)}, status_code=202)


@app.post("/api/archive/retry")
async def retry_archive():
    async def work(cancelled, progress):
        try:
            return await asyncio.to_thread(storage.retry_archive, cancelled=cancelled, progress=progress)
        finally:
            _invalidate_read_caches()
    return JSONResponse({"job": jobs.runner.start("archive", work)}, status_code=202)


@app.get("/api/jobs/{kind}")
async def maintenance_job(kind: str):
    if kind not in jobs.KINDS:
        raise HTTPException(404, "未知维护任务")
    return {"job": await asyncio.to_thread(jobs.status, kind)}


class FavoriteBody(BaseModel):
    favorite: bool


@app.post("/api/posts/{sid}/favorite")
async def set_favorite(sid: str, body: FavoriteBody):
    """收藏开关：独立标记，不影响 pending/kept/deleted；删帖不清收藏。"""
    def _apply():
        if not db.get_post(sid):
            return False
        db.set_favorite(sid, body.favorite)
        return True

    if not await asyncio.to_thread(_apply):
        raise HTTPException(404)
    _invalidate_read_caches()
    return {"ok": True, "favorite": body.favorite}


class TagsBody(BaseModel):
    add: list[str] = []
    remove: list[str] = []


@app.post("/api/posts/{sid}/tags")
async def set_tags(sid: str, body: TagsBody):
    """标签增删：用户自定义归类维度，与决定状态/收藏正交，不改 decision_version。"""
    def _apply():
        if not db.get_post(sid):
            return None
        return db.set_post_tags(sid, body.add, body.remove)

    try:
        tags = await asyncio.to_thread(_apply)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if tags is None:
        raise HTTPException(404)
    _invalidate_read_caches()
    return {"ok": True, "tags": tags}


@app.get("/api/tags")
async def tags_list():
    """全部标签与帖数（视图面板标签行的 chip 数据源）。"""
    return {"ok": True, "tags": await asyncio.to_thread(db.list_tags)}


@app.get("/api/search/live")
async def search_live(q: str = "", q_scope: str = "all", limit: int = 15):
    """实时搜索：在 SQLite 中快速跨作者、标签、内容与评论多维度检索。"""
    return await asyncio.to_thread(db.search_live, q, q_scope, limit)


# ---------- 评论（单人批注时间线：只增不覆盖、每条带首发时间、无楼层） ----------

class CommentBody(BaseModel):
    body: str


def _clean_comment(body: str) -> str:
    text = (body or "").strip()
    if not text:
        raise HTTPException(400, "评论内容不能为空")
    if len(text) > 500:
        raise HTTPException(400, "评论最长 500 字")
    return text


@app.post("/api/posts/{sid}/comments")
async def create_comment(sid: str, body: CommentBody):
    text = _clean_comment(body.body)

    def _apply():
        if not db.get_post(sid):
            return None
        return db.add_comment(sid, text)

    comment = await asyncio.to_thread(_apply)
    if comment is None:
        raise HTTPException(404, "帖子不存在")
    _invalidate_read_caches()
    await _sync_archive_notes(sid)
    return {"ok": True, "comment": comment}


@app.put("/api/posts/{sid}/comments/{comment_id}")
async def edit_comment(sid: str, comment_id: int, body: CommentBody):
    """改写正文不改动 created_at：时间保持首发时刻，编辑记录只进 updated_at。"""
    comment = await asyncio.to_thread(db.update_comment, sid, comment_id,
                                      _clean_comment(body.body))
    if comment is None:
        raise HTTPException(404, "评论不存在")
    _invalidate_read_caches()
    await _sync_archive_notes(sid)
    return {"ok": True, "comment": comment}


@app.delete("/api/posts/{sid}/comments/{comment_id}")
async def remove_comment(sid: str, comment_id: int):
    if not await asyncio.to_thread(db.delete_comment, sid, comment_id):
        raise HTTPException(404, "评论不存在")
    _invalidate_read_caches()
    await _sync_archive_notes(sid)
    return {"ok": True}


async def _sync_archive_notes(sid: str) -> None:
    """批注增改删后同步归档文件夹的 评论.txt（仅保留帖有归档日）。"""
    def _sync():
        p = db.get_post(sid)
        if p and p["status"] == "kept" and p.get("kept_at"):
            storage.rebuild_day_notes(p["kept_at"][:10])

    await asyncio.to_thread(_sync)


@app.get("/api/comments")
async def list_all_comments(cursor: str = "", limit: int = 100):
    """最近批注流：全库评论按时间倒排（新→旧），带帖作者/缩略图；点行回详情评论区。"""
    if not 1 <= limit <= 200:
        limit = 100
    before = 10 ** 18
    if cursor:
        try:
            before = int(cursor)
        except ValueError:
            raise HTTPException(400, "分页游标无效")

    def _load():
        with db.conn() as c:
            total = c.execute("SELECT COUNT(*) FROM post_comments").fetchone()[0]
            rows = c.execute(
                "SELECT c.id, c.sid, c.body, c.created_at, p.author, p.avatar, p.status, "
                "(SELECT thumb FROM pics WHERE sid = c.sid ORDER BY position, id LIMIT 1) thumb "
                "FROM post_comments c JOIN posts p ON p.sid = c.sid "
                "WHERE c.id < ? ORDER BY c.id DESC LIMIT ?",
                (before, limit + 1)).fetchall()
        has_more = len(rows) > limit
        return total, rows[:limit], has_more

    total, rows, has_more = await asyncio.to_thread(_load)
    items = [dict(r) for r in rows]
    next_cursor = str(items[-1]["id"]) if has_more and items else None
    return {"total": total, "items": items, "next_cursor": next_cursor, "has_more": has_more}


# ---------- 回看 ----------

@app.get("/api/review/next")
async def review_next(request: Request, limit: int = 30):
    """回看选组：实时执行、无定时；选组规则见 db.review_candidates。
    被选中即记一次出场（shown_count+1、刷新 last_shown_at、追加事件）；只读设备不写状态。"""
    limit = max(1, min(limit, 200))
    record_view = request.state.device["role"] != "viewer"

    def _load_review():
        sids = db.review_candidates(limit)
        if not sids:
            return []
        qmarks = ",".join("?" * len(sids))
        with db.conn() as c:
            rows = c.execute(
                f"SELECT p.*, COALESCE(g.name, p.list_id) group_name FROM posts p"
                f" LEFT JOIN groups g ON g.list_id = p.list_id"
                f" WHERE p.sid IN ({qmarks})", sids).fetchall()
        by_order = {sid: i for i, sid in enumerate(sids)}
        rows.sort(key=lambda r: by_order[r["sid"]])
        if record_view:
            db.record_shown(sids)
        posts = _post_payloads(rows)
        comments_by_sid = db.comments_map(sids)
        for p in posts:
            p["comments"] = comments_by_sid.get(p["sid"], [])
        return posts

    posts = await asyncio.to_thread(_load_review)
    return {"ok": True, "posts": posts, "total": len(posts)}


@app.post("/api/posts/{sid}/reaffirm")
async def reaffirm_post(sid: str):
    """回看「点赞」：keep_count+1，三态状态机不动；viewer 在中间件已被拒。"""
    r = await asyncio.to_thread(db.reaffirm, sid)
    if r is None:
        raise HTTPException(404)
    if "error" in r:
        raise HTTPException(409, r["error"])
    _invalidate_read_caches()
    return {"ok": True, **r}


@app.delete("/api/posts/{sid}/reaffirm")
async def undo_reaffirm_post(sid: str):
    """撤销点赞（回看内 6 秒撤销用）：keep_count−1，不低于建账的 1。"""
    r = await asyncio.to_thread(db.undo_reaffirm, sid)
    if r is None:
        raise HTTPException(404)
    if "error" in r:
        raise HTTPException(409, r["error"])
    _invalidate_read_caches()
    return {"ok": True, **r}


class BulkBody(BaseModel):
    sids: list[str]
    status: str


@app.post("/api/posts/bulk-status")
async def bulk_status(body: BulkBody):
    """批量标记（选择模式）：状态单事务批量提交；保留的帖一次性查出图片批量归档。"""
    if body.status not in ("pending", "kept", "discarded"):
        raise HTTPException(400, "status 只能是 pending/kept/discarded")
    sids = list(dict.fromkeys(s for s in body.sids if s))
    if len(sids) > 500:
        raise HTTPException(400, "每次最多操作 500 帖，请分批选择")

    def _apply_bulk():
        updated = exported = 0
        report = {"failed_count": 0, "archive_complete": True}
        to = ""
        if sids:
            qmarks = ",".join("?" * len(sids))
            with db.conn() as c:
                existing = [r[0] for r in c.execute(
                    f"SELECT sid FROM posts WHERE sid IN ({qmarks})", sids).fetchall()]
            if existing:
                db.set_status_bulk(existing, body.status)
                updated = len(existing)
                if body.status == "kept":
                    day = datetime.now(config.NOW_TZ).strftime("%Y-%m-%d")
                    eqmarks = ",".join("?" * len(existing))
                    with db.conn() as c:
                        files = [r["file"] for r in c.execute(
                            f"SELECT DISTINCT file FROM pics WHERE file != '' AND sid IN "
                            f"({eqmarks})", existing).fetchall()]
                    report = storage.export_report(files, day)
                    exported = report["exported"]
                    to = report["to"]
        return updated, exported, to, report

    updated, exported, to, report = await asyncio.to_thread(_apply_bulk)
    if updated:
        _invalidate_read_caches()
    return {"ok": True, "updated": updated, "exported": exported, "to": to,
            "failed_count": report["failed_count"], "archive_complete": report["archive_complete"]}


class BulkTagsBody(BaseModel):
    sids: list[str]
    add: list[str] = []
    remove: list[str] = []


@app.post("/api/posts/bulk-tags")
async def bulk_tags(body: BulkTagsBody):
    """批量打标签（选择模式）：单事务提交；加完会超单帖上限的帖跳过不计入。"""
    sids = list(dict.fromkeys(s for s in body.sids if s))
    if len(sids) > 500:
        raise HTTPException(400, "每次最多操作 500 帖，请分批选择")
    if not body.add and not body.remove:
        raise HTTPException(400, "没有要增删的标签")
    r = await asyncio.to_thread(db.set_post_tags_bulk, sids, body.add, body.remove)
    _invalidate_read_caches()
    return {"ok": True, **r}


def _render_qr_svg(text: str) -> bytes:
    import qrcode.image.svg  # 子模块懒加载，必须显式导入
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


@app.get("/qr.svg")
async def qr_svg(text: str = ""):
    """连接地址 → 二维码（SVG），给「手机扫码连接」用。"""
    text = text.strip()
    if not text.startswith(("http://", "https://")):
        raise HTTPException(400, "地址需以 http(s):// 开头")
    if len(text) > 512:
        raise HTTPException(400, "地址过长")
    svg = await asyncio.to_thread(_render_qr_svg, text)
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


# ---------- 详情 / 作者 / 批量打包 / 清理 ----------
# 注意：字面路由（bulk-status）必须注册在 /api/posts/{sid} 之前，否则被吞。


@app.get("/api/posts/{sid}")
async def post_detail(sid: str):
    p = await asyncio.to_thread(db.get_post, sid)
    if not p:
        raise HTTPException(404)
    p["source_uid"] = p["uid"]
    p["uid"] = db.author_key(p["source"], p["uid"])
    return {"ok": True, "post": p}


@functools.lru_cache(maxsize=4096)
def _name_key(name: str) -> str:
    """博主排序键：中文转拼音后按字母序，英文/数字自然参与。"""
    from pypinyin import lazy_pinyin
    return "".join(lazy_pinyin(name or "")).lower()

@app.get("/api/lan")
async def lan():
    """宿主当前局域网 IP（make 启动时探测注入），给扫码地址预填用。"""
    return {"ok": True, "ip": config.PICWALL_LAN_IP, "port": 8082}


@app.get("/api/download/options")
async def download_options(scope: str = "kept", authors: str = "", dates: str = "",
                           select: str = "", thumb: int = 0):
    return await asyncio.to_thread(downloads.preview, scope, authors, dates, select, bool(thumb))


def _name_initial(name: str) -> str:
    """拼音首字母（A–Z 或 #），供作者索引与报刊亭筛选复用。"""
    k = _name_key(name).strip()
    ch = k[:1].upper() if k else ""
    return ch if "A" <= ch <= "Z" else "#"


@app.get("/api/authors")
async def authors(group: str = "all", status: str = "all", sort: str = "name",
                  source: str = "all"):
    """作者清单：n 跟随当前标签页状态（待处理/保留/已删除各算各的，0 也返回）。

    sort 六键 = 字段_方向：name_asc/name_desc（拼音）、time_desc/time_asc
    （按作者最新一帖时间，新→旧为 desc）、count_desc/count_asc（按当前标签页帖数）；
    旧值 name 仍接受，等价 name_asc。同级再按拼音稳定排。
    聚合口径与列表筛选一致，排序在聚合结果上做。
    """
    if sort not in ("name", "name_asc", "name_desc", "time_desc", "time_asc",
                    "count_desc", "count_asc"):
        sort = "name_asc"

    def _load():
        rows = db.list_authors(group, status, source)
        pinyin = lambda a: (_name_key(a["author"]), a["uid"])  # 稳定次键：同值再按拼音排

        if sort in ("name", "name_asc"):
            rows.sort(key=pinyin)
        elif sort == "name_desc":
            rows.sort(key=pinyin, reverse=True)
        elif sort == "count_desc":
            rows.sort(key=lambda a: (-a["n"], _name_key(a["author"]), a["uid"]))
        elif sort == "count_asc":
            rows.sort(key=lambda a: (a["n"], _name_key(a["author"]), a["uid"]))
        elif sort == "time_desc":
            rows.sort(key=lambda a: (a["last_at"] or "", _name_key(a["author"]),
                                     a["uid"]), reverse=True)
        else:  # time_asc
            rows.sort(key=lambda a: (a["last_at"] or "", _name_key(a["author"]),
                                     a["uid"]))
        return [{**r, "initial": _name_initial(r["author"])} for r in rows]

    return {"ok": True, "authors": await asyncio.to_thread(_load)}


@app.get("/api/authors/kiosk")
async def authors_kiosk(request: Request):
    """报刊亭 / 博主管理大厅：聚合统计、封面内页、缓存签名、拼音索引、分组关系与当前设备权限。

    纯本地查询，签名只读 meta 缓存；网络拉取留给内页打开时的 /bio 端点，互不拖累。
    """
    def _load():
        shelf = db.kiosk_shelf()
        meta = db.kiosk_meta()
        enriched = [{
            **d,
            "pinyin": _name_key(d["author"]),
            "initial": _name_initial(d["author"]),
        } for d in shelf]
        return {"ok": True, "shelf": enriched, "groups": meta["groups"]}

    payload = await asyncio.to_thread(_load)
    payload["role"] = request.state.device["role"]
    return payload


@app.get("/api/stats")
async def stats_data():
    """回看统计报告（/stats 页数据源）：全本地聚合，重查询进线程池不拖事件循环。"""
    return {"ok": True, **await asyncio.to_thread(db.stats_report)}


@app.get("/api/authors/{uid}/bio")
async def author_bio(uid: str, request: Request):
    """作者签名：优先本地缓存；无登录态时降级为空（前端只显示头像+昵称）。

    微博传裸 uid；X 传 x:<rest_id>（rest_id → handle → UserByScreenName）。
    只读设备仅返回缓存，不可触发外部请求或写入 meta。
    """
    if ".." in uid:
        raise HTTPException(400, "作者标识无效")
    if uid.startswith("x:"):
        if not re.fullmatch(r"x:[A-Za-z0-9_.-]{1,64}", uid):
            raise HTTPException(400, "作者标识无效")
        return await _x_author_bio(uid, request.state.device["role"])
    if ":" in uid:
        if not re.fullmatch(r"[a-z0-9_]+:[A-Za-z0-9_.-]{1,64}", uid):
            raise HTTPException(400, "作者标识无效")
        return {"ok": False, "bio": "", "source": uid.split(":", 1)[0], "need_browser": True}
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", uid):
        raise HTTPException(400, "作者标识无效")
    cached = await asyncio.to_thread(db.get_meta, f"author_bio:{uid}")
    if cached:
        return {"ok": True, "bio": cached, "source": "cache"}
    if request.state.device["role"] == "viewer":
        raise HTTPException(403, "只读设备仅可查看已缓存的作者简介")
    if not uid.isdigit():
        return {"ok": False, "bio": "", "need_browser": True}
    cookie = await asyncio.to_thread(config.get_cookie)
    if not cookie:
        return {"ok": False, "bio": "", "need_browser": True}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            bio = await weibo.profile_bio(client, cookie, uid)
    except Exception as e:
        logging.getLogger("picwall").warning("weibo author_bio failed uid=%s err=%s", uid, _redact_err(e))
        return JSONResponse({"ok": False, "bio": "", "msg": "获取作者简介失败，请检查 Cookie 或网络状态"},
                            status_code=502)
    if bio:
        await asyncio.to_thread(db.set_meta, f"author_bio:{uid}", bio)
    return {"ok": True, "bio": bio, "source": "weibo"}


async def _x_author_bio(uid: str, role: str):
    """X 作者简介：缓存 → handle 解析 → UserByScreenName；错误按类降级。"""
    def _lookup():
        cached = db.get_meta(f"author_bio:{uid}")
        if cached:
            return cached, "", "", ""
        token = config.get_x_auth()
        csrf = config.get_x_csrf() if token else ""
        handle = db.x_handle_for_uid(uid.split(":", 1)[1]) if token else ""
        return "", token, csrf, handle

    cached, token, csrf, handle = await asyncio.to_thread(_lookup)
    if cached:
        return {"ok": True, "bio": cached, "source": "cache"}
    if role == "viewer":
        raise HTTPException(403, "只读设备仅可查看已缓存的作者简介")
    if not token:
        return {"ok": False, "bio": "", "source": "x", "need_browser": True}
    if not handle:
        return {"ok": False, "bio": "", "source": "x", "msg": "本地没有该作者的 handle，无法查询简介"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 proxy=config.X_PROXY_URL or None) as client:
        try:
            profile = await xapi.XGraphQL(token, csrf).user_profile(client, handle)
        except xapi.XApiError as e:
            body = {"ok": False, "bio": "", "source": "x", "msg": _redact_err(e)}
            if e.kind == "auth":
                body["need_browser"] = True   # 登录态失效与未配置同路：前端提示重发 Cookie
            return JSONResponse(body, status_code=502)
        except httpx.HTTPError as e:
            logging.getLogger("picwall.x").warning("x author_bio failed uid=%s err=%s", uid, _redact_err(e))
            return JSONResponse({"ok": False, "bio": "", "source": "x",
                                 "msg": "X 简介查询失败，请检查网络或代理设置"}, status_code=502)
    bio = profile.get("description") or ""
    if bio:
        await asyncio.to_thread(db.set_meta, f"author_bio:{uid}", bio)
    return {"ok": True, "bio": bio, "source": "x"}


@app.get("/api/authors/{key}/profile")
async def author_profile(key: str):
    """博主视图头部：某作者跨状态、跨分组的全量聚合（纯本地查询，无网络请求）。

    key = author_key 口径（微博裸 uid、X 为 x:<id>）；签名的网络获取留给
    /api/authors/{uid}/bio，前端分开调，超时互不拖累。
    """
    source, uid = key.split(":", 1) if ":" in key else ("weibo", key)
    if source not in ("x", "weibo") or not uid:
        raise HTTPException(404, "作者不存在")
    row = await asyncio.to_thread(db.author_profile, source, uid)
    if not row:
        raise HTTPException(404, "作者不存在")
    return {"ok": True, **row}


@app.post("/api/purge")
async def run_purge(days: int = 0):
    """手动跑一轮过期清理（定时任务每天 04:10 自动跑同一段逻辑）。"""
    result = await asyncio.to_thread(purge.purge_expired, days if days > 0 else None)
    _invalidate_read_caches()
    return {"ok": True, "result": result}


def _unlink_quiet(path: str) -> None:
    """后台删临时 zip：Windows 上杀软/索引器短暂握句柄时 unlink 会抛，静默留待系统清理。"""
    try:
        os.unlink(path)
    except OSError:
        pass


@app.get("/api/download/zip")
async def download_zip(request: Request, status: str = "pending", group: str = "all", author: str = "all",
                       range: str = "all", select: str = "",
                       authors: str = "", dates: str = "", subfolder: int = 0,
                       thumb: int = 0, scope: str = ""):
    """批量打包：select 给 sid 清单（多选模式），authors/dates 给细分勾选，
    否则按当前筛选；文件按路径全局去重，命名含正文前 30 字。"""
    if request.state.device["role"] != "admin":
        raise HTTPException(403, "只有管理设备可批量打包下载")
    if scope:
        status = scope
    plan = await asyncio.to_thread(downloads.plan, status, group, author, range,
                                   authors, dates, select, bool(thumb), bool(subfolder))
    if plan["posts"] > downloads.MAX_POSTS:
        raise HTTPException(400, "单包最多 500 帖，请按博主或日期分批选择；没有截断下载内容")
    entries = plan["entries"]
    if not entries:
        raise HTTPException(404, "没有可打包的图片")
    fd, tmp = tempfile.mkstemp(prefix="zip-", suffix=".zip", dir=_app_tmp_dir())
    os.close(fd)
    notes = await asyncio.to_thread(downloads.notes_text, plan["rows"])

    def build() -> None:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for name, p in entries.items():
                z.write(p, name)
            if notes:   # 范围内有批注才附带；纯文本、"> " 引用协议与详情一致
                z.writestr("评论.txt", notes)

    try:
        await asyncio.to_thread(build)
    except BaseException:
        _unlink_quiet(tmp)   # 打包失败也别把临时 zip 留在容器里
        raise
    return FileResponse(tmp, media_type="application/zip",
                        headers={"Content-Disposition":
                                 "attachment; filename=picwall-batch.zip"},
                        background=BackgroundTask(_unlink_quiet, tmp))


@app.post("/api/download/zip")
async def download_zip_start(status: str = "pending", group: str = "all", author: str = "all",
                             range: str = "all", select: str = "",
                             authors: str = "", dates: str = "", subfolder: int = 0,
                             thumb: int = 0, scope: str = ""):
    """批量打包改走任务框架：先按 GET 同款语义校验范围，登记 job 后由前端轮询进度。

    打包本身在后台执行，本端点立即返回 job 记录；产物经 /api/download/zip-file 领取。
    """
    if scope:
        status = scope
    plan = await asyncio.to_thread(downloads.plan, status, group, author, range,
                                   authors, dates, select, bool(thumb), bool(subfolder))
    if plan["posts"] > downloads.MAX_POSTS:
        raise HTTPException(400, "单包最多 500 帖，请按博主或日期分批选择；没有截断下载内容")
    if not plan["entries"]:
        raise HTTPException(404, "没有可打包的图片")
    params = {"status": status, "group": group, "author": author, "range": range,
              "select": select, "authors": authors, "dates": dates,
              "subfolder": subfolder, "thumb": thumb, "scope": scope}
    return {"job": jobs.runner.start("download", downloads.build_work(params))}


def _release_download_zip(job_id: str, path: str) -> None:
    """交付完成后清理：注销打包产物登记 + 删临时 zip（容错删除同 _unlink_quiet）。"""
    try:
        downloads.pop_result_file(job_id)
        _unlink_quiet(path)
    finally:
        with _ZIP_DOWNLOAD_LOCK:
            _ZIP_CLAIMED.discard(job_id)


@app.get("/api/download/zip-file")
async def download_zip_file(job: str, request: Request):
    """领取打包产物：任务完成且临时文件仍在才交付；响应发完后后台注销登记并删文件。"""
    if request.state.device["role"] != "admin":
        raise HTTPException(403, "只有管理设备可领取打包文件")
    record = await asyncio.to_thread(jobs.status, "download")
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    if record.get("id") != job:
        raise HTTPException(404, "打包任务不存在或已过期，请重新打包")
    if record["state"] in jobs.ACTIVE:
        raise HTTPException(409, "打包尚未完成，请稍后再取")
    if record["state"] != "complete":
        raise HTTPException(404, "打包没有产出文件，请重新发起打包")
    with _ZIP_DOWNLOAD_LOCK:
        tmp = (downloads.result_file(job) if hasattr(downloads, "result_file")
               else result.get("file"))
        if not tmp:
            raise HTTPException(404, "打包文件已过期或已被领取，请重新打包")
        if job in _ZIP_CLAIMED:
            raise HTTPException(409, "打包文件正在下载中，请勿重复领取")
        if not os.path.isfile(tmp):
            raise HTTPException(404, "打包文件已过期或已被领取，请重新打包")
        _ZIP_CLAIMED.add(job)
    try:
        return FileResponse(tmp, media_type="application/zip",
                            headers={"Content-Disposition":
                                     "attachment; filename=picwall-batch.zip"},
                            background=BackgroundTask(_release_download_zip, job, tmp))
    except BaseException:
        with _ZIP_DOWNLOAD_LOCK:
            _ZIP_CLAIMED.discard(job)
        raise


# ---------- 分组管理 ----------

class GroupBody(BaseModel):
    list_id: str
    name: str = ""


X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


WB_UID_RE = re.compile(r"^\d{6,16}$")
WB_URL_RE = re.compile(r"weibo\.com/(?:u/)?(\d{6,16})")


def _extract_wb_uid(raw: str) -> str | None:
    raw = raw.strip()
    if raw.startswith(("wbu:", "wb:")):
        uid = re.sub(r"^(wbu:|wb:)", "", raw).strip()
        return uid if WB_UID_RE.fullmatch(uid) else None
    m = WB_URL_RE.search(raw)
    if m:
        return m.group(1)
    return None


async def _ensure_wb_user(uid: str, name_hint: str = "") -> tuple[str, str]:
    """校验微博博主存在并获取昵称，返回 (wbu:<uid>, 显示名)；失败给出可操作错误。"""
    cookie = await asyncio.to_thread(config.get_cookie)
    if not cookie:
        raise HTTPException(400, "还没有微博 Cookie：先在右上角「Cookie」保存")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        try:
            info = await weibo.profile_info(client, cookie, uid)
        except weibo.CookieExpired as e:
            raise HTTPException(400, f"微博 Cookie 过期或失效：{e}")
        except weibo.WeiboError as e:
            raise HTTPException(400, f"微博博主 {uid} 校验失败：{e}")
    lid = f"wbu:{uid}"
    name = name_hint.strip() or (f"@{info['screen_name']}" if info.get("screen_name") else f"博主 {uid}")
    return lid, name


async def _ensure_x_user(handle: str) -> str:
    """校验 X 用户存在并返回归一的 xu: 分组 id；失败给出可操作的错误。"""
    token, csrf = await asyncio.to_thread(lambda: (config.get_x_auth(), config.get_x_csrf()))
    if not token:
        raise HTTPException(400, "还没有 X Cookie：先在「Cookie」里保存，"
                                 "或终端 make picwall-safari-cookie 从 Safari 现取")
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 proxy=config.X_PROXY_URL or None) as client:
        try:
            rest_id = await xapi.XGraphQL(token, csrf).user_id(client, handle)
            await asyncio.to_thread(db.set_meta, f"x_user_rest_id:{handle.lower()}", rest_id)
        except xapi.XApiError as e:
            raise HTTPException(400, f"X 用户 @{handle} 校验失败：{e}")
    return f"xu:{handle.lower()}"


@app.post("/api/groups")
async def add_group(body: GroupBody):
    raw = body.list_id.strip()
    if raw == "xg:":   # 新建本地分组：slug 服务端生成（永不与抓取目标撞），组名必填
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "分组名不能为空")
        lid = f"xg:{secrets.token_hex(4)}"
        await asyncio.to_thread(db.add_group, lid, name)
        _invalidate_read_caches()
        return {"ok": True, "msg": f"已建分组「{name}」，展开后把 X 用户加进来", "list_id": lid}
    wb_uid = _extract_wb_uid(raw)
    if wb_uid:
        lid, name = await _ensure_wb_user(wb_uid, body.name)
        await asyncio.to_thread(db.add_group, lid, name)
        _invalidate_read_caches()
        return {"ok": True, "msg": f"已添加 {name}，立即抓取或等下轮定时"}
    if raw.startswith("xu:") or X_HANDLE.fullmatch(raw.lstrip("@")):
        handle = raw.removeprefix("xu:").lstrip("@")
        if not X_HANDLE.fullmatch(handle):
            raise HTTPException(400, "X 用户名只能是字母、数字、下划线（≤15 位）")
        lid = await _ensure_x_user(handle)
        name = body.name.strip() or f"@{handle}"
        await asyncio.to_thread(db.add_group, lid, name)
        _invalidate_read_caches()
        return {"ok": True, "msg": f"已添加 {name}，立即抓取或等下轮定时"}
    is_weibo = raw.isdigit()
    is_x = raw.startswith("x:") and raw[2:].isdigit()
    if not (is_weibo or is_x):
        raise HTTPException(400, "分组 ID 应是纯数字（微博 groupstimeline 里的值）、"
                                 "带 x: 前缀的 X 列表 ID、X 用户名，或微博博主 UID/主页链接")
    await asyncio.to_thread(db.add_group, raw, body.name.strip() or f"分组 {raw}")
    _invalidate_read_caches()
    return {"ok": True}


@app.delete("/api/groups/{list_id}")
async def remove_group(list_id: str):
    await asyncio.to_thread(db.remove_group, list_id)
    _invalidate_read_caches()
    return {"ok": True}


# ---------- X 本地分组成员管理 ----------

class MemberBody(BaseModel):
    member: str


def _require_local_group(list_id: str) -> None:
    if not list_id.startswith("xg:"):
        raise HTTPException(400, "只有系统内分组（xg:）能管理成员")
    if not any(g["list_id"] == list_id for g in db.active_groups()):
        raise HTTPException(404, "分组不存在或已移除")


@app.get("/api/group-members")
async def list_all_members():
    """全部组员关系 + 各本地组成员明细（管理弹窗一次取齐）。"""
    def _load():
        members = db.all_group_members()
        active_names = {g["list_id"]: g["name"] for g in db.active_groups()}
        by_group: dict[str, list] = {}
        for m in members:
            by_group.setdefault(m["list_id"], []).append(
                {**m, "name": active_names.get(m["member_id"], m["member_id"])})
        return {"ok": True, "members": members, "by_group": by_group}

    return await asyncio.to_thread(_load)


@app.post("/api/groups/{list_id}/members")
async def add_member(list_id: str, body: MemberBody):
    member = body.member.strip().lstrip("@").lower()   # @handle / 裸 handle / xu:handle 都收
    if member.startswith("xu:"):
        member = member.removeprefix("xu:")
    if not X_HANDLE.fullmatch(member):
        raise HTTPException(400, "成员应是 X 用户名（字母、数字、下划线，≤15 位）")
    member = "xu:" + member

    def _apply():
        _require_local_group(list_id)
        active_x = {g["list_id"]: g["name"] for g in db.active_groups(source="x")}
        # 必须是已添加的用户目标：本地组只做归组，不代抓新用户
        if member not in active_x:
            raise HTTPException(404, f"{body.member} 还不是已添加的 X 用户，先在上方添加")
        db.add_group_member(list_id, member)
        return active_x[member]

    name = await asyncio.to_thread(_apply)
    _invalidate_read_caches()
    return {"ok": True, "msg": f"已把 {name} 加入分组（历史帖子已归组）"}


@app.delete("/api/groups/{list_id}/members/{member_id}")
async def remove_member(list_id: str, member_id: str):
    def _apply():
        _require_local_group(list_id)
        db.remove_group_member(list_id, member_id)

    await asyncio.to_thread(_apply)
    _invalidate_read_caches()
    return {"ok": True, "msg": "已移出分组（已归组的帖子保留）"}


@app.post("/api/groups/discover")
async def discover():
    try:
        cands = await crawler.discover_groups()
    except Exception as e:
        logging.getLogger("picwall").warning("discover groups failed: %s", _redact_err(e))
        return JSONResponse({"ok": False,
                             "msg": "拉取失败，请检查 Cookie 或网络连通性（可在下方手动添加分组 ID）"},
                            status_code=502)
    added = await asyncio.to_thread(lambda: {g["list_id"] for g in db.active_groups()})
    return {"ok": True,
            "candidates": [c | {"added": c["list_id"] in added} for c in cands]}


@app.post("/api/groups/{list_id}/backfill")
async def backfill_group(list_id: str, days: int = 0):
    """对已配置的分组手动深度回补（days<=0 时用 CRAWL_BACKFILL_DAYS 默认窗口）。"""
    if crawler.state["running"]:
        return JSONResponse({"ok": False, "msg": "已有抓取在进行中"}, status_code=409)
    if x_source.is_x_group(list_id):
        has_x, has_group = await asyncio.to_thread(
            lambda: (bool(config.get_x_auth()),
                     any(g["list_id"] == list_id for g in db.active_groups(source="x")))
        )
        if not has_x:
            raise HTTPException(400, "还没有 X Cookie：先在「Cookie」里保存，或从扩展发送")
        if not has_group:
            raise HTTPException(404, "分组不存在或已移除")
        pages = min(config.CRAWL_BACKFILL_MAX_PAGES, 10)
        _spawn(_crawl_x(only=list_id, max_pages=pages))
        await asyncio.sleep(0)
        return {"ok": True, "msg": "X 回补已开始，可在页面顶部看到进度"}
    has_wb, has_group = await asyncio.to_thread(
        lambda: (bool(config.get_cookie()),
                 any(g["list_id"] == list_id for g in db.active_groups()))
    )
    if not has_wb:
        raise HTTPException(400, "还没有填 Cookie，先点右上角「Cookie」")
    if not has_group:
        raise HTTPException(404, "分组不存在或已移除")
    # days=0 的语义是「用默认窗口」而不是「不回补」，否则按钮静默变成普通抓取
    _spawn(_run_crawl(trigger="backfill", only=list_id,
                      backfill_days=days if days > 0 else config.CRAWL_BACKFILL_DAYS))
    await asyncio.sleep(0)
    return {"ok": True, "msg": "回补已开始，可在页面顶部看到进度"}


# ---------- 浏览器代抓导入 ----------
# 服务端没有 Cookie 时的抓取通道：由已登录微博的浏览器页面（同源 fetch 自动带
# HttpOnly 凭证）翻页拿分组时间线，POST 给本接口；图片由服务端直连 sinaimg 下载
# （无需 Cookie）。CORS 只放行 https://weibo.com 单一来源，其他网页碰不到本服务。

_HARVEST_CORS = {
    "Access-Control-Allow-Origin": "https://weibo.com",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type, authorization",
    # 公网页面 → localhost 属于跨网络空间访问，Chrome PNA 要求预检响应带这个头
    "Access-Control-Allow-Private-Network": "true",
}


@app.options("/api/ingest/harvest")
async def ingest_preflight():
    return Response(status_code=204, headers=_HARVEST_CORS)


@app.post("/api/ingest/harvest")
async def ingest_harvest(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "请求体不是 JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")
    list_id = str(body.get("list_id", "")).strip()
    name = str(body.get("name", "") or "").strip() or f"分组 {list_id}"
    statuses = body.get("statuses", [])
    if not list_id.isdigit() or not isinstance(statuses, list):
        raise HTTPException(400, "list_id 必须是纯数字且 statuses 必须是数组")
    if len(statuses) > 100:
        raise HTTPException(400, "单批最多 100 条，请分批发送；没有截断导入内容")
    status_code, result = await crawler.ingest_harvest(list_id, name, statuses)
    _invalidate_read_caches()
    return JSONResponse(result, status_code=status_code, headers=_HARVEST_CORS)


# ---------- X（Twitter）浏览器代抓导入 ----------
# 与微博同款架构：已登录 x.com 的浏览器页面采集推文（List/时间线），POST 给本接口；
# 图片由服务端直连 pbs.twimg.com 下载（公网可取，无需凭据）。官方 API 免费档读不了
# List、GraphQL queryId 高频漂移，均不作为实现；采集端换路线不影响本接口的输入契约。

_X_CORS = {
    "Access-Control-Allow-Origin": "https://x.com",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type, authorization",
    "Access-Control-Allow-Private-Network": "true",
}


@app.options("/api/ingest/x")
async def ingest_x_preflight():
    return Response(status_code=204, headers=_X_CORS)


class XIngestBody(BaseModel):
    list_id: str
    name: str = ""
    tweets: list[dict] = []


@app.post("/api/ingest/x")
async def ingest_x(body: XIngestBody):
    if len(body.tweets) > 100:
        raise HTTPException(400, "单批最多 100 条，请分批发送；没有截断导入内容")
    if crawler.state["running"]:
        return JSONResponse({"ok": False, "msg": "已有抓取在进行中"},
                            status_code=409, headers=_X_CORS)
    try:
        lid = x_source.group_list_id(body.list_id)   # 校验并加 x: 前缀
    except source_errors.StructureError as e:
        raise HTTPException(400, str(e))
    xlog = logging.getLogger("picwall.x")            # 日志只含来源/阶段/错误类别，无凭据
    xlog.info("x ingest start list=%s received=%d", lid, len(body.tweets))
    crawler.state["running"] = True
    started = time.monotonic()
    try:
        posts = x_source.normalize_tweets(body.tweets)
        new_posts = new_pics = 0
        avatar_cache: dict[str, str] = {}
        def _prepare_post(post: dict) -> tuple[bool, bool]:
            db.observe_post(post, lid)
            existed = db.has_post(post["sid"])
            skip = (existed and not crawler.needs_images(post["sid"])) or db.has_tombstone(post["sid"])
            return skip, existed

        async with _x_http_client() as client:   # 代抓落图与抓取同口径：被墙环境走 X_PROXY_URL
            for p in posts:
                skip, existed = await asyncio.to_thread(_prepare_post, p)
                if skip:
                    continue
                saved = await crawler._store_post(client, p, lid, avatar_cache)
                new_pics += saved
                if saved:   # 一张图都没下成的帖没入库，不计新增（与 _crawl_group 一致）
                    new_posts += int(not existed)
        name = body.name.strip() or f"X 列表 {body.list_id}"

        def _finish():
            db.add_group(lid, name)
            # 只写 X 自己的键：动 last_error/last_crawl 会抹掉微博侧的 Cookie 错误提示
            db.set_meta("x_last_crawl", datetime.now(config.NOW_TZ).isoformat(timespec="seconds"))

        await asyncio.to_thread(_finish)
        _invalidate_read_caches()
        xlog.info("x ingest done list=%s normalized=%d new_posts=%d new_pics=%d cost=%.1fs",
                  lid, len(posts), new_posts, new_pics, time.monotonic() - started)
        return JSONResponse({"ok": True, "new_posts": new_posts, "new_pics": new_pics,
                             "received": len(body.tweets), "normalized": len(posts)},
                            headers=_X_CORS)
    except Exception as e:   # 诊断进状态页与日志，不让 X 导入静默失败
        kind = "network" if isinstance(e, httpx.HTTPError) else type(e).__name__
        await asyncio.to_thread(db.set_meta, "x_last_error", f"ingest-x: {kind}")
        xlog.warning("x ingest failed list=%s stage=ingest kind=%s err=%s", lid, kind, _redact_err(e))
        return JSONResponse({"ok": False, "msg": "X 导入失败，请查看服务端日志与连接状态"},
                            status_code=502, headers=_X_CORS)
    finally:
        crawler.state["running"] = False


@app.exception_handler(HTTPException)
async def _ingest_cors_http(request: Request, exc: HTTPException):
    """ingest 通道的错误响应也带上预检许诺过的 CORS 头，页面 JS 才能看到诊断信息。"""
    resp = JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=exc.headers)
    if request.url.path.startswith("/api/ingest/"):
        resp.headers.update(_X_CORS if request.url.path.endswith("/x") else _HARVEST_CORS)
    return resp


@app.exception_handler(Exception)
async def _ingest_cors_500(request: Request, exc: Exception):
    resp = JSONResponse({"detail": "服务器内部错误"}, status_code=500)
    if request.url.path.startswith("/api/ingest/"):
        resp.headers.update(_X_CORS if request.url.path.endswith("/x") else _HARVEST_CORS)
    return resp


# ---------- Cookie ----------

class CookieBody(BaseModel):
    cookie: str
    verify: bool = True


_connection_locks = {s: asyncio.Lock() for s in ("weibo", "x")}


async def _candidate(source: str, credential: str, verify: bool, ct0: str = "") -> dict:
    result = await connections.probe(source, credential, ct0) if verify else {"state": "saved", "error": ""}
    if result["state"] in ("expired", "error"):
        raise HTTPException(400 if result["state"] == "expired" else 503,
                            result["error"] + "；原有凭据已保留")
    return result


@app.post("/api/settings/cookie")
async def save_cookie(body: CookieBody):
    try:
        c = connections.normalize_weibo(body.cookie)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    async with _connection_locks["weibo"]:
        result = await _candidate("weibo", c, body.verify)

        def _persist():
            config.save_cookie(c)
            connections.record("weibo", c, **result, updated=True)

        await asyncio.to_thread(_persist)
    return {"ok": True, "state": result["state"],
            "msg": "微博连接已验证并保存" if result["state"] == "ok" else "已保存，待验证"}


@app.delete("/api/settings/cookie")
async def drop_cookie():
    async with _connection_locks["weibo"]:
        def _clear():
            config.clear_cookie()
            fallback = config.get_cookie()
            connections.record("weibo", fallback, "saved" if fallback else "unset", updated=True)
            return fallback

        fallback = await asyncio.to_thread(_clear)
    return {"ok": True, "msg": "已清除网页保存的 Cookie，当前使用环境配置，需重新校验" if fallback else "微博连接已清除"}


class XAuthBody(BaseModel):
    auth_token: str = ""
    ct0: str = ""
    verify: bool = True


@app.post("/api/settings/x-auth")
async def save_x_auth(body: XAuthBody):
    """Cookie 助手扩展送来 X 的 auth_token（供服务端定时抓 X 列表）。"""
    try:
        token, ct0 = connections.normalize_x(body.auth_token, body.ct0)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    async with _connection_locks["x"]:
        result = await _candidate("x", token, body.verify, ct0)

        def _persist():
            config.save_x_auth(token, ct0)
            connections.record("x", token, **result, updated=True)

        await asyncio.to_thread(_persist)
    return {"ok": True, "state": result["state"],
            "msg": "X 连接已验证并保存" if result["state"] == "ok" else result["error"] or "X 已保存，待验证"}


@app.delete("/api/settings/x-auth")
async def drop_x_auth():
    async with _connection_locks["x"]:
        def _clear():
            config.X_AUTH_FILE.unlink(missing_ok=True)
            connections.record("x", "", "unset", updated=True)

        await asyncio.to_thread(_clear)
    return {"ok": True, "msg": "X 连接已清除"}


@app.post("/api/settings/{source}/check")
async def check_connection(source: str):
    if source not in _connection_locks:
        raise HTTPException(404, "未知来源")
    async with _connection_locks[source]:
        credential, csrf = await asyncio.to_thread(
            lambda: (connections.current(source), config.get_x_csrf() if source == "x" else "")
        )
        if not credential:
            raise HTTPException(400, "请先保存该平台的 Cookie")
        result = await connections.probe(source, credential, csrf)
        await asyncio.to_thread(lambda: connections.record(source, credential, **result))
    return {"ok": result["state"] == "ok", "state": result["state"],
            "msg": "连接校验通过" if result["state"] == "ok" else result["error"]}


# ---------- 演示数据 ----------

@app.post("/api/demo")
async def demo_seed():
    out = await asyncio.to_thread(demo.seed)
    _invalidate_read_caches()
    return out


@app.delete("/api/demo")
async def demo_clear():
    out = await asyncio.to_thread(demo.clear)
    _invalidate_read_caches()
    return out


# ---------- 图片与打包下载 ----------

# pics/ 下文件名全部内容寻址（sha1 前缀），同一路径永远是同一张图，本可长缓存；
# 但 device_boundary 中间件对 /media/ 统一盖 Cache-Control: no-store——0.5.4 安全
# 冲刺的隐私决定：配对设备的浏览器不长期留存私人媒体。此处不再声明 immutable，
# 免得写一个永远不生效的头。
@app.get("/media/{path:path}")
async def media(path: str, dl: int = 0):
    try:
        p = storage.resolve_pic(path)
    except ValueError:
        raise HTTPException(404)
    if not p.is_file():
        raise HTTPException(404)
    if dl:
        # 内容寻址文件名本身就是 sha1，直接用，省一次整文件读
        return FileResponse(p, filename=f"picwall-{p.stem}{p.suffix}")
    return FileResponse(p)


def _build_zip(day_dir, dst) -> None:
    base = day_dir.resolve()
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_STORED) as z:
        for f in sorted(day_dir.iterdir()):
            if f.is_symlink() or not f.is_file() or not f.resolve().is_relative_to(base):
                continue
            z.write(f, f.name)


@app.get("/api/export/zip")
async def export_zip(date: str, request: Request):
    if request.state.device["role"] != "admin":
        raise HTTPException(403, "只有管理设备可导出归档")
    base = config.EXPORT_DIR.resolve()
    raw_dir = config.EXPORT_DIR / date
    if raw_dir.is_symlink() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(404, "该日期没有保留的图")

    def _prepare():
        day_dir = raw_dir.resolve()
        if not day_dir.is_relative_to(base) or not day_dir.is_dir():
            return None
        fd, tmp = tempfile.mkstemp(prefix="zip-", suffix=".zip", dir=_app_tmp_dir())
        os.close(fd)
        try:
            _build_zip(day_dir, tmp)
        except BaseException:
            _unlink_quiet(tmp)
            raise
        return tmp

    tmp = await asyncio.to_thread(_prepare)
    if tmp is None:
        raise HTTPException(404, "该日期没有保留的图")
    return FileResponse(tmp, media_type="application/zip",
                        headers={"Content-Disposition":
                                 f"attachment; filename=picwall-kept-{date}.zip"},
                        background=BackgroundTask(_unlink_quiet, tmp))
