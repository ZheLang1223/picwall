"""X（Twitter）服务端抓取：GraphQL 客户端，把 List 时间线转成标准推文条目。

请求构造与解析逻辑移植自开源项目 gallery-dl（GPL-2.0）的 twitter 提取器；
X 改版导致 403/空结果时，对照上游更新 ENDPOINTS/FEATURES 常量即可。

Cookie 来源：Cookie 助手浏览器扩展读取 auth_token 后 POST 给
/api/settings/x-auth，服务端存 data/x_auth.txt（含 httpOnly，比 F12 手动复制完整）。
输出契约：与 sources/x.py 的 normalize_tweet 输入一致
（id/url/author{text,name,avatar}/text/created_at/images/via）。
"""
import asyncio
import html
import json
import re
import secrets
from datetime import datetime

import httpx

from .. import db
from . import x as x_source

ROOT = "https://x.com"
# 公开 web client 的 Bearer（与网页版相同，非私密凭据）
BEARER = ("Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu"
          "4FA33AGWWjCpTnA")

# queryId 硬编码表：X 改版导致 403 时，对照上游 gallery-dl 更新这几行
ENDPOINTS = {
    "list_latest": "/graphql/06JtmwM8k_1cthpFZITVVA/ListLatestTweetsTimeline",
    "user_by": "/graphql/ck5KkZ8t5cOmoLssopN99Q/UserByScreenName",
    "user_tweets": "/graphql/E8Wq-_jFSaU7hxVcuOPR9g/UserTweets",
}

# GraphQL 必需的功能开关串（gallery-dl features_pagination，原样携带）
FEATURES = {
    "rweb_video_screen_enabled": False,
    "payments_enabled": False,
    "rweb_xchat_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
    # UserByScreenName 需要的两项（gallery-dl 同款）
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
}

_X_DATE = "%a %b %d %H:%M:%S %z %Y"


def _scrub_error(err: BaseException | str) -> str:
    text = str(err)
    text = re.sub(r"(https?|socks5h?)://[^:/@\s]+:[^@\s]+@", r"\1://***:***@", text, flags=re.IGNORECASE)
    return re.sub(r"(https?|socks5h?)://[^/@\s]+@", r"\1://***@", text, flags=re.IGNORECASE)


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


class XApiError(Exception):
    """X 接口错误。kind: auth=登录态失效 / ratelimit=限流 / structure=接口变更 / network=网络"""

    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


