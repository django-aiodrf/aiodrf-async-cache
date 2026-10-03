# Migrating from Django's RedisCache and django-redis

## Compatible behaviour

- **Keys.** The backends build keys with Django's `make_key()`
  (`KEY_PREFIX:VERSION:key`) and honour `KEY_FUNCTION`, as Django's
  `RedisCache` and django-redis do.
- **Values.** The default codec is Django's `RedisSerializer`: pickle, with
  plain integers stored as Redis integers, as django-redis's default client
  also does. `tests/services/test_redis.py` writes `None`, booleans,
  integers, floats, text, bytes and a nested dict with the native backend
  and reads them with Django's `RedisCache` and django-redis, and the other
  way round.
- **Counters.** `aincr`/`adecr` use the server's `INCRBY` on the same
  integer encoding, so a counter can be shared between the aliases.
- **Timeouts and versions** follow Django's cache contract.

A native alias and a synchronous alias with the same `LOCATION`,
`KEY_PREFIX`, `VERSION` and codec therefore read each other's entries.

## Incompatibilities

- **django-redis compressors and serializers.** Values written with
  `COMPRESSOR` or with its JSON or msgpack serializers are not readable by
  the default codec. `CompressedCodec` is not django-redis's compression
  format either.
- **django-redis client plugins** (`CLIENT_CLASS`, sharded and Herd
  clients) and its extra methods (`keys()`, `delete_pattern()`, `ttl()`,
  `lock()`, `iter_keys()`). Use `cache.async_client` for server commands.
- **Synchronous callers.** The native backends implement only the async
  methods. DRF's throttles, `cache_page` on synchronous views, the admin,
  sessions with the cache backend, template fragment caching and any other
  code calling `cache.get()` need a second alias that uses Django's
  `RedisCache` (or django-redis) on the same server:

```python
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://cache:6379/1",
        "KEY_PREFIX": "myapp",
    },
    "native": {
        "BACKEND": "aiodrf_async_cache.redis.AsyncRedisCache",
        "LOCATION": "redis://cache:6379/1",
        "KEY_PREFIX": "myapp",
    },
}
```

The native alias is opened by a lifespan (`cache_lifespan("native")`), not
through `caches["native"]` in each request.

## Steps

1. Add the native alias next to the existing one, with the same `LOCATION`,
   `KEY_PREFIX`, `VERSION` and the default codec.
2. Move async code to the native alias. Both aliases keep reading and writing
   the same entries during the change.
3. Change the codec, or enable compression, only once no synchronous alias
   reads those keys, and with a new `KEY_PREFIX` or `VERSION`: Django's
   `RedisCache` cannot read `MsgspecCodec` or compressed values.
