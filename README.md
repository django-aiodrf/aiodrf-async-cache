# aiodrf-async-cache

Native async Redis and Valkey backends for Django's cache framework, built on
the async clients of redis-py and valkey-py. It requires Python 3.12+ and
Django 5.2+, and depends on neither Django REST framework nor django-aiodrf.

```console
pip install "aiodrf-async-cache[redis]"    # redis-py
pip install "aiodrf-async-cache[valkey]"   # valkey-py
```

## Usage

```python
# settings.py
CACHES = {
    "default": {
        "BACKEND": "aiodrf_async_cache.redis.AsyncRedisCache",
        "LOCATION": "redis://localhost:6379/1",
        "TIMEOUT": 300,
        "KEY_PREFIX": "myapp",
        "VERSION": 1,
    },
}
```

For Valkey, use `aiodrf_async_cache.valkey.AsyncValkeyCache` with a
`valkey://` URL; it does not need django-valkey.

```python
from django.core.cache import caches


async def cached_total():
    cache = caches["default"]
    await cache.aset("total", 42)
    return await cache.aget("total")
```

The backends subclass Django's `BaseCache` and implement its
[asynchronous API](https://docs.djangoproject.com/en/6.1/topics/cache/#asynchronous-support):
`aadd`, `aget`, `aset`, `atouch`, `adelete`, `aget_many`, `aset_many`,
`adelete_many`, `ahas_key`, `aget_or_set`, `aincr`, `adecr`,
`aincr_version`, `adecr_version`, `aclear` and `aclose`, with Django's
parameter names, defaults, and key, version and timeout rules. A missing
value differs from a cached `None`, and `aset_many` returns the keys it could
not set. Without a timeout the entry uses `TIMEOUT`; `None` keeps it, and
zero or a negative timeout expires it at once. `KEY_FUNCTION` and a
subclass's key validation apply as in Django.

Configuration and backend lookup stay in Django, as its
[cache design](https://docs.djangoproject.com/en/6.1/misc/design-philosophies/#cache-design-philosophy)
expects: there is no service registry and no patch. The default codec is
Django's `RedisSerializer`, so integers stay usable by the server's atomic
increments. Codec work for a batch runs in one worker call, and callbacks
keep their order.

The backends are async only. Django's synchronous cache middleware, template
fragment caching and other synchronous callers need a separate synchronous
cache alias. Django 6.1's cache middleware is not asynchronous; the
middleware in `aiodrf_async_cache.middleware` applies Django's page-cache
policy with these backends.

## Connection ownership

A backend's client belongs to the first event loop that uses it: close it on
that loop with `await cache.aclose()` once its users are done, and do not
share a backend between event loops. `cache_lifespan()` builds a backend of
its own from a Django cache alias and closes it on exit, for resources that a
worker owns:

```python
from contextlib import asynccontextmanager

from aiodrf_async_cache.lifespan import cache_lifespan


@asynccontextmanager
async def resources():
    async with cache_lifespan("default") as cache:
        yield cache
```

With `cache_lifespan("default", ping=True)` the backend sends `PING` before
it is yielded, so a server that cannot reach Redis fails at startup with the
driver's connection error, not on the first request. The default, `False`,
opens nothing until the first command.

The context manager works with any lifespan owner. `AsyncCacheMiddleware`
reads the cache from the lifespan of
[aiodrf-asgi-lifespan](https://github.com/django-aiodrf/aiodrf-asgi-lifespan)
(the `lifespan` extra) by default: set
`DJANGO_LIFESPAN = "project.lifecycle.resources"` and serve its ASGI
application. When the lifespan holds several clients, subclass the
middleware and return the cache from `get_cache(request)`; such a subclass
does not need the lifespan package.

## Options and limits

`OPTIONS` accepts:

- `callback_mode`: `"thread"` (the default) runs synchronous callbacks in a
  worker thread; `"inline"` runs them on the event loop, for callbacks that
  never block.
- `serializer`: a dotted path, a class or an object with `dumps` and `loads`.
- `max_connections` (20), `async_pool_class` and `async_pool_kwargs`.

Other options go to the driver's async client. Async callbacks are awaited,
and so is an awaitable that a synchronous callback returns. Decoded text
responses (`decode_responses`) are refused: the codecs need bytes.

`topology="sentinel"` needs `sentinels=[(host, port), ...]` and takes the
service name as `LOCATION`; `sentinel_kwargs` configures the discovery
connections. `topology="cluster"` takes a seed URL as `LOCATION` and sends
batches as separate commands per key, so their keys may be in different
slots; such batches are not atomic. `aclear()` empties the whole selected
database, or every primary in a cluster, whatever the `KEY_PREFIX`: see
[Security](docs/security.md).

`aincr` and `adecr` need a codec that stores integers as Redis integers:
Django's `RedisSerializer`, or a codec whose `supports_integer_operations`
attribute is true.

## Codecs

The default codec, Django's `RedisSerializer`, uses pickle: only connect to a
cache that only trusted code can write to.

`aiodrf_async_cache.codecs.MsgspecCodec` (the `msgspec` extra) stores
MessagePack, optionally decoded into a msgspec type:

```python
from aiodrf_async_cache.codecs import MsgspecCodec

CACHES["default"]["OPTIONS"] = {"serializer": MsgspecCodec()}
```

It stores integers as Redis integers, so `aincr` and `adecr` work with it.
Its values are not readable by Django's `RedisCache`, and it does not suit
the page-cache middleware, which caches response objects.

`CompressedCodec` wraps another codec and compresses its values of at least
`min_size` bytes:

```python
from django.core.cache.backends.redis import RedisSerializer

from aiodrf_async_cache.codecs import CompressedCodec

CACHES["default"]["OPTIONS"] = {
    "serializer": CompressedCodec(RedisSerializer(), compressor="zlib", min_size=1024)
}
```

- A compressed value starts with the byte `0xC1`, which MessagePack never
  uses and pickle and Redis integers never start with, and a format byte
  (1 for zlib, 2 for zstd). Smaller values are stored as the inner codec
  wrote them, so values written before compression was enabled stay
  readable.
- Integers are never compressed, so `aincr` works when the inner codec
  supports it.
- `compressor="zstd"` needs the standard library's `compression.zstd`
  (CPython 3.14 or later); elsewhere it raises `ImproperlyConfigured`.
- Django's `RedisCache` cannot read compressed values: do not enable it on
  entries a synchronous alias reads.

## Single-flight `aget_or_set`

When a popular key expires, every concurrent request computes it again.
`aiodrf_async_cache.singleflight.aget_or_set` has the signature of Django's
`aget_or_set` and lets concurrent misses for the same key share one
computation:

```python
from aiodrf_async_cache.singleflight import aget_or_set


async def load_user():
    return await User.objects.aget(pk=42)


user = await aget_or_set(cache, "user:42", load_user, timeout=300)
```

- `default` is a value or a callable; what a coroutine function returns is
  awaited.
- Calls share a computation when they use the same cache object and the
  same final key (prefix, version and key). Callers that share one receive
  the same object.
- A cancelled caller does not cancel the computation, which still stores its
  value. If the computation raises, every waiting caller gets the exception,
  nothing is cached, and the next call computes again.
- Deduplication is per event loop, so each worker process computes a
  missing value once at a time. It works with any cache that implements
  Django's async API.

The `django-valkey` extra provides
`aiodrf_async_cache.django_valkey.LifespanConnectionFactory`, which gives
django-valkey's own backend pools owned by one lifespan. It is independent of
the native backends.

## Performance

The network I/O is native, but synchronous codec calls run in a worker
thread by default, and that hop costs more than the codec on small values.
[Performance](docs/performance.md) explains when
`callback_mode="inline"` is safe. `tools/benchmark.py` measures both modes
with both codecs.

## Further documentation

- [Security](docs/security.md): pickle, TLS and ACL users, credentials,
  `aclear()`.
- [Servers, connections and operations](docs/operations.md): test
  coverage, timeouts and pools, retries, closing, health checks,
  OpenTelemetry, locks.
- [Migrating from Django's RedisCache and django-redis](docs/migration.md).
- [Performance](docs/performance.md): execution context and `callback_mode`.
- [Recipes](docs/recipes.md): namespace invalidation with `version=`,
  hashed keys.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).
