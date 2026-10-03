"""Keep synchronous callback prefixes off the event loop."""

import asyncio
import inspect
import threading
from collections.abc import Callable
from typing import Any

from asgiref.sync import iscoroutinefunction, sync_to_async


def awaits_inline(func: Callable[..., Any]) -> bool:
    return iscoroutinefunction(func) or (
        not inspect.isroutine(func) and iscoroutinefunction(type(func).__call__)
    )


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def run_sync_and_await(
    func: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    """
    Call ``func`` in one hop (asgiref) and await what it returns
    if that is awaitable: a synchronous wrapper of a coroutine function runs
    its own code in the worker and the coroutine on the event loop. If the
    caller is cancelled during the hop, the coroutine is closed, not started.
    """
    returned = []
    cancelled = threading.Event()

    def call() -> Any:
        result = func(*args, **kwargs)
        if inspect.iscoroutine(result):
            returned.append(result)
            if cancelled.is_set():
                result.close()
        return result

    call.__qualname__ = getattr(func, "__qualname__", None) or type(func).__qualname__
    try:
        result = await sync_to_async(call, thread_sensitive=True)()
    except asyncio.CancelledError:
        cancelled.set()
        for coroutine in returned:
            coroutine.close()
        raise
    return await maybe_await(result)
