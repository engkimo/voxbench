"""Provider-agnostic observation hooks for realtime voice applications."""

from __future__ import annotations

import base64
import json
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SipDirection = Literal["in", "out"]
RtpDirection = Literal["received", "sent"]
TimelineCategory = Literal[
    "conversation",
    "signaling",
    "transport",
    "buffer",
    "pipeline",
    "provider",
    "runtime",
    "session",
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


@dataclass(frozen=True)
class MetricPoint:
    name: str
    value: float
    stage: str | None = None
    ts: datetime = field(default_factory=_utc_now)

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "name": self.name,
            "value": self.value,
            "ts": _timestamp(self.ts),
        }


@dataclass(frozen=True)
class AudioChunk:
    stage: str
    pcm_s16le: bytes
    sample_rate_hz: int
    channels: int = 1
    ts: datetime = field(default_factory=_utc_now)

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "pcm_s16le_base64": base64.b64encode(self.pcm_s16le).decode("ascii"),
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "ts": _timestamp(self.ts),
        }


@dataclass(frozen=True)
class SipEvent:
    method: str
    direction: SipDirection
    call_id: str | None = None
    status_code: int | None = None
    summary_alias: str | None = None
    ts: datetime = field(default_factory=_utc_now)

    def to_payload(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "method": self.method,
            "direction": self.direction,
            "ts": _timestamp(self.ts),
            "status_code": self.status_code,
            "summary_alias": self.summary_alias,
        }


@dataclass(frozen=True)
class RtpStats:
    jitter_ms: float | None = None
    loss_pct: float | None = None
    mos: float | None = None
    direction: RtpDirection | None = None
    rtt_ms: float | None = None
    ts: datetime = field(default_factory=_utc_now)

    def to_payload(self) -> dict[str, Any]:
        return {
            "ts": _timestamp(self.ts),
            "jitter_ms": self.jitter_ms,
            "loss_pct": self.loss_pct,
            "mos": self.mos,
            "direction": self.direction,
            "rtt_ms": self.rtt_ms,
        }


@dataclass(frozen=True)
class TimelineEvent:
    event_id: str
    category: TimelineCategory
    name: str
    source: str
    correlation_alias: str | None = None
    clock_domain: str = "control_plane_wall"
    alignment_uncertainty_ms: float | None = None
    direction: str | None = None
    stage: str | None = None
    stream_alias: str | None = None
    attributes: dict[str, str | int | float | bool | None] = field(default_factory=dict)
    ts: datetime = field(default_factory=_utc_now)

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "category": self.category,
            "name": self.name,
            "ts": _timestamp(self.ts),
            "clock_domain": self.clock_domain,
            "alignment_uncertainty_ms": self.alignment_uncertainty_ms,
            "direction": self.direction,
            "stage": self.stage,
            "stream_alias": self.stream_alias,
            "source": self.source,
            "correlation_alias": self.correlation_alias,
            "attributes": self.attributes,
        }


@dataclass(frozen=True)
class ObservationBatch:
    run_id: str
    metrics: tuple[MetricPoint, ...] = ()
    audio_chunks: tuple[AudioChunk, ...] = ()
    sip_events: tuple[SipEvent, ...] = ()
    rtp_stats: tuple[RtpStats, ...] = ()
    timeline_events: tuple[TimelineEvent, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "metrics": [metric.to_payload() for metric in self.metrics],
            "audio_chunks": [chunk.to_payload() for chunk in self.audio_chunks],
            "sip_events": [event.to_payload() for event in self.sip_events],
            "rtp_stats": [stats.to_payload() for stats in self.rtp_stats],
            "timeline_events": [event.to_payload() for event in self.timeline_events],
        }

    @property
    def item_count(self) -> int:
        return (
            len(self.metrics)
            + len(self.audio_chunks)
            + len(self.sip_events)
            + len(self.rtp_stats)
            + len(self.timeline_events)
        )


class ObservationTransport(Protocol):
    """Destination for batches produced by :class:`VoxBenchObserver`."""

    def send(self, batch: ObservationBatch) -> None:
        """Send one observation batch or raise without silently dropping it."""


