"""Deterministic offline artifacts for synthetic verification scenarios."""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from voxbench.engine_harness.models import MetricArtifact, RecordingArtifact


@dataclass(frozen=True)
class SyntheticAudioSpec:
    sample_rate_hz: int
    channels: int
    duration_seconds: float
    amplitude: int
    frequency_hz: float


@dataclass(frozen=True)
class SyntheticStageDegradation:
    duration_scale: float = 1.0
    level_scale: float = 1.0
    frames_in: float | None = None
    frames_out: float | None = None
    frame_cadence_jitter_ms: float | None = None


@dataclass(frozen=True)
class SyntheticArtifacts:
    reference_uri: str
    recordings: list[RecordingArtifact]
    metrics: list[MetricArtifact]


def generate_synthetic_artifacts(
    *,
    resolved_config: dict[str, Any],
    output_root: Path,
    audio_spec: SyntheticAudioSpec,
    degradations: dict[str, SyntheticStageDegradation] | None = None,
) -> SyntheticArtifacts:
    """Generate local WAV recordings and cadence metrics for a resolved config."""

    _validate_audio_spec(audio_spec)
    degradations = degradations or {}
    output_root.mkdir(parents=True, exist_ok=True)

    reference_path = output_root / "reference.wav"
    _write_sine_wav(reference_path, audio_spec=audio_spec)

    recordings = []
    metrics = []
    for stage in resolved_config["spec"]["media"]["pipeline"]:
        stage_name = stage["type"]
        degradation = degradations.get(stage_name, SyntheticStageDegradation())
        stage_spec = SyntheticAudioSpec(
            sample_rate_hz=audio_spec.sample_rate_hz,
            channels=audio_spec.channels,
            duration_seconds=audio_spec.duration_seconds * degradation.duration_scale,
            amplitude=round(audio_spec.amplitude * degradation.level_scale),
            frequency_hz=audio_spec.frequency_hz,
        )
        _validate_audio_spec(stage_spec)
        stage_path = output_root / f"{stage_name}.wav"
        _write_sine_wav(stage_path, audio_spec=stage_spec)
        recordings.append(
            RecordingArtifact(
                stage=stage_name,
                uri=stage_path.as_uri(),
                format={
                    "encoding": "pcm16",
                    "rate": audio_spec.sample_rate_hz,
                    "channels": audio_spec.channels,
                },
                duration_ms=stage_spec.duration_seconds * 1000.0,
            )
        )
        metrics.extend(
            _cadence_metrics(
                resolved_config=resolved_config,
                stage=stage,
                duration_seconds=audio_spec.duration_seconds,
                degradation=degradation,
            )
        )

    return SyntheticArtifacts(
        reference_uri=reference_path.as_uri(),
        recordings=recordings,
        metrics=metrics,
    )


def _cadence_metrics(
    *,
    resolved_config: dict[str, Any],
    stage: dict[str, Any],
    duration_seconds: float,
    degradation: SyntheticStageDegradation,
) -> list[MetricArtifact]:
    if "isochronous" not in stage.get("invariants_applicable", []):
        return []

    ptime_ms = resolved_config["spec"].get("transport", {}).get("ptime_ms")
    if not isinstance(ptime_ms, int | float) or ptime_ms <= 0:
        return []

    nominal_frames = duration_seconds * 1000.0 / ptime_ms
    frames_in = degradation.frames_in if degradation.frames_in is not None else nominal_frames
    frames_out = degradation.frames_out if degradation.frames_out is not None else nominal_frames
    jitter_ms = (
        degradation.frame_cadence_jitter_ms
        if degradation.frame_cadence_jitter_ms is not None
        else 0.0
    )
    ts = datetime.now(UTC)
    stage_name = stage["type"]
    return [
        MetricArtifact(stage=stage_name, name="frames_in", value=frames_in, ts=ts),
        MetricArtifact(stage=stage_name, name="frames_out", value=frames_out, ts=ts),
        MetricArtifact(stage=stage_name, name="frame_cadence_jitter_ms", value=jitter_ms, ts=ts),
        MetricArtifact(
            stage=stage_name,
            name="expected_frame_interval_ms",
            value=float(ptime_ms),
            ts=ts,
        ),
    ]


def _write_sine_wav(path: Path, *, audio_spec: SyntheticAudioSpec) -> None:
    frame_count = round(audio_spec.sample_rate_hz * audio_spec.duration_seconds)
    frames = bytearray()
    for index in range(frame_count):
        sample = round(
            audio_spec.amplitude
            * math.sin(2.0 * math.pi * audio_spec.frequency_hz * index / audio_spec.sample_rate_hz)
        )
        packed = struct.pack("<h", sample)
        for _ in range(audio_spec.channels):
            frames.extend(packed)

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(audio_spec.channels)
        wav.setsampwidth(2)
        wav.setframerate(audio_spec.sample_rate_hz)
        wav.writeframes(bytes(frames))


def _validate_audio_spec(audio_spec: SyntheticAudioSpec) -> None:
    if audio_spec.sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if audio_spec.channels <= 0:
        raise ValueError("channels must be positive")
    if audio_spec.duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if not 0 <= audio_spec.amplitude <= 32767:
        raise ValueError("amplitude must fit signed 16-bit PCM")
    if audio_spec.frequency_hz <= 0:
        raise ValueError("frequency_hz must be positive")
