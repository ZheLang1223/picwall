"""两个入口共用一次生命周期；任一入口退出时一起关停。"""
import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

from app import serve


class ServeTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_lifecycle_and_two_listeners_stop_together(self):
        events = []
        servers = []

        @asynccontextmanager
        async def lifecycle(_app):
            events.append("start")
            yield
            events.append("stop")

        class FakeServer:
            def __init__(self, config):
                self.config = config
                self.should_exit = False
                servers.append(self)

            async def serve(self):
                self.assert_started = events == ["start"]
                if self.config.port == 8080:
                    return
                while not self.should_exit:
                    await asyncio.sleep(0)
                events.append("owner-stopped")

        with (patch.object(serve, "ManagedServer", FakeServer), patch.object(serve, "lifespan", lifecycle),
              patch.object(serve, "server_configs", return_value=serve.server_configs(True))):
            await serve.run()
        self.assertEqual(events, ["start", "owner-stopped", "stop"])
        self.assertTrue(all(s.assert_started and s.should_exit for s in servers))
        self.assertEqual([s.config.port for s in servers], [8080, 8082])
        self.assertTrue(all(s.config.lifespan == "off" for s in servers))
        self.assertIsInstance(servers[1].config.app, serve.LocalOwner)

    def test_native_owner_listener_is_bound_to_loopback(self):
        configs = serve.server_configs(False)
        self.assertEqual((configs[0].host, configs[0].port), ("0.0.0.0", 8082))
        self.assertEqual((configs[1].host, configs[1].port), ("127.0.0.1", 8081))
        self.assertIsInstance(configs[1].app, serve.LocalOwner)