class HttpObservationTransport:
    """Small dependency-free HTTP client for the VoxBench Control Plane."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def start_run(self, run_payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/runs/observed", dict(run_payload))

    def send(self, batch: ObservationBatch) -> None:
        self._request("POST", "/v1/observations", batch.to_payload())

    def complete_run(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/runs/{run_id}/complete", {})

    def fail_run(self, run_id: str, failure_alias: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/runs/{run_id}/fail",
            {"failure_alias": failure_alias},
        )

    def _request(self, method: str, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"content-type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"VoxBench request failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"VoxBench Control Plane is unavailable: {exc.reason}") from exc
        if not isinstance(result, dict):
            raise RuntimeError("VoxBench Control Plane returned a non-object response")
        return result


class VoxBenchObserver:
    """Collect audio and telemetry without coupling to a provider or media framework."""

    def __init__(self, run_id: str, transport: ObservationTransport) -> None:
        self.run_id = run_id
        self.transport = transport
        self._metrics: list[MetricPoint] = []
        self._audio_chunks: list[AudioChunk] = []
        self._sip_events: list[SipEvent] = []
        self._rtp_stats: list[RtpStats] = []
        self._timeline_events: list[TimelineEvent] = []
        self._lock = Lock()

    def observe_stage_audio(
        self,
        *,
        stage: str,
        input_pcm_s16le: bytes,
        output_pcm_s16le: bytes,
        sample_rate_hz: int,
        channels: int = 1,
        gain_applied: float | None = None,
        record_output: bool = True,
        ts: datetime | None = None,
    ) -> None:
        """Measure one stage boundary and optionally retain its output as a WAV tap."""

        observed_at = ts or _utc_now()
        input_rms = pcm_s16le_rms(input_pcm_s16le)
        output_rms = pcm_s16le_rms(output_pcm_s16le)
        metrics = [
            MetricPoint(stage=stage, name="input_rms", value=input_rms, ts=observed_at),
            MetricPoint(stage=stage, name="output_rms", value=output_rms, ts=observed_at),
            MetricPoint(
                stage=stage,
                name="delta_db",
                value=_delta_db(input_rms, output_rms),
                ts=observed_at,
            ),
        ]
        if gain_applied is not None:
            metrics.append(
                MetricPoint(
                    stage=stage,
                    name="gain_applied",
                    value=float(gain_applied),
                    ts=observed_at,
                )
            )
        with self._lock:
            self._metrics.extend(metrics)
            if record_output and output_pcm_s16le:
                self._audio_chunks.append(
                    AudioChunk(
                        stage=stage,
                        pcm_s16le=output_pcm_s16le,
                        sample_rate_hz=sample_rate_hz,
                        channels=channels,
                        ts=observed_at,
                    )
                )

    def observe_metric(
        self,
        name: str,
        value: float,
        *,
        stage: str | None = None,
        ts: datetime | None = None,
    ) -> None:
        with self._lock:
            self._metrics.append(
                MetricPoint(stage=stage, name=name, value=float(value), ts=ts or _utc_now())
            )

    def observe_sip_event(self, event: SipEvent) -> None:
        with self._lock:
            self._sip_events.append(event)

    def observe_rtp_stats(self, stats: RtpStats) -> None:
        with self._lock:
            self._rtp_stats.append(stats)

    def observe_timeline_event(self, event: TimelineEvent) -> None:
        with self._lock:
            self._timeline_events.append(event)

    def flush(self) -> int:
        """Send pending observations and restore them if the transport fails."""

        with self._lock:
            batch = ObservationBatch(
                run_id=self.run_id,
                metrics=tuple(self._metrics),
                audio_chunks=tuple(self._audio_chunks),
                sip_events=tuple(self._sip_events),
                rtp_stats=tuple(self._rtp_stats),
                timeline_events=tuple(self._timeline_events),
            )
            self._metrics.clear()
            self._audio_chunks.clear()
            self._sip_events.clear()
            self._rtp_stats.clear()
            self._timeline_events.clear()
        if batch.item_count == 0:
            return 0
        try:
            self.transport.send(batch)
        except Exception:
            with self._lock:
                self._metrics[0:0] = batch.metrics
                self._audio_chunks[0:0] = batch.audio_chunks
                self._sip_events[0:0] = batch.sip_events
                self._rtp_stats[0:0] = batch.rtp_stats
                self._timeline_events[0:0] = batch.timeline_events
            raise
        return batch.item_count

    @property
    def pending_count(self) -> int:
        with self._lock:
            return (
                len(self._metrics)
                + len(self._audio_chunks)
                + len(self._sip_events)
                + len(self._rtp_stats)
                + len(self._timeline_events)
            )


def pcm_s16le_rms(pcm: bytes) -> float:
    """Return RMS for signed little-endian 16-bit PCM without optional audio deps."""

    if len(pcm) % 2:
        raise ValueError("PCM16LE data length must be divisible by two")
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return 0.0
    samples = struct.iter_unpack("<h", pcm)
    square_sum = sum(sample[0] * sample[0] for sample in samples)
    return math.sqrt(square_sum / sample_count)


def _delta_db(input_rms: float, output_rms: float) -> float:
    if input_rms <= 0.0 or output_rms <= 0.0:
        return 0.0
    return 20.0 * math.log10(output_rms / input_rms)
