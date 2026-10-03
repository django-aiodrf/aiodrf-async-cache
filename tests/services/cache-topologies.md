# Cache topology tests

These disposable Linux/Docker services use host networking and bind only to
loopback. Ports 17631–17636 and Cluster bus ports 27631–27633 must be free.
There are no production credentials, durable volumes or external endpoints.
Do not substitute a production Sentinel URL: the test explicitly promotes a replica.

```console
docker compose -f tests/services/cache-topologies.yaml up -d --wait
AIODRF_TEST_CACHE_TOPOLOGIES=1 uv run --no-sync pytest -q tests/services/test_topologies.py
docker compose -f tests/services/cache-topologies.yaml down -v
```

`--wait` returns once every service's health check passes: the `cluster`
service has joined the three nodes and each of them reports
`cluster_state:ok`, the replica's link to the primary is up, and Sentinel
lists the replica. CI runs the same commands in the `topologies` job.

Install both `redis` and `valkey` extras in the test environment. The cases
cover both clients' Sentinel and Cluster operations, controlled failover, and
cancelled Cluster batches. Cross-slot batches include nested values, cached
None, missing values, TTL, add-if-absent, counters, deletion and zero expiry.
Each test owns a random prefix. Shutdown removes only this Compose project's
disposable containers and data.

To repeat against Valkey Server, stop the Redis profile first and use:

```console
CACHE_IMAGE=valkey/valkey:8.1-alpine CACHE_SERVER=valkey-server docker compose -f tests/services/cache-topologies.yaml up -d --wait
AIODRF_TEST_CACHE_TOPOLOGIES=1 uv run --no-sync pytest -q tests/services/test_topologies.py
docker compose -f tests/services/cache-topologies.yaml down -v
```

This small topology verifies client contracts, not quorum loss, network partitions,
Cluster resharding, automatic primary failure detection, TLS/ACL policy or an HA
deployment. The failover test waits for replication and promotion, then disconnects
the old data pool to verify discovery. It does not assert zero interruption or
exactly-once delivery across an unplanned failure.
