"""Recording storage sinks for StageTap artifacts."""

from __future__ import annotations

import ipaddress
import re
import tempfile
import wave
from contextlib import suppress
from pathlib import Path
from threading import BoundedSemaphore
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlparse

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


class MinioObjectResponseLike(Protocol):
    def read(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


class MinioReadClientLike(Protocol):
    def get_object(
        self,
        *,
        bucket_name: str,
        object_name: str,
        length: int,
    ) -> MinioObjectResponseLike: ...


class RemoteRecordingReader(Protocol):
    timeout_seconds: float

    def read_wav(self, *, uri: str, run_id: str, stage: str) -> bytes:
        """Read one strictly identified, bounded WAV object."""


class RemoteRecordingReadError(RuntimeError):
    """Safe base error for remote recording reads."""


class RemoteRecordingIdentityError(RemoteRecordingReadError):
    pass


class RemoteRecordingTooLargeError(RemoteRecordingReadError):
    pass


class RemoteRecordingUnavailableError(RemoteRecordingReadError):
    pass


class RemoteRecordingBusyError(RemoteRecordingReadError):
    pass


class RemoteRecordingTimeoutError(RemoteRecordingReadError):
    pass


class RemoteRecordingInvalidContentError(RemoteRecordingReadError):
    pass


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


class MinioRecordingReader:
    """Bounded reader for credential-free URIs produced by MinioRecordingSink."""

    def __init__(
        self,
        *,
        client: MinioReadClientLike,
        bucket: str,
        prefix: str = "recordings",
        max_bytes: int = 10 * 1024 * 1024,
        timeout_seconds: float = 5.0,
        max_concurrent_reads: int = 2,
    ) -> None:
        _validate_bucket(bucket)
        if max_bytes < 44:
            raise ValueError("max_bytes must allow a WAV header")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_concurrent_reads <= 0:
            raise ValueError("max_concurrent_reads must be positive")
        if max_bytes * max_concurrent_reads > 128 * 1024 * 1024:
            raise ValueError("remote recording in-flight capacity must not exceed 128 MiB")
        self.client = client
        self.bucket = bucket
        self.prefix = _validate_prefix(prefix)
        self.max_bytes = max_bytes
        self.timeout_seconds = timeout_seconds
        self._read_slots = BoundedSemaphore(max_concurrent_reads)

    def read_wav(self, *, uri: str, run_id: str, stage: str) -> bytes:
        _validate_object_segment(run_id, field="run_id")
        _validate_object_segment(stage, field="stage")
        object_name = "/".join(
            part for part in (self.prefix, run_id, f"{stage}.wav") if part
        )
        parsed = urlparse(uri)
        if (
            parsed.scheme != "s3"
            or parsed.netloc != self.bucket
            or parsed.path != f"/{object_name}"
            or parsed.params
            or parsed.query
            or parsed.fragment
        ):
            raise RemoteRecordingIdentityError("remote-recording-identity-mismatch")
        if not self._read_slots.acquire(blocking=False):
            raise RemoteRecordingBusyError("remote-recording-reader-busy")
        try:
            return self._read_object(object_name)
        finally:
            self._read_slots.release()

    def _read_object(self, object_name: str) -> bytes:
        try:
            response = self.client.get_object(
                bucket_name=self.bucket,
                object_name=object_name,
                length=self.max_bytes + 1,
            )
        except Exception:
            raise RemoteRecordingUnavailableError("remote-recording-read-failed") from None

        deadline = monotonic() + self.timeout_seconds
        payload = bytearray()
        try:
            while len(payload) <= self.max_bytes:
                if monotonic() >= deadline:
                    raise RemoteRecordingTimeoutError("remote-recording-read-timeout")
                amount = min(64 * 1024, self.max_bytes + 1 - len(payload))
                chunk = response.read(amount)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise RemoteRecordingUnavailableError("remote-recording-read-failed")
                payload.extend(chunk)
        except RemoteRecordingReadError:
            raise
        except Exception:
            raise RemoteRecordingUnavailableError("remote-recording-read-failed") from None
        finally:
            with suppress(Exception):
                response.close()

        if len(payload) > self.max_bytes:
            raise RemoteRecordingTooLargeError("remote-recording-too-large")
        if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
            raise RemoteRecordingInvalidContentError("remote-recording-invalid-content")
        return bytes(payload)


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
