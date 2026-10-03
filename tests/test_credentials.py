"""Where a password given in LOCATION or OPTIONS can and cannot appear."""

import pytest

pytestmark = pytest.mark.unit

PASSWORD = "s3cret-Pa55word"


@pytest.fixture(params=["redis", "valkey"])
def backend_class(request):
    pytest.importorskip(request.param)
    if request.param == "redis":
        from aiodrf_async_cache.redis import AsyncRedisCache

        return AsyncRedisCache
    from aiodrf_async_cache.valkey import AsyncValkeyCache

    return AsyncValkeyCache


@pytest.mark.parametrize("in_url", [True, False])
async def test_connection_errors_and_reprs_omit_the_password(backend_class, in_url):
    scheme = "redis" if backend_class.__name__ == "AsyncRedisCache" else "valkey"
    # Nothing listens on port 1; the connection is refused at once.
    if in_url:
        location, options = f"{scheme}://user:{PASSWORD}@127.0.0.1:1/0", {}
    else:
        location = f"{scheme}://127.0.0.1:1/0"
        options = {"username": "user", "password": PASSWORD}
    cache = backend_class(
        location, {"OPTIONS": {**options, "socket_connect_timeout": 2}}
    )
    try:
        with pytest.raises(Exception) as error:
            await cache.aget("key")
        assert "Connection" in type(error.value).__name__
        text = "".join(
            str(part)
            for part in (error.value, repr(error.value), error.value.__cause__)
        )
        assert PASSWORD not in text
        assert PASSWORD not in repr(cache)
        assert PASSWORD not in repr(cache.async_client)
        assert PASSWORD not in repr(cache.async_client.connection_pool)
        # The backend and the driver keep it to authenticate; anything that
        # prints their attributes (a debugger, an error reporter that records
        # local variables) can show it.
        assert PASSWORD in repr(vars(cache)) or PASSWORD in repr(
            cache.async_client.connection_pool.connection_kwargs
        )
    finally:
        await cache.aclose()


def test_django_error_reports_hide_a_password_in_options_but_not_in_location():
    from django.views.debug import SafeExceptionReporterFilter

    cleansed = SafeExceptionReporterFilter().cleanse_setting(
        "CACHES",
        {
            "url": {"LOCATION": f"redis://user:{PASSWORD}@cache:6379/0"},
            "options": {
                "LOCATION": "redis://cache:6379/0",
                "OPTIONS": {"password": PASSWORD},
            },
        },
    )
    assert PASSWORD in cleansed["url"]["LOCATION"]
    assert PASSWORD not in repr(cleansed["options"])
