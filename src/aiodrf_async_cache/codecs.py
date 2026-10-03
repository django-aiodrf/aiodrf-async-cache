"""Optional value codecs for the native Redis and Valkey cache backends.

The cache awaits async callbacks and offloads these synchronous codecs by
default. Their wire formats are not Django's pickle-based page-cache format.
"""

from collections.abc import Callable
from typing import Any

__all__ = ["MsgspecCodec"]


class MsgspecCodec:
    """MessagePack values, optionally decoded into a msgspec type.

    ``enc_hook`` and ``dec_hook`` retain msgspec's synchronous callback contract.
    They execute with the codec in the cache's configured callback context.
    """

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
        return self.encoder.encode(value)

    def loads(self, value: bytes) -> Any:
        return self.decoder.decode(value)
