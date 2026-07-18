"""Recording storage sinks for StageTap artifacts."""

from __future__ import annotations

import ipaddress
import re
import tempfile
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


class MinioClientLike(Protocol):
    def fput_object(
        self,
        *,
        bucket_name: str,
        object_name: str,
        file_path: str,
        content_type: str,
    ) -> Any: ...


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


class MinioRecordingSink:
    """MinIO/S3-compatible sink with credential-free artifact URIs."""

    def __init__(
        self,
        *,
        client: MinioClientLike,
        bucket: str,
        prefix: str = "recordings",
    ) -> None:
        _validate_bucket(bucket)
        self.client = client
        self.bucket = bucket
        self.prefix = _validate_prefix(prefix)

    def write_stage_wav(
        self,
        *,
        run_id: str,
        stage: str,
        audio_format: dict[str, Any],
        duration_ms: float,
    ) -> RecordingArtifact:
        _validate_object_segment(run_id, field="run_id")
        _validate_object_segment(stage, field="stage")
        parts = [part for part in (self.prefix, run_id, f"{stage}.wav") if part]
        object_name = "/".join(parts)
        with tempfile.TemporaryDirectory(prefix="voxbench-recording-") as temporary_dir:
            path = Path(temporary_dir) / "recording.wav"
            _write_silent_wav(path, audio_format, duration_ms)
            self.client.fput_object(
                bucket_name=self.bucket,
                object_name=object_name,
                file_path=str(path),
                content_type="audio/wav",
            )
        return RecordingArtifact(
            stage=stage,
            uri=f"s3://{self.bucket}/{object_name}",
            format=dict(audio_format),
            duration_ms=duration_ms,
        )


_BUCKET_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_OBJECT_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validate_bucket(bucket: str) -> None:
    if not _BUCKET_NAME.fullmatch(bucket) or ".." in bucket:
        raise ValueError("bucket must be a valid DNS-style S3 bucket name")
    try:
        ipaddress.ip_address(bucket)
    except ValueError:
        return
    raise ValueError("bucket must not be formatted as an IP address")


def _validate_prefix(prefix: str) -> str:
    normalized = prefix.strip("/")
    if not normalized:
        return ""
    for segment in normalized.split("/"):
        _validate_object_segment(segment, field="prefix")
    return normalized


def _validate_object_segment(value: str, *, field: str) -> None:
    if not _OBJECT_SEGMENT.fullmatch(value):
        raise ValueError(f"{field} must be a safe object-key segment")


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
