# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/).

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
