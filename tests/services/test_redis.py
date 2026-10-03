"""Native network I/O, connection ownership and Django-compatible cache values."""

import asyncio
import os
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.cache.backends.redis import RedisCache
from django_redis.cache import RedisCache as VendorRedisCache

from aiodrf_async_cache.redis import AsyncRedisCache

URL = os.environ.get("AIODRF_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="Set AIODRF_TEST_REDIS_URL to a dedicated service"
)


@pytest.mark.parametrize("initial", [2**53, 2**63 - 2, -(2**53) - 2])
async def test_large_counter_result_matches_stored_integer(cache, initial):
    await cache.aset("large-counter", initial, timeout=30)
    result = await cache.aincr("large-counter")
    assert result == initial + 1
    assert result == await cache.aget("large-counter")


@pytest.fixture
async def cache():
    backend = AsyncRedisCache(
        URL,
        {
            "KEY_PREFIX": "native-test-" + uuid4().hex,
            "TIMEOUT": 30,
            "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
        },
    )
    try:
        yield backend
    finally:
        # Delete only this test's keys, never flush a shared database.
        keys = [
            key
            async for key in backend.async_client.scan_iter(match=backend.make_key("*"))
        ]
        if keys:
            await backend.async_client.delete(*keys)
        await backend.aclose()


async def test_all_aiodrf_async_cache_operations_are_native(cache):
    # Inline CPU callbacks are an explicit opt-in; network I/O never bridges.
    cache.callback_mode = "inline"
    with patch(
        "asgiref.sync.SyncToAsync.__call__", side_effect=AssertionError("sync bridge")
    ):
        assert await cache.aget("absent", "fallback") == "fallback"
        assert await cache.aadd("value", {"nested": [1, True, None]})
        assert not await cache.aadd("value", "second")
        assert await cache.aget("value") == {"nested": [1, True, None]}
        assert await cache.ahas_key("value")
        assert await cache.atouch("value", 15)
        assert await cache.aset_many({"one": 1, "two": 2, "none": None}) == []
        assert await cache.aget_many(["one", "two", "none", "missing"]) == {
            "one": 1,
            "two": 2,
            "none": None,
        }
        assert await cache.aget_or_set("one", 9) == 1
        assert await cache.aget_or_set("new", lambda: 4) == 4
        assert await cache.aincr("one", 2) == 3
        assert await cache.adecr("one") == 2
        assert await cache.aincr_version("two") == 2
        assert await cache.aget("two", version=2) == 2
        assert await cache.adecr_version("two", version=2) == 1
        assert await cache.adelete("two")
        await cache.adelete_many(["one", "none"])
        assert await cache.aget_many(["one", "none"]) == {}
        assert await cache.aget_many([]) == {}
        assert await cache.aset_many({}) == []
        await cache.adelete_many([])


@pytest.mark.parametrize("backend_class", [RedisCache, VendorRedisCache])
@pytest.mark.parametrize(
    "value", [None, True, False, 0, -12, 3.5, "text", b"bytes", {"a": [1, 2]}]
)
async def test_native_and_django_sync_backends_share_values(
    cache, backend_class, value
):
    from asgiref.sync import sync_to_async

    sync = backend_class(URL, {"KEY_PREFIX": cache.key_prefix})
    await cache.aset("roundtrip", value)
    assert await sync_to_async(sync.get)("roundtrip") == value
    await sync_to_async(sync.set)("roundtrip", value, 30)
    assert await cache.aget("roundtrip") == value


async def test_expiry_add_atomic_counter_and_pool_reuse(cache):
    client = cache.async_client
    await cache.aset("counter", 0, timeout=30)
    await asyncio.gather(*(cache.aincr("counter") for _ in range(40)))
    assert await cache.aget("counter") == 40
    assert 0 < await client.ttl(cache.make_key("counter")) <= 30
    with pytest.raises(ValueError, match="not found"):
        await cache.aincr("missing")
    assert not await cache.ahas_key("missing")
    await cache.aset("expiring", 1, timeout=0)
    assert not await cache.ahas_key("expiring")
    await cache.aset("existing", 1)
    assert not await cache.aadd("existing", 2, timeout=0)
    assert await cache.aget("existing") == 1
    assert await cache.aadd("empty", 2, timeout=0)
    assert not await cache.ahas_key("empty")
    assert cache.async_client is client


async def test_native_pool_is_closed_and_backend_cannot_reopen():
    backend = AsyncRedisCache(URL, {})
    client = backend.async_client
    await client.ping()
    await backend.aclose()
    await backend.aclose()
    assert all(
        not connection.is_connected
        for connection in client.connection_pool._available_connections
    )
    with pytest.raises(RuntimeError, match="closed"):
        await backend.aget("unused")


