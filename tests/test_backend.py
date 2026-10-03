"""Cache callback execution and topology contracts without external services."""

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from django.core.exceptions import ImproperlyConfigured

pytest.importorskip("redis.asyncio")


from aiodrf_async_cache.redis import AsyncRedisCache

pytestmark = pytest.mark.unit


async def test_async_key_serializer_and_default_callbacks_are_awaited():
    calls = []

    async def key_function(key, prefix, version):
        calls.append((key, prefix, version))
        await asyncio.sleep(0)
        return f"{prefix}:{version}:{key}"

    class Codec:
        async def dumps(self, value):
            await asyncio.sleep(0)
            return str(value).encode()

        async def loads(self, value):
            await asyncio.sleep(0)
            return int(value)

    cache = AsyncRedisCache(
        "redis://localhost",
        {
            "KEY_PREFIX": "test",
            "KEY_FUNCTION": key_function,
            "OPTIONS": {"serializer": Codec},
        },
    )
    cache._async_client = AsyncMock()
    cache._async_client.get.side_effect = [None, b"9"]

    async def default():
        return 9

    assert await cache.aget_or_set("key", default) == 9
    cache._async_client.set.assert_awaited_once_with(
        "test:1:key", b"9", ex=300, nx=True
    )
    assert calls == [("key", "test", 1)] * 3


async def test_sync_callbacks_run_off_loop_and_keep_async_wrappers():
    loop_thread = threading.get_ident()

    def key_function(key, prefix, version):
        assert threading.get_ident() != loop_thread
        return key

    async def result():
        assert threading.get_ident() == loop_thread
        return 7

    def default():
        assert threading.get_ident() != loop_thread
        return result()

    cache = AsyncRedisCache("redis://localhost", {"KEY_FUNCTION": key_function})
    cache._async_client = AsyncMock()
    cache._async_client.get.side_effect = [None, b"7"]
    assert await cache.aget_or_set("key", default) == 7


async def test_cluster_batches_use_single_key_commands_without_transactions():
    cache = AsyncRedisCache(
        "redis://localhost:7000", {"OPTIONS": {"topology": "cluster"}}
    )
    pipeline = MagicMock()
    pipeline.__aenter__.return_value = pipeline
    pipeline.execute = AsyncMock(return_value=[b"1", None, b"2"])
    cache._async_client = MagicMock()
    cache._async_client.pipeline.return_value = pipeline
    assert await cache.aget_many(["a", "b", "c"]) == {"a": 1, "c": 2}
    await cache.aset_many({"a": 1, "b": 2})
    await cache.adelete_many(["a", "b"])
    pipeline.mget.assert_not_called()
    pipeline.mset.assert_not_called()
    assert all(
        call.kwargs == {"transaction": False}
        for call in cache._async_client.pipeline.call_args_list
    )
    cache._async_client.flushdb = AsyncMock(return_value={"one": True, "two": True})
    assert await cache.aclear() is True
    cache._async_client.flushdb.assert_awaited_once_with(
        target_nodes=cache.cluster_class.PRIMARIES
    )
    cache._async_client.flushdb.return_value = {"one": True, "two": False}
    assert await cache.aclear() is False


@pytest.mark.parametrize(
    "options",
    [
        {"topology": "unknown"},
        {"topology": "sentinel"},
        {"callback_mode": "unknown"},
        {"decode_responses": True},
    ],
)
def test_invalid_configuration_is_rejected(options):
    with pytest.raises(ImproperlyConfigured):
        AsyncRedisCache("redis://localhost", {"OPTIONS": options})


async def test_decorated_async_callbacks_do_not_run_sync_prefix_on_loop():
    from functools import wraps

    owner = threading.get_ident()

    async def build():
        assert threading.get_ident() == owner
        return 8

    @wraps(build)
    def callback():
        assert threading.get_ident() != owner
        return build()

    cache = AsyncRedisCache("redis://localhost", {})
    assert await cache._callback(callback) == 8


async def test_cancelled_codec_does_not_publish_and_preserves_context():
    from contextvars import ContextVar

    started = asyncio.Event()
    scope = ContextVar("cache-callback-test", default="tenant")

    class Codec:
        async def dumps(self, value):
            assert scope.get() == "tenant"
            started.set()
            await asyncio.Event().wait()

        def loads(self, value):
            return value

    cache = AsyncRedisCache("redis://localhost", {"OPTIONS": {"serializer": Codec}})
    cache._async_client = AsyncMock()
    task = asyncio.create_task(cache.aset("value", 1))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cache._async_client.set.assert_not_awaited()


