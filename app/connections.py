"""登录连接：规范化输入、只读校验、按凭据版本记录状态。绝不返回凭据。"""
import hashlib
import re
from datetime import datetime

import httpx

from . import config, db, weibo
from .sources import xapi


def cookie_fields(value: str) -> dict[str, str]:
    value = re.sub(r"^Cookie\s*:\s*", "", value.strip(), flags=re.IGNORECASE)
    if len(value) > 32768 or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("请只粘贴 Cookie 值，去掉其他请求头或换行")
    fields = {}
    for part in value.split(";"):
        if not part.strip():
            continue
        name, sep, val = part.strip().partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Cookie 格式不正确，应为 名称=值; 名称=值")
        if name in fields and fields[name] != val.strip():
            raise ValueError("Cookie 中有同名但不同值的条目，请重新复制")
        fields[name] = val.strip()
    return fields


def normalize_weibo(value: str) -> str:
    fields = cookie_fields(value)
    if not fields.get("SUB") or not fields.get("XSRF-TOKEN"):
        raise ValueError("微博 Cookie 需要非空的 SUB 和 XSRF-TOKEN，请从已登录的 weibo.com 重新获取")
    return "; ".join(f"{k}={v}" for k, v in fields.items())


def normalize_x(value: str, ct0: str = "") -> tuple[str, str]:
    value = value.strip()
    if "=" in value or value.lower().startswith("cookie:"):
        fields = cookie_fields(value)
        value, ct0 = fields.get("auth_token", ""), fields.get("ct0", ct0)
    if not re.fullmatch(r"[A-Za-z0-9]{20,256}", value):
        raise ValueError("X 需要有效的 auth_token，可粘贴完整 Cookie 或单独的 token 值")
    if ct0 and not re.fullmatch(r"[A-Za-z0-9]{1,256}", ct0):
        raise ValueError("X 的 ct0 格式不正确，请重新复制")
    return value, ct0


def current(source: str) -> str:
    return config.get_cookie() if source == "weibo" else config.get_x_auth()


def _prefix(source: str) -> str:
    return "cookie" if source == "weibo" else "x_cookie"


def record(source: str, credential: str, state: str, error: str = "", updated=False) -> None:
    # 旧抓取／校验在用户更新 Cookie 后返回时，不得覆盖新连接的状态。
    if current(source) != credential:
        return
    prefix = _prefix(source)
    db.set_meta(prefix + "_state", state)
    db.set_meta(prefix + "_error", error)
    db.set_meta(prefix + "_fingerprint", hashlib.sha256(credential.encode()).hexdigest())
    now = datetime.now(config.NOW_TZ).isoformat(timespec="seconds")
    if state != "saved":
        db.set_meta(prefix + "_checked_at", now)
    else:
        db.set_meta(prefix + "_checked_at", "")
    if updated:
        db.set_meta(prefix + "_updated_at", now)


def status(source: str) -> dict:
    credential = current(source)
    prefix = _prefix(source)
    src = config.cookie_source() if source == "weibo" else "web" if credential else "none"
    fingerprint = db.get_meta(prefix + "_fingerprint")
    stale = bool(fingerprint and fingerprint != hashlib.sha256(credential.encode()).hexdigest())
    state = db.get_meta(prefix + "_state") if credential and not stale else ""
    return {"state": state if state in ("ok", "expired", "error", "saved") else "saved" if credential else "unset",
            "source": src, "error": db.get_meta(prefix + "_error") if credential and not stale else "",
            "updated_at": db.get_meta(prefix + "_updated_at") if not stale else "",
            "checked_at": db.get_meta(prefix + "_checked_at") if credential and not stale else ""}


async def probe(source: str, credential: str, ct0: str = "") -> dict:
    """验证连接只请求分组／时间线，不抓取或导入帖子。"""
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            if source == "weibo":
                await weibo.all_groups(client, credential)
            else:
                groups = db.active_groups(source="x")
                if not groups:
                    return {"state": "saved", "error": "已保存；添加 X 列表或博主后可校验连接"}
                api = xapi.XGraphQL(credential, ct0)
                list_target = next((g for g in groups if g["list_id"].startswith("x:")), None)
                user_target = next((g for g in groups if g["list_id"].startswith("xu:")), None)
                if list_target:
                    raw = list_target["list_id"].removeprefix("x:")
                    data = await api._call(client, xapi.ENDPOINTS["list_latest"],
                                           {"variables": xapi._json({"listId": raw, "count": 1}),
                                            "features": xapi._json(xapi.FEATURES)})
                    content = data.get("data") or {}
                    if not isinstance(content, dict) or not isinstance(content.get("list"), dict):
                        raise xapi.XApiError("structure", "X 列表暂不可访问，请核对列表后重试")
                elif user_target:
                    handle = user_target["list_id"].removeprefix("xu:")
                    rest_id = await api.user_id(client, handle)
                    db.set_meta(f"x_user_rest_id:{handle.lower()}", rest_id)
        return {"state": "ok", "error": ""}
    except weibo.CookieExpired:
        return {"state": "expired", "error": "微博登录已失效，请登录后重新获取 Cookie"}
    except xapi.XApiError as e:
        return {"state": "expired" if e.kind == "auth" else "error", "error": str(e)}
    except httpx.HTTPError:
        return {"state": "error", "error": "暂时无法连接平台，请检查网络后重试"}
    except weibo.WeiboError:
        return {"state": "error", "error": "微博接口暂不可用，请稍后重试校验"}
