# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-03

### Added

- `codecs.CompressedCodec`: compresses another codec's values of at least
  `min_size` bytes with zlib, or with zstd where the standard library has
  `compression.zstd` (CPython 3.14). Compressed values start with the byte
  `0xC1` and a format byte; smaller values, integers and values written
  before it was enabled are read as before. Django's `RedisCache` cannot
  read compressed values.
- `singleflight.aget_or_set()`: Django's `aget_or_set` contract, with
  concurrent misses for the same key in one event loop sharing one
  computation. A cancelled caller does not cancel it; a failure reaches every
  waiting caller and caches nothing.
- `cache_lifespan(alias, *, ping=False)`: with `ping=True`, a server that
  cannot be reached fails startup with the driver's connection error, and
  the backend is closed.
- `tools/benchmark.py`, which times the operations with Django's
  `RedisCache` and with both callback modes and both codecs.
- Documentation in `docs/`: security (pickle, TLS and ACL users,
  credentials, `aclear()`), what is tested on which servers, timeouts and
  pools, retries, closing, health checks, OpenTelemetry, the drivers' locks,
  migrating from Django's `RedisCache` and django-redis, when
  `callback_mode="inline"` is safe, and recipes for namespace invalidation
  with `version=` and hashed keys.
- CI runs the Sentinel and Cluster tests on Redis 7.4 and Valkey 8.1.

### Changed

- `MsgspecCodec` stores integers as Redis integers and declares
  `supports_integer_operations`, so `aincr` and `adecr` work with it. A value
  is read as an integer only if it is `-?[0-9]+` in full. **Upgrade note:**
  0.1.0 stored the integers 0 to 127 as one MessagePack byte; of those, 48 to
  57 were the bytes `b"0"` to `b"9"` and now read back as 0 to 9. Projects
  that cached values with 0.1.0's `MsgspecCodec` should change `VERSION` or
  `KEY_PREFIX` when they upgrade.
- `AsyncValkeyCache` lets a cancelled Cluster batch (`aget_many`,
  `aset_many`, `adelete_many`) finish reading its replies in a task of its
  own. valkey-py 6.1 does not return the node's connection when the pipeline
  is cancelled, so each cancellation used up one of the node's
  `max_connections` until the node refused every command.
- Configuration errors name the rejected value, for example
  `callback_mode must be thread or inline; got 'threads'.`
- The topology Compose project has health checks and joins the Cluster
  itself, so `docker compose up --wait` returns when the services are ready.

## [0.1.0] - 2026-10-03

The first release, extracted from django-aiodrf 0.0.2 so that projects that
do not use Django REST framework can use it.

### Added

- `AsyncRedisCache` (redis-py) and `AsyncValkeyCache` (valkey-py): Django
  cache backends implementing the asynchronous cache API natively, for
  standalone, Sentinel and Cluster servers.
- `AsyncCacheMiddleware`, `AsyncFetchFromCacheMiddleware` and
  `AsyncUpdateCacheMiddleware`: Django's page-cache policy on these
  backends.
- `cache_lifespan()`: a backend owned by one lifespan, closed on exit.
- `codecs.MsgspecCodec` (`msgspec` extra) and
  `django_valkey.LifespanConnectionFactory` (`django-valkey` extra).
