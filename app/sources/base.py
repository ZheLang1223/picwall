"""Source 层公共契约：错误分类 + post dict 校验。

外部平台都会变：来源故障必须收敛成有限的几种错误类别，UI 才能给
普通人看得懂的提示；字段结构变化在 normalize 层被拦下，不往库里漏。
"""


class SourceError(Exception):
    """来源访问失败基类。kind 供 UI/日志分类，不向用户展示原始异常。"""

    kind = "unknown"      # auth | network | rate_limit | structure | unknown


class AuthError(SourceError):
    kind = "auth"         # 登录态缺失/过期，需要用户重新连接


class NetworkError(SourceError):
    kind = "network"      # 超时/断网/DNS


class RateLimitError(SourceError):
    kind = "rate_limit"   # 请求受限，稍后再试


class StructureError(SourceError):
    kind = "structure"    # 响应结构与预期不符（平台改版），需要更新适配器


# _store_post / insert_post 消费的键集合；所有来源的 normalize 输出必须覆盖。
# 契约测试（tests/test_sources.py）把缺键失败从运行时前移到单测。
POST_CONTRACT = {
    "sid", "source", "uid", "author", "avatar", "text",
    "created_at", "fetched_at", "weibo_url", "via", "is_long", "pics",
}


def validate_post(p: dict) -> None:
    """normalize 输出的最后一道闸：缺键/空 sid 直接抛 StructureError。"""
    if not isinstance(p, dict):
        raise StructureError("post 不是 dict")
    missing = POST_CONTRACT - set(p)
    if missing:
        raise StructureError(f"post 缺键: {sorted(missing)}")
    if not p.get("sid"):
        raise StructureError("post 缺 sid")
