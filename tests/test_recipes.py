"""The code of docs/recipes.md, run as written there."""

import hashlib

import pytest
from django.core.cache.backends.locmem import LocMemCache

pytestmark = pytest.mark.unit


async def generation(cache, namespace):
    key = f"generation:{namespace}"
    value = await cache.aget(key, version=1)
    if value is None:
        await cache.aadd(key, 1, timeout=None, version=1)
        value = await cache.aget(key, version=1)
    return value


async def invalidate(cache, namespace):
    try:
        await cache.aincr(f"generation:{namespace}", version=1)
    except ValueError:
        pass  # No counter yet: nothing was cached under it.


async def get_product(cache, product_id):
    version = await generation(cache, "products")
    return await cache.aget(f"product:{product_id}", version=version)


def hashed_key(key, key_prefix, version):
    digest = hashlib.sha256(str(key).encode()).hexdigest()
    return f"{key_prefix}:{version}:{digest}"


async def test_generation_counter_invalidates_a_namespace():
    cache = LocMemCache("recipes-generation", {})
    await invalidate(cache, "products")
    assert await get_product(cache, 1) is None
    await cache.aset("product:1", "first", version=await generation(cache, "products"))
    assert await get_product(cache, 1) == "first"
    await invalidate(cache, "products")
    assert await generation(cache, "products") == 2
    assert await get_product(cache, 1) is None


async def test_generation_counter_counts_with_msgspec_codec():
    pytest.importorskip("msgspec")
    from unittest.mock import AsyncMock

    from aiodrf_async_cache.codecs import MsgspecCodec
    from aiodrf_async_cache.redis import AsyncRedisCache

    cache = AsyncRedisCache(
        "redis://localhost", {"OPTIONS": {"serializer": MsgspecCodec()}}
    )
    cache._async_client = AsyncMock()
    cache._async_client.eval.return_value = b"2"
    await invalidate(cache, "products")
    cache._async_client.eval.assert_awaited_once()


async def test_hashed_key_function_avoids_key_warnings():
    from aiodrf_async_cache.redis import AsyncRedisCache

    cache = AsyncRedisCache(
        "redis://localhost",
        {
            "KEY_PREFIX": "myapp",
            "KEY_FUNCTION": hashed_key,
            "OPTIONS": {"callback_mode": "inline"},
        },
    )
    # Warnings are errors in this suite: a 300-character key with spaces
    # would raise CacheKeyWarning with Django's key function.
    key = await cache.amake_key("a key " * 50, version=2)
    assert key.startswith("myapp:2:")
    assert len(key) == len("myapp:2:") + 64
