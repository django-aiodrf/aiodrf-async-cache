"""Own an async cache independently of Django's request-local registry."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured

from .middleware import AsyncCache


@asynccontextmanager
async def cache_lifespan(
    alias: str = "native", *, ping: bool = False
) -> AsyncGenerator[AsyncCache, None]:
    """Own one backend outside Django's request-local cache registry.

    The backend must release its pools in ``aclose()``. For django-valkey,
    configure ``LifespanConnectionFactory`` and ``CLOSE_CONNECTION=True``.

    With ``ping=True``, ``async_client.ping()`` is awaited before the backend
    is yielded, so an unreachable server fails startup with the driver's
    connection error instead of failing the first request.
    """
    cache = caches.create_connection(alias)
    if not getattr(cache, "is_async", False):
        raise ImproperlyConfigured("cache_lifespan requires a native async backend.")
    try:
        if ping:
            if not hasattr(type(cache), "async_client"):
                raise ImproperlyConfigured(
                    "ping=True requires a backend with async_client; "
                    f"got {type(cache).__qualname__}."
                )
            await cache.async_client.ping()  # type: ignore[attr-defined]
        yield cache
    finally:
        await cache.aclose()