async def test_disconnected_sockets_reconnect_without_replacing_the_pool(cache):
    from tests.services.resilience import check_reconnect

    await check_reconnect(cache, cache.async_client)


async def test_default_standalone_policy_does_not_replay_accepted_writes(cache):
    from redis.exceptions import ConnectionError

    from tests.services.resilience import check_reply_loss

    await check_reply_loss(
        cache,
        cache.async_client,
        ConnectionError,
        retries=0,
        operation="write",
        permanent=False,
    )


@pytest.mark.parametrize(
    ("retries", "operation", "permanent"),
    [(0, "write", False), (2, "read", False), (2, "read", True)],
)
async def test_opt_in_driver_retries_bound_reply_loss(retries, operation, permanent):
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff
    from redis.exceptions import ConnectionError

    from tests.services.resilience import check_reply_loss

    cache = AsyncRedisCache(
        URL,
        {
            "KEY_PREFIX": "retry-test-" + uuid4().hex,
            "OPTIONS": {
                "retry": Retry(NoBackoff(), retries),
                "retry_on_error": [ConnectionError],
                "health_check_interval": 15,
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
                "async_pool_kwargs": {"max_connections": 1, "timeout": 1},
            },
        },
    )
    try:
        await check_reply_loss(
            cache,
            cache.async_client,
            ConnectionError,
            retries=retries,
            operation=operation,
            permanent=permanent,
        )
    finally:
        try:
            await cache.adelete("counter")
        finally:
            await cache.aclose()


async def test_cancellation_during_retry_backoff_returns_pool_connection():
    from redis.asyncio.retry import Retry
    from redis.backoff import NoBackoff
    from redis.exceptions import ConnectionError

    from tests.services.resilience import check_cancelled_backoff

    waiting = asyncio.Event()

    class DelayedBackoff(NoBackoff):
        def compute(self, failures):
            waiting.set()
            return 60

    cache = AsyncRedisCache(
        URL,
        {
            "KEY_PREFIX": "cancel-retry-" + uuid4().hex,
            "OPTIONS": {
                "retry": Retry(DelayedBackoff(), 2),
                "retry_on_error": [ConnectionError],
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
                "async_pool_kwargs": {"max_connections": 1, "timeout": 1},
            },
        },
    )
    try:
        await check_cancelled_backoff(
            cache, cache.async_client, ConnectionError, waiting
        )
    finally:
        try:
            await cache.adelete("counter")
        finally:
            await cache.aclose()


async def test_async_pool_waits_without_blocking_and_releases_cancelled_waiter():
    backend = AsyncRedisCache(
        URL, {"OPTIONS": {"async_pool_kwargs": {"max_connections": 1, "timeout": 1}}}
    )
    client = backend.async_client
    try:
        async with client.pipeline() as holder:
            await holder.watch("native-cache-pool-test")
            waiter = asyncio.create_task(client.ping())
            await asyncio.sleep(0.01)
            assert not waiter.done()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        assert await client.ping()
    finally:
        await backend.aclose()


async def test_cross_loop_access_and_close_cannot_break_the_owning_loop(cache):
    from asgiref.sync import sync_to_async

    await cache.aset("value", 1)
    for operation in (lambda: cache.aget("value"), cache.aclose):
        with pytest.raises(RuntimeError, match="loop"):
            await sync_to_async(lambda operation=operation: asyncio.run(operation()))()
    assert await cache.aget("value") == 1


@pytest.mark.parametrize(
    "options",
    [{"async_pool_class": "redis.ConnectionPool"}, {"decode_responses": True}],
)
async def test_incompatible_pool_configuration_fails_before_network_io(options):
    from django.core.exceptions import ImproperlyConfigured

    with pytest.raises(ImproperlyConfigured):
        _ = AsyncRedisCache(URL, {"OPTIONS": options}).async_client


async def test_clear_uses_native_client_without_flushing_a_live_database():
    from unittest.mock import AsyncMock

    backend = AsyncRedisCache(URL, {})
    try:
        with patch.object(
            backend.async_client, "flushdb", new_callable=AsyncMock, return_value=True
        ) as flush:
            assert await backend.aclear()
            flush.assert_awaited_once_with()
    finally:
        await backend.aclose()


async def test_raw_pipeline_uses_the_owned_pool(cache):
    async with cache.async_client.pipeline() as pipeline:
        pipeline.set(cache.make_key("raw"), 4, ex=30)
        pipeline.incr(cache.make_key("raw"))
        assert await pipeline.execute() == [True, 5]
    assert await cache.aget("raw") == 5