class XGraphQL:
    """用 auth_token 调 X 的 GraphQL 接口（gallery-dl 同款请求头组合）。"""

    def __init__(self, auth_token: str, ct0: str = ""):
        self.auth_token = auth_token.strip()
        self.csrf = ct0 or secrets.token_hex(16)
        self.headers = {
            "Accept": "*/*",
            "Referer": ROOT + "/",
            "content-type": "application/json",
            "authorization": BEARER,
            # X 的 GraphQL 对非浏览器 UA 直接回空 404（2026-10 实测），必须带浏览器 UA
            "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            "x-twitter-auth-type": "OAuth2Session",
            "x-csrf-token": self.csrf,
            "x-twitter-active-user": "yes",
            "cookie": f"auth_token={self.auth_token}; ct0={self.csrf}",
        }

    async def list_latest(self, client: httpx.AsyncClient, list_id: str,
                          max_pages: int = 1):
        """List 最新时间线，逐页 yield 标准推文契约条目；最多翻 max_pages 页。"""
        endpoint = ENDPOINTS["list_latest"]
        variables = {"listId": str(list_id), "count": 50}
        cursor = None
        for _ in range(max(1, max_pages)):
            params = {"variables": _json(variables), "features": _json(FEATURES)}
            if cursor:
                params["variables"] = _json({**variables, "cursor": cursor})
            data = await self._call(client, endpoint, params)
            tweets, cursor = _walk_tweets(data)
            for t in tweets:
                inner, via = _inner_tweet(t)
                item = _tweet_to_item(inner, via=via)
                if item:
                    yield item
            if not cursor:
                break

    async def user_profile(self, client: httpx.AsyncClient, handle: str) -> dict:
        """用户名 → 档案（rest_id/昵称/签名）。签名里的 t.co 短链展开为原始链接。"""
        params = {"variables": _json({"screen_name": handle, "withGrokTranslatedBio": False}),
                  "features": _json(FEATURES),
                  "fieldToggles": _json({"withAuxiliaryUserLabels": True})}
        data = await self._call(client, ENDPOINTS["user_by"], params)
        result = _as_dict(_as_dict(_as_dict(data.get("data")).get("user")).get("result"))
        if not result:
            raise XApiError("structure", f"X 上找不到 @{handle}，请核对用户名")
        core = _as_dict(result.get("core"))
        legacy = _as_dict(result.get("legacy"))
        bio = html.unescape(legacy.get("description") or "")
        desc_urls = _as_dict(_as_dict(legacy.get("entities")).get("description")).get("urls") or []
        for u in desc_urls:
            if not isinstance(u, dict):
                continue
            short, full = u.get("url") or "", u.get("expanded_url") or ""
            if short and full:
                bio = bio.replace(short, full)
        return {"rest_id": str(result.get("rest_id") or legacy.get("id_str") or ""),
                "handle": core.get("screen_name") or legacy.get("screen_name") or handle,
                "name": core.get("name") or legacy.get("name") or "",
                "description": bio.strip()}

    async def user_id(self, client: httpx.AsyncClient, handle: str) -> str:
        """用户名 → rest_id（X 内部数字 id，UserTweets 的入参）。"""
        rest_id = (await self.user_profile(client, handle))["rest_id"]
        if not rest_id:
            raise XApiError("structure", "X 返回里没有该用户的信息，请稍后重试")
        return rest_id

    async def user_latest(self, client: httpx.AsyncClient, user_id: str,
                          max_pages: int = 1):
        """用户主页时间线，逐页 yield 标准推文契约条目（转发解包后 via=转推者）。"""
        variables = {"userId": str(user_id), "count": 50,
                     "includePromotedContent": False,
                     "withQuickPromoteEligibilityTweetFields": False,
                     "withVoice": True}
        cursor = None
        for _ in range(max(1, max_pages)):
            params = {"variables": _json({**variables, **({"cursor": cursor} if cursor else {})}),
                      "features": _json(FEATURES),
                      "fieldToggles": _json({"withArticlePlainText": False})}
            data = await self._call(client, ENDPOINTS["user_tweets"], params)
            tweets, cursor = _walk_tweets(data, root="user")
            for t in tweets:
                inner, via = _inner_tweet(t)
                item = _tweet_to_item(inner, via=via)
                if item:
                    yield item
            if not cursor:
                break

    async def _call(self, client: httpx.AsyncClient, endpoint: str, params: dict) -> dict:
        r = await client.get(ROOT + "/i/api" + endpoint, params=params,
                             headers=self.headers, follow_redirects=False)
        ct0 = r.cookies.get("ct0")
        if ct0:   # 随响应自更新 csrf（gallery-dl 同款自愈）
            self.csrf = ct0
            self.headers["x-csrf-token"] = ct0
            self.headers["cookie"] = f"auth_token={self.auth_token}; ct0={self.csrf}"
        if r.is_redirect:
            loc = (r.headers.get("location") or "").lower()
            raise XApiError("auth" if "login" in loc or "flow" in loc else "network",
                            "X 接口返回意外跳转，请稍后重试")
        if r.status_code == 401:
            raise XApiError("auth", "X 登录态失效，请重新发送 Cookie")
        if r.status_code == 404:
            raise XApiError("structure", "X 接口返回 404：目标可能不存在或已删除，"
                                         "请到「管理分组」核对后再试")
        if r.status_code == 429:
            reset_ts = r.headers.get("x-rate-limit-reset")
            msg = "X 接口限流，请稍后再试"
            if reset_ts and reset_ts.isdigit():
                try:
                    reset_dt = datetime.fromtimestamp(int(reset_ts), tz=datetime.now().astimezone().tzinfo)
                    msg += f"（预计 {reset_dt.strftime('%H:%M')} 恢复）"
                except Exception:
                    pass
            raise XApiError("ratelimit", msg)
        try:
            data = r.json()
        except ValueError:
            if r.status_code == 403:
                raise XApiError("access", "X 暂时拒绝访问，可能是访问限制；请稍后重试")
            raise XApiError("structure", f"X 接口返回非 JSON（{r.status_code}）")
        if not isinstance(data, dict):
            raise XApiError("structure", "X 接口响应结构发生变化，请稍后重试")
        errors = [e for e in (data.get("errors") or []) if isinstance(e, dict)]
        msgs = "; ".join(str(e.get("message") or "") for e in errors)
        if any(e.get("code") in (32, 89, 135, 215) for e in errors) or "could not authenticate" in msgs.lower():
            raise XApiError("auth", "X 登录态失效，请重新发送 Cookie")
        if r.status_code == 403:
            raise XApiError("access", "X 暂时拒绝访问，请核对列表权限或稍后重试")
        if r.status_code != 200:
            raise XApiError("network", f"X 接口暂不可用（{r.status_code}），请稍后重试")
        if errors and not data.get("data"):
            raise XApiError("structure", f"X 接口报错：{msgs[:120]}")
        return data


