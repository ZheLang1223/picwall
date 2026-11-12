"""微博网页版 ajax 接口的薄封装与响应解析。

端点形态参考公开逆向资料（jackwener/weibo-cli、cv-cat/WeiboApis）：
  分组时间线  GET https://weibo.com/ajax/feed/groupstimeline?list_id=<id>&page=<n>
  分组列表    GET https://weibo.com/ajax/feed/allGroups
鉴权 = 完整 Cookie（核心是 SUB）+ X-XSRF-TOKEN 请求头（取自同名 Cookie）。
432/跳 passport = Cookie 过期（当年崔庆龙归档已验证 432 是缺 Cookie 而非 TLS 指纹问题）。
"""
import html
import re
from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://weibo.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
_TZ8 = TZ8 = timezone(timedelta(hours=8))


class WeiboError(Exception):
    """接口访问失败（网络/限流/字段变更等）。"""


class CookieExpired(WeiboError):
    """Cookie 缺失或过期，需要重新填。"""


def xsrf_token(cookie: str) -> str:
    m = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", cookie)
    return m.group(1) if m else ""


def headers(cookie: str) -> dict:
    return {
        "User-Agent": UA,
        "Cookie": cookie,
        "Referer": BASE + "/",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "X-XSRF-TOKEN": xsrf_token(cookie),
    }


async def _get_json(client: httpx.AsyncClient, cookie: str, url: str) -> dict:
    r = await client.get(url, headers=headers(cookie), follow_redirects=False)
    loc = r.headers.get("location", "")
    if r.status_code in (301, 302, 401, 432) or "passport" in loc:
        raise CookieExpired(f"登录失效（HTTP {r.status_code}）")
    if r.status_code != 200:
        raise WeiboError(f"HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise WeiboError("响应不是 JSON，平台可能暂时限制访问") from e
    if isinstance(data, dict) and data.get("ok") == 0:
        code = data.get("code")
        if code in (21332, 21331):   # 未登录/登录态失效
            raise CookieExpired(f"ok=0 code={code}")
        raise WeiboError(f"ok=0 code={code} msg={data.get('msg', '')}")
    return data


async def group_timeline(client: httpx.AsyncClient, cookie: str,
                         list_id: str, page: int = 1) -> dict:
    url = f"{BASE}/ajax/feed/groupstimeline?list_id={list_id}&page={page}"
    return await _get_json(client, cookie, url)


async def all_groups(client: httpx.AsyncClient, cookie: str) -> list[dict]:
    """拉取我的分组（仅可抓取的：自建分组 sysgroup=0 与特别关注 sysgroup=1）。

    实测（2026-09-12）：全部关注/最新微博等系统分组（sysgroup=2）调
    groupstimeline 会 400，只有真实分组可用，故在解析层直接过滤。
    """
    data = await _get_json(client, cookie, f"{BASE}/ajax/feed/allGroups")
    if not isinstance(data, dict) or not ("groups" in data or isinstance(data.get("data"), (dict, list))):
        raise WeiboError("微博分组响应结构发生变化，请稍后重试")
    return parse_groups(data)


def parse_groups(data) -> list[dict]:
    """allGroups 响应 → [{list_id, name}]。结构防御式解析。

    实测信封：{"groups": [{"title": "默认分组", "group": [
      {"gid": "…", "title": "科学", "sysgroup": 0, …}, …]}, …]}
    """
    out: dict[str, str] = {}

    def walk(node):
        if isinstance(node, dict):
            lid = node.get("list_id") or node.get("gid")
            name = node.get("group_name") or node.get("name") or node.get("title")
            if lid is not None and name:
                sg = node.get("sysgroup")
                if sg != 2:   # 系统聚合分组不支持 groupstimeline，过滤
                    out[str(lid)] = str(name)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data.get("data", data) if isinstance(data, dict) else data)
    return [{"list_id": k, "name": v} for k, v in out.items()]


async def profile_info(client: httpx.AsyncClient, cookie: str, uid: str) -> dict:
    """作者基础信息（uid/screen_name/description/avatar）。校验用户存在。"""
    data = await _get_json(client, cookie, f"{BASE}/ajax/profile/info?uid={uid}")
    user = ((data.get("data") or {}).get("user") or {}) if isinstance(data, dict) else {}
    if not user or not user.get("id"):
        raise WeiboError(f"未找到微博博主 {uid}")
    return {
        "uid": str(user["id"]),
        "screen_name": str(user.get("screen_name") or ""),
        "description": str(user.get("description") or "").strip(),
        "avatar": _https(user.get("avatar_hd") or user.get("profile_image_url") or ""),
    }


async def profile_bio(client: httpx.AsyncClient, cookie: str, uid: str) -> str:
    """作者简介（签名）。拿不到就抛错，调用方优雅降级。"""
    try:
        info = await profile_info(client, cookie, uid)
        return info.get("description", "")
    except WeiboError:
        return ""


async def user_timeline(client: httpx.AsyncClient, cookie: str,
                         uid: str, page: int = 1) -> dict:
    """拉取单博主主页时间线（全部微博）。"""
    url = f"{BASE}/ajax/statuses/mymblog?uid={uid}&page={page}&feature=0"
    return await _get_json(client, cookie, url)


