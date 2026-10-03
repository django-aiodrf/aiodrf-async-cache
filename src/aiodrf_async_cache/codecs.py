"""Optional value codecs for the native Redis and Valkey cache backends.

The cache awaits async callbacks and offloads these synchronous codecs by
default. Their wire formats are not Django's pickle-based page-cache format.
"""

import importlib
import zlib
from collections.abc import Callable
from typing import Any

from django.core.cache.backends.redis import RedisSerializer
from django.core.exceptions import ImproperlyConfigured

__all__ = ["CompressedCodec", "MsgspecCodec"]


def _is_redis_integer(value: bytes) -> bool:
    # The whole value must be ASCII digits with an optional leading "-", as
    # Redis writes the result of INCRBY.
    return (value[1:] if value[:1] == b"-" else value).isdigit()


class MsgspecCodec:
    """MessagePack values, optionally decoded into a msgspec type.

    Integers are stored as Redis integers, as Django's serializer stores them,
    so that ``aincr()`` and ``adecr()`` count with the server's atomic
    ``INCRBY``; they are decoded into ``type`` like any other value.

    ``enc_hook`` and ``dec_hook`` retain msgspec's synchronous callback contract.
    They execute with the codec in the cache's configured callback context.
    """

    supports_integer_operations = True

    def __init__(
        self,
        type: Any = Any,
        *,
        enc_hook: Callable[[Any], Any] | None = None,
        dec_hook: Callable[[type, Any], Any] | None = None,
    ) -> None:
        import msgspec

        self.encoder = msgspec.msgpack.Encoder(enc_hook=enc_hook)
        self.decoder = msgspec.msgpack.Decoder(type, dec_hook=dec_hook)

    def dumps(self, value: Any) -> bytes:
        encoded = self.encoder.encode(value)
        if isinstance(value, int) and not isinstance(value, bool):
            # Encoded first: an integer MessagePack cannot hold fails here, as
            # any other value it cannot hold does, not in ``loads``.
            return b"%d" % value
        if encoded[0] < 0x80:
            # An integer from 0 to 127 another value encodes as (an enum
            # member's, an ``enc_hook``'s): MessagePack's one byte would
            # read back as ASCII digits ("1" for 49).
            return b"%d" % encoded[0]
        return encoded

    def loads(self, value: bytes) -> Any:
        # Every MessagePack value but an integer from 0 to 127, which ``dumps``
        # never writes, starts with a byte of 0x80 or more; a Redis integer is
        # ASCII. Only a whole integer is read as one: 0.1.0 wrote 0 to 127 as
        # MessagePack's single byte, and of those only b"0" to b"9" (48 to 57)
        # now read as digits.
        if _is_redis_integer(value):
            value = self.encoder.encode(int(value))
        return self.decoder.decode(value)


# 0xC1 is the one byte MessagePack never uses. Pickle (protocol 2 and later)
# starts with 0x80 and a Redis integer with an ASCII digit or "-", so no value
# the supported codecs write starts with it.
_MAGIC = b"\xc1"
_ZLIB = 1
_ZSTD = 2
_FORMATS = {"zlib": _ZLIB, "zstd": _ZSTD}


def _zstd() -> Any:
    try:
        return importlib.import_module("compression.zstd")
    except ImportError:
        raise ImproperlyConfigured(
            "zstd compression requires the compression.zstd module "
            "(the standard library of CPython 3.14 or later)."
        )


class CompressedCodec:
    """Compress another codec's values of at least ``min_size`` bytes.

    A compressed value starts with the byte 0xC1 and a format byte (1 for
    zlib, 2 for zstd). Smaller values and integers are stored as the inner
    codec wrote them, so values written before compression was enabled stay
    readable and ``aincr()`` keeps working when the inner codec supports it.
    Django's ``RedisCache`` cannot read compressed values.
    """

    def __init__(
        self, codec: Any, *, compressor: str = "zlib", min_size: int = 1024
    ) -> None:
        if not all(callable(getattr(codec, name, None)) for name in ("dumps", "loads")):
            raise ImproperlyConfigured(
                f"codec must provide dumps() and loads(); got {type(codec).__qualname__}."
            )
        if compressor not in _FORMATS:
            raise ImproperlyConfigured(
                f"compressor must be zlib or zstd; got {compressor!r}."
            )
        if type(min_size) is not int or min_size < 0:
            raise ImproperlyConfigured(
                f"min_size must be a non-negative integer; got {min_size!r}."
            )
        self.codec = codec
        self.min_size = min_size
        self._format = _FORMATS[compressor]
        self._compress = _zstd().compress if compressor == "zstd" else zlib.compress
        # The backend's own rule for counting with a codec.
        self.supports_integer_operations = type(codec) is RedisSerializer or bool(
            getattr(codec, "supports_integer_operations", False)
        )

    def dumps(self, value: Any) -> Any:
        data = self.codec.dumps(value)
        if not isinstance(data, bytes) or _is_redis_integer(data):
            # RedisSerializer returns an integer itself; redis-py writes it as
            # digits. Either way INCRBY must find a plain integer.
            return data
        if len(data) >= self.min_size or data[:1] == _MAGIC:
            # An inner value that happens to start with the magic byte is
            # always wrapped, so that ``loads`` cannot mistake it.
            return _MAGIC + bytes((self._format,)) + self._compress(data)
        return data

    def loads(self, value: bytes) -> Any:
        if value[:1] == _MAGIC:
            fmt = value[1]
            if fmt == _ZLIB:
                value = zlib.decompress(value[2:])
            elif fmt == _ZSTD:
                value = _zstd().decompress(value[2:])
            else:
                raise ValueError(f"Unknown compressed cache value format {fmt}.")
        return self.codec.loads(value)
