"""Process-local single-flight aget_or_set on any cache with Django's async API."""

import asyncio
import inspect

import pytest
from django.core.cache.backends.base import BaseCache
from django.core.cache.backends.locmem import LocMemCache

from aiodrf_async_cache import singleflight
from aiodrf_async_cache.singleflight import aget_or_set

pytestmark = pytest.mark.unit


@pytest.fixture
def cache(request):
    # A distinct name per test: LocMemCache shares storage between instances
    # of the same name.
    return LocMemCache(f"singleflight-{request.node.name}", {"TIMEOUT": 30})


def in_flight():
    return {
        key: task
        for flights in singleflight._flights.values()
        for key, task in flights.items()
    }


async def drained():
    # The entry is removed by a done callback, which runs one step after the
    # task completes.
    for _ in range(100):
        if not in_flight():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"entries remain: {in_flight()}")


def test_signature_mirrors_django():
    expected = inspect.signature(BaseCache.aget_or_set).parameters
    actual = inspect.signature(aget_or_set).parameters
    assert list(actual)[1:] == list(expected)[1:]
    for name, parameter in list(expected.items())[1:]:
        assert actual[name].default == parameter.default, name
        assert actual[name].kind == parameter.kind, name


async def test_concurrent_callers_run_the_loader_once(cache):
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"id": 42}

    results = await asyncio.gather(
        *(aget_or_set(cache, "user", load) for _ in range(100))
    )
    assert calls == 1
    assert results == [{"id": 42}] * 100
    assert await cache.aget("user") == {"id": 42}
    await drained()


async def test_values_and_sync_callables_follow_django(cache):
    assert await aget_or_set(cache, "value", 3) == 3
    assert await aget_or_set(cache, "value", 4) == 3
    assert await aget_or_set(cache, "callable", lambda: "x") == "x"
    assert await aget_or_set(cache, "timeout", "t", timeout=0) == "t"
    assert not await cache.ahas_key("timeout")
    await drained()


async def test_a_cached_none_is_a_hit(cache):
    await cache.aset("none", None)

    async def load():
        raise AssertionError("loader called for a cached None")

    assert await aget_or_set(cache, "none", load) is None


async def test_cancelled_waiters_do_not_cancel_the_computation(cache):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "computed"

    callers = [asyncio.create_task(aget_or_set(cache, "key", load)) for _ in range(100)]
    await asyncio.wait_for(started.wait(), 2)
    for caller in callers[:99]:
        caller.cancel()
    await asyncio.gather(*callers[:99], return_exceptions=True)
    callers[99].cancel()
    with pytest.raises(asyncio.CancelledError):
        await callers[99]
    assert all(caller.cancelled() for caller in callers)
    (task,) = in_flight().values()
    release.set()
    assert await task == "computed"
    assert calls == 1
    assert await cache.aget("key") == "computed"
    await drained()


async def test_an_exception_reaches_every_caller_and_is_not_cached(cache):
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        raise ValueError("loader failed")

    results = await asyncio.gather(
        *(aget_or_set(cache, "key", load) for _ in range(10)), return_exceptions=True
    )
    assert calls == 1
    assert all(
        isinstance(result, ValueError) and str(result) == "loader failed"
        for result in results
    )
    assert results[0] is results[-1]
    await drained()
    assert not await cache.ahas_key("key")

    async def recover():
        return "ok"

    assert await aget_or_set(cache, "key", recover) == "ok"


async def test_versions_compute_separately(cache):
    calls = []

    def loader(version):
        async def load():
            calls.append(version)
            await asyncio.sleep(0.01)
            return version

        return load

    results = await asyncio.gather(
        aget_or_set(cache, "key", loader(1), version=1),
        aget_or_set(cache, "key", loader(2), version=2),
        aget_or_set(cache, "key", loader(1), version=1),
    )
    assert results == [1, 2, 1]
    assert sorted(calls) == [1, 2]
    await drained()


async def test_caches_with_different_prefixes_do_not_share(request):
    first = LocMemCache(f"sf-a-{request.node.name}", {"KEY_PREFIX": "a"})
    second = LocMemCache(f"sf-b-{request.node.name}", {"KEY_PREFIX": "b"})
    calls = []

    def loader(name):
        async def load():
            calls.append(name)
            await asyncio.sleep(0.01)
            return name

        return load

    assert await asyncio.gather(
        aget_or_set(first, "key", loader("a")),
        aget_or_set(second, "key", loader("b")),
    ) == ["a", "b"]
    assert sorted(calls) == ["a", "b"]


def test_closed_event_loops_free_their_entries():
    import gc
    import weakref

    class Cache:
        # No sync_to_async: asgiref keeps a reference to the last loop it used.
        def __init__(self):
            self.values = {}

        def make_key(self, key, version=None):
            return key

        async def aget(self, key, default=None, version=None):
            return self.values.get(key, default)

        async def aadd(self, key, value, timeout=None, version=None):  # noqa: ASYNC109 -- Django cache TTL
            self.values.setdefault(key, value)

    cache = Cache()

    async def run():
        return await aget_or_set(cache, "key", lambda: 1)

    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(run()) == 1
        assert singleflight._flights[loop] == {}
    finally:
        loop.close()
    reference = weakref.ref(loop)
    del loop
    gc.collect()
    assert reference() is None
