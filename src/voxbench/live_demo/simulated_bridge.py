"""Simulated live bridge for the softphone realtime demo.

This module produces real WAV tap artifacts and timeline metrics without
opening a SIP/RTP or provider network connection.
"""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from voxbench.engine_harness.models import (
    MetricArtifact,
    RecordingArtifact,
    TimelineEventArtifact,
)
from voxbench.engine_harness.plan import build_stage_plan

SipDirection = Literal["in", "out"]
LiveDemoScenario = Literal["clean", "rtp-gap"]


@dataclass(frozen=True)
class SimulatedSipEvent:
    call_id: str | None
    method: str
    direction: SipDirection
    ts: datetime
    status_code: int | None = None
    summary_alias: str | None = None


@dataclass(frozen=True)
class SimulatedRtpStat:
    ts: datetime
    jitter_ms: float | None = None
    loss_pct: float | None = None
    mos: float | None = None
    direction: Literal["received", "sent"] | None = None
    rtt_ms: float | None = None


@dataclass(frozen=True)
class LiveDemoBridgeResult:
    recordings: list[RecordingArtifact]
    metrics: list[MetricArtifact]
    sip_events: list[SimulatedSipEvent]
    rtp_stats: list[SimulatedRtpStat]
    timeline_events: list[TimelineEventArtifact]


def run_simulated_live_bridge(
    *,
    run_id: str,
    call_id: str | None,
    resolved_config: dict[str, Any],
    artifact_root: Path,
    started_at: datetime,
    input_rms: float = 2600.0,
    duration_ms: int = 1200,
    scenario: LiveDemoScenario = "clean",
) -> LiveDemoBridgeResult:
    recordings: list[RecordingArtifact] = []
    metrics: list[MetricArtifact] = []

    stage_input_rms = max(input_rms, 0.0)
    ts = datetime.now(UTC)
    for stage_plan in build_stage_plan(resolved_config):
        stage_config = _stage_config(resolved_config, stage_plan.stage)
        stage_output_rms, gain_applied = _stage_output_rms(
            stage=stage_plan.stage,
            stage_config=stage_config,
            input_rms=stage_input_rms,
        )
        recording = _write_stage_tone_wav(
            artifact_root=artifact_root,
            run_id=run_id,
            stage=stage_plan.stage,
            audio_format=stage_plan.format,
            rms=stage_output_rms,
            duration_ms=duration_ms,
        )
        recordings.append(recording)
        metrics.extend(
            _stage_metrics(
                stage=stage_plan.stage,
                input_rms=stage_input_rms,
                output_rms=stage_output_rms,
                gain_applied=gain_applied,
                ts=ts,
                ptime_ms=resolved_config.get("spec", {}).get("transport", {}).get("ptime_ms"),
                stage_config=stage_config,
            )
        )
        stage_input_rms = stage_output_rms
        ts += timedelta(milliseconds=120)

    return LiveDemoBridgeResult(
        recordings=recordings,
        metrics=metrics,
        sip_events=_sip_events(call_id=call_id, started_at=started_at),
        rtp_stats=_rtp_stats(started_at=started_at),
        timeline_events=_rtp_packet_events(started_at=started_at, scenario=scenario),
    )


def _stage_config(resolved_config: dict[str, Any], stage_name: str) -> dict[str, Any]:
    for stage in resolved_config.get("spec", {}).get("media", {}).get("pipeline", []):
        if isinstance(stage, dict) and stage.get("type") == stage_name:
            return stage
    return {}


def _stage_output_rms(
    *,
    stage: str,
    stage_config: dict[str, Any],
    input_rms: float,
) -> tuple[float, float | None]:
    if stage == "agc":
        params = stage_config.get("params", {})
        target_rms = _positive_float(params.get("target_rms"), default=input_rms)
        max_gain = _positive_float(params.get("max_gain"), default=1.0)
        noise_floor = _positive_float(params.get("noise_floor"), default=0.0)
        if input_rms <= 0 or input_rms < noise_floor:
            return input_rms, 1.0
        gain = min(target_rms / input_rms, max_gain)
        return input_rms * gain, gain

    if stage == "limiter":
        ceiling = _positive_float(stage_config.get("params", {}).get("ceiling"), default=1.0)
        return min(input_rms, 32767.0 * ceiling), None

    return input_rms, None


def _stage_metrics(
    *,
    stage: str,
    input_rms: float,
    output_rms: float,
    gain_applied: float | None,
    ts: datetime,
    ptime_ms: object,
    stage_config: dict[str, Any],
) -> list[MetricArtifact]:
    metrics = [
        MetricArtifact(stage=stage, name="input_rms", value=input_rms, ts=ts),
        MetricArtifact(stage=stage, name="output_rms", value=output_rms, ts=ts),
        MetricArtifact(stage=stage, name="delta_db", value=_delta_db(input_rms, output_rms), ts=ts),
    ]
    if gain_applied is not None:
        metrics.append(MetricArtifact(stage=stage, name="gain_applied", value=gain_applied, ts=ts))

    if "isochronous" in stage_config.get("invariants_applicable", []) and isinstance(
        ptime_ms, int | float
    ):
        metrics.extend(
            [
                MetricArtifact(stage=stage, name="frames_in", value=60.0, ts=ts),
                MetricArtifact(stage=stage, name="frames_out", value=60.0, ts=ts),
                MetricArtifact(stage=stage, name="frame_cadence_jitter_ms", value=0.4, ts=ts),
                MetricArtifact(
                    stage=stage,
                    name="expected_frame_interval_ms",
                    value=float(ptime_ms),
                    ts=ts,
                ),
            ]
        )
    return metrics


