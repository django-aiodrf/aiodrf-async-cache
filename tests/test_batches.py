"""Batch serialization retains one worker boundary for the default codec."""

from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import sync_to_async

from aiodrf_async_cache.redis import AsyncRedisCache


async def test_get_many_decodes_standard_values_in_one_worker_call():
    cache = AsyncRedisCache("redis://localhost", {})
    cache._async_client = AsyncMock()
    cache._async_client.mget.return_value = [b"1"] * 100
    with patch(
        "aiodrf_async_cache._callbacks.sync_to_async", wraps=sync_to_async
    ) as bridge:
        result = await cache.aget_many([str(i) for i in range(100)])
    assert result == dict.fromkeys(map(str, range(100)), 1)
    assert bridge.call_count == 1
    await cache.aclose()


async def test_set_many_encodes_standard_values_in_one_worker_call():
    cache = AsyncRedisCache("redis://localhost", {})
    pipeline = MagicMock()
    pipeline.__aenter__.return_value = pipeline
    pipeline.execute = AsyncMock()
    cache._async_client = MagicMock()
    cache._async_client.pipeline.return_value = pipeline
    with patch(
        "aiodrf_async_cache._callbacks.sync_to_async", wraps=sync_to_async
    ) as bridge:
        assert await cache.aset_many(dict.fromkeys(map(str, range(100)), 1)) == []
    assert bridge.call_count == 1
    assert pipeline.set.call_count == 100
    await cache.aclose()
