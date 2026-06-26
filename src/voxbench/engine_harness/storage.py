"""Recording storage sinks for StageTap artifacts."""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any, Protocol

from voxbench.engine_harness.models import RecordingArtifact


class RecordingSink(Protocol):
    def write_stage_wav(
        self,
        *,
        run_id: str,
        stage: str,
        audio_format: dict[str, Any],
        duration_ms: float,
    ) -> RecordingArtifact:
        """Persist a stage WAV artifact and return its URI."""


class LocalRecordingSink:
    """Filesystem-backed sink used by tests and local development."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def write_stage_wav(
        self,
        *,
        run_id: str,
        stage: str,
        audio_format: dict[str, Any],
        duration_ms: float,
    ) -> RecordingArtifact:
        path = self.root / run_id / f"{stage}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_silent_wav(path, audio_format, duration_ms)
        return RecordingArtifact(
            stage=stage,
            uri=path.as_uri(),
            format=audio_format,
            duration_ms=duration_ms,
        )


def _write_silent_wav(
    path: Path,
    audio_format: dict[str, Any],
    duration_ms: float,
) -> None:
    rate = audio_format.get("rate") or audio_format.get("output_rate")
    channels = audio_format.get("channels")
    if not isinstance(rate, int) or not isinstance(channels, int):
        raise ValueError(
            f"stage audio format must include integer rate and channels: {audio_format}"
        )
    if duration_ms < 0:
        raise ValueError(f"stage duration must be non-negative: {duration_ms}")

    sample_width = _sample_width(audio_format.get("encoding"))
    frame_count = round(rate * (duration_ms / 1000.0))
    silence_frame = _silence_frame(sample_width, channels)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(rate)
        wav.writeframes(silence_frame * frame_count)


def _silence_frame(sample_width: int, channels: int) -> bytes:
    if sample_width == 1:
        return bytes([128]) * channels
    return b"\x00" * sample_width * channels


def _sample_width(encoding: object) -> int:
    if encoding == "mulaw":
        return 1
    return 2
