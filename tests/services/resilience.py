"""Fault contracts shared by the isolated Redis and Valkey service profiles."""

import asyncio
from unittest.mock import patch

import pytest


async def check_reconnect(cache, client):
    """Drop only this test's sockets; subsequent calls must reuse the pool."""
    await cache.aset("counter", 40, timeout=30)
    pool = client.connection_pool
    previous = await client.client_id()
    await pool.disconnect()
    assert await cache.aincr("counter") == 41
    assert await client.client_id() != previous
    assert client.connection_pool is pool
    assert await cache.aget("counter") == 41


async def check_reply_loss(cache, client, error, *, retries, operation, permanent):
    """Lose an already-read reply, so even a failed write reached the server."""
    await cache.aset("counter", 40, timeout=30)
    parse_response = client.parse_response
    attempts = []
    command = "GET" if operation == "read" else "EVAL"

    async def lose_reply(connection, command_name, **kwargs):
        result = await parse_response(connection, command_name, **kwargs)
        if command_name == command:
            attempts.append(command_name)
            if permanent or len(attempts) == 1:
                raise error("simulated lost reply")
        return result

    call = cache.aget if operation == "read" else cache.aincr
    with patch.object(client, "parse_response", side_effect=lose_reply):
        if permanent or not retries:
            with pytest.raises(error, match="lost reply"):
                await call("counter")
        else:
            assert await cache.aget("counter") == 40
    assert len(attempts) == (retries + 1 if permanent else min(retries + 1, 2))
    # In particular, a write without retries must not become a second INCR.
    assert await cache.aget("counter") == (40 if operation == "read" else 41)


async def check_cancelled_backoff(cache, client, error, waiting):
    """Cancellation during the native driver's sleep releases its connection."""
    await cache.aset("counter", 40, timeout=30)
    parse_response = client.parse_response

    async def lose_reply(connection, command_name, **kwargs):
        result = await parse_response(connection, command_name, **kwargs)
        if command_name == "GET":
            raise error("simulated lost reply")
        return result

    with patch.object(client, "parse_response", side_effect=lose_reply):
        task = asyncio.create_task(cache.aget("counter"))
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await cache.aget("counter") == 40


BATCHES = {
    # The operation, and the reply read at which it stalls. MGET has one
    # reply; a MULTI/EXEC pipeline stalls after MULTI's, mid-transaction.
    "aget_many": (lambda cache: cache.aget_many(["one", "two"]), 1),
    "aset_many": (lambda cache: cache.aset_many({"one": 10, "two": 20}), 2),
    "adelete_many": (lambda cache: cache.adelete_many(["one", "two"]), 2),
}


async def check_cancelled_batch(cache, parser_class, in_use, operation, stall_at):
    """Cancel a batch while its replies are unread; no connection may leak.

    The stall replaces only the parser's read, so the driver's own handling of
    a cancelled read (disconnect, then release) is what runs.
    """
    await cache.aset_many({"one": 1, "two": 2}, timeout=30)
    read_response = parser_class.read_response
    reads = 0
    stalled = asyncio.Event()
    release = asyncio.Event()

    async def stall(self, *args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == stall_at:
            stalled.set()
            await release.wait()
        return await read_response(self, *args, **kwargs)

    with patch.object(parser_class, "read_response", stall):
        task = asyncio.create_task(BATCHES[operation][0](cache))
        try:
            await asyncio.wait_for(stalled.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # A shielded Cluster pipeline (valkey-py) still reads its replies once
        # the server answers; the driver's read was not cancelled.
        release.set()
        async with asyncio.timeout(2):
            await asyncio.gather(*cache._pipelines)
    assert in_use() == 0
    # A connection holding an unread reply would answer the next command
    # with it.
    await cache.aset_many({"one": 3, "two": 4}, timeout=30)
    assert await cache.aget_many(["one", "two"]) == {"one": 3, "two": 4}
    assert await cache.aincr("one") == 4
    assert in_use() == 0


async def check_close_with_commands_in_flight(cache, client, error):
    """aclose() returns while other tasks wait on the server or the pool."""
    key = cache.make_key("queue")
    # Two commands wait on the server (BLPOP never returns by itself) and hold
    # the pool's two connections; three cache reads wait for a connection.
    blocked = [asyncio.create_task(client.blpop([key], 0)) for _ in range(2)]
    pool = client.connection_pool
    async with asyncio.timeout(2):
        while len(pool._in_use_connections) < 2:  # noqa: ASYNC110 -- bounded polling of the driver's pool
            await asyncio.sleep(0.01)
    waiting = [asyncio.create_task(cache.aget("value")) for _ in range(3)]
    await asyncio.sleep(0.05)
    assert not any(task.done() for task in blocked + waiting)
    async with asyncio.timeout(2):
        await cache.aclose()
        done, _ = await asyncio.wait(blocked + waiting)
    for task in blocked:
        with pytest.raises(error):
            task.result()
    # A read that was waiting for a connection takes one that the failed
    # BLPOP released, and the pool reconnects it: aclose() does not stop
    # commands that other tasks have already started.
    for task in waiting:
        assert task.result() is None
    assert not pool._in_use_connections
    # Those reconnected sockets stay open until disconnected or collected.
    assert any(connection.is_connected for connection in pool._available_connections)
    await pool.disconnect()
