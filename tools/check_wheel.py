"""Check an installed aiodrf-async-cache wheel without its optional extras.

Every module imports with Django alone, except the modules of optional
drivers, which need their extras. The wheel carries the type marker.
"""

import importlib
import importlib.resources
import pkgutil

import django
from django.conf import settings

settings.configure()
django.setup()

import aiodrf_async_cache  # noqa: E402

OPTIONAL = (
    "aiodrf_async_cache.redis",
    "aiodrf_async_cache.valkey",
    "aiodrf_async_cache.django_valkey",
    "aiodrf_async_cache.codecs",
)
modules = sorted(
    info.name
    for info in pkgutil.walk_packages(
        aiodrf_async_cache.__path__, "aiodrf_async_cache."
    )
)
for name in modules:
    try:
        importlib.import_module(name)
    except ImportError as exc:
        if name not in OPTIONAL:
            raise
        print(f"{name}: needs its extra ({exc})")

assert importlib.resources.files("aiodrf_async_cache").joinpath("py.typed").is_file(), (
    "py.typed is missing"
)
print(f"{len(modules)} modules checked")
