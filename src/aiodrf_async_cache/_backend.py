"""Shared Django cache semantics for lifespan-owned Redis and Valkey clients."""

import asyncio
from collections.abc import Callable, Iterable
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import parse_qs, urlsplit

from django.core.cache.backends.base import DEFAULT_TIMEOUT, BaseCache, default_key_func
from django.core.cache.backends.redis import RedisSerializer
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from ._callbacks import awaits_inline, maybe_await, run_sync_and_await

# Check existence and increment in one server command, preserving expiration.
# A missing key must raise instead of Redis INCRBY creating a new counter.
_INCREMENT = """
if redis.call('exists', KEYS[1]) == 0 then return false end
redis.call('incrby', KEYS[1], ARGV[1])
return redis.call('get', KEYS[1])
"""


_CLUSTER_RETRY_OPTIONS = frozenset(
    {"retry", "cluster_error_retry_attempts", "connection_error_retry_attempts"}
)


class NativeCache(BaseCache):
    """Async-only cache; concrete drivers supply client and pool classes.

    Standard synchronous middleware uses a separate synchronous alias. There is
    no implicit async-to-sync loop, process-global pool or shared client registry.
    """

    is_async = True
    client_class: Any
    pool_class: Any
    blocking_pool_class: Any
    sentinel_class: Any
    cluster_class: Any
    retry_class: Any
    no_backoff_class: Any
    # Run Cluster pipelines in a task of their own that a cancelled caller
    # does not interrupt, for a driver that leaks the node's connection then.
    _shield_cluster_pipelines = False

    def __init__(self, server: str, params: dict[str, Any]) -> None:
        options = dict(params.get("OPTIONS", {}))
        super().__init__(params)
        if not isinstance(server, str) or not server:
            raise ImproperlyConfigured(
                "LOCATION must be one URL, or a Sentinel service name."
            )
        self._server = server
        self.topology = options.pop("topology", "standalone")
        if self.topology not in {"standalone", "sentinel", "cluster"}:
            raise ImproperlyConfigured(
                "topology must be standalone, sentinel or cluster; "
                f"got {self.topology!r}."
            )
        self._sentinels = options.pop("sentinels", None)
        self._sentinel_kwargs = options.pop("sentinel_kwargs", {})
        if self.topology == "sentinel" and not self._sentinels:
            raise ImproperlyConfigured(
                "Sentinel requires sentinels=[(host, port), ...]; LOCATION is the service name."
            )
        self.callback_mode = options.pop("callback_mode", "thread")
        if self.callback_mode not in {"thread", "inline"}:
            raise ImproperlyConfigured(
                f"callback_mode must be thread or inline; got {self.callback_mode!r}."
            )
        serializer = options.pop("serializer", RedisSerializer)
        if isinstance(serializer, str):
            serializer = import_string(serializer)
        self._async_serializer = (
            serializer() if isinstance(serializer, type) else serializer
        )
        if not all(
            callable(getattr(self._async_serializer, name, None))
            for name in ("dumps", "loads")
        ):
            raise ImproperlyConfigured(
                "serializer must provide dumps() and loads(); "
                f"got {type(self._async_serializer).__qualname__}."
            )
        pool_class = options.pop("async_pool_class", self.blocking_pool_class)
        if isinstance(pool_class, str):
            pool_class = import_string(pool_class)
        if not isinstance(pool_class, type) or not issubclass(
            pool_class, self.pool_class
        ):
            # redis-py has a sync and an async class of the same name.
            got = (
                f"{pool_class.__module__}.{pool_class.__qualname__}"
                if isinstance(pool_class, type)
                else repr(pool_class)
            )
            raise ImproperlyConfigured(
                f"async_pool_class must be the driver's async ConnectionPool; got {got}."
            )
        pool_options = {"max_connections": options.pop("max_connections", 20)}
        if self.topology == "standalone" and issubclass(
            pool_class, self.blocking_pool_class
        ):
            pool_options["timeout"] = 2
        pool_options.update(options.pop("async_pool_kwargs", {}))
        if self.topology != "standalone" and pool_class is not self.blocking_pool_class:
            raise ImproperlyConfigured(
                "async_pool_class is only supported for standalone connections."
            )
        self._async_pool_class: Any = pool_class
        self._async_options = options
        self._async_options.update(pool_options)
        if self._async_options.get("decode_responses") or parse_qs(
            urlsplit(server).query
        ).get("decode_responses", ["false"])[0].lower() not in {
            "false",
            "0",
            "no",
            "f",
        }:
            raise ImproperlyConfigured("Native caches require decode_responses=False.")
        self._async_client: Any = None
        self._resources = AsyncExitStack()
        # The first event loop that uses the cache owns it. Construction
        # opens nothing, so Django's system checks, which build every alias
        # outside any loop, can construct it.
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._closed = False
        self._pipelines: set[asyncio.Task[Any]] = set()

    @property
    def async_client(self) -> Any:
        """The raw native client for explicit pipelines and server commands."""
        if self._closed:
            raise RuntimeError("This async cache lifespan is closed.")
        loop = asyncio.get_running_loop()
        if self._owner_loop is None:
            self._owner_loop = loop
        elif self._owner_loop is not loop:
            raise RuntimeError("An async cache cannot be shared between event loops.")
        if self._async_client is None:
            self._async_client = self._create_client()
        return self._async_client

    def _create_client(self) -> Any:
        if self.topology == "cluster":
            options = self._async_options
            if options.keys().isdisjoint(_CLUSTER_RETRY_OPTIONS):
                # The drivers retry cluster commands by default; a replayed
                # EVAL or SET NX would apply twice. Standalone and Sentinel
                # connections do not retry either.
                options = {
                    **options,
                    "retry": self.retry_class(self.no_backoff_class(), 0),
                }
            client = self.cluster_class.from_url(self._server, **options)
        elif self.topology == "sentinel":
            sentinel = self.sentinel_class(
                self._sentinels,
                sentinel_kwargs=self._sentinel_kwargs,
                **self._async_options,
            )
            # Both drivers expose discovery clients; own their pools as well.
            for node in sentinel.sentinels:
                self._resources.push_async_callback(node.aclose)
            client = sentinel.master_for(self._server)
        else:
            pool = self._async_pool_class.from_url(self._server, **self._async_options)
            if pool.connection_kwargs.get("decode_responses"):
                raise ImproperlyConfigured(
                    "Native caches require decode_responses=False."
                )
            self._resources.push_async_callback(pool.disconnect)
            client = self.client_class(connection_pool=pool)
        self._resources.push_async_callback(client.aclose)
        return client

    async def _callback(
        self, callback: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        if awaits_inline(callback) or self.callback_mode == "inline":
            return await maybe_await(callback(*args, **kwargs))
        return await run_sync_and_await(callback, *args, **kwargs)

    def _standard_codec(self) -> bool:
        return type(self._async_serializer) is RedisSerializer and not {
            "dumps",
            "loads",
        }.intersection(vars(self._async_serializer))

    async def _codec_many(self, method: str, values: list[Any]) -> list[Any]:
        if not values:
            return []
        callback = getattr(self._async_serializer, method)
        if not self._standard_codec():
            return [await self._callback(callback, value) for value in values]

        def convert() -> list[Any]:
            return [callback(value) for value in values]

        return await self._callback(convert)

    async def _pipeline(self, fill: Callable[[Any], None]) -> list[Any]:
        async def execute() -> list[Any]:
            async with self.async_client.pipeline(
                transaction=self.topology != "cluster"
            ) as pipeline:
                fill(pipeline)
                return await pipeline.execute()

        if self.topology != "cluster" or not self._shield_cluster_pipelines:
            return await execute()
        # The task finishes reading its replies (within socket_timeout) and
        # returns its connection after the caller is cancelled.
        task = asyncio.create_task(execute())
        self._pipelines.add(task)
        task.add_done_callback(self._pipelines.discard)
        return await asyncio.shield(task)

    async def amake_key(self, key: Any, version: int | None = None) -> Any:
        """Await KEY_FUNCTION and preserve Django's make_key/validate_key hooks."""
        if (
            self.key_func is default_key_func
            and type(self).make_key is BaseCache.make_key
        ):
            key = self.make_key(key, version=version)
        elif type(self).make_key is BaseCache.make_key:
            key = await self._callback(
                self.key_func,
                key,
                self.key_prefix,
                self.version if version is None else version,
            )
        else:
            key = await self._callback(self.make_key, key, version=version)
        if type(self).validate_key is BaseCache.validate_key:
            self.validate_key(key)
        else:
            await self._callback(self.validate_key, key)
        return key

    def get_backend_timeout(self, timeout: Any = DEFAULT_TIMEOUT) -> int | None:
        if timeout is DEFAULT_TIMEOUT:
            timeout = self.default_timeout
        return None if timeout is None else max(0, int(timeout))

    async def aget(
        self, key: Any, default: Any | None = None, version: int | None = None
    ) -> Any:
        key = await self.amake_key(key, version=version)
        value = await self.async_client.get(key)
        return (
            default
            if value is None
            else await self._callback(self._async_serializer.loads, value)
        )

    async def aset(
        self,
        key: Any,
        value: Any,
        timeout: Any = DEFAULT_TIMEOUT,  # noqa: ASYNC109 -- Django cache TTL
        version: int | None = None,
    ) -> None:
        key = await self.amake_key(key, version=version)
        timeout = self.get_backend_timeout(timeout)
        if timeout == 0:
            await self.async_client.delete(key)
        else:
            await self.async_client.set(
                key,
                await self._callback(self._async_serializer.dumps, value),
                ex=timeout,
            )

    async def aadd(
        self,
        key: Any,
        value: Any,
        timeout: Any = DEFAULT_TIMEOUT,  # noqa: ASYNC109 -- Django cache TTL
        version: int | None = None,
    ) -> bool:
        key = await self.amake_key(key, version=version)
        timeout = self.get_backend_timeout(timeout)
        if timeout == 0:
            return not await self.async_client.exists(key)
        return bool(
            await self.async_client.set(
                key,
                await self._callback(self._async_serializer.dumps, value),
                ex=timeout,
                nx=True,
            )
        )

    async def atouch(
        self,
        key: Any,
        timeout: Any = DEFAULT_TIMEOUT,  # noqa: ASYNC109 -- Django cache TTL
        version: int | None = None,
    ) -> bool:
        key = await self.amake_key(key, version=version)
        timeout = self.get_backend_timeout(timeout)
        if timeout is None:
            return bool(await self.async_client.persist(key))
        return bool(await self.async_client.expire(key, timeout))

    async def adelete(self, key: Any, version: int | None = None) -> bool:
        return bool(
            await self.async_client.delete(await self.amake_key(key, version=version))
        )

    async def ahas_key(self, key: Any, version: int | None = None) -> bool:
        return bool(
            await self.async_client.exists(await self.amake_key(key, version=version))
        )

    async def aget_many(
        self, keys: Iterable[Any], version: int | None = None
    ) -> dict[Any, Any]:
        keys = list(keys)
        if not keys:
            return {}
        encoded_keys = [await self.amake_key(key, version=version) for key in keys]
        if self.topology == "cluster":

            def fill(pipeline: Any) -> None:
                for key in encoded_keys:
                    pipeline.get(key)

            values = await self._pipeline(fill)
        else:
            values = await self.async_client.mget(encoded_keys)
        found = [
            (key, value)
            for key, value in zip(keys, values, strict=True)
            if value is not None
        ]
        decoded = await self._codec_many("loads", [value for _, value in found])
        return dict(zip((key for key, _ in found), decoded, strict=True))

    async def aset_many(
        self,
        data: dict[Any, Any],
        timeout: Any = DEFAULT_TIMEOUT,  # noqa: ASYNC109 -- Django cache TTL
        version: int | None = None,
    ) -> list[Any]:
        if not data:
            return []
        timeout = self.get_backend_timeout(timeout)
        if (
            self._standard_codec()
            and self.key_func is default_key_func
            and type(self).amake_key is NativeCache.amake_key
            and type(self).make_key is BaseCache.make_key
            and type(self).validate_key is BaseCache.validate_key
        ):
            keys = [await self.amake_key(key, version=version) for key in data]
            values = await self._codec_many("dumps", list(data.values()))
            encoded = dict(zip(keys, values, strict=True))
        else:
            # Custom keys/codecs retain their per-item ordering and awaitables.
            encoded = {
                await self.amake_key(key, version=version): await self._callback(
                    self._async_serializer.dumps, value
                )
                for key, value in data.items()
            }

        def fill(pipeline: Any) -> None:
            # Each SET carries its TTL, including on cross-slot Cluster batches.
            for key, value in encoded.items():
                if timeout == 0:
                    pipeline.delete(key)
                else:
                    pipeline.set(key, value, ex=timeout)

        await self._pipeline(fill)
        return []

    async def adelete_many(
        self, keys: Iterable[Any], version: int | None = None
    ) -> None:
        encoded = [await self.amake_key(key, version=version) for key in keys]
        if encoded:

            def fill(pipeline: Any) -> None:
                for key in encoded:
                    pipeline.delete(key)

            await self._pipeline(fill)

    async def aincr(self, key: Any, delta: int = 1, version: int | None = None) -> Any:
        if type(self._async_serializer) is not RedisSerializer and not getattr(
            self._async_serializer, "supports_integer_operations", False
        ):
            raise NotImplementedError(
                "This codec does not declare raw integer counter compatibility."
            )
        key = await self.amake_key(key, version=version)
        value = await self.async_client.eval(_INCREMENT, 1, key, delta)
        if value is None:
            raise ValueError(f"Key '{key}' not found.")
        return int(value)

    async def aclear(self) -> Any:
        """Clear the selected database, including keys outside KEY_PREFIX."""
        if self.topology == "cluster":
            results = await self.async_client.flushdb(
                target_nodes=self.cluster_class.PRIMARIES
            )
            return all(results.values()) if isinstance(results, dict) else bool(results)
        return bool(await self.async_client.flushdb())

    async def aget_or_set(
        self,
        key: Any,
        default: Any | None,
        timeout: Any = DEFAULT_TIMEOUT,  # noqa: ASYNC109 -- Django cache TTL
        version: int | None = None,
    ) -> Any:
        value = await self.aget(key, self._missing_key, version=version)
        if value is not self._missing_key:
            return value
        if callable(default):
            default = await self._callback(default)
        await self.aadd(key, default, timeout=timeout, version=version)
        return await self.aget(key, default, version=version)

    async def aclose(self, **kwargs: Any) -> None:
        if self._owner_loop not in (None, asyncio.get_running_loop()):
            raise RuntimeError("Close the async cache on its owning event loop.")
        self._closed = True
        try:
            await self._resources.aclose()
        finally:
            self._async_client = None
