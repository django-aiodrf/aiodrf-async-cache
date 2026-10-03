"""Codec wire formats: integers, legacy payloads and compression headers."""

import enum
import pickle
import sys
import zlib
from unittest.mock import AsyncMock, patch

import pytest
from django.core.cache.backends.redis import RedisSerializer
from django.core.exceptions import ImproperlyConfigured

from aiodrf_async_cache.codecs import CompressedCodec

pytestmark = pytest.mark.unit


@pytest.fixture
def msgspec_codec():
    pytest.importorskip("msgspec")
    from aiodrf_async_cache.codecs import MsgspecCodec

    return MsgspecCodec


class Level(enum.IntEnum):
    LOW = 5


def test_msgspec_codec_declares_integer_operations(msgspec_codec):
    assert msgspec_codec.supports_integer_operations is True


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        (0, b"0"),
        (5, b"5"),
        (-3, b"-3"),
        (128, b"128"),
        (2**63 - 1, b"%d" % (2**63 - 1)),
    ],
)
def test_msgspec_codec_writes_integers_as_redis_integers(msgspec_codec, value, stored):
    codec = msgspec_codec()
    assert codec.dumps(value) == stored
    assert codec.loads(stored) == value


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1])
def test_msgspec_codec_refuses_out_of_range_integers_when_writing(msgspec_codec, value):
    with pytest.raises(OverflowError):
        msgspec_codec().dumps(value)


def test_msgspec_codec_does_not_store_bool_as_integer(msgspec_codec):
    codec = msgspec_codec()
    assert codec.dumps(True) == b"\xc3"
    assert codec.loads(codec.dumps(True)) is True
    assert codec.loads(codec.dumps(False)) is False


def test_msgspec_codec_rewrites_small_hook_and_enum_results_as_digits(
    msgspec_codec,
):
    class Small:
        pass

    codec = msgspec_codec(enc_hook=lambda value: 7)
    assert codec.dumps(Small()) == b"7"
    assert codec.loads(b"7") == 7
    assert msgspec_codec().dumps(Level.LOW) == b"5"
    assert msgspec_codec(Level).loads(b"5") is Level.LOW


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        (0, 0),
        (5, 5),
        (45, 45),
        (47, 47),
        # The ASCII digits: 0.1.0 wrote 48 as b"0", which is now the integer 0.
        (48, 0),
        (57, 9),
        (58, 58),
        (127, 127),
        (128, 128),
        (-3, -3),
    ],
)
def test_msgspec_codec_reads_payloads_written_by_0_1_0(
    msgspec_codec, written, expected
):
    import msgspec

    # 0.1.0 stored every value as plain MessagePack.
    assert msgspec_codec().loads(msgspec.msgpack.encode(written)) == expected


@pytest.mark.parametrize("payload", [b"-", b"", b"1.5", b"+1", b"1-", b"--1", b" 1"])
def test_msgspec_codec_reads_only_full_integers_as_redis_integers(
    msgspec_codec, payload
):
    import msgspec

    codec = msgspec_codec()
    try:
        expected = msgspec.msgpack.decode(payload)
    except msgspec.DecodeError:
        with pytest.raises(msgspec.DecodeError):
            codec.loads(payload)
    else:
        assert codec.loads(payload) == expected


def test_msgspec_codec_round_trips_structures(msgspec_codec):
    codec = msgspec_codec()
    value = {"a": [1, None, True, "ü"], "b": 1.5, "c": b"\x00"}
    assert codec.loads(codec.dumps(value)) == value


@pytest.mark.parametrize("compressor", ["zlib", "zstd"])
def test_compressed_codec_writes_header_from_threshold(compressor):
    if compressor == "zstd":
        pytest.importorskip("compression.zstd")
    codec = CompressedCodec(RedisSerializer(), compressor=compressor, min_size=64)
    small = "x" * 10
    assert len(pickle.dumps(small)) < 64
    assert codec.dumps(small) == RedisSerializer().dumps(small)
    for value in ("y" * 100, ["z"] * 300):
        stored = codec.dumps(value)
        assert stored[:2] == b"\xc1" + (b"\x01" if compressor == "zlib" else b"\x02")
        assert codec.loads(stored) == value


def test_compressed_codec_threshold_is_inclusive():
    inner = RedisSerializer()
    value = "x" * 40
    size = len(inner.dumps(value))
    assert CompressedCodec(inner, min_size=size).dumps(value)[:1] == b"\xc1"
    assert CompressedCodec(inner, min_size=size + 1).dumps(value) == inner.dumps(value)


