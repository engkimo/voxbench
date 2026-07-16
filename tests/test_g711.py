from __future__ import annotations

import struct

import pytest

from voxbench.media import (
    linear16_to_mulaw,
    mulaw_to_linear16,
    mulaw_to_pcm16le,
    pcm16le_to_mulaw,
)


@pytest.mark.parametrize(
    ("code", "sample"),
    [
        (0xFF, 0),
        (0x7F, 0),
        (0x80, 32_124),
        (0x00, -32_124),
    ],
)
def test_mulaw_known_decode_values(code: int, sample: int) -> None:
    assert mulaw_to_linear16(code) == sample


def test_linear16_encoder_uses_canonical_zero_and_full_scale_codes() -> None:
    assert linear16_to_mulaw(0) == 0xFF
    assert linear16_to_mulaw(32_767) == 0x80
    assert linear16_to_mulaw(-32_768) == 0x00


def test_all_mulaw_codes_decode_within_linear16_and_reencode_safely() -> None:
    decoded = [mulaw_to_linear16(code) for code in range(256)]
    reencoded = [linear16_to_mulaw(sample) for sample in decoded]

    assert min(decoded) == -32_124
    assert max(decoded) == 32_124
    assert all(-32_768 <= sample <= 32_767 for sample in decoded)
    assert all(0 <= code <= 255 for code in reencoded)
    assert reencoded == [0xFF if code == 0x7F else code for code in range(256)]


def test_pcm_buffer_round_trip_preserves_sample_count_and_quantizes() -> None:
    samples = (-32_768, -10_000, -1, 0, 1, 10_000, 32_767)
    pcm = struct.pack(f"<{len(samples)}h", *samples)

    encoded = pcm16le_to_mulaw(pcm)
    decoded = mulaw_to_pcm16le(encoded)

    assert len(encoded) == len(samples)
    assert len(decoded) == len(pcm)
    assert struct.unpack(f"<{len(samples)}h", decoded) != samples


def test_g711_rejects_invalid_code_and_partial_pcm_sample() -> None:
    with pytest.raises(ValueError, match="between 0 and 255"):
        mulaw_to_linear16(256)
    with pytest.raises(ValueError, match="divisible by two"):
        pcm16le_to_mulaw(b"\x00")
