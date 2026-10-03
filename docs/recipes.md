# Recipes

These build on Django's own extension points. `tests/test_recipes.py` runs
the code shown here.

## Invalidating a group of keys with a generation counter

Django's `version=` argument is enough to invalidate a namespace at once:
keep a counter in the cache and pass it as the version of every key in the
namespace. Incrementing the counter makes the old entries unreachable; they
expire with their timeout.

```python
async def generation(cache, namespace):
    key = f"generation:{namespace}"
    value = await cache.aget(key, version=1)
    if value is None:
        await cache.aadd(key, 1, timeout=None, version=1)
        value = await cache.aget(key, version=1)
    return value


async def invalidate(cache, namespace):
    try:
        await cache.aincr(f"generation:{namespace}", version=1)
    except ValueError:
        pass  # No counter yet: nothing was cached under it.


async def get_product(cache, product_id):
    version = await generation(cache, "products")
    return await cache.aget(f"product:{product_id}", version=version)
```

Each read costs one more round trip, for the counter. The counter needs a
codec that supports `aincr`: the default one or `MsgspecCodec`.

## Hashing keys with `KEY_FUNCTION`

Django warns with `CacheKeyWarning` about keys that memcached would refuse:
longer than 250 characters, or containing spaces or control characters. A
`KEY_FUNCTION` that hashes the key avoids them and keeps the prefix and
version readable:

```python
import hashlib


def hashed_key(key, key_prefix, version):
    digest = hashlib.sha256(str(key).encode()).hexdigest()
    return f"{key_prefix}:{version}:{digest}"
```

```python
CACHES["native"]["KEY_FUNCTION"] = "myproject.cache.hashed_key"
```

- It changes every key: entries written before are not found, and a
  synchronous alias sharing the entries needs the same `KEY_FUNCTION`.
- A custom `KEY_FUNCTION` is a callback: with `callback_mode="thread"`,
  each key costs a worker-thread hop (see [performance](performance.md)).
  This one does not block, so `callback_mode="inline"` suits it.