def _write_stage_tone_wav(
    *,
    artifact_root: Path,
    run_id: str,
    stage: str,
    audio_format: dict[str, Any],
    rms: float,
    duration_ms: int,
) -> RecordingArtifact:
    rate = audio_format.get("rate") or audio_format.get("output_rate")
    channels = audio_format.get("channels", 1)
    if not isinstance(rate, int) or not isinstance(channels, int):
        raise ValueError(f"stage audio format must include integer rate/channels: {audio_format}")

    path = artifact_root.resolve() / run_id / f"{stage}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = round(rate * (duration_ms / 1000.0))
    sample_width = 1 if audio_format.get("encoding") == "mulaw" else 2
    frames = _tone_frames(
        frame_count=frame_count,
        rate=rate,
        channels=channels,
        sample_width=sample_width,
        rms=rms,
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(rate)
        wav.writeframes(frames)

    return RecordingArtifact(
        stage=stage,
        uri=path.as_uri(),
        format=audio_format,
        duration_ms=float(duration_ms),
    )


def _tone_frames(
    *,
    frame_count: int,
    rate: int,
    channels: int,
    sample_width: int,
    rms: float,
) -> bytes:
    frequency = 440.0
    if sample_width == 1:
        amplitude = min(127.0, max(0.0, (rms / 32767.0) * 127.0 * math.sqrt(2.0)))
        samples = bytearray()
        for index in range(frame_count):
            value = round(128.0 + amplitude * math.sin((2.0 * math.pi * frequency * index) / rate))
            samples.extend(bytes([min(255, max(0, value))]) * channels)
        return bytes(samples)

    amplitude = min(32767.0, max(0.0, rms * math.sqrt(2.0)))
    samples = bytearray()
    for index in range(frame_count):
        value = round(amplitude * math.sin((2.0 * math.pi * frequency * index) / rate))
        packed = struct.pack("<h", min(32767, max(-32768, value)))
        samples.extend(packed * channels)
    return bytes(samples)


def _sip_events(*, call_id: str | None, started_at: datetime) -> list[SimulatedSipEvent]:
    return [
        SimulatedSipEvent(
            call_id=call_id,
            method="INVITE",
            direction="in",
            ts=started_at + timedelta(milliseconds=20),
            summary_alias="local-softphone-invite",
        ),
        SimulatedSipEvent(
            call_id=call_id,
            method="100",
            direction="out",
            status_code=100,
            ts=started_at + timedelta(milliseconds=80),
            summary_alias="asterisk-trying",
        ),
        SimulatedSipEvent(
            call_id=call_id,
            method="200",
            direction="out",
            status_code=200,
            ts=started_at + timedelta(milliseconds=180),
            summary_alias="local-bridge-answered",
        ),
        SimulatedSipEvent(
            call_id=call_id,
            method="ACK",
            direction="in",
            ts=started_at + timedelta(milliseconds=230),
            summary_alias="local-softphone-ack",
        ),
        SimulatedSipEvent(
            call_id=call_id,
            method="BYE",
            direction="in",
            ts=started_at + timedelta(milliseconds=1250),
            summary_alias="simulated-call-ended",
        ),
    ]


def _rtp_stats(*, started_at: datetime) -> list[SimulatedRtpStat]:
    return [
        SimulatedRtpStat(
            ts=started_at + timedelta(milliseconds=260),
            jitter_ms=0.7,
            loss_pct=0.0,
            mos=4.4,
        ),
        SimulatedRtpStat(
            ts=started_at + timedelta(milliseconds=760),
            jitter_ms=1.2,
            loss_pct=0.0,
            mos=4.3,
        ),
        SimulatedRtpStat(
            ts=started_at + timedelta(milliseconds=1180),
            jitter_ms=1.5,
            loss_pct=0.1,
            mos=4.2,
        ),
    ]


def _rtp_packet_events(
    *,
    started_at: datetime,
    scenario: LiveDemoScenario,
) -> list[TimelineEventArtifact]:
    if scenario == "clean":
        packet_points = (
            (1000, 0, 300),
            (1001, 160, 320),
            (1002, 320, 340),
            (1003, 480, 360),
        )
    else:
        packet_points = (
            (1000, 0, 300),
            (1001, 160, 320),
            (1003, 320, 340),
            (1004, 480, 500),
        )

    return [
        TimelineEventArtifact(
            event_id=f"rtp-packet:{ordinal}",
            category="transport",
            name="rtp.packet_arrived",
            source="simulated_rtp_packet_observer",
            correlation_alias="simulated-caller-audio",
            direction="received",
            stream_alias="simulated-caller-audio",
            attributes={
                "sequence_number": sequence_number,
                "rtp_timestamp": rtp_timestamp,
                "payload_type": 0,
                "clock_rate_hz": 8000,
                "marker": ordinal == 0,
            },
            ts=started_at + timedelta(milliseconds=arrival_ms),
        )
        for ordinal, (sequence_number, rtp_timestamp, arrival_ms) in enumerate(
            packet_points
        )
    ]


def _positive_float(value: object, *, default: float) -> float:
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return default


def _delta_db(input_rms: float, output_rms: float) -> float:
    if input_rms <= 0 or output_rms <= 0:
        return 0.0
    return 20.0 * math.log10(output_rms / input_rms)
