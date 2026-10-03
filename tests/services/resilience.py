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
