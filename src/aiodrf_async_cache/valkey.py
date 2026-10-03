"""Native Valkey cache backend using valkey-py async clients."""

from valkey.asyncio import Valkey, ValkeyCluster
from valkey.asyncio.connection import BlockingConnectionPool, ConnectionPool
from valkey.asyncio.retry import Retry
from valkey.asyncio.sentinel import Sentinel
from valkey.backoff import NoBackoff

from ._backend import NativeCache

__all__ = ["AsyncValkeyCache"]


class AsyncValkeyCache(NativeCache):
    """Native Valkey cache with awaited callbacks and Sentinel/Cluster support.

    Uses valkey-py's async driver, not django-valkey's client plugin system.
    Keep the vendor backend and LifespanConnectionFactory when its additional
    commands or serializers are required. Both use Django's cache interface.
    """

    client_class = Valkey
    pool_class = ConnectionPool
    blocking_pool_class = BlockingConnectionPool
    sentinel_class = Sentinel
    cluster_class = ValkeyCluster
    retry_class = Retry
    no_backoff_class = NoBackoff
