"""Django's page-cache policy with independently awaited cache operations."""

import asyncio
import pickle

import pytest
from django.http import HttpResponse, StreamingHttpResponse
from django.middleware.cache import CacheMiddleware
from django.template.response import SimpleTemplateResponse
from django.test import RequestFactory, override_settings
from django.utils.cache import patch_vary_headers

from aiodrf_async_cache.middleware import (
    AsyncCacheMiddleware,
    AsyncFetchFromCacheMiddleware,
    AsyncUpdateCacheMiddleware,
)

pytestmark = pytest.mark.unit


class MemoryCache:
    """A serialized cache shared by the synchronous policy and async adapter."""

    def __init__(self):
        self.values = {}
        self.operations = []

    def get(self, key, default=None):
        value = self.values.get(key)
        return pickle.loads(value) if value is not None else default

    def set(self, key, value, timeout=None):
        self.operations.append((key, timeout))
        self.values[key] = pickle.dumps(value)

    async def aget(self, key, default=None):
        await asyncio.sleep(0)
        return self.get(key, default)

    async def aset(self, key, value, timeout=None):  # noqa: ASYNC109 -- Django cache TTL
        await asyncio.sleep(0)
        self.set(key, value, timeout)


def async_middleware(cache, view):
    class Middleware(AsyncCacheMiddleware):
        def get_cache(self, request):
            return cache

    return Middleware(view)


def django_middleware(cache):
    class Middleware(CacheMiddleware):
        @property
        def cache(self):
            return cache

    return Middleware(lambda request: HttpResponse())


@pytest.mark.parametrize("method", ["get", "head", "post", "put", "delete", "options"])
@pytest.mark.parametrize("status", [200, 201, 304, 404, 500])
async def test_native_cache_matches_django_methods_and_statuses(method, status):
    native, reference = MemoryCache(), MemoryCache()
    factory = RequestFactory()
    calls = []

    async def view(request):
        calls.append(request.method)
        return HttpResponse(b"response", status=status)

    middleware = async_middleware(native, view)
    policy = django_middleware(reference)
    for _ in range(2):
        request = getattr(factory, method)("/")
        expected_request = getattr(factory, method)("/")
        expected = policy.process_request(expected_request)
        if expected is None:
            expected = policy.process_response(
                expected_request, HttpResponse(b"response", status=status)
            )
        actual = await middleware(request)
        assert actual.status_code == expected.status_code
        assert actual.content == expected.content
        assert set(native.values) == set(reference.values)
        assert request._cache_update_cache == expected_request._cache_update_cache
    assert len(calls) == (1 if method in {"get", "head"} and status == 200 else 2)


@pytest.mark.parametrize(
    "control",
    [
        "private",
        'private="Set-Cookie"',
        "no-store",
        "no-cache",
        "max-age=0",
        "public, max-age=30",
    ],
)
async def test_native_cache_reuses_django_cache_control_decisions(control):
    cache, reference = MemoryCache(), MemoryCache()

    def response():
        return HttpResponse(b"body", headers={"Cache-Control": control})

    async def view(request):
        return response()

    req = RequestFactory().get("/", HTTP_AUTHORIZATION="Bearer one")
    expected_req = RequestFactory().get("/", HTTP_AUTHORIZATION="Bearer one")
    policy = django_middleware(reference)
    policy.process_request(expected_req)
    expected = policy.process_response(expected_req, response())
    actual = await async_middleware(cache, view)(req)
    assert set(cache.values) == set(reference.values)
    assert actual.get("Vary") == expected.get("Vary")
    assert actual.get("Cache-Control") == expected.get("Cache-Control")


async def test_vary_keys_head_and_cached_response_are_interoperable():
    cache = MemoryCache()
    calls = []

    async def view(request):
        calls.append(request.headers.get("X-Tenant"))
        response = HttpResponse(request.headers.get("X-Tenant", ""))
        patch_vary_headers(response, ["X-Tenant"])
        return response

    middleware = async_middleware(cache, view)
    factory = RequestFactory()
    for tenant in ("one", "two", "one"):
        result = await middleware(factory.get("/", HTTP_X_TENANT=tenant))
        assert result.content == tenant.encode()
    result = await middleware(factory.head("/", HTTP_X_TENANT="one"))
    assert result.content == b"one"
    result = django_middleware(cache).process_request(
        factory.get("/", HTTP_X_TENANT="two")
    )
    assert result.content == b"two"
    assert calls == ["one", "two"]


