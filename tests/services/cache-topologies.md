# Cache topology tests

These disposable Linux/Docker services use host networking and bind only to
loopback. Ports 17631–17636 and Cluster bus ports 27631–27633 must be free.
There are no production credentials, durable volumes or external endpoints.
Do not substitute a production Sentinel URL: the test explicitly promotes a replica.

```console
docker compose -f tests/services/cache-topologies.yaml up -d
docker compose -f tests/services/cache-topologies.yaml exec -T cluster-one redis-cli --cluster create 127.0.0.1:17631 127.0.0.1:17632 127.0.0.1:17633 --cluster-replicas 0 --cluster-yes
AIODRF_TEST_CACHE_TOPOLOGIES=1 uv run --no-sync pytest -q tests/services/test_topologies.py
docker compose -f tests/services/cache-topologies.yaml down
```

Install both `redis` and `valkey` extras in the test environment. The six cases
cover both clients' Sentinel and Cluster operations, plus controlled failover.
Cross-slot batches include nested values, cached None, missing values, TTL,
add-if-absent, counters, deletion and zero expiry. Each test owns a random prefix.
Shutdown removes only this Compose project's disposable containers and data.

To repeat against Valkey Server, stop the Redis profile first and use:

```console
CACHE_IMAGE=valkey/valkey:9.0.6 CACHE_SERVER=valkey-server docker compose -f tests/services/cache-topologies.yaml up -d
docker compose -f tests/services/cache-topologies.yaml exec -T cluster-one valkey-cli --cluster create 127.0.0.1:17631 127.0.0.1:17632 127.0.0.1:17633 --cluster-replicas 0 --cluster-yes
AIODRF_TEST_CACHE_TOPOLOGIES=1 uv run --no-sync pytest -q tests/services/test_topologies.py
docker compose -f tests/services/cache-topologies.yaml down
```

This small topology verifies client contracts, not quorum loss, network partitions,
Cluster resharding, automatic primary failure detection, TLS/ACL policy or an HA
deployment. The failover test waits for replication and promotion, then disconnects
the old data pool to verify discovery. It does not assert zero interruption or
exactly-once delivery across an unplanned failure.
