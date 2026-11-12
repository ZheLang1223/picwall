"""路径与运行配置：全部可用环境变量覆盖，本地开发设 PICWALL_DATA_DIR 即可。

数据目录默认值按运行环境决定：
- 容器内（存在 /.dockerenv）→ /app/data（docker-compose 卷挂载点，历史默认不变）
- 裸进程/桌面 → 平台标准用户数据目录（Windows %LOCALAPPDATA%\\picwall、
  macOS ~/Library/Application Support/picwall、Linux XDG_DATA_HOME/picwall），
  避免从不同盘符/目录启动时数据位置漂移。
"""
import json
import os
import sys
from datetime import timedelta, timezone
from pathlib import Path

NOW_TZ = timezone(timedelta(hours=8))   # 业务日界固定东八区，与微博时间戳同口径，不依赖容器 TZ


def _env_int(name: str, default: int) -> int:
    """compose 的 `${VAR:-}` 空默认会把变量注入成空串：空/非法值一律回落默认。"""
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _default_data_dir() -> Path:
    if os.path.exists("/.dockerenv"):
        return Path("/app/data")
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / "picwall"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "picwall"
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "picwall"


DATA_DIR = Path(os.environ.get("PICWALL_DATA_DIR") or _default_data_dir())
PICS_DIR = DATA_DIR / "pics"        # 内容寻址原图 + 缩略图 + 头像
EXPORT_DIR = DATA_DIR / "export"    # 「保留」的图按保留日归档，就是留给设备同步的文件夹
DB_PATH = DATA_DIR / "picwall.db"
COOKIE_FILE = DATA_DIR / "cookie.txt"

WB_COOKIE_ENV = os.environ.get("WB_COOKIE", "").strip()
CRAWL_TIME = os.environ.get("CRAWL_TIME", "09:30")          # HH:MM，东八区
CRAWL_MAX_PAGES = _env_int("CRAWL_MAX_PAGES", 2)
CRAWL_BACKFILL_DAYS = _env_int("CRAWL_BACKFILL_DAYS", 30)
CRAWL_BACKFILL_MAX_PAGES = _env_int("CRAWL_BACKFILL_MAX_PAGES", 100)
PURGE_DAYS = _env_int("PURGE_DAYS", 3)                      # 「已删除」多少天后硬删
# 备份 blob 库去向：local = 本地 backups/blobs/（默认，历史行为）；quark = 夸克网盘异地。
# 只有宿主侧 make backup / tools 走云；容器内服务从不执行备份，无需 node/CLI。
BLOB_STORE = os.environ.get("PICWALL_BLOB_STORE", "local").strip().lower()
# 夸克 CLI（quarkclouddrive skill）入口与目标目录；目录 FID 留空则按名称幂等创建
QUARK_CLI = Path(os.environ.get("PICWALL_QUARK_CLI", "")
                 or "~/.zcode/skills/quarkclouddrive/scripts/quark-drive.cjs").expanduser()
QUARK_BACKUP_DIR = os.environ.get("PICWALL_QUARK_DIR", "Somedia备份")
QUARK_DIR_FID = os.environ.get("PICWALL_QUARK_DIR_FID", "").strip()
QUARK_UPLOAD_BATCH = _env_int("PICWALL_QUARK_BATCH", 100)
# 宿主当前局域网 IP：由 make picwall / make start 启动时探测注入（ipconfig getifaddr），
# 供「手机扫码连接」生成当前有效的二维码地址；容器内探测不到宿主 IP
PICWALL_LAN_IP = os.environ.get("PICWALL_LAN_IP", "").strip()
ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"} | {
    h.strip().lower() for h in os.environ.get("PICWALL_ALLOWED_HOSTS", "").split(",") if h.strip()
}
if PICWALL_LAN_IP:
    ALLOWED_HOSTS.add(PICWALL_LAN_IP.lower())
