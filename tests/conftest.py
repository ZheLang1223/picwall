"""Test session isolation helpers."""

import os

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
