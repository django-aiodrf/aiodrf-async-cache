# Servers, connections and operations

## Test coverage

| Setup | Where it runs |
| --- | --- |
| Standalone Redis 7.4 and Valkey 8.1 | CI (`services` job), every push and pull request |
| Sentinel and Cluster on Redis 7.4 and Valkey 8.1 | CI (`topologies` job, `tests/services/cache-topologies.yaml`) |
| Standalone Valkey 9.0 | Locally only |
| TLS, ACL users, Redis 8, managed services | Not tested |

CI installs the latest redis-py and valkey-py releases (redis-py 8.1 and
valkey-py 6.1 at the time of writing); the minimum versions in
`pyproject.toml` are not tested.

The two drivers differ in ways that matter to users:

- Both ship the same asyncio lock (see [Locks](#locks)).
- redis-py 8's `ConnectionPool.get_connection_count()` has no valkey-py
  equivalent.
- redis-py 8 has its own OpenTelemetry metrics; valkey-py has none.
- valkey-py 6.1 does not return a Cluster node's connection when a pipeline
  is cancelled. `AsyncValkeyCache` therefore lets a Cluster batch
  (`aget_many`, `aset_many`, `adelete_many`) finish in a task of its own when
  its caller is cancelled; the caller still gets `CancelledError` at once.

## Timeouts and the connection pool

Three limits apply, and each raises a different error (the driver's
`TimeoutError` or `ConnectionError`; `tests/test_timeouts.py`):

| Option | Limits | Error when exceeded |
| --- | --- | --- |
| `socket_connect_timeout` | opening a TCP connection | `TimeoutError("Timeout connecting to server")` |
| `socket_timeout` | waiting for one reply | `TimeoutError("Timeout reading from ...")` |
| the pool's `timeout` | waiting for a free connection | `ConnectionError("No connection available.")` |

Both drivers default to 5 seconds for each socket timeout (redis-py 8.1,
valkey-py 6.1). `socket_timeout` applies to every reply, including a large
`aget_many`'s, so lowering it fails faster on a dead server but also fails
slow replies.

A standalone backend uses the driver's `BlockingConnectionPool` with
`max_connections=20` and `timeout=2`: the 21st concurrent command waits up
to two seconds for a connection. Change them with `max_connections` and
`async_pool_kwargs`:

```python
"OPTIONS": {
    "socket_connect_timeout": 1,
    "socket_timeout": 1,
    "max_connections": 50,
    "async_pool_kwargs": {"timeout": 0.5},
}
```

`async_pool_class="redis.asyncio.ConnectionPool"` (or
`"valkey.asyncio.ConnectionPool"`) selects the non-blocking pool, which
raises `ConnectionError("Too many connections")` at once when all
connections are in use. Sentinel and Cluster use the drivers' own pools,
which do not wait either; `max_connections` applies to the Sentinel primary
and to each Cluster node.

With redis-py 8, the pool reports its connections:

```python
pool = cache.async_client.connection_pool
pool.get_connection_count()  # [(idle, attributes), (in use, attributes)]
```

valkey-py has no public equivalent.

## Retries are off

No command is retried by default: a command whose reply is lost after the
server ran it (an `INCRBY` in `aincr`, a `SET NX` in `aadd`) would run
twice. Standalone and Sentinel connections use the drivers' default of no
retries, and the backend turns off the drivers' default Cluster retries.

To retry, pass the driver's retry policy, knowing that writes can then apply
twice:

```python
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError, TimeoutError

"OPTIONS": {
    "retry": Retry(ExponentialBackoff(cap=0.5, base=0.05), 2),
    "retry_on_error": [ConnectionError, TimeoutError],
}
```

For Cluster, `retry` (or the driver's `cluster_error_retry_attempts`)
replaces the backend's no-retry default.

## Closing

`aclose()` closes the client and disconnects the pool. It does not wait for
commands that other tasks already started: a command reading a reply fails
with the driver's `ConnectionError`, and one that was waiting for a free
connection can still take a connection released by such a failure and
reconnect it (`tests/services/resilience.py`). Close a backend after its
users are done, as an ASGI server does when it runs lifespan shutdown after
the last request.

## Health checks

`cache_lifespan("default", ping=True)` sends `PING` before the application
starts, so an unreachable server fails startup instead of the first request.
It raises the driver's connection error and closes the backend.

django-health-check's cache check uses Django's synchronous `caches`
registry, which an async-only backend cannot serve. A health view that
pings the lifespan's backend is three lines:

```python
from aiodrf_asgi_lifespan.asgi import get_lifespan_state
from django.http import HttpResponse

from aiodrf_async_cache.redis import AsyncRedisCache


async def cache_health(request):
    await get_lifespan_state(request, AsyncRedisCache).async_client.ping()
    return HttpResponse("ok")
```

A failed ping raises, and Django answers 500.

## OpenTelemetry

The backends use the drivers' clients unchanged, so driver-level
instrumentation sees their commands:

- `opentelemetry-instrumentation-redis` instruments redis-py's asyncio
  client: call `RedisInstrumentor().instrument()` at startup, before the
  first backend is used.
- redis-py 8 records its own metrics once enabled:

  ```python
  from redis.observability.config import OTelConfig
  from redis.observability.providers import get_observability_instance

  get_observability_instance().init(OTelConfig())
  ```

  It uses the global `MeterProvider`, which must be set first.

valkey-py has neither.

## Locks

Both drivers ship an asyncio lock (`SET NX PX` with a random token, release
by a Lua compare-and-delete, `extend()`, `blocking_timeout`):

```python
from redis.exceptions import LockError  # valkey.exceptions for Valkey

try:
    async with cache.async_client.lock(
        "myapp:lock:nightly-report", timeout=30, blocking_timeout=5
    ):
        await build_report()
except LockError:
    ...  # not acquired within 5 seconds, or lost before release
```

- The name goes to the server as given: `KEY_PREFIX` and `make_key` do not
  apply, so give it a prefix of your own.
- It is a lock on one server, not Redlock. After a failover to a replica
  that had not received it, two holders can exist.
- `timeout` bounds how long the lock is held if its holder dies; work longer
  than that needs `extend()`.
- In a Cluster the name decides the node, like any key.
