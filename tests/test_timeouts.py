"""Which error each of the three connection limits raises."""

import asyncio
import importlib
import socket

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(params=["redis", "valkey"])
def driver(request):
    pytest.importorskip(request.param)
    if request.param == "redis":
        from aiodrf_async_cache.redis import AsyncRedisCache as backend_class
    else:
        from aiodrf_async_cache.valkey import AsyncValkeyCache as backend_class
    exceptions = importlib.import_module(f"{request.param}.exceptions")
    return request.param, backend_class, exceptions


async def test_socket_timeout_limits_a_reply(driver):
    scheme, backend_class, exceptions = driver

    async def silent(reader, writer):
        await reader.read()
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    cache = backend_class(
        f"{scheme}://127.0.0.1:{port}/0",
        {"OPTIONS": {"socket_timeout": 0.1, "socket_connect_timeout": 2}},
    )
    try:
        with pytest.raises(exceptions.TimeoutError, match="Timeout reading"):
            await cache.aget("key")
    finally:
        await cache.aclose()
        server.close()
        await server.wait_closed()


async def test_socket_connect_timeout_limits_the_connection(driver):
    scheme, backend_class, exceptions = driver
    # A listener whose accept queue is full: further connections hang.
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(0)
    port = listener.getsockname()[1]
    fillers = []
    for _ in range(3):
        filler = socket.socket()
        filler.setblocking(False)
        try:
            filler.connect(("127.0.0.1", port))
        except BlockingIOError:
            pass
        fillers.append(filler)
    cache = backend_class(
        f"{scheme}://127.0.0.1:{port}/0",
        {"OPTIONS": {"socket_timeout": 5, "socket_connect_timeout": 0.1}},
    )
    try:
        async with asyncio.timeout(2):
            with pytest.raises(exceptions.TimeoutError, match="Timeout connecting"):
                await cache.aget("key")
    finally:
        await cache.aclose()
        for filler in fillers:
            filler.close()
        listener.close()


async def test_pool_timeout_limits_the_wait_for_a_connection(driver):
    scheme, backend_class, exceptions = driver
    cache = backend_class(
        f"{scheme}://127.0.0.1:1/0",
        {"OPTIONS": {"max_connections": 1, "async_pool_kwargs": {"timeout": 0.1}}},
    )
    pool = cache.async_client.connection_pool
    # Checked out without connecting: the only connection is in use.
    held = pool.get_available_connection()
    try:
        with pytest.raises(exceptions.ConnectionError, match="No connection available"):
            await cache.aget("key")
    finally:
        await pool.release(held)
        await cache.aclose()


def test_the_standalone_pool_waits_two_seconds_by_default(driver):
    scheme, backend_class, _ = driver
    cache = backend_class(f"{scheme}://127.0.0.1:1/0", {})
    assert cache._async_options["timeout"] == 2
    assert cache._async_options["max_connections"] == 20


async def test_driver_socket_timeouts_default_to_five_seconds(driver):
    scheme, backend_class, _ = driver
    cache = backend_class(f"{scheme}://127.0.0.1:1/0", {})
    try:
        connection = cache.async_client.connection_pool.make_connection()
        assert connection.socket_timeout == 5
        assert connection.socket_connect_timeout == 5
    finally:
        await cache.aclose()


async def test_the_non_blocking_pool_refuses_at_once(driver):
    scheme, backend_class, exceptions = driver
    cache = backend_class(
        f"{scheme}://127.0.0.1:1/0",
        {
            "OPTIONS": {
                "async_pool_class": f"{scheme}.asyncio.ConnectionPool",
                "max_connections": 1,
            }
        },
    )
    pool = cache.async_client.connection_pool
    held = pool.get_available_connection()
    try:
        async with asyncio.timeout(1):
            # redis-py raises its MaxConnectionsError, a ConnectionError.
            with pytest.raises(
                exceptions.ConnectionError, match="Too many connections"
            ):
                await cache.aget("key")
    finally:
        await pool.release(held)
        await cache.aclose()
