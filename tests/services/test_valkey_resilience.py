"""Reconnect, bounded retries and cancellation for both native Valkey backends."""

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from django_valkey.async_cache.cache import AsyncValkeyCache as VendorCache
from valkey.asyncio.retry import Retry
from valkey.backoff import NoBackoff
from valkey.exceptions import ConnectionError

from aiodrf_async_cache.valkey import AsyncValkeyCache
from tests.services.resilience import (
    check_cancelled_backoff,
    check_reconnect,
    check_reply_loss,
)

URL = os.environ.get("AIODRF_TEST_VALKEY_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Requires a Valkey test service")


@pytest.fixture(params=["vendor", "contrib"])
def cache_factory(request):
    @asynccontextmanager
    async def create(*, retry=None):
        pool_options = {"max_connections": 1, "timeout": 1, "health_check_interval": 15}
        if retry is not None:
            pool_options.update(retry=retry, retry_on_error=[ConnectionError])
        if request.param == "vendor":
            options = {
                "CONNECTION_FACTORY": "aiodrf_async_cache.django_valkey.LifespanConnectionFactory",
                "CONNECTION_POOL_CLASS": "valkey.asyncio.connection.BlockingConnectionPool",
                "CONNECTION_POOL_KWARGS": pool_options,
                "SOCKET_CONNECT_TIMEOUT": 2,
                "SOCKET_TIMEOUT": 2,
                "CLOSE_CONNECTION": True,
            }
            backend_class = VendorCache
        else:
            options = {
                "async_pool_kwargs": pool_options,
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
            }
            backend_class = AsyncValkeyCache
        cache = backend_class(
            URL,
            {"KEY_PREFIX": "valkey-resilience-" + uuid4().hex, "OPTIONS": options},
        )
        try:
            client = (
                await cache.client.get_client()
                if request.param == "vendor"
                else cache.async_client
            )
            yield cache, client
        finally:
            try:
                await cache.adelete("counter")
            finally:
                await cache.aclose()

    return create


async def test_disconnected_sockets_reconnect_without_replacing_the_pool(cache_factory):
    async with cache_factory() as (cache, client):
        await check_reconnect(cache, client)


async def test_default_standalone_policy_does_not_replay_accepted_writes(cache_factory):
    async with cache_factory() as (cache, client):
        await check_reply_loss(
            cache,
            client,
            ConnectionError,
            retries=0,
            operation="write",
            permanent=False,
        )


@pytest.mark.parametrize(
    ("retries", "operation", "permanent"),
    [(0, "write", False), (2, "read", False), (2, "read", True)],
)
async def test_opt_in_driver_retries_bound_reply_loss(
    cache_factory, retries, operation, permanent
):
    async with cache_factory(retry=Retry(NoBackoff(), retries)) as (cache, client):
        await check_reply_loss(
            cache,
            client,
            ConnectionError,
            retries=retries,
            operation=operation,
            permanent=permanent,
        )


async def test_cancellation_during_retry_backoff_returns_pool_connection(cache_factory):
    waiting = asyncio.Event()

    class DelayedBackoff(NoBackoff):
        def compute(self, failures):
            waiting.set()
            return 60

    async with cache_factory(retry=Retry(DelayedBackoff(), 2)) as (cache, client):
        await check_cancelled_backoff(cache, client, ConnectionError, waiting)


@pytest.mark.parametrize("operation", ["aget_many", "aset_many", "adelete_many"])
async def test_cancelled_batch_returns_no_connection_with_an_unread_reply(operation):
    from tests.services.resilience import BATCHES, check_cancelled_batch

    cache = AsyncValkeyCache(
        URL,
        {
            "KEY_PREFIX": "valkey-cancel-batch-" + uuid4().hex,
            "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
        },
    )
    try:
        await cache.async_client.ping()
        pool = cache.async_client.connection_pool
        (connection,) = pool._available_connections
        await check_cancelled_batch(
            cache,
            type(connection._parser),
            lambda: len(pool._in_use_connections),
            operation,
            BATCHES[operation][1],
        )
    finally:
        try:
            await cache.adelete_many(["one", "two"])
        finally:
            await cache.aclose()


async def test_close_with_commands_in_flight_returns():
    from tests.services.resilience import check_close_with_commands_in_flight

    cache = AsyncValkeyCache(
        URL,
        {
            "KEY_PREFIX": "valkey-close-in-flight-" + uuid4().hex,
            "OPTIONS": {"max_connections": 2, "socket_connect_timeout": 2},
        },
    )
    await check_close_with_commands_in_flight(
        cache, cache.async_client, ConnectionError
    )