async def test_all_owned_resources_close_even_after_close_error():
    cache = AsyncRedisCache("redis://localhost", {})
    first = AsyncMock()
    last = AsyncMock(side_effect=OSError("close failed"))
    cache._resources.push_async_callback(first)
    cache._resources.push_async_callback(last)
    with pytest.raises(OSError, match="close failed"):
        await cache.aclose()
    first.assert_awaited_once()
    last.assert_awaited_once()
    with pytest.raises(RuntimeError, match="closed"):
        _ = cache.async_client


def test_system_checks_can_construct_the_backend():
    # Django's cache checks build every alias, outside any event loop.
    from django.core.cache import caches
    from django.core.management import call_command
    from django.test import override_settings

    config = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
        "native": {
            "BACKEND": "aiodrf_async_cache.redis.AsyncRedisCache",
            "LOCATION": "redis://cache.invalid",
        },
    }
    with override_settings(CACHES=config):
        try:
            call_command("check", tags=["caches"])
        finally:
            caches._settings = None


def test_the_first_event_loop_to_use_the_backend_owns_it():
    # Built on a thread; no pool exists until an event loop uses it.
    cache = AsyncRedisCache("redis://localhost", {})

    async def client():
        return cache.async_client

    asyncio.run(client())
    with pytest.raises(RuntimeError, match="shared between event loops"):
        asyncio.run(client())

    async def close():
        await cache.aclose()

    with pytest.raises(RuntimeError, match="owning event loop"):
        asyncio.run(close())


async def test_lifespan_closes_on_error_without_registering_request_cache():
    import gc
    import weakref

    from django.core.cache import caches
    from django.test import override_settings

    from aiodrf_async_cache.lifespan import cache_lifespan

    config = {
        "native": {
            "BACKEND": "aiodrf_async_cache.redis.AsyncRedisCache",
            "LOCATION": "redis://cache.invalid",
        }
    }
    with override_settings(CACHES=config):
        with pytest.raises(ValueError, match="application failure"):  # noqa: PT012 -- exercise exceptional context-manager exit
            async with cache_lifespan() as backend:
                reference = weakref.ref(backend)
                assert caches.all(initialized_only=True) == []
                raise ValueError("application failure")
        with pytest.raises(RuntimeError, match="closed"):
            _ = backend.async_client
        assert caches.all(initialized_only=True) == []
    del backend
    gc.collect()
    assert reference() is None


async def test_async_key_and_validation_subclass_hooks_are_preserved():
    calls = []

    class Cache(AsyncRedisCache):
        async def make_key(self, key, version=None):
            calls.append((key, version))
            return "custom:" + key

        async def validate_key(self, key):
            calls.append(key)

    cache = Cache("redis://localhost", {})
    assert await cache.amake_key("key", version=2) == "custom:key"
    assert calls == [("key", 2), "custom:key"]


async def test_pool_defaults_do_not_override_explicit_driver_capacity():
    cache = AsyncRedisCache("redis://localhost", {"OPTIONS": {"max_connections": 3}})
    assert cache.async_client.connection_pool.max_connections == 3
    await cache.aclose()


async def test_cluster_commands_are_not_retried_by_default():
    # A replayed EVAL or SET NX would apply twice. Standalone and Sentinel
    # connections do not retry either.
    cache = AsyncRedisCache(
        "redis://127.0.0.1:1/0", {"OPTIONS": {"topology": "cluster"}}
    )
    try:
        assert cache.async_client.retry.get_retries() == 0
    finally:
        await cache.aclose()


async def test_a_configured_cluster_retry_is_kept():
    from redis.asyncio.retry import Retry
    from redis.backoff import ConstantBackoff

    retry = Retry(ConstantBackoff(0.01), 2)
    cache = AsyncRedisCache(
        "redis://127.0.0.1:1/0",
        {"OPTIONS": {"topology": "cluster", "retry": retry}},
    )
    try:
        assert cache.async_client.retry is retry
    finally:
        await cache.aclose()
