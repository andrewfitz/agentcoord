"""Lossless, bounded UTF-8 archive storage; hashes always describe decoded text."""
from __future__ import annotations

import hashlib
import struct
import zlib

_MAGIC = b"agentcoord-zlib-utf8-v1\0"
_MAX_BYTES = 64 * 1024 * 1024
_HEADER_BYTES = len(_MAGIC) + 8 + 32


def encode(text: str) -> str | bytes:
    """Keep small/uncompressible text unchanged; never rewrite semantic JSON."""
    raw = text.encode("utf-8")
    if len(raw) < 1024 or len(raw) > _MAX_BYTES:
        return text
    packed = zlib.compress(raw, 6)
    if len(packed) + _HEADER_BYTES >= len(raw) * 0.9:
        return text
    return _MAGIC + struct.pack(">Q", len(raw)) + hashlib.sha256(raw).digest() + packed


def decode(value: str | bytes) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, bytes) or not value.startswith(_MAGIC) or len(value) <= _HEADER_BYTES:
        raise ValueError("Invalid compressed archive representation")
    length = struct.unpack(">Q", value[len(_MAGIC):len(_MAGIC) + 8])[0]
    if length > _MAX_BYTES:
        raise ValueError("Compressed archive exceeds decode budget")
    expected = value[len(_MAGIC) + 8:_HEADER_BYTES]
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(value[_HEADER_BYTES:], length + 1)
        if (len(raw) != length or not decompressor.eof or decompressor.unused_data
                or decompressor.unconsumed_tail or hashlib.sha256(raw).digest() != expected):
            raise ValueError("Compressed archive length, digest or stream is invalid")
        return raw.decode("utf-8")
    except (zlib.error, UnicodeError) as error:
        raise ValueError("Compressed archive is corrupt") from error
