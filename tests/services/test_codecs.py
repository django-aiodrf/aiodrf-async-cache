"""Counters, batches and compression through non-default codecs on real servers."""

import os
from uuid import uuid4

import pytest
from django.core.cache.backends.redis import RedisSerializer

from aiodrf_async_cache.codecs import CompressedCodec


def _msgspec():
    pytest.importorskip("msgspec")
    from aiodrf_async_cache.codecs import MsgspecCodec

    return MsgspecCodec()


def _fastdrf_msgspec():
    pytest.importorskip("msgspec")
    codecs = pytest.importorskip("fastdrf.codecs")
    if not getattr(codecs.MsgspecCodec, "supports_integer_operations", False):
        pytest.skip("This fastdrf's MsgspecCodec does not declare integer support")
    return codecs.MsgspecCodec()


CODECS = {
    "msgspec": _msgspec,
    "fastdrf-msgspec": _fastdrf_msgspec,
    "zlib-pickle": lambda: CompressedCodec(RedisSerializer(), min_size=0),
    "zlib-msgspec": lambda: CompressedCodec(_msgspec(), min_size=0),
}


@pytest.fixture(params=["redis", "valkey"])
def location(request):
    url = os.getenv(f"AIODRF_TEST_{request.param.upper()}_URL")
    if not url:
        pytest.skip("Requires a dedicated test service URL")
    pytest.importorskip(request.param)
    return request.param, url


@pytest.fixture(params=list(CODECS))
async def cache(request, location):
    driver, url = location
    codec = CODECS[request.param]()
    if driver == "redis":
        from aiodrf_async_cache.redis import AsyncRedisCache as backend_class
    else:
        from aiodrf_async_cache.valkey import AsyncValkeyCache as backend_class
    backend = backend_class(
        url,
        {
            "KEY_PREFIX": "codec-test-" + uuid4().hex,
            "TIMEOUT": 30,
            "OPTIONS": {
                "serializer": codec,
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
            },
        },
    )
    try:
        yield backend
    finally:
        keys = [
            key
            async for key in backend.async_client.scan_iter(match=backend.make_key("*"))
        ]
        if keys:
            await backend.async_client.delete(*keys)
        await backend.aclose()


async def test_counters_through_the_codec(cache):
    await cache.aset("counter", 5)
    assert await cache.aincr("counter") == 6
    assert await cache.adecr("counter", 10) == -4
    assert await cache.aget("counter") == -4
    raw = await cache.async_client.get(await cache.amake_key("counter"))
    assert raw == b"-4"


async def test_get_or_set_with_an_integer_default(cache):
    assert await cache.aget_or_set("counter", 41) == 41
    assert await cache.aincr("counter") == 42
    assert await cache.aget_or_set("counter", 0) == 42


async def test_batches_mix_integers_and_structures(cache):
    data = {
        "int": 7,
        "negative": -300,
        "large": 2**63 - 1,
        "bool": True,
        "none": None,
        "nested": {"a": [1, "two", None], "b": "x" * 2000},
    }
    assert await cache.aset_many(data) == []
    assert await cache.aget_many([*data, "missing"]) == data
    assert await cache.aincr("int") == 8
    assert await cache.aget("bool") is True


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1])
async def test_out_of_range_integers_are_refused_when_written(cache, value):
    if isinstance(cache._async_serializer, CompressedCodec) and isinstance(
        cache._async_serializer.codec, RedisSerializer
    ):
        pytest.skip("Django's serializer stores any integer as digits")
    with pytest.raises(OverflowError):
        await cache.aset("big", value)
    assert not await cache.ahas_key("big")


async def test_compressed_values_are_not_readable_by_django_redis_cache(location):
    import pickle

    from asgiref.sync import sync_to_async
    from django.core.cache.backends.redis import RedisCache

    driver, url = location
    if driver != "redis":
        pytest.skip("Django's RedisCache uses redis-py")
    from aiodrf_async_cache.redis import AsyncRedisCache

    prefix = "codec-interop-" + uuid4().hex
    native = AsyncRedisCache(
        url,
        {
            "KEY_PREFIX": prefix,
            "OPTIONS": {"serializer": CompressedCodec(RedisSerializer(), min_size=64)},
        },
    )
    sync = RedisCache(url, {"KEY_PREFIX": prefix})
    try:
        # Django writes uncompressed values the native cache reads...
        await sync_to_async(sync.set)("page", "p" * 500, 30)
        assert await native.aget("page") == "p" * 500
        # ...but cannot read what it compresses.
        await native.aset("page", "p" * 500, timeout=30)
        raw = await native.async_client.get(native.make_key("page"))
        assert raw[:2] == b"\xc1\x01"
        assert len(raw) < 500
        with pytest.raises(pickle.UnpicklingError):
            await sync_to_async(sync.get)("page")
        await native.aset("small", "s", timeout=30)
        assert await sync_to_async(sync.get)("small") == "s"
    finally:
        await native.adelete_many(["page", "small"])
        await native.aclose()
        await sync_to_async(sync.close)()
