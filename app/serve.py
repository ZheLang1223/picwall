"""同一应用、数据库和调度器的两个入口。

8082 是宿主仅映射到回环地址的本机入口；8080 是需要配对的局域网入口。
本机权限来自监听器，不能由客户端 Host、IP 转发头或 Cookie 声明。
"""
import asyncio
import signal
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from .server import app, lifespan


class LocalOwner:
    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, pwall_local_owner=True)
        await self.application(scope, receive, send)


class ManagedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # 两个监听器共用下面的信号处理，不互相覆盖，也不重复转发信号。
        yield

    def install_signal_handlers(self):
        # uvicorn 0.29 的兼容入口。
        pass


def server_configs(in_container=None):
    if in_container is None:
        in_container = Path("/.dockerenv").is_file()
    # Docker 的安全边界是宿主端口映射；裸进程必须由监听器自身绑定回环地址。
    return [
        uvicorn.Config(app, host="0.0.0.0", port=8080 if in_container else 8082,
                       lifespan="off", proxy_headers=False),
        uvicorn.Config(LocalOwner(app), host="0.0.0.0" if in_container else "127.0.0.1",
                       port=8082 if in_container else 8081, lifespan="off", proxy_headers=False),
    ]


async def run():
    servers = [ManagedServer(config) for config in server_configs()]
    loop = asyncio.get_running_loop()

    def stop():
        for server in servers:
            server.should_exit = True

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    for sig in previous:
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop))
    try:
        # 先初始化数据库、启动一次调度器，再开放两个监听器。
        async with lifespan(app):
            tasks = [asyncio.create_task(server.serve()) for server in servers]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                stop()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    asyncio.run(run())
