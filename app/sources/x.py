"""X（Twitter）来源适配：采集端送来的推文条目 → 统一 post dict。

采集通道 = 浏览器代抓（与微博同款架构）：用户在已登录 x.com 的浏览器页面里
由采集脚本整理出下面的输入结构，POST 给服务端 /api/ingest/x；图片由服务端
直连 pbs.twimg.com 下载（公网可取，无需登录凭据）。

官方 API 路线被付费墙挡住（免费档无 Lists/时间线读取权限），服务端直连
GraphQL 又依赖高频漂移的 queryId —— 两者都不作为本轮实现；本模块是
「可独立替换的数据获取实现」的服务端半边，采集端换路线不影响这里。

输入条目契约（采集端负责产出）：
  id            推文 id（纯数字字符串，必填）
  url           原帖链接（可空，缺省用 handle+id 拼）
  author        {uid: handle或id, name: 显示名, avatar: 头像URL可空}
  text          纯文本（采集端剥好 HTML）
  created_at    ISO8601 或空（空则入库时刻）
  via           转推者昵称可空（本条是被转推的原帖时）
  images        pbs.twimg.com/media/... URL 列表（与 videos 至少一项非空，否则跳过）
  videos        可选：[{poster, url, variants:[{url,bitrate}], aspect_ratio:[w,h],
                  duration_millis}]——X 的 mp4 视频链路（poster 与变体 URL 都要过
                  media_fetch 白名单：poster 在 pbs.twimg.com、mp4 在 video.twimg.com）
"""
import re
from datetime import datetime, timedelta, timezone

from ..media_fetch import permitted
from .base import StructureError, validate_post

SOURCE_ID = "x"
SID_PREFIX = "x:"
_GROUP_PREFIX = "x:"          # groups.list_id 里 X 列表的前缀（防与微博纯数字撞）
_USER_PREFIX = "xu:"          # X 用户主页目标（xu:<handle 小写>）
_LOCAL_GROUP_PREFIX = "xg:"   # 系统内自定义组（成员映射在 group_members，自身不是抓取目标）
_TZ8 = timezone(timedelta(hours=8))


def is_x_group(list_id: str) -> bool:
    return str(list_id).startswith((_GROUP_PREFIX, _USER_PREFIX, _LOCAL_GROUP_PREFIX))


def group_list_id(x_list_id: str) -> str:
    """X 列表 id → groups.list_id（带前缀入库，防跨来源撞）；已带前缀的原样校验通过。"""
    lid = str(x_list_id)
    raw = lid[2:] if lid.startswith(_GROUP_PREFIX) else lid
    if not raw.isdigit():
        raise StructureError(f"X 列表 id 应为纯数字，得到 {lid!r}")
    return _GROUP_PREFIX + raw


def group_user_id(handle: str) -> str:
    """X 用户名 → groups.list_id（xu: 小写归一，防大小写重复建组）。"""
    h = str(handle).lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", h):
        raise StructureError(f"X 用户名只能是字母、数字、下划线（≤15 位），得到 {handle!r}")
    return _USER_PREFIX + h.lower()


def _https(url: str) -> str:
    if not isinstance(url, str) or not url:
        return ""
    return "https:" + url if url.startswith("//") else url


def _orig(url: str) -> str:
    """pbs 媒体 URL → 原图画质：剥掉老式 `:large` 后缀与 `name=` 参数，统一 name=orig。"""
    u = _https(url)
    u = re.sub(r":\w+$", "", u)
    u = re.sub(r"[?&]name=\w+", "", u)
    sep = "&" if "?" in u else "?"
    return u + f"{sep}name=orig"


def normalize_created_at(s: str) -> str:
    """归一到东八区 ISO8601（库内跨来源排序/今日筛选按字符串比较，时区必须一致）。"""
    if s:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))\
                .astimezone(_TZ8).isoformat(timespec="seconds")
        except ValueError:
            pass
    return datetime.now(_TZ8).isoformat(timespec="seconds")


def _best_video_url(v: dict) -> str:
    """视频条目 → 最高码率 mp4 变体 URL：优先采集端预选的 url，否则从 variants 挑。"""
    u = _https(str(v.get("url") or ""))
    if u and permitted(u):
        return u
    variants = [x for x in (v.get("variants") or []) if isinstance(x, dict)]
    allowed = [(_https(str(x.get("url") or "")), x.get("bitrate") or 0) for x in variants]
    allowed = [(u, b) for u, b in allowed if u and permitted(u)]
    return max(allowed, key=lambda t: t[1])[0] if allowed else ""


def _video_pic_items(t: dict) -> list[dict]:
    """推文条目的 videos → pics item（kind='video'）；任一环不合白名单跳过该视频。"""
    out = []
    for v in t.get("videos") or []:
        if not isinstance(v, dict):
            continue
        full = _best_video_url(v)
        poster = _https(str(v.get("poster") or ""))
        if not full or not poster or not permitted(poster):
            continue
        ar = v.get("aspect_ratio") or []
        try:
            aw = max(1, int(ar[0])) if len(ar) >= 2 else 1
            ah = max(1, int(ar[1])) if len(ar) >= 2 else 1
        except (TypeError, ValueError):
            aw = ah = 1
        try:
            dur = max(0, int(v.get("duration_millis") or 0))
        except (TypeError, ValueError):
            dur = 0
        out.append({"pid": full, "full": full, "mid": full, "kind": "video",
                    "poster": poster, "aspect_w": aw, "aspect_h": ah, "dur_ms": dur})
    return out


def normalize_tweet(t: dict, fetched_at: str | None = None) -> dict | None:
    """一条推文条目 → 统一 post dict；无图无视频/结构不完整返回 None（调用方跳过）。"""
    if not isinstance(t, dict):
        return None
    tid = str(t.get("id") or "").strip()
    if not tid or not tid.isdigit():
        return None
    images = [_https(u) for u in (t.get("images") or []) if _https(u)]
    if any(not permitted(u) for u in images):
        return None
    video_pics = _video_pic_items(t)
    if not images and not video_pics:
        return None
    author = t.get("author") if isinstance(t.get("author"), dict) else {}
    uid = str(author.get("id") or author.get("uid") or "").strip()
    handle = str(author.get("handle") or author.get("uid") or "").strip()
    name = str(author.get("name") or "").strip()
    if not uid or not name:
        return None
    pics = [{"pid": u, "full": _orig(u), "mid": u} for u in images] + video_pics
    url = _https(t.get("url") or "")
    if not url.lower().startswith(("http://", "https://")):
        # 采集端来的链接只认 http(s)：伪协议（javascript: 等）回落到站内规范地址
        url = f"https://x.com/{handle}/status/{tid}"
    p = {
        "sid": SID_PREFIX + tid,
        "source": SOURCE_ID,
        "uid": uid,
        "handle": handle,
        "text_complete": not bool(t.get("truncated")),
        "author": name,
        "avatar": _https(author.get("avatar") or ""),
        "text": str(t.get("text") or "").strip(),
        "is_long": 0,
        "created_at": normalize_created_at(str(t.get("created_at") or "")),
        "fetched_at": fetched_at or datetime.now(_TZ8).isoformat(timespec="seconds"),
        "weibo_url": url,          # 历史列名，语义 = originalUrl
        "via": str(t.get("via") or ""),
        "pics": pics,
    }
    validate_post(p)
    return p


def normalize_tweets(items, fetched_at: str | None = None) -> list[dict]:
    out = []
    for t in items or []:
        try:
            p = normalize_tweet(t, fetched_at)
        except StructureError:
            continue
        if p:
            out.append(p)
    return out
