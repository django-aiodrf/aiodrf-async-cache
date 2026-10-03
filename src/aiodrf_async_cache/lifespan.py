"""Own an async cache independently of Django's request-local registry."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured

from .middleware import AsyncCache


@asynccontextmanager
async def cache_lifespan(alias: str = "native") -> AsyncGenerator[AsyncCache, None]:
    """Own one backend outside Django's request-local cache registry.

    The backend must release its pools in ``aclose()``. For django-valkey,
    configure ``LifespanConnectionFactory`` and ``CLOSE_CONNECTION=True``.
    """
    cache = caches.create_connection(alias)
    if not getattr(cache, "is_async", False):
        raise ImproperlyConfigured("cache_lifespan requires a native async backend.")
    try:
        yield cache
    finally:
        await cache.aclose()