# 固定设备配对码（.env 的 PWALL_PAIR_CODE）：长期有效、可重复使用，免去每次临时签发；
# 归一化到与 exchange 输入同一形态（去横线大写），比对走哈希。角色非法时回退分拣——
# 环境变量由操作者控制，宁可落到 editor 也不静默禁用配对。
PAIR_CODE = os.environ.get("PWALL_PAIR_CODE", "").strip().upper().replace("-", "")
PAIR_ROLE = os.environ.get("PWALL_PAIR_ROLE", "editor").strip().lower() or "editor"
if PAIR_ROLE not in ("admin", "editor", "viewer", "collector"):
    PAIR_ROLE = "editor"
# 固定配对码强度提醒：低于 12 位（32 字母表）在配对限流上限（约 1.4 万次/天）内
# 可被穷举。只告警不禁用——码由操作者设定，宁可提醒也不静默改变配对行为。
PAIR_CODE_MIN_LEN = 12
PAIR_CODE_WEAK = 0 < len(PAIR_CODE) < PAIR_CODE_MIN_LEN
# X（Twitter）服务端抓取：auth_token 由 Cookie 助手扩展送来存盘；间隔小时数，0=关闭
X_AUTH_FILE = DATA_DIR / "x_auth.txt"
X_CRAWL_INTERVAL_HOURS = _env_int("X_CRAWL_INTERVAL_HOURS", 2)
X_CRAWL_MAX_PAGES = _env_int("X_CRAWL_MAX_PAGES", 1)
# X 抓取专用代理（x.com 与 pbs.twimg.com 在容器内被墙时配
# http://host.docker.internal:<port>）；空 = 直连。只影响 X 链路，微博照旧直连。
X_PROXY_URL = os.environ.get("X_PROXY_URL", "").strip()
MAX_IMAGE_BYTES = _env_int("PICWALL_MAX_IMAGE_BYTES", 64 * 1024 * 1024)
# 视频体积上限：X 的 mp4 变体常大于图片（短视频几 MB ~ 数十 MB），与图片预算分开设
MAX_VIDEO_BYTES = _env_int("PICWALL_MAX_VIDEO_BYTES", 512 * 1024 * 1024)
MAX_IMAGE_PIXELS = _env_int("PICWALL_MAX_IMAGE_PIXELS", 100000000)


def get_x_auth() -> str:
    """X 的 auth_token；不存在返回空串。"""
    try:
        raw = X_AUTH_FILE.read_text().strip()
        return str(json.loads(raw).get("auth_token", "")) if raw.startswith("{") else raw
    except (OSError, ValueError, AttributeError):
        return ""


def get_x_csrf() -> str:
    try:
        return str(json.loads(X_AUTH_FILE.read_text()).get("ct0", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def _save_secret(path: Path, value: str) -> None:
    """先在同目录写入仅用户可读的临时文件，再原子替换，读者永远拿到完整凭据。"""
    import tempfile
    ensure_dirs()
    fd, name = tempfile.mkstemp(prefix=".credential-", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def save_x_auth(token: str, ct0: str = "") -> None:
    _save_secret(X_AUTH_FILE, json.dumps({"auth_token": token, "ct0": ct0}))


def ensure_dirs() -> None:
    for p in (DATA_DIR, PICS_DIR, EXPORT_DIR):
        p.mkdir(parents=True, exist_ok=True)


def get_cookie() -> str:
    """网页「Cookie」按钮保存的 cookie.txt 优先于环境变量。"""
    try:
        s = COOKIE_FILE.read_text(encoding="utf-8").strip()
        if s:
            return s
    except OSError:
        pass
    return WB_COOKIE_ENV


def cookie_source() -> str:
    try:
        if COOKIE_FILE.read_text(encoding="utf-8").strip():
            return "web"
    except OSError:
        pass
    return "env" if WB_COOKIE_ENV else "none"


def save_cookie(cookie: str) -> None:
    _save_secret(COOKIE_FILE, cookie.strip())


def clear_cookie() -> None:
    COOKIE_FILE.unlink(missing_ok=True)


def crawl_hm() -> tuple[int, int]:
    h, m = CRAWL_TIME.split(":")
    return int(h), int(m)
