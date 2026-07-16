"""Dependency-free ITU-T G.711 mu-law conversion helpers."""

from __future__ import annotations

import struct

_MULAW_BIAS = 0x84
_MULAW_CLIP = 32_635


def linear16_to_mulaw(sample: int) -> int:
    """Encode one signed 16-bit linear PCM sample as an 8-bit mu-law code."""

    sample = max(-32_768, min(32_767, sample))
    sign = 0x80 if sample < 0 else 0
    magnitude = min(-sample if sample < 0 else sample, _MULAW_CLIP)
    biased = magnitude + _MULAW_BIAS
    exponent = min(7, max(0, biased.bit_length() - 8))
    mantissa = (biased >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def mulaw_to_linear16(code: int) -> int:
    """Decode one 8-bit mu-law code to signed 16-bit linear PCM."""

    if not 0 <= code <= 0xFF:
        raise ValueError("mu-law code must be between 0 and 255")
    inverted = ~code & 0xFF
    magnitude = ((inverted & 0x0F) << 3) + _MULAW_BIAS
    magnitude <<= (inverted & 0x70) >> 4
    return _MULAW_BIAS - magnitude if inverted & 0x80 else magnitude - _MULAW_BIAS


def pcm16le_to_mulaw(pcm_s16le: bytes) -> bytes:
    """Encode interleaved little-endian PCM16 samples as mu-law octets."""

    if len(pcm_s16le) % 2:
        raise ValueError("PCM16LE data length must be divisible by two")
    return bytes(linear16_to_mulaw(sample[0]) for sample in struct.iter_unpack("<h", pcm_s16le))


def mulaw_to_pcm16le(mulaw: bytes) -> bytes:
    """Decode mu-law octets as interleaved little-endian PCM16 samples."""

    return b"".join(struct.pack("<h", mulaw_to_linear16(code)) for code in mulaw)