def _json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _walk_tweets(data: dict, root: str = "list"):
    """遍历时间线 JSON，产出 tweet result 节点与下一页 cursor。

    列表结构：data.data.list.tweets_timeline.timeline.instructions[]
    用户主页：data.data.user.result.timeline.timeline.instructions[]
          → content.itemContent.tweet_results.result
    （GraphQL 完整响应嵌在 data.data 下；转推取被转原帖，via 由调用方从外层取。）
    """
    try:
        if root == "list":
            instructions = data["data"]["list"]["tweets_timeline"]["timeline"]["instructions"]
        else:
            instructions = data["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
    except (KeyError, TypeError):
        raise XApiError("structure", "X 时间线响应结构变化，请对照 gallery-dl 更新解析")
    if not isinstance(instructions, list):
        raise XApiError("structure", "X 时间线响应结构变化，请对照 gallery-dl 更新解析")
    tweets, cursor = [], None
    for ins in instructions:
        if not isinstance(ins, dict):
            continue
        for entry in ins.get("entries") or ([ins.get("entry")] if ins.get("entry") else []):
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("entryId") or "")
            content = _as_dict(entry.get("content"))
            if entry_id.startswith("cursor-"):
                cur = content.get("value") or _as_dict(content.get("item")).get("value")
                if entry_id.startswith("cursor-bottom-") and cur:
                    cursor = cur
                continue
            if content.get("entryType") != "TimelineTimelineItem":
                continue
            result = _as_dict(_as_dict(_as_dict(content.get("itemContent")).get("tweet_results")).get("result"))
            if result:
                tweets.append(result)
    return tweets, cursor


def _inner_tweet(result: dict):
    """转推解包：有 retweeted_status_result 时返回（原帖, 转推者昵称）。"""
    result = _as_dict(result)
    legacy = _as_dict(result.get("legacy"))
    rt = _as_dict(_as_dict(legacy.get("retweeted_status_result")).get("result"))
    if rt:
        user_res = _as_dict(_as_dict(_as_dict(result.get("core")).get("user_results")).get("result"))
        via = (_as_dict(user_res.get("core")).get("screen_name")
               or _as_dict(user_res.get("legacy")).get("screen_name") or "")
        return rt, via
    return result, ""


def _video_from_media(m: dict) -> dict | None:
    """单个 media 实体的 video_info → 契约条目（最高码率 video/mp4 变体 + 海报）。

    variants 里混有 application/x-mpegURL（HLS，浏览器原生播不了且下载无用），
    只留 video/mp4 按码率降序取最高；海报 = 同实体的 media_url_https（pbs 静帧）。
    没有 mp4 变体的条目返回 None（跳过该视频，不影响同帖其他媒体）。
    """
    info = _as_dict(m.get("video_info"))
    variants = [v for v in (info.get("variants") or []) if isinstance(v, dict)]
    mp4s = [v for v in variants
            if v.get("content_type") == "video/mp4" and v.get("url")]
    if not mp4s:
        return None
    best = max(mp4s, key=lambda v: v.get("bitrate") or 0)
    ar = info.get("aspect_ratio") or []
    return {
        "poster": m.get("media_url_https") or "",
        "url": best["url"],   # 预选的最高码率 mp4；normalize 也可从 variants 自行取最优
        "variants": [{"url": v["url"], "bitrate": v.get("bitrate") or 0} for v in mp4s],
        "aspect_ratio": [ar[0], ar[1]] if isinstance(ar, list) and len(ar) >= 2 and ar[0] and ar[1] else [1, 1],
        "duration_millis": info.get("duration_millis") or 0,
    }


