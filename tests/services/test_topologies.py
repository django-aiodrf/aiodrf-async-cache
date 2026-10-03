"""Dedicated-service Sentinel discovery/failover and cross-slot Cluster I/O."""

import asyncio
import os
from uuid import uuid4

import pytest

from aiodrf_async_cache.redis import AsyncRedisCache

pytestmark = pytest.mark.skipif(
    not os.getenv("AIODRF_TEST_CACHE_TOPOLOGIES"),
    reason="Requires the dedicated cache-topologies Compose project",
)


@pytest.fixture(params=["redis", "valkey"])
def backend_class(request):
    if request.param == "redis":
        return AsyncRedisCache
    pytest.importorskip("valkey")
    from aiodrf_async_cache.valkey import AsyncValkeyCache

    return AsyncValkeyCache


@pytest.mark.parametrize("topology", ["cluster", "sentinel"])
async def test_native_topology_contract(backend_class, topology):
    options = {"topology": topology, "socket_timeout": 2, "socket_connect_timeout": 2}
    if topology == "sentinel":
        location = "aiodrf-test"
        options["sentinels"] = [("127.0.0.1", 17636)]
        options["sentinel_kwargs"] = {"socket_timeout": 2, "socket_connect_timeout": 2}
    else:
        location = "redis://127.0.0.1:17631/0"
        if backend_class.__name__ == "AsyncValkeyCache":
            location = location.replace("redis://", "valkey://")
    cache = backend_class(
        location, {"KEY_PREFIX": "topology-" + uuid4().hex, "OPTIONS": options}
    )
    try:
        assert (
            await cache.aset_many(
                {"one": 1, "two": {"nested": [None, True]}, "three": None}, timeout=30
            )
            == []
        )
        assert await cache.aget_many(["one", "two", "three", "missing"]) == {
            "one": 1,
            "two": {"nested": [None, True]},
            "three": None,
        }
        assert await cache.aincr("one") == 2
        assert not await cache.aadd("one", 999)
        await cache.atouch("one", 10)
        assert 0 < await cache.async_client.ttl(await cache.amake_key("one")) <= 10
        await cache.adelete_many(["one", "two", "three"])
        assert await cache.aget_many(["one", "two", "three"]) == {}
        await cache.aset_many({"one": 1, "two": 2}, timeout=0)
        assert await cache.aget_many(["one", "two"]) == {}
        assert cache.async_client is cache.async_client
    finally:
        await cache.adelete_many(["one", "two", "three"])
        await cache.aclose()


async def test_explicit_sentinel_failover_preserves_owned_client(backend_class):
    """Only the disposable service named aiodrf-test may be failed over."""
    from redis.asyncio import Redis
    from redis.exceptions import ConnectionError as RedisConnectionError

    cache = backend_class(
        "aiodrf-test",
        {
            "KEY_PREFIX": "failover-" + uuid4().hex,
            "OPTIONS": {
                "topology": "sentinel",
                "sentinels": [("127.0.0.1", 17636)],
                "socket_timeout": 1,
                "socket_connect_timeout": 1,
            },
        },
    )
    sentinel = Redis(host="127.0.0.1", port=17636)
    try:
        await cache.aset("value", 41)
        # Confirm replication before requesting a controlled promotion.
        async with asyncio.timeout(20):
            while await cache.async_client.wait(1, 100) != 1:  # noqa: ASYNC110 -- bounded polling of an external service
                await asyncio.sleep(0.1)
        previous = await sentinel.sentinel_get_master_addr_by_name("aiodrf-test")
        await sentinel.sentinel_failover("aiodrf-test")
        async with asyncio.timeout(20):
            while (  # noqa: ASYNC110 -- bounded polling of Sentinel's external state
                await sentinel.sentinel_get_master_addr_by_name("aiodrf-test")
                == previous
            ):
                await asyncio.sleep(0.1)
        # Force rediscovery rather than relying on a stale healthy socket.
        await cache.async_client.connection_pool.disconnect()
        assert await cache.aget("value") == 41
        assert await cache.aincr("value") == 42
        # Sentinel publishes the new address before the old primary has been
        # reconfigured. Wait for that step before another test starts a failover.
        async with Redis(host=previous[0], port=previous[1]) as old_primary:
            async with asyncio.timeout(20):
                while True:
                    try:
                        info = await old_primary.info("replication")
                    except RedisConnectionError:
                        # Reconfiguration/full synchronization may close clients.
                        # Retry only this read, never the non-idempotent writes.
                        info = {}
                    if (
                        info.get("role") == "slave"
                        and info.get("master_link_status") == "up"
                    ):
                        break
                    await asyncio.sleep(0.1)
    finally:
        await cache.adelete("value")
        await cache.aclose()
        await sentinel.aclose()
