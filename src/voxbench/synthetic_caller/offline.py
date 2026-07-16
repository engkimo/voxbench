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
from voxbench.engine_harness.plan import build_stage_plan
from voxbench.media import mulaw_to_pcm16le, pcm16le_to_mulaw


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
    stage_references: list[StageReferenceArtifact]
    recordings: list[RecordingArtifact]
    metrics: list[MetricArtifact]


@dataclass(frozen=True)
class StageReferenceArtifact:
    stage: str
    uri: str
    stage_format: dict[str, Any]
    comparison_format: dict[str, Any]
    duration_ms: float
    transformations: tuple[str, ...]
    comparison_ready: bool
    blocked_reason: str | None = None


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
    stage_references = _stage_references(
        resolved_config=resolved_config,
        output_root=output_root,
        audio_spec=audio_spec,
    )

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
        stage_references=stage_references,
        recordings=recordings,
        metrics=metrics,
    )


def _stage_references(
    *,
    resolved_config: dict[str, Any],
    output_root: Path,
    audio_spec: SyntheticAudioSpec,
) -> list[StageReferenceArtifact]:
    references_root = output_root / "references"
    references_root.mkdir(parents=True, exist_ok=True)
    references = []
    for stage_plan in build_stage_plan(resolved_config):
        rate = stage_plan.format.get("rate") or stage_plan.format.get("output_rate")
        channels = stage_plan.format.get("channels")
        if not isinstance(rate, int) or rate <= 0:
            raise ValueError(f"stage '{stage_plan.stage}' reference format requires a rate")
        if not isinstance(channels, int) or channels <= 0:
            raise ValueError(f"stage '{stage_plan.stage}' reference format requires channels")

        reference_spec = SyntheticAudioSpec(
            sample_rate_hz=rate,
            channels=channels,
            duration_seconds=audio_spec.duration_seconds,
            amplitude=audio_spec.amplitude,
            frequency_hz=audio_spec.frequency_hz,
        )
        _validate_audio_spec(reference_spec)
        encoding = stage_plan.format.get("encoding")
        codec_round_trip = "mulaw" if encoding == "mulaw" and rate == 8_000 else None
        path = references_root / f"{stage_plan.stage}.wav"
        _write_sine_wav(
            path,
            audio_spec=reference_spec,
            codec_round_trip=codec_round_trip,
        )

        comparison_ready = encoding in {"pcm16", "linear16"} or codec_round_trip is not None
        transformations = _reference_transformations(
            source_spec=audio_spec,
            target_spec=reference_spec,
            stage_encoding=encoding,
        )
        references.append(
            StageReferenceArtifact(
                stage=stage_plan.stage,
                uri=path.as_uri(),
                stage_format=dict(stage_plan.format),
                comparison_format={
                    "encoding": "pcm16",
                    "rate": rate,
                    "channels": channels,
                },
                duration_ms=audio_spec.duration_seconds * 1_000.0,
                transformations=transformations,
                comparison_ready=comparison_ready,
                blocked_reason=(
                    None
                    if comparison_ready
                    else f"codec-round-trip-required:{encoding}"
                ),
            )
        )
    return references


def _reference_transformations(
    *,
    source_spec: SyntheticAudioSpec,
    target_spec: SyntheticAudioSpec,
    stage_encoding: object,
) -> tuple[str, ...]:
    transformations = []
    if source_spec.sample_rate_hz != target_spec.sample_rate_hz:
        transformations.append(
            f"resample:{source_spec.sample_rate_hz}->{target_spec.sample_rate_hz}"
        )
    if source_spec.channels != target_spec.channels:
        transformations.append(f"channel-map:{source_spec.channels}->{target_spec.channels}")
    if stage_encoding not in {None, "pcm16", "linear16"}:
        transformations.append(f"codec-round-trip:{stage_encoding}")
    return tuple(transformations)


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


def _write_sine_wav(
    path: Path,
    *,
    audio_spec: SyntheticAudioSpec,
    codec_round_trip: str | None = None,
) -> None:
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

    encoded_frames = bytes(frames)
    if codec_round_trip == "mulaw":
        encoded_frames = mulaw_to_pcm16le(pcm16le_to_mulaw(encoded_frames))
    elif codec_round_trip is not None:
        raise ValueError(f"unsupported reference codec round-trip: {codec_round_trip}")

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(audio_spec.channels)
        wav.setsampwidth(2)
        wav.setframerate(audio_spec.sample_rate_hz)
        wav.writeframes(encoded_frames)


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
    if audio_spec.frequency_hz >= audio_spec.sample_rate_hz / 2:
        raise ValueError("frequency_hz must be below the Nyquist frequency")