def _tweet_to_item(result: dict, via: str = "") -> dict | None:
    """GraphQL tweet result → 标准契约条目；无图无视频返回 None（与库内边界一致）。"""
    result = _as_dict(result)
    legacy = _as_dict(result.get("legacy"))
    tid = str(legacy.get("id_str") or "")
    if not tid:
        return None
    user = _as_dict(_as_dict(_as_dict(result.get("core")).get("user_results")).get("result"))
    ucore = _as_dict(user.get("core"))
    ulegacy = _as_dict(user.get("legacy"))
    handle = ucore.get("screen_name") or ulegacy.get("screen_name") or ""
    name = ucore.get("name") or ulegacy.get("name") or ""
    # 新版响应头像在 result.avatar.image_url；legacy.profile_image_url_https 作旧格式兜底
    avatar = (_as_dict(user.get("avatar")).get("image_url")
              or ulegacy.get("profile_image_url_https") or "").replace("_normal", "")
    media = _as_dict(legacy.get("extended_entities")).get("media") or []
    images, videos = [], []
    for m in media:
        if not isinstance(m, dict):
            continue
        if m.get("type") == "photo":
            u = m.get("media_url_https")
            if u:
                images.append(u + "?name=orig")
        elif m.get("type") in ("video", "animated_gif"):   # GIF 实际也是 mp4 变体
            v = _video_from_media(m)
            if v:
                videos.append(v)
    if not images and not videos:
        return None
    created = ""
    try:
        created = (datetime.strptime(legacy["created_at"], _X_DATE)
                   .astimezone().isoformat())
    except (KeyError, ValueError):
        pass
    item = {
        "id": tid,
        "url": f"https://x.com/{handle}/status/{tid}",
        "author": {"id": str(user.get("rest_id") or ulegacy.get("id_str") or ""),
                   "uid": handle, "handle": handle, "name": name, "avatar": avatar},
        "text": html.unescape(legacy.get("full_text") or ""),
        "created_at": created,
        "via": via,
        "images": images,
    }
    if videos:
        item["videos"] = videos
    return item


def parse_list_timeline(data: dict) -> tuple[list[dict], str | None]:
    """时间线 JSON → (标准契约条目列表, 下一页 cursor)。转推解包后 via=转推者。"""
    tweets, cursor = _walk_tweets(data)
    items = []
    for result in tweets:
        inner, via = _inner_tweet(result)
        item = _tweet_to_item(inner, via=via)
        if item:
            items.append(item)
    return items, cursor


async def crawl_lists(client: httpx.AsyncClient, auth_token: str,
                      lists: list[dict], max_pages: int = 1,
                      on_list=None, ct0: str = "") -> list[dict]:
    """服务端抓取入口：对每个启用的 X 列表拉取最新时间线并入库。

    lists = [{list_id, name}]（list_id 为纯数字或 x: 前缀）。
    on_list(idx, summary)：每列表开抓时以 summary=None 回调（标 doing），
    收尾时回调该列表的最终 summary（含 error），供服务端写实时进度。
    返回 summary 列表 [{group, new_posts, new_pics, error}]；
    复用 /api/ingest/x 相同的标准化与入库路径，幂等（has_post 跳过）。
    """
    summaries = []
    rate_limited = False
    for idx, lst in enumerate(lists):
        if on_list:
            on_list(idx, None)
        raw = str(lst.get("list_id", "")).removeprefix("x:")
        name = lst.get("name") or f"X 列表 {raw}"
        summary = {"group": name, "new_posts": 0, "new_pics": 0, "error": ""}
        summaries.append(summary)
        if rate_limited:
            summary["error"] = "X 接口限流，跳过本轮等待恢复"
            summary["error_kind"] = "ratelimit"
            if on_list:
                on_list(idx, summary)
            continue
        api = None
        try:
            api = XGraphQL(auth_token, ct0)
            items = []
            async for item in api.list_latest(client, raw, max_pages=max_pages):
                items.append(item)
            await _ingest_items(client, items, x_source.group_list_id(raw), name, summary)
        except XApiError as e:
            summary["error"] = _scrub_error(e)
            summary["error_kind"] = e.kind
            if e.kind == "ratelimit":
                rate_limited = True
        except Exception as e:   # 网络等意外错误，保留现场继续下一组
            summary["error"] = f"{type(e).__name__}: {_scrub_error(e)}"
        finally:
            if api is not None and api.csrf:
                ct0 = api.csrf
        if on_list:
            on_list(idx, summary)
        await asyncio.sleep(1)
    return summaries


