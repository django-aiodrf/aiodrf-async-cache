# Performance

## Execution context

Network I/O is native: every command goes through the driver's asyncio
client on the event loop, and no cache operation is sent through
`sync_to_async` (`test_all_aiodrf_async_cache_operations_are_native` in
`tests/services/test_redis.py`).

Synchronous callbacks run in a worker thread by default. A codec's `dumps`
and `loads`, a custom `KEY_FUNCTION`, a subclass's `make_key` or
`validate_key`, and a callable `aget_or_set` default are callbacks. With
`callback_mode="thread"` (the default), each call is one
`sync_to_async(thread_sensitive=True)` hop, and all of them share one thread
per process. Async callbacks run on the event loop in either mode.

Measured on the reference machine of django-fastdrf's `docs/benchmarks.md`
(i7-14700F, one performance core with `taskset -c 4`, CPython 3.14.4,
Django 6.1.1), `RedisSerializer().dumps()` of a small dict took 0.54 µs
called on the event loop and 37.05 µs through `sync_to_async`. The hop, not
the codec, is the largest per-call cost the package adds.

Batches reduce it only for the default codec: with `RedisSerializer`,
`aget_many` and `aset_many` convert all their values in one hop
(`tests/test_batches.py`). Any other codec, `CompressedCodec` included, is
called once per value, and each call is a hop in thread mode.

## Choosing `callback_mode`

`callback_mode="inline"` runs synchronous callbacks on the event loop. It
is safe when every callback is short and never blocks:

- a C codec such as `MsgspecCodec`, on values of moderate size;
- the default codec on small values;
- no `enc_hook`, `dec_hook` or `KEY_FUNCTION` that does I/O or takes locks.

Keep `"thread"` when a callback can take long or block:

- pickling large objects (cached pages, big querysets' results), or
  compressing large values with `CompressedCodec`;
- hooks that read files, the database or the network.

While an inline callback runs, nothing else on that event loop runs.

## Measuring

`tools/benchmark.py` times `aget`/`aset` (64 B, 1 KiB, 100 KiB),
`aget_many`/`aset_many` (100 keys) and `aincr` with Django's `RedisCache`
through its async methods and with `AsyncRedisCache` in both callback
modes, with each codec, at 1 and 50 concurrent tasks. See its docstring for
how to run it, and measure on the hardware the cache will run on.