@pytest.mark.parametrize("kind", ["cookie", "vary-star", "stream"])
async def test_uncacheable_policy_matches_installed_django(kind):
    cache = MemoryCache()

    async def view(request):
        if kind == "stream":
            return StreamingHttpResponse(iter([b"stream"]))
        response = HttpResponse(b"private")
        if kind == "cookie":
            response.set_cookie("session", "value")
            patch_vary_headers(response, ["Cookie"])
        else:
            patch_vary_headers(response, ["*"])
        return response

    await async_middleware(cache, view)(RequestFactory().get("/"))
    reference = MemoryCache()
    policy = django_middleware(reference)
    request = RequestFactory().get("/")
    policy.process_request(request)
    policy.process_response(request, await view(request))
    assert set(cache.values) == set(reference.values)


@pytest.mark.parametrize("phase", ["read", "write"])
async def test_native_cache_failure_does_not_replay_view(phase):
    calls = []

    class FailingCache(MemoryCache):
        async def aget(self, *args):
            if phase == "read":
                raise OSError("offline")
            return await super().aget(*args)

        async def aset(self, *args):
            raise OSError("offline")

    async def view(request):
        calls.append(True)
        return HttpResponse(b"body")

    with pytest.raises(OSError, match="offline"):
        await async_middleware(FailingCache(), view)(RequestFactory().get("/"))
    assert calls == ([] if phase == "read" else [True])


async def test_native_adapter_requires_an_async_middleware_chain():
    with pytest.raises(ValueError, match="async"):
        AsyncCacheMiddleware(lambda request: HttpResponse())


async def test_split_middleware_observes_intermediate_vary_headers():
    cache = MemoryCache()
    calls = []

    class Fetch(AsyncFetchFromCacheMiddleware):
        def get_cache(self, request):
            return cache

    class Update(AsyncUpdateCacheMiddleware):
        def get_cache(self, request):
            return cache

    async def view(request):
        calls.append(request.headers["X-Tenant"])
        return HttpResponse(request.headers["X-Tenant"])

    fetch = Fetch(view)

    async def vary_middleware(request):
        response = await fetch(request)
        patch_vary_headers(response, ["X-Tenant"])
        return response

    middleware = Update(vary_middleware)
    for tenant in ("one", "two", "one"):
        result = await middleware(RequestFactory().get("/", HTTP_X_TENANT=tenant))
        assert result.content == tenant.encode()
    assert calls == ["one", "two"]


async def test_native_middleware_never_adapts_cache_io_to_a_thread(monkeypatch):
    def no_bridge(*args, **kwargs):
        pytest.fail("Native cache I/O used a sync bridge")

    monkeypatch.setattr("asgiref.sync.SyncToAsync.__call__", no_bridge)

    async def view(request):
        return HttpResponse(request.path)

    middleware = async_middleware(MemoryCache(), view)
    responses = await asyncio.gather(
        *(middleware(RequestFactory().get(f"/{i}/")) for i in range(20))
    )
    assert [response.content for response in responses] == [
        f"/{i}/".encode() for i in range(20)
    ]


async def test_cancelling_a_cache_read_does_not_call_the_view():
    started = asyncio.Event()

    class WaitingCache(MemoryCache):
        async def aget(self, *args):
            started.set()
            await asyncio.Event().wait()

    async def view(request):
        pytest.fail("Cancelled cache lookup called the view")

    task = asyncio.create_task(
        async_middleware(WaitingCache(), view)(RequestFactory().get("/"))
    )
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_unrendered_response_is_rejected_before_adding_sync_callbacks():
    from django.core.exceptions import ImproperlyConfigured

    async def view(request):
        return SimpleTemplateResponse("unused.html")

    with pytest.raises(ImproperlyConfigured, match="rendered response"):
        await async_middleware(MemoryCache(), view)(RequestFactory().get("/"))


@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("zone", ["UTC", "Europe/Istanbul"])
async def test_keys_keep_django_locale_timezone_and_prefix(language, zone):
    from zoneinfo import ZoneInfo

    from django.utils import timezone, translation

    cache, reference = MemoryCache(), MemoryCache()

    async def view(request):
        return HttpResponse(b"body")

    with (
        override_settings(
            CACHE_MIDDLEWARE_KEY_PREFIX="site", USE_I18N=True, USE_TZ=True
        ),
        translation.override(language),
        timezone.override(ZoneInfo(zone)),
    ):
        await async_middleware(cache, view)(RequestFactory().get("/?page=2"))
        policy = django_middleware(reference)
        request = RequestFactory().get("/?page=2")
        policy.process_request(request)
        policy.process_response(request, HttpResponse(b"body"))
    assert set(cache.values) == set(reference.values)


async def test_head_specific_response_is_reused_when_get_is_missing():
    cache = MemoryCache()
    calls = []

    async def view(request):
        calls.append(request.method)
        return HttpResponse(b"head")

    middleware = async_middleware(cache, view)
    factory = RequestFactory()
    await middleware(factory.head("/"))
    assert (await middleware(factory.head("/"))).content == b"head"
    assert calls == ["HEAD"]
