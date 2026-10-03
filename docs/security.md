# Security

## Pickle: trust the cache server

The default codec is Django's `RedisSerializer`, which stores every value
except a plain `int` with `pickle`. Unpickling runs code named by the data,
so anyone who can write to the cache database can run code in every process
that reads it. Connect only to a server, and a database, that only trusted
code can write to. The same holds for Django's own `RedisCache`.

`MsgspecCodec` (the `msgspec` extra) decodes MessagePack and does not run
code from the data. It still trusts the data's shape unless it is given a
type to decode into.

## TLS and ACL users

TLS and ACL credentials are the driver's options. Give them in the URL or in
`OPTIONS`, which the backend passes to the driver's connection pool:

```python
CACHES = {
    "default": {
        "BACKEND": "aiodrf_async_cache.redis.AsyncRedisCache",
        "LOCATION": "rediss://cache.internal:6380/1",
        "OPTIONS": {
            "username": "app",
            "password": os.environ["CACHE_PASSWORD"],
            "ssl_ca_certs": "/etc/ssl/certs/cache-ca.pem",
        },
    },
}
```

`rediss://` enables TLS; for Valkey, `valkeys://`. Sentinel's own
connections take their options from `sentinel_kwargs`.

## Credentials in `LOCATION`

`tests/test_credentials.py` and `tests/services/test_redis.py` check what
happens to a password, with redis-py 8.1 and valkey-py 6.1:

- A refused connection's error message and `repr()` do not contain it,
  whether it was given in the URL or as `OPTIONS["password"]`.
- A rejected password (`AuthenticationError: invalid username-password
  pair`) does not contain it either.
- The `repr()` of the backend, of `async_client` and of its connection pool
  do not contain it.
- The backend keeps `LOCATION` as given, and the driver keeps the password in
  its connection arguments, because it needs it to authenticate. Anything
  that prints these objects' attributes can show it: a debugger, or an error
  reporter that records local variables.
- Django's debug error page hides a setting whose key contains `PASS`, so it
  hides `OPTIONS["password"]`. It does not hide a password inside
  `LOCATION`.

The package does not redact anything itself. Prefer `OPTIONS["password"]`,
read from the environment, to a password in the URL.

## `aclear()` empties the whole database

`aclear()` sends `FLUSHDB`: it deletes every key of the selected database,
or of every primary in a Cluster, whatever the `KEY_PREFIX`. Use a database
of its own for each cache alias that may be cleared. On a shared database,
delete your keys by prefix instead.
