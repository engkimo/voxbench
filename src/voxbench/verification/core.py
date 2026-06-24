"""Phase 2 signal invariant verification."""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from voxbench.engine_harness.models import MetricArtifact, RecordingArtifact

SUPPORTED_INVARIANTS = {"duration_preserving", "level_preserving", "isochronous"}
DEFAULT_DURATION_TOLERANCE_RATIO = 0.02
DEFAULT_LEVEL_TOLERANCE_DB = 3.0
DEFAULT_FRAME_RATIO_TOLERANCE = 0.02
DEFAULT_CADENCE_JITTER_TOLERANCE_RATIO = 0.25


@dataclass(frozen=True)
class VerificationResult:
    stage: str
    invariant: str
    passed: bool
    observed: dict[str, Any]
    expected: dict[str, Any]
    detail: str


@dataclass(frozen=True)
class WavStats:
    duration_ms: float
    rms: float | None
    sample_count: int


def verify_recordings(
    *,
    resolved_config: dict[str, Any],
    recordings: list[RecordingArtifact],
    metrics: list[MetricArtifact] | None = None,
    duration_tolerance_ratio: float = DEFAULT_DURATION_TOLERANCE_RATIO,
    level_tolerance_db: float = DEFAULT_LEVEL_TOLERANCE_DB,
    frame_ratio_tolerance: float = DEFAULT_FRAME_RATIO_TOLERANCE,
    cadence_jitter_tolerance_ratio: float = DEFAULT_CADENCE_JITTER_TOLERANCE_RATIO,
) -> list[VerificationResult]:
    """Verify declared signal invariants across adjacent stage recordings."""

    by_stage = {recording.stage: recording for recording in recordings}
    metrics_by_stage = _metrics_by_stage(metrics or [])
    previous: RecordingArtifact | None = None
    results: list[VerificationResult] = []

    transport = resolved_config["spec"].get("transport", {})
    config_ptime_ms = transport.get("ptime_ms") if isinstance(transport, dict) else None

    for stage in resolved_config["spec"]["media"]["pipeline"]:
        current = by_stage.get(stage["type"])

        invariants = [
            invariant
            for invariant in stage.get("invariants_applicable", [])
            if invariant in SUPPORTED_INVARIANTS
        ]
        if "isochronous" in invariants:
            result = _verify_isochronous(
                stage=stage["type"],
                metrics=metrics_by_stage.get(stage["type"], {}),
                config_ptime_ms=config_ptime_ms,
                frame_ratio_tolerance=frame_ratio_tolerance,
                cadence_jitter_tolerance_ratio=cadence_jitter_tolerance_ratio,
            )
            if result is not None:
                results.append(result)

        signal_invariants = [
            invariant
            for invariant in invariants
            if invariant in {"duration_preserving", "level_preserving"}
        ]
        if current is not None and previous is not None and signal_invariants:
            previous_stats = _try_read_wav_stats(previous)
            current_stats = _try_read_wav_stats(current)
            if previous_stats is None or current_stats is None:
                previous = current
                continue
            for invariant in signal_invariants:
                if invariant == "duration_preserving":
                    result = _verify_duration(
                        stage=current.stage,
                        previous=previous_stats,
                        current=current_stats,
                        tolerance_ratio=duration_tolerance_ratio,
                    )
                else:
                    result = _verify_level(
                        stage=current.stage,
                        previous=previous_stats,
                        current=current_stats,
                        tolerance_db=level_tolerance_db,
                    )
                if result is not None:
                    results.append(result)

        if current is not None:
            previous = current

    return results


def _try_read_wav_stats(recording: RecordingArtifact) -> WavStats | None:
    try:
        return _read_wav_stats(recording)
    except (OSError, ValueError, wave.Error):
        return None


def _verify_duration(
    *,
    stage: str,
    previous: WavStats,
    current: WavStats,
    tolerance_ratio: float,
) -> VerificationResult | None:
    if previous.duration_ms <= 0 or current.duration_ms <= 0:
        return None

    ratio = current.duration_ms / previous.duration_ms
    lower = 1.0 - tolerance_ratio
    upper = 1.0 + tolerance_ratio
    passed = lower <= ratio <= upper
    return VerificationResult(
        stage=stage,
        invariant="duration_preserving",
        passed=passed,
        observed={
            "input_duration_ms": previous.duration_ms,
            "output_duration_ms": current.duration_ms,
            "duration_ratio": ratio,
        },
        expected={"duration_ratio_min": lower, "duration_ratio_max": upper},
        detail="duration ratio within tolerance" if passed else "duration ratio outside tolerance",
    )


