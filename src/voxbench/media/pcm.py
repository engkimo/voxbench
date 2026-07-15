"""Small PCM16 helpers for demo-grade realtime format boundaries."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


@dataclass
class Pcm16MonoStreamResampler:
    """Stateful linear PCM16LE resampler that preserves phase across chunks."""

    input_rate: int
    output_rate: int
    _samples: list[int] = field(default_factory=list)
    _position_numerator: int = 0

    def __post_init__(self) -> None:
        if self.input_rate <= 0 or self.output_rate <= 0:
            raise ValueError("sample rates must be positive")

    def process(self, pcm_s16le: bytes) -> bytes:
        if len(pcm_s16le) % 2:
            raise ValueError("PCM16LE data length must be divisible by two")
        if self.input_rate == self.output_rate:
            return pcm_s16le
        self._samples.extend(sample[0] for sample in struct.iter_unpack("<h", pcm_s16le))
        return self._produce(require_right_sample=True)

    def flush(self) -> bytes:
        output = self._produce(require_right_sample=False)
        self._samples.clear()
        self._position_numerator = 0
        return output

    def _produce(self, *, require_right_sample: bool) -> bytes:
        output = bytearray()
        while self._samples:
            left_index = self._position_numerator // self.output_rate
            if left_index >= len(self._samples):
                break
            right_index = left_index + 1
            if right_index >= len(self._samples):
                if require_right_sample:
                    break
                right_index = left_index
            fraction_numerator = self._position_numerator % self.output_rate
            left = self._samples[left_index]
            right = self._samples[right_index]
            value = round(
                (
                    left * (self.output_rate - fraction_numerator)
                    + right * fraction_numerator
                )
                / self.output_rate
            )
            output.extend(struct.pack("<h", value))
            self._position_numerator += self.input_rate

        consumed = max(0, self._position_numerator // self.output_rate - 1)
        if consumed:
            del self._samples[:consumed]
            self._position_numerator -= consumed * self.output_rate
        return bytes(output)


def resample_pcm16_mono(pcm_s16le: bytes, *, input_rate: int, output_rate: int) -> bytes:
    """Linearly resample one mono PCM16LE chunk.

    This is intentionally dependency-free for the demo bridge. Production
    integrations can replace it with their existing streaming resampler.
    """

    resampler = Pcm16MonoStreamResampler(input_rate=input_rate, output_rate=output_rate)
    return resampler.process(pcm_s16le) + resampler.flush()
