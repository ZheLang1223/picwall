"""Test session isolation helpers."""

import os
from pathlib import Path

import pytest

_INHERITED_PROXY_VARS = (
    "all_proxy",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
)


def pytest_configure(config) -> None:
    """Strip host shell proxy env vars so httpx ASGI tests never depend on socksio or external proxies."""
    del config
    for name in _INHERITED_PROXY_VARS:
        os.environ.pop(name, None)


def _tree(root: Path) -> set[Path] | None:
    """真实目录的递归路径快照；目录不存在返回 None。"""
    if not root.exists():
        return None
    return set(root.rglob("*"))


@pytest.fixture(autouse=True)
def _guard_real_data_dir():
    """测试碰了真实数据目录（默认 ~/.local/share/picwall）就当场失败。

    config 的凭据路径（X_AUTH_FILE / COOKIE_FILE）在 import 时按真实 DATA_DIR
    算出；用例若只重定向 config.DATA_DIR 而漏掉 *_FILE，save_x_auth 一类写入
    会覆盖宿主真实登录态。每个用例前后各拍一次快照，多出的路径即泄漏。
    """
    from app import config

    real = Path(config.DATA_DIR)
    before = _tree(real)
    yield
    after = _tree(real)
    created = sorted(after - before) if before is not None and after is not None else after or set()
    removed = sorted(before - after) if before is not None and after is not None else before or set()
    if created or removed:
        pytest.fail(
            f"测试触碰了真实数据目录 {real}："
            f"新建 {created or '无'}，删除 {removed or '无'}。"
            "请把 config.X_AUTH_FILE / COOKIE_FILE 等 *_FILE 也 patch 进临时目录。",
            pytrace=False,
        )