def test_compressed_codec_reads_uncompressed_values_written_before_it():
    inner = RedisSerializer()
    codec = CompressedCodec(inner, min_size=0)
    for value in ("text", {"a": [1, 2]}, None, 2.5):
        assert codec.loads(inner.dumps(value)) == value
    assert codec.loads(b"12") == 12


def test_compressed_codec_keeps_integers_raw():
    codec = CompressedCodec(RedisSerializer(), min_size=0)
    assert codec.dumps(12) == 12
    assert codec.loads(b"12") == 12
    assert codec.supports_integer_operations is True


def test_compressed_codec_keeps_msgspec_integers_raw(msgspec_codec):
    inner = msgspec_codec()
    codec = CompressedCodec(inner, min_size=0)
    assert codec.supports_integer_operations is True
    assert codec.dumps(-12) == b"-12"
    assert codec.loads(b"-12") == -12
    value = {"key": "v" * 2000}
    stored = codec.dumps(value)
    assert stored[:2] == b"\xc1\x01"
    assert len(stored) < len(inner.dumps(value))
    assert codec.loads(stored) == value


def test_compressed_codec_takes_integer_support_from_the_inner_codec():
    class Codec:
        def dumps(self, value):
            return repr(value).encode()

        def loads(self, value):
            return value

    assert CompressedCodec(Codec()).supports_integer_operations is False
    Codec.supports_integer_operations = True
    assert CompressedCodec(Codec()).supports_integer_operations is True


def test_compressed_codec_wraps_inner_output_that_starts_with_the_magic_byte():
    class Raw:
        def dumps(self, value):
            return value

        def loads(self, value):
            return value

    codec = CompressedCodec(Raw(), min_size=1024)
    assert codec.loads(codec.dumps(b"\xc1\x01abc")) == b"\xc1\x01abc"


def test_compressed_codec_refuses_unknown_formats():
    codec = CompressedCodec(RedisSerializer())
    with pytest.raises(ValueError, match="format 9"):
        codec.loads(b"\xc1\x09" + zlib.compress(b"x"))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"compressor": "lz4"}, "compressor must be zlib or zstd; got 'lz4'."),
        ({"min_size": -1}, "min_size must be a non-negative integer; got -1."),
        ({"min_size": "1k"}, "min_size must be a non-negative integer; got '1k'."),
    ],
)
def test_compressed_codec_configuration_errors_name_the_value(kwargs, message):
    with pytest.raises(ImproperlyConfigured) as error:
        CompressedCodec(RedisSerializer(), **kwargs)
    assert str(error.value) == message


def test_compressed_codec_refuses_codecs_without_dumps_and_loads():
    with pytest.raises(ImproperlyConfigured, match="dumps"):
        CompressedCodec(object())


def test_zstd_requires_the_standard_library_module():
    with patch.dict(sys.modules, {"compression.zstd": None}):
        with pytest.raises(ImproperlyConfigured) as error:
            CompressedCodec(RedisSerializer(), compressor="zstd")
        assert "compression.zstd" in str(error.value)
        assert "3.14" in str(error.value)
        # A zlib codec cannot read a zstd value without the module either.
        with pytest.raises(ImproperlyConfigured, match=r"compression\.zstd"):
            CompressedCodec(RedisSerializer()).loads(b"\xc1\x02abc")


async def test_backend_counts_through_a_compressed_codec():
    from aiodrf_async_cache.redis import AsyncRedisCache

    codec = CompressedCodec(RedisSerializer(), min_size=0)
    cache = AsyncRedisCache("redis://localhost", {"OPTIONS": {"serializer": codec}})
    cache._async_client = AsyncMock()
    cache._async_client.eval.return_value = b"3"
    assert await cache.aincr("counter", 2) == 3


async def test_backend_refuses_counting_through_a_compressed_codec_that_cannot():
    from aiodrf_async_cache.redis import AsyncRedisCache

    class Codec:
        def dumps(self, value):
            return repr(value).encode()

        def loads(self, value):
            return value

    codec = CompressedCodec(Codec())
    cache = AsyncRedisCache("redis://localhost", {"OPTIONS": {"serializer": codec}})
    cache._async_client = AsyncMock()
    with pytest.raises(NotImplementedError):
        await cache.aincr("counter")
