"""Opt-in redis.asyncio cache with standalone, Sentinel and Cluster connections."""

from redis.asyncio import Redis, RedisCluster
from redis.asyncio.connection import BlockingConnectionPool, ConnectionPool
from redis.asyncio.retry import Retry
from redis.asyncio.sentinel import Sentinel
from redis.backoff import NoBackoff

from ._backend import NativeCache

__all__ = ["AsyncRedisCache"]


class AsyncRedisCache(NativeCache):
    """Native async Django cache API, with one client per worker lifespan.

    Use a separate Django RedisCache or django-redis alias for synchronous
    callers. Default serialization and key formatting are compatible with both.
    This backend does not implement django-redis's client or compressor plugins.
    """

    client_class = Redis
    pool_class = ConnectionPool
    blocking_pool_class = BlockingConnectionPool
    sentinel_class = Sentinel
    cluster_class = RedisCluster
    retry_class = Retry
    no_backoff_class = NoBackoff