async def crawl_users(client: httpx.AsyncClient, auth_token: str,
                      users: list[dict], max_pages: int = 1,
                      on_user=None, ct0: str = "") -> list[dict]:
    """服务端抓取入口：对每个启用的 X 用户（xu: 分组）拉主页时间线并入库。

    users = [{handle, name}]（handle 不带 @；name 形如 @luoluokun_）。
    回调与 summary 语义同 crawl_lists。
    """
    summaries = []
    rate_limited = False
    for idx, u in enumerate(users):
        if on_user:
            on_user(idx, None)
        handle = str(u.get("handle", "")).lstrip("@")
        name = u.get("name") or f"@{handle}"
        summary = {"group": name, "new_posts": 0, "new_pics": 0, "error": ""}
        summaries.append(summary)
        if rate_limited:
            summary["error"] = "X 接口限流，跳过本轮等待恢复"
            summary["error_kind"] = "ratelimit"
            if on_user:
                on_user(idx, summary)
            continue
        api = None
        try:
            api = XGraphQL(auth_token, ct0)
            cache_key = f"x_user_rest_id:{handle.lower()}"
            rest_id = db.get_meta(cache_key)
            if not rest_id:
                rest_id = await api.user_id(client, handle)
                db.set_meta(cache_key, rest_id)
            items = []
            try:
                async for item in api.user_latest(client, rest_id, max_pages=max_pages):
                    items.append(item)
            except XApiError as e:
                # 若用缓存的 rest_id 遇到结构异常，自愈刷新一次 rest_id 重试
                if e.kind == "structure":
                    fresh_id = await api.user_id(client, handle)
                    if fresh_id != rest_id:
                        db.set_meta(cache_key, fresh_id)
                        items = []
                        async for item in api.user_latest(client, fresh_id, max_pages=max_pages):
                            items.append(item)
                    else:
                        raise
                else:
                    raise
            await _ingest_items(client, items, x_source.group_user_id(handle), name, summary)
        except XApiError as e:
            summary["error"] = _scrub_error(e)
            summary["error_kind"] = e.kind
            if e.kind == "ratelimit":
                rate_limited = True
        except Exception as e:
            summary["error"] = f"{type(e).__name__}: {_scrub_error(e)}"
        finally:
            if api is not None and api.csrf:
                ct0 = api.csrf
        if on_user:
            on_user(idx, summary)
        await asyncio.sleep(1)
    return summaries


async def _ingest_items(client: httpx.AsyncClient, items: list[dict],
                        lid: str, name: str, summary: dict) -> None:
    """标准推文条目 → 入库（列表与用户两条抓取路径共用）；幂等，has_post 跳过。"""
    from .. import crawler  # 局部导入避免 crawler↔xapi 循环依赖

    posts = x_source.normalize_tweets(items)
    if posts:
        unique = {}
        for p in posts:
            unique.setdefault(p["sid"], p)
        sids = list(unique)
        tombstones: set[str] = set()
        existing: set[str] = set()
        need_imgs: set[str] = set()
        with db.conn() as c:
            for i in range(0, len(sids), 400):
                chunk = sids[i:i + 400]
                qmarks = ",".join("?" * len(chunk))
                tombstones.update(r[0] for r in c.execute(f"SELECT sid FROM tombstones WHERE sid IN ({qmarks})", chunk))
                existing.update(r[0] for r in c.execute(f"SELECT sid FROM posts WHERE sid IN ({qmarks})", chunk))
                need_imgs.update(r[0] for r in c.execute(f"SELECT sid FROM image_jobs WHERE sid IN ({qmarks})", chunk))
        fresh = [
            p for sid, p in unique.items()
            if sid not in tombstones and (sid not in existing or sid in need_imgs)
        ]
        if fresh:
            db.observe_posts(fresh, lid)
            crawler.queue_posts(fresh, lid)
            avatar_cache: dict[str, str] = {}
            post_sem = asyncio.Semaphore(4)
            dl_sem = asyncio.Semaphore(6)
            avatar_locks: dict[str, asyncio.Lock] = {}
            token = crawler._STORE_CTX.set({
                "queued": {p["sid"] for p in fresh},
                "observed": {p["sid"] for p in fresh},
                "dl_sem": dl_sem,
                "avatar_locks": avatar_locks,
            })
            try:
                async def _store_one(p: dict) -> tuple[bool, int]:
                    async with post_sem:
                        existed = p["sid"] in existing
                        saved = await crawler._store_post(client, p, lid, avatar_cache)
                        return existed, saved

                results = await asyncio.gather(*(_store_one(p) for p in fresh))
            finally:
                crawler._STORE_CTX.reset(token)
            for existed, saved in results:
                summary["new_pics"] += saved
                if saved:   # 一张图都没下成的帖没入库，不计新增（与微博侧口径一致）
                    summary["new_posts"] += int(not existed)
    db.add_group(lid, name)
