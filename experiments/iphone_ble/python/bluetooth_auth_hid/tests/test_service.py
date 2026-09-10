"""验证单次请求，不启动真实蓝牙服务。"""

import asyncio
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bluetooth_auth_hid import daemon


class Backend:
    def __init__(self, results, on_wait=None):
        self.results = list(results)
        self.on_wait = on_wait
        self.disconnect_count = 0
        self.queries = 0

    async def connected(self):
        self.queries += 1
        value = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(value, Exception):
            raise value
        return value

    async def wait_change(self, timeout):
        if self.on_wait:
            await self.on_wait(self)
        else:
            await asyncio.sleep(timeout)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def api(self, backend, timeout=0.02):
        api = daemon.Authentication(backend, timeout, query_timeout=0.02)
        self.addAsyncCleanup(api.close)
        return api

    async def test_connected_only_queries_once(self):
        backend = Backend([True])
        self.assertTrue(await self.api(backend).ask_or_connect())
        self.assertEqual(backend.queries, 1)

    async def test_disconnected_waits_for_one_success(self):
        backend = Backend([False, True])
        self.assertTrue(await self.api(backend).ask_or_connect())
        self.assertEqual(backend.queries, 2)

    async def test_timeout_stops_all_work_without_retry(self):
        backend = Backend([False])
        self.assertFalse(await self.api(backend).ask_or_connect())
        queries = backend.queries
        await asyncio.sleep(0.03)
        self.assertEqual(backend.queries, queries)

    async def test_query_error_returns_failure_without_connect_wait(self):
        backend = Backend([RuntimeError("private-value")])
        self.assertFalse(await self.api(backend).ask_or_connect())
        self.assertEqual(backend.queries, 1)

    async def test_initial_query_is_bounded(self):
        backend = Backend([False])
        async def slow():
            await asyncio.sleep(10)
        backend.connected = slow
        self.assertFalse(await self.api(backend).ask_or_connect())

    async def test_disconnect_ends_attempt_even_if_phone_connects_again(self):
        async def disconnect(backend):
            backend.disconnect_count += 1
            backend.results = [True]
        backend = Backend([False], disconnect)
        self.assertFalse(await self.api(backend).ask_or_connect())
        self.assertEqual(backend.queries, 3)

    async def test_concurrent_requests_share_only_in_progress_operation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait(backend):
            entered.set()
            await release.wait()
            backend.results = [True]
        backend = Backend([False], wait)
        api = self.api(backend, 1)
        first = asyncio.create_task(api.ask_or_connect())
        await entered.wait()
        second = asyncio.create_task(api.ask_or_connect())
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [True, True])
        self.assertEqual(backend.queries, 3)
        backend.results = [RuntimeError("unavailable")]
        self.assertFalse(await api.ask_or_connect())
        self.assertEqual(backend.queries, 4)

    async def test_cancelled_client_does_not_cancel_shared_operation(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait(backend):
            entered.set()
            await release.wait()
            backend.results = [True]
        backend = Backend([False], wait)
        api = self.api(backend, 1)
        first = asyncio.create_task(api.ask_or_connect())
        await entered.wait()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        second = asyncio.create_task(api.ask_or_connect())
        release.set()
        self.assertTrue(await second)
        self.assertEqual(backend.queries, 3)

    async def test_stop_cancels_pending_work(self):
        entered = asyncio.Event()
        async def wait(_backend):
            entered.set()
            await asyncio.sleep(10)
        backend = Backend([False], wait)
        api = self.api(backend, 1)
        pending = asyncio.create_task(api.ask_or_connect())
        await entered.wait()
        await api.close()
        await asyncio.gather(pending, return_exceptions=True)
        self.assertFalse(await api.ask_or_connect())
        self.assertEqual(backend.queries, 2)


class AddressTests(unittest.TestCase):
    def test_private_address_file_and_invalid_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "phone"
            path.write_text("AA:BB:CC:DD:EE:FF")
            path.chmod(0o600)
            self.assertEqual(daemon.load_address(path), "AA:BB:CC:DD:EE:FF")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                daemon.load_address(path)
            path.chmod(0o600)
            for value in (b"private-value", b"", b"x" * 129, bytes([255])):
                path.write_bytes(value)
                with self.assertRaises(ValueError):
                    daemon.load_address(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                daemon.load_address(link)

    def test_missing_address_does_not_open_backend_or_reveal_private_values(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(daemon, "BlueZBackend") as backend:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(daemon.main([]), 78)
            backend.assert_not_called()
            self.assertNotIn("/home/", output.getvalue())


if __name__ == "__main__":
    unittest.main()
