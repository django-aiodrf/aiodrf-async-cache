"""Django's documented async cache API against dedicated Redis/Valkey services."""

import asyncio
import inspect
import os
from uuid import uuid4

import pytest
from django.core.cache import caches
from django.core.cache.backends.base import DEFAULT_TIMEOUT, BaseCache, CacheKeyWarning
from django.test import override_settings

from aiodrf_async_cache.redis import AsyncRedisCache


def test_async_cache_signatures_match_django():
    for name, member in inspect.getmembers(BaseCache, inspect.iscoroutinefunction):
        expected = inspect.signature(member).parameters
        actual = inspect.signature(getattr(AsyncRedisCache, name)).parameters
        assert list(actual) == list(expected), name
        for key, parameter in expected.items():
            assert actual[key].kind == parameter.kind, (name, key)
            assert actual[key].default == parameter.default, (name, key)


@pytest.fixture(params=["redis", "valkey"])
async def cache(request):
    url = os.getenv(f"AIODRF_TEST_{request.param.upper()}_URL")
    if not url:
        pytest.skip("Requires a dedicated test service URL")
    driver = request.param
    pytest.importorskip(driver)
    backend = f"aiodrf_async_cache.{driver}.Async{driver.title()}Cache"
    prefix = "contract-" + uuid4().hex
    with override_settings(
        CACHES={
            "native": {
                "BACKEND": backend,
                "LOCATION": url,
                "KEY_PREFIX": prefix,
                "VERSION": 3,
                "TIMEOUT": 30,
                "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
            }
        }
    ):
        # Use Django's registry, as an ordinary async consumer does.
        instance = caches["native"]
        try:
            yield instance
        finally:
            keys = [
                key
                async for key in instance.async_client.scan_iter(match=prefix + ":*")
            ]
            if keys:
                await instance.async_client.delete(*keys)
            await instance.aclose()


async def test_values_defaults_and_batch_results(cache):
    sentinel = object()
    assert await cache.aget("missing", sentinel) is sentinel
    assert await cache.aset("null", None) is None
    assert await cache.aget("null", sentinel) is None
    assert await cache.ahas_key("null") is True
    assert await cache.aadd("null", 10) is False
    assert await cache.aadd("new", {"values": [None, True, 2]}) is True
    assert await cache.aset_many({"a": 1, "b": False}) == []
    assert await cache.aget_many(["a", "b", "null", "missing"]) == {
        "a": 1,
        "b": False,
        "null": None,
    }
    assert await cache.aget_many([]) == {}
    assert await cache.aset_many({}) == []
    assert await cache.adelete("a") is True
    assert await cache.adelete("a") is False
    assert await cache.adelete_many(["b", "null", "missing"]) is None


@pytest.mark.parametrize("timeout", [DEFAULT_TIMEOUT, None, 0, -1, 15])
async def test_timeout_contract(cache, timeout):  # noqa: ASYNC109
    await cache.aset("expires", 7, timeout=timeout)
    if timeout in (0, -1):
        assert await cache.aget("expires") is None
    else:
        assert await cache.aget("expires") == 7
        ttl = await cache.async_client.ttl(cache.make_key("expires"))
        if timeout is None:
            assert ttl == -1
        else:
            assert 0 < ttl <= (30 if timeout is DEFAULT_TIMEOUT else timeout)
    assert await cache.atouch("absent", 20) is False
    await cache.aset("touch", 1)
    assert await cache.atouch("touch", None) is True
    assert await cache.async_client.ttl(cache.make_key("touch")) == -1


async def test_versions_and_callable_defaults(cache):
    await cache.aset("versioned", "original")
    await cache.aset("versioned", "other", version=4)
    assert await cache.aget("versioned") == "original"
    assert await cache.aincr_version("versioned") == 4
    assert await cache.aget("versioned") is None
    assert await cache.aget("versioned", version=4) == "original"
    assert await cache.adecr_version("versioned", version=4) == 3
    assert (
        await cache.aget_or_set("versioned", lambda: pytest.fail("default called"))
        == "original"
    )
    assert await cache.aget_or_set("callable", lambda: 5) == 5
    with pytest.raises(ValueError, match="not found"):
        await cache.aincr_version("missing")


@pytest.mark.parametrize(
    "initial,delta", [(2**53, 1), (2**63 - 2, 1), (-(2**53) - 2, 1), (-(2**63) + 1, -1)]
)
async def test_full_integer_range_and_ttl(cache, initial, delta):
    await cache.aset("counter", initial)
    assert await cache.aincr("counter", delta) == initial + delta
    assert await cache.aget("counter") == initial + delta
    assert 0 < await cache.async_client.ttl(cache.make_key("counter")) <= 30
    assert await cache.adecr("counter", delta) == initial


async def test_concurrent_increments_and_missing_keys(cache):
    await cache.aset("counter", 0)
    results = await asyncio.gather(*(cache.aincr("counter") for _ in range(20)))
    assert sorted(results) == list(range(1, 21))
    for method in (cache.aincr, cache.adecr):
        with pytest.raises(ValueError, match="not found"):
            await method("missing")


async def test_key_validation_uses_djangos_warning():
    cache = AsyncRedisCache("redis://localhost", {})
    with pytest.warns(CacheKeyWarning):
        await cache.amake_key("not portable")
    await cache.aclose()
