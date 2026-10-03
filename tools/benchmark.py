"""Time the cache operations under each callback mode and codec.

Compares, against one local Redis server:

- Django's ``RedisCache`` through its async methods (one ``sync_to_async``
  call per operation);
- ``AsyncRedisCache`` with ``callback_mode="thread"`` (the default);
- ``AsyncRedisCache`` with ``callback_mode="inline"``;

each with Django's ``RedisSerializer`` and with ``MsgspecCodec``, for
``aget``/``aset`` of 64 B, 1 KiB and 100 KiB values, ``aget_many``/``aset_many``
of 100 keys, and ``aincr``, with 1 and with 50 concurrent tasks.

Run from the repository root with the redis and msgspec extras installed,
pinned to one performance core (the method of django-fastdrf's
``docs/benchmarks.md``)::

    uv sync --extra redis --extra msgspec
    taskset -c 4 .venv/bin/python tools/benchmark.py

The server defaults to ``redis://127.0.0.1:6380/15``; pass ``--url`` or set
``AIODRF_BENCH_REDIS_URL`` to use another one. Use a database nothing else
uses: the script writes keys under a random ``KEY_PREFIX`` and deletes only
those keys at the end. It never flushes a database.

Each figure is the median over ``--repeat`` samples of one sample's wall time
divided by its number of operations, in microseconds. With one task that is
the latency of one operation; with 50 tasks it is the time per operation
while 50 run at once, the inverse of throughput.
"""

import argparse
import asyncio
import os
import platform
import statistics
import time
import uuid
from importlib.metadata import version

import django
from django.conf import settings

from aiodrf_async_cache import __version__

SIZES = {"64 B": 64, "1 KiB": 1024, "100 KiB": 100 * 1024}
MANY = 100


def cpu_model():
    try:
        with open("/proc/cpuinfo") as cpuinfo:
            for line in cpuinfo:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def operations():
    """(name, setup, run, check) for each timed operation."""
    cases = []
    for label, size in SIZES.items():
        value = "x" * size

        async def set_value(cache, key, value=value):
            await cache.aset(key, value, timeout=300)

        async def get_value(cache, key):
            return await cache.aget(key)

        cases.append((f"aset {label}", None, set_value, None))
        cases.append(
            (
                f"aget {label}",
                set_value,
                get_value,
                lambda result, value=value: result == value,
            )
        )
    data = {f"many-{index}": "x" * 64 for index in range(MANY)}

    async def set_many(cache, key, data=data):
        await cache.aset_many(data, timeout=300)

    async def get_many(cache, key, data=data):
        return await cache.aget_many(list(data))

    cases.append((f"aset_many {MANY}", None, set_many, None))
    cases.append(
        (
            f"aget_many {MANY}",
            set_many,
            get_many,
            lambda result, data=data: result == data,
        )
    )

    async def set_counter(cache, key):
        await cache.aset(key, 0, timeout=300)

    async def increment(cache, key):
        return await cache.aincr(key)

    cases.append(("aincr", set_counter, increment, lambda result: result > 0))
    return cases


async def sample(cache, run, key, tasks, count):
    per_task = max(1, count // tasks)

    async def worker(index):
        own = f"{key}-{index}"
        for _ in range(per_task):
            await run(cache, own)

    started = time.perf_counter()
    await asyncio.gather(*(worker(index) for index in range(tasks)))
    elapsed = time.perf_counter() - started
    return elapsed / (per_task * tasks) * 1e6


async def measure(cache, args, results, config, codec):
    for name, setup, run, check in operations():
        for tasks in args.concurrency:
            key = f"{name.replace(' ', '-')}-{tasks}"
            for index in range(tasks):
                if setup is not None:
                    await setup(cache, f"{key}-{index}")
            if check is not None:
                result = await run(cache, f"{key}-0")
                if not check(result):
                    raise AssertionError(f"{config}/{codec}/{name}: wrong result")
            # One discarded sample warms the pool, the codec and the thread.
            await sample(cache, run, key, tasks, args.operations)
            timings = [
                await sample(cache, run, key, tasks, args.operations)
                for _ in range(args.repeat)
            ]
            median = statistics.median(timings)
            results.append((config, codec, name, tasks, median))
            print(f"{config:<14} {codec:<16} {name:<16} {tasks:>3} {median:>10.2f}")


async def delete_prefix(url, prefix):
    from redis.asyncio import Redis

    client = Redis.from_url(url)
    try:
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        for start in range(0, len(keys), 1000):
            await client.delete(*keys[start : start + 1000])
        return len(keys)
    finally:
        await client.aclose()


async def main(args):
    from asgiref.sync import sync_to_async
    from django.core.cache.backends.redis import RedisCache, RedisSerializer

    from aiodrf_async_cache.codecs import MsgspecCodec
    from aiodrf_async_cache.redis import AsyncRedisCache

    prefix = "aiodrf-bench-" + uuid.uuid4().hex
    codecs = {"RedisSerializer": RedisSerializer, "MsgspecCodec": MsgspecCodec}
    results = []
    print(f"{'configuration':<14} {'codec':<16} {'operation':<16} {'n':>3} {'µs':>10}")
    try:
        for codec_name, codec in codecs.items():
            params = {"KEY_PREFIX": prefix, "OPTIONS": {"serializer": codec}}
            cache = RedisCache(args.url, params)
            try:
                await measure(cache, args, results, "django", codec_name)
            finally:
                await sync_to_async(cache.close)()
            for mode in ("thread", "inline"):
                cache = AsyncRedisCache(
                    args.url,
                    {
                        "KEY_PREFIX": prefix,
                        "OPTIONS": {
                            "serializer": codec,
                            "callback_mode": mode,
                            "max_connections": max(args.concurrency),
                        },
                    },
                )
                try:
                    await measure(cache, args, results, f"native {mode}", codec_name)
                finally:
                    await cache.aclose()
    finally:
        deleted = await delete_prefix(args.url, prefix)
        print(f"\nDeleted {deleted} benchmark keys under {prefix}.")
    return results


def parse_args():
    options = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    options.add_argument(
        "--url",
        default=os.environ.get("AIODRF_BENCH_REDIS_URL", "redis://127.0.0.1:6380/15"),
        help="Redis URL, with a database number nothing else uses",
    )
    options.add_argument("--repeat", type=int, default=7)
    options.add_argument(
        "--operations",
        type=int,
        default=500,
        help="operations per sample, shared between the tasks",
    )
    options.add_argument(
        "--concurrency",
        type=lambda text: [int(part) for part in text.split(",")],
        default=[1, 50],
        help="comma-separated task counts (default 1,50)",
    )
    args = options.parse_args()
    if min(args.repeat, args.operations, *args.concurrency) < 1:
        options.error("repeat, operations and concurrency must be positive")
    return args


if __name__ == "__main__":
    arguments = parse_args()
    settings.configure(SECRET_KEY="benchmark", USE_TZ=True)
    django.setup()
    affinity = (
        sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
    )
    print(f"CPU: {cpu_model()}; affinity: {affinity or 'unknown'}")
    if len(affinity) != 1:
        print("Not pinned to one core: run under `taskset -c N` for stable figures.")
    print(
        f"Python {platform.python_version()} ({platform.python_implementation()}), "
        f"Django {version('django')}, redis-py {version('redis')}, "
        f"msgspec {version('msgspec')}, aiodrf-async-cache {__version__}"
    )
    print(
        f"Server: {arguments.url}; {arguments.repeat} samples of "
        f"{arguments.operations} operations each\n"
    )
    asyncio.run(main(arguments))
