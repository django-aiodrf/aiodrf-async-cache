"""Native cache I/O with Django's page-cache policy and ASGI-owned resources."""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

from asgiref.sync import markcoroutinefunction
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.middleware.cache import CacheMiddleware

__all__ = [
    "AsyncCache",
    "AsyncCacheMiddleware",
    "AsyncFetchFromCacheMiddleware",
    "AsyncUpdateCacheMiddleware",
]


class AsyncCache(Protocol):
    """The cache operations required by native page-cache middleware."""

    async def aget(self, key: str, default: Any = None) -> Any: ...

    async def aset(self, key: str, value: Any, timeout: int | None) -> Any: ...  # noqa: ASYNC109 -- Django cache TTL, not an I/O deadline

    async def aclose(self, **kwargs: Any) -> None: ...


class _PolicyCache:
    """Request-local cache reads and writes used by Django's synchronous policy.

    Missing reads are fetched asynchronously before evaluating the policy again.
    Only Django's policy is evaluated again, never the view or cache writes.
    This keeps version-specific security rules and key construction in Django.
    """

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.missing: set[str] = set()
        self.writes: list[tuple[str, Any, int | None]] = []

    def get(self, key: str, default: Any = None) -> Any:
        if key not in self.values:
            self.missing.add(key)
        return self.values.get(key, default)

    def set(self, key: str, value: Any, timeout: int | None = None) -> None:
        self.writes.append((key, value, timeout))


class _Policy(CacheMiddleware):
    def __init__(
        self,
        get_response: Callable[[HttpRequest], Awaitable[HttpResponseBase]],
        storage: _PolicyCache,
    ) -> None:
        self.storage = storage
        super().__init__(get_response)

    @property
    def cache(self) -> Any:
        return self.storage


class _AsyncCacheMiddleware:
    sync_capable = False
    async_capable = True

    async def __call__(self, request: HttpRequest) -> HttpResponseBase | str:
        raise NotImplementedError

    def __init__(
        self, get_response: Callable[[HttpRequest], Awaitable[HttpResponseBase]]
    ) -> None:
        if not inspect.iscoroutinefunction(get_response):
            raise ValueError(
                "Native cache middleware requires an async response chain."
            )
        self.get_response = get_response
        markcoroutinefunction(self)

    def get_cache(self, request: HttpRequest) -> AsyncCache:
        """Use the lifespan's backend; override for a composite resource object."""
        try:
            from aiodrf_asgi_lifespan.asgi import get_lifespan_state
        except ModuleNotFoundError as exc:
            if exc.name != "aiodrf_asgi_lifespan":
                raise
            raise ImproperlyConfigured(
                "Install aiodrf-async-cache[lifespan] or override get_cache()."
            ) from exc
        cache = get_lifespan_state(request, object)
        if not callable(getattr(cache, "aget", None)) or not callable(
            getattr(cache, "aset", None)
        ):
            raise ImproperlyConfigured(
                "Cache middleware requires a cache lifespan or a get_cache() override."
            )
        return cast(AsyncCache, cache)

    async def fetch(self, request: HttpRequest) -> HttpResponse | None:
        storage = _PolicyCache()
        policy = _Policy(self.get_response, storage)
        cache = None
        while True:
            response = policy.process_request(request)
            if not storage.missing:
                return response
            if cache is None:
                cache = self.get_cache(request)
            for key in storage.missing:
                storage.values[key] = await cache.aget(key)
            storage.missing.clear()

    async def update(
        self, request: HttpRequest, response: HttpResponseBase | str
    ) -> HttpResponseBase | str:
        if getattr(response, "is_rendered", True) is False:
            raise ImproperlyConfigured(
                "Native cache middleware must receive a rendered response from Django."
            )
        storage = _PolicyCache()
        response = _Policy(self.get_response, storage).process_response(
            request, response
        )
        if storage.writes:
            cache = self.get_cache(request)
            # Keep Django's header-list-before-page publication order. A missing
            # page is a cache miss; an unrendered response is never published.
            for key, value, timeout in storage.writes:
                await cache.aset(key, value, timeout)
        return response


class AsyncFetchFromCacheMiddleware(_AsyncCacheMiddleware):
    """Django's fetch policy using native ``aget()``; place last in MIDDLEWARE."""

    async def __call__(self, request: HttpRequest) -> HttpResponseBase | str:
        response = await self.fetch(request)
        return response if response is not None else await self.get_response(request)


class AsyncUpdateCacheMiddleware(_AsyncCacheMiddleware):
    """Django's update policy using native ``aset()``; place first in MIDDLEWARE."""

    async def __call__(self, request: HttpRequest) -> HttpResponseBase | str:
        return await self.update(request, await self.get_response(request))


class AsyncCacheMiddleware(_AsyncCacheMiddleware):
    """Combined native middleware for sites without intermediate Vary changes."""

    async def __call__(self, request: HttpRequest) -> HttpResponseBase | str:
        response = await self.fetch(request)
        if response is not None:
            return response
        return await self.update(request, await self.get_response(request))
