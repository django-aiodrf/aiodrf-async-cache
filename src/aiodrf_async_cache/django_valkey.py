"""Instance-owned pools for the optional django-valkey backend."""

from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django_valkey.async_cache.pool import AsyncConnectionFactory
from valkey.asyncio.connection import ConnectionPool

__all__ = ["LifespanConnectionFactory"]


class LifespanConnectionFactory(AsyncConnectionFactory):
    """Reuse pools within one lifespan without the vendor's global registry."""

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        if not issubclass(self.pool_cls, ConnectionPool):
            raise ImproperlyConfigured(
                "LifespanConnectionFactory requires an async Valkey pool."
            )
        self._owned_pools: dict[str, ConnectionPool] = {}

    def get_or_create_connection_pool(self, params: dict[str, Any]) -> ConnectionPool:
        # No await between lookup and publication: this factory belongs to one
        # event loop. Separate instances never share pools, even for one URL.
        url = params["url"]
        if url not in self._owned_pools:
            self._owned_pools[url] = self.get_connection_pool(params.copy())
        return self._owned_pools[url]