def _verify_level(
    *,
    stage: str,
    previous: WavStats,
    current: WavStats,
    tolerance_db: float,
) -> VerificationResult | None:
    if previous.rms is None or current.rms is None or previous.rms <= 0 or current.rms <= 0:
        return None

    delta_db = 20.0 * math.log10(current.rms / previous.rms)
    passed = abs(delta_db) <= tolerance_db
    return VerificationResult(
        stage=stage,
        invariant="level_preserving",
        passed=passed,
        observed={
            "input_rms": previous.rms,
            "output_rms": current.rms,
            "delta_db": delta_db,
        },
        expected={"delta_db_min": -tolerance_db, "delta_db_max": tolerance_db},
        detail="RMS delta within tolerance" if passed else "RMS delta outside tolerance",
    )


def _verify_isochronous(
    *,
    stage: str,
    metrics: dict[str, float],
    config_ptime_ms: object,
    frame_ratio_tolerance: float,
    cadence_jitter_tolerance_ratio: float,
) -> VerificationResult | None:
    frames_in = metrics.get("frames_in")
    frames_out = metrics.get("frames_out")
    jitter_ms = metrics.get("frame_cadence_jitter_ms")
    expected_interval_ms = metrics.get("expected_frame_interval_ms")
    if expected_interval_ms is None and isinstance(config_ptime_ms, int | float):
        expected_interval_ms = float(config_ptime_ms)

    if (
        frames_in is None
        or frames_out is None
        or jitter_ms is None
        or expected_interval_ms is None
        or frames_in <= 0
        or expected_interval_ms <= 0
    ):
        return None

    frame_ratio = frames_out / frames_in
    ratio_lower = 1.0 - frame_ratio_tolerance
    ratio_upper = 1.0 + frame_ratio_tolerance
    jitter_limit_ms = expected_interval_ms * cadence_jitter_tolerance_ratio
    ratio_passed = ratio_lower <= frame_ratio <= ratio_upper
    jitter_passed = jitter_ms <= jitter_limit_ms
    passed = ratio_passed and jitter_passed
    return VerificationResult(
        stage=stage,
        invariant="isochronous",
        passed=passed,
        observed={
            "frames_in": frames_in,
            "frames_out": frames_out,
            "frames_out_in_ratio": frame_ratio,
            "frame_cadence_jitter_ms": jitter_ms,
        },
        expected={
            "frames_out_in_ratio_min": ratio_lower,
            "frames_out_in_ratio_max": ratio_upper,
            "frame_cadence_jitter_max_ms": jitter_limit_ms,
        },
        detail="frame cadence within tolerance" if passed else "frame cadence outside tolerance",
    )


def _read_wav_stats(recording: RecordingArtifact) -> WavStats:
    path = _file_uri_to_path(recording.uri)
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        frame_count = wav.getnframes()
        rate = wav.getframerate()
        frames = wav.readframes(frame_count)

    duration_ms = (frame_count / rate) * 1000.0 if rate > 0 else 0.0
    if frame_count == 0:
        return WavStats(duration_ms=duration_ms, rms=None, sample_count=0)

    if sample_width != 2:
        return WavStats(duration_ms=duration_ms, rms=None, sample_count=frame_count * channels)

    sample_count = len(frames) // sample_width
    if sample_count == 0:
        return WavStats(duration_ms=duration_ms, rms=None, sample_count=0)

    samples = struct.unpack(f"<{sample_count}h", frames)
    square_sum = sum(sample * sample for sample in samples)
    return WavStats(
        duration_ms=duration_ms,
        rms=math.sqrt(square_sum / sample_count),
        sample_count=sample_count,
    )


def _file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"verification only supports file:// recording URIs for now: {uri}")
    return Path(unquote(parsed.path))


def _metrics_by_stage(metrics: list[MetricArtifact]) -> dict[str, dict[str, float]]:
    grouped: dict[str, dict[str, float]] = {}
    for metric in metrics:
        if metric.stage is None:
            continue
        grouped.setdefault(metric.stage, {})[metric.name] = metric.value
    return grouped