def extract_statuses(data) -> list:
    """groupstimeline 或 mymblog 响应 → 条目列表。

    实测信封：
      groupstimeline: {"ok":1, "statuses":[…], "total_number":296, …}
      mymblog:        {"ok":1, "data":{"list":[…], "total":…}}
    statuses 为空列表时 or 链会落到 data，对调用方等价。
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        st = data.get("statuses") or data.get("data")
        if isinstance(st, dict):
            st = st.get("list") or st.get("statuses") or []
        return st if isinstance(st, list) else []
    return []


# ---------- 解析 ----------

def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " ", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def parse_time(s: str) -> str:
    """微博时间串 → ISO8601（+08:00）。兼容网页版标准格式与客户端相对时间。"""
    now = datetime.now(_TZ8)
    s = (s or "").strip()
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")\
            .astimezone(_TZ8).isoformat(timespec="seconds")
    except ValueError:
        pass
    m = re.fullmatch(r"(\d+)\s*秒前", s)
    if m:
        return (now - timedelta(seconds=int(m.group(1)))).isoformat(timespec="seconds")
    m = re.fullmatch(r"(\d+)\s*分钟前", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).isoformat(timespec="seconds")
    m = re.fullmatch(r"(\d+)\s*小时前", s)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).isoformat(timespec="seconds")
    if s == "刚刚":
        return now.isoformat(timespec="seconds")
    try:
        m = re.fullmatch(r"今天\s*(\d{1,2}):(\d{2})", s)
        if m:
            return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                               second=0, microsecond=0).isoformat(timespec="seconds")
        m = re.fullmatch(r"昨天\s*(\d{1,2}):(\d{2})", s)
        if m:
            d = now - timedelta(days=1)
            return d.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                             second=0, microsecond=0).isoformat(timespec="seconds")
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", s)
        if m:
            d = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                            hour=12, minute=0, second=0, microsecond=0)
            if d > now:   # 「12-15」在 1 月初看到 = 去年的 12-15，别解析成未来
                d = d.replace(year=d.year - 1)
            return d.isoformat(timespec="seconds")
    except ValueError:
        pass   # 相对时间非法（02-30/13-05/25:99）：按抓取时刻兜底，别炸整轮抓取
    return now.isoformat(timespec="seconds")


def _https(url: str) -> str:
    if not url:
        return ""
    return "https:" + url if url.startswith("//") else url


def _to_large(url: str) -> str:
    """任何缩放档 → /large/ 原图档。orj960/sti960 是 feed 常用缩放档
    （漏改写会把 960px 小图当原图入库，曾致全库清晰度受损）；GIF 的
    sti960 → large 即完整动图。"""
    return re.sub(r"/(thumbnail|bmiddle|square|thumb\d+|mw690|orj\d+|sti\d+)/",
                  "/large/", url)


def _norm_pic(pid: str, full: str, mid: str) -> dict:
    full = _to_large(_https(full))
    mid = _https(mid) or full   # mid 保持中档：large 拉不到时的回退位
    return {"pid": pid, "full": full, "mid": mid}


def extract_pics(mblog: dict) -> list[dict]:
    """优先 pic_infos（全量图），退化用 pics 列表；统一到 large 原图 + mw2000 中图。

    原图档优先级 largest > large > mw2000 > bw2000（开源工具共识，largest
    即微博保留的最高质量版本）。
    """
    out, seen = [], set()

    def add(p):
        if p["full"] and p["pid"] not in seen:
            seen.add(p["pid"])
            out.append(p)

    infos = mblog.get("pic_infos")
    if isinstance(infos, dict):
        for pid, info in infos.items():
            if not isinstance(info, dict):
                continue
            full = ((info.get("largest") or {}).get("url")
                    or (info.get("large") or {}).get("url")
                    or (info.get("mw2000") or {}).get("url")
                    or (info.get("bw2000") or {}).get("url"))
            mid = ((info.get("mw2000") or {}).get("url")
                   or (info.get("mw690") or {}).get("url") or full)
            add(_norm_pic(str(pid), full, mid))
    if not out:
        for p in mblog.get("pics") or []:
            if not isinstance(p, dict):
                continue
            full = ((p.get("largest") or {}).get("url")
                    or (p.get("large") or {}).get("url")
                    or p.get("url") or "")
            add(_norm_pic(str(p.get("pid") or full), full, p.get("url") or full))
    return out


def parse_mblog(item: dict) -> dict | None:
    """一条时间线 item → 统一结构；无图（纯文字/视频）返回 None 跳过。

    转发帖取被转原博的图与作者，via 记转发者；weibo_url/mblogid 都用原博，
    跳转后能看到图所在的原微博（转发链也在该页内）。
    """
    if not isinstance(item, dict):
        return None
    m = item.get("mblog") or item
    if not isinstance(m, dict):
        return None
    rt = m.get("retweeted_status")
    src = rt if isinstance(rt, dict) and rt else m
    pics = extract_pics(src)
    if not pics:
        return None
    user = src.get("user") or {}
    uid = str(user.get("id") or "")
    sid = src.get("mblogid") or m.get("mblogid")
    if not sid or not uid:
        return None
    text = strip_html(src.get("text") or "")
    if rt:
        reason = strip_html(m.get("text") or "")
        via = ((m.get("user") or {}).get("screen_name") or "").strip()
        text = f"{text}（@{via} 转发：{reason}）" if reason else text
    return {
        "sid": str(sid),
        "source": "weibo",
        "text_complete": not bool(src.get("isLongText") or src.get("truncated")),
        "uid": uid,
        "author": (user.get("screen_name") or "微博用户").strip(),
        "avatar": _https(user.get("avatar_hd") or user.get("profile_image_url") or ""),
        "text": text,
        "is_long": 1 if src.get("isLongText") else 0,
        "created_at": parse_time(src.get("created_at") or m.get("created_at") or ""),
        "fetched_at": datetime.now(_TZ8).isoformat(timespec="seconds"),
        "weibo_url": f"{BASE}/{uid}/{sid}",
        "via": ((m.get("user") or {}).get("screen_name") or "") if rt else "",
        "pics": pics,
    }
