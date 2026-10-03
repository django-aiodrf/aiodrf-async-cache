"""Share one computation between concurrent cache misses in one event loop.

Deduplication is local to the event loop and therefore to the worker process:
each process computes a missing value at most once at a time, not once in
total. Callers that share a computation receive the same object.
"""

import asyncio
import inspect
import threading
import weakref
from typing import Any

from django.core.cache.backends.base import DEFAULT_TIMEOUT

__all__ = ["aget_or_set"]

_MISSING = object()

# In-flight computations of each event loop, keyed by (cache, final key). An
# entry is removed when its task finishes, so a dict holds at most one task per
# key being computed; a closed loop's dict goes with the loop.
_flights: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[Any, Any], asyncio.Task[Any]]
] = weakref.WeakKeyDictionary()
_flights_lock = threading.Lock()


async def _make_key(cache: Any, key: Any, version: int | None) -> Any:
    # The native backends await KEY_FUNCTION; Django's own backends do not.
    make_key = getattr(cache, "amake_key", None)
    if make_key is not None:
        return await make_key(key, version=version)
    return cache.make_key(key, version=version)


async def _compute(
    cache: Any,
    key: Any,
    default: Any,
    timeout: Any,  # noqa: ASYNC109 -- Django cache TTL
    version: int | None,
) -> Any:
    # Django's BaseCache.aget_or_set from the miss on, except that what a
    # callable returns is awaited when it is awaitable.
    if callable(default):
        default = default()
        if inspect.isawaitable(default):
            default = await default
    await cache.aadd(key, default, timeout=timeout, version=version)
    # Read again, as Django does: another process may have added a value.
    return await cache.aget(key, default, version=version)


def _finish(
    flights: dict[tuple[Any, Any], asyncio.Task[Any]],
    entry: tuple[Any, Any],
    task: asyncio.Task[Any],
) -> None:
    # A failure that no caller is left to receive is logged by asyncio: by
    # asyncio.shield() on Python 3.14, as "Task exception was never
    # retrieved" before.
    if flights.get(entry) is task:
        del flights[entry]


async def aget_or_set(
    cache: Any,
    key: Any,
    default: Any,
    timeout: Any = DEFAULT_TIMEOUT,  # noqa: ASYNC109 -- Django cache TTL
    version: int | None = None,
) -> Any:
    """``cache.aget_or_set()`` that computes a missing value once per event loop.

    ``default`` is a value or a callable, as in Django; a coroutine function's
    result is awaited. A synchronous callable runs on the event loop, as in
    Django's ``aget_or_set``. Concurrent calls for the same cache and final key
    (prefix, version and key) await one computation. Cancelling a caller does
    not cancel the computation, which still stores its value. If it raises,
    every waiting caller gets the exception and nothing is cached.
    """
    loop = asyncio.get_running_loop()
    flights = _flights.get(loop)
    if flights is None:
        with _flights_lock:
            flights = _flights.setdefault(loop, {})
    entry = (cache, await _make_key(cache, key, version))
    task = flights.get(entry)
    if task is None:
        value = await cache.aget(key, _MISSING, version=version)
        if value is not _MISSING:
            return value
        # Another caller may have missed and started while this one read.
        task = flights.get(entry)
        if task is None:
            task = loop.create_task(_compute(cache, key, default, timeout, version))
            flights[entry] = task
            task.add_done_callback(lambda task: _finish(flights, entry, task))
    return await asyncio.shield(task)
