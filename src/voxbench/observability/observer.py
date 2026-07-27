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
PCM16_FULL_SCALE = 32768.0
PCM16_SILENCE_THRESHOLD_DBFS = -60.0
PCM16_SILENCE_AMPLITUDE = round(
    PCM16_FULL_SCALE * 10 ** (PCM16_SILENCE_THRESHOLD_DBFS / 20.0)
)
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
_SENSITIVE_REFERENCE_MARKERS = ("http://", "https://", "<@", "slack://")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _validate_safe_reference(
    value: str,
    *,
    field_name: str,
    max_length: int,
) -> None:
    if (
        not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
        or any(marker in value.lower() for marker in _SENSITIVE_REFERENCE_MARKERS)
    ):
        raise ValueError(
            f"{field_name} must contain 1 to {max_length} safe alias characters"
        )


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
class RtpPacket:
    stream_alias: str
    direction: RtpDirection
    sequence_number: int
    rtp_timestamp: int
    payload_type: int
    clock_rate_hz: int
    marker: bool = False
    clock_domain: str = "control_plane_wall"
    alignment_uncertainty_ms: float | None = None
    ts: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        _validate_safe_reference(
            self.stream_alias,
            field_name="stream_alias",
            max_length=128,
        )
        if self.direction not in {"received", "sent"}:
            raise ValueError("direction must be received or sent")
        if (
            isinstance(self.sequence_number, bool)
            or not isinstance(self.sequence_number, int)
            or not 0 <= self.sequence_number <= 0xFFFF
        ):
            raise ValueError("sequence_number must fit in 16 bits")
        if (
            isinstance(self.rtp_timestamp, bool)
            or not isinstance(self.rtp_timestamp, int)
            or not 0 <= self.rtp_timestamp <= 0xFFFFFFFF
        ):
            raise ValueError("rtp_timestamp must fit in 32 bits")
        if (
            isinstance(self.payload_type, bool)
            or not isinstance(self.payload_type, int)
            or not 0 <= self.payload_type <= 127
        ):
            raise ValueError("payload_type must fit in 7 bits")
        if (
            isinstance(self.clock_rate_hz, bool)
            or not isinstance(self.clock_rate_hz, int)
            or not 0 < self.clock_rate_hz <= 384_000
        ):
            raise ValueError("clock_rate_hz must be between 1 and 384000")
        _validate_safe_reference(
            self.clock_domain,
            field_name="clock_domain",
            max_length=64,
        )
        if (
            self.alignment_uncertainty_ms is not None
            and (
                isinstance(self.alignment_uncertainty_ms, bool)
                or not isinstance(self.alignment_uncertainty_ms, int | float)
                or not math.isfinite(self.alignment_uncertainty_ms)
                or self.alignment_uncertainty_ms < 0
            )
        ):
            raise ValueError(
                "alignment_uncertainty_ms must be finite and non-negative"
            )
        if not isinstance(self.ts, datetime):
            raise ValueError("ts must be a datetime")


def rtp_packet_from_datagram(
    datagram: bytes,
    *,
    stream_alias: str,
    direction: RtpDirection,
    clock_rate_hz: int,
    clock_domain: str = "control_plane_wall",
    alignment_uncertainty_ms: float | None = None,
    ts: datetime | None = None,
) -> RtpPacket:
    """Decode only the fixed RTP header fields required for cadence evidence."""

    if len(datagram) < 12:
        raise ValueError("RTP datagram must contain the 12-byte fixed header")
    version = datagram[0] >> 6
    if version != 2:
        raise ValueError("RTP datagram version must be 2")
    return RtpPacket(
        stream_alias=stream_alias,
        direction=direction,
        sequence_number=int.from_bytes(datagram[2:4], byteorder="big"),
        rtp_timestamp=int.from_bytes(datagram[4:8], byteorder="big"),
        payload_type=datagram[1] & 0x7F,
        clock_rate_hz=clock_rate_hz,
        marker=bool(datagram[1] & 0x80),
        clock_domain=clock_domain,
        alignment_uncertainty_ms=alignment_uncertainty_ms,
        ts=ts or _utc_now(),
    )


@dataclass(frozen=True)
class RtpCaptureHealthSnapshot:
    stream_alias: str
    direction: RtpDirection
    observed_packet_count: int
    capture_drop_count: int
    decode_error_count: int
    capture_drop_counter_supported: bool
    continuity: Literal["verified", "compromised", "not independently verified"]
    window_duration_ms: float
    clock_domain: str
    alignment_uncertainty_ms: float | None
    ts: datetime

    def __post_init__(self) -> None:
        _validate_safe_reference(
            self.stream_alias,
            field_name="stream_alias",
            max_length=128,
        )
        _validate_safe_reference(
            self.clock_domain,
            field_name="clock_domain",
            max_length=64,
        )
        if self.direction not in {"received", "sent"}:
            raise ValueError("direction must be received or sent")
        for field_name, value in (
            ("observed_packet_count", self.observed_packet_count),
            ("capture_drop_count", self.capture_drop_count),
            ("decode_error_count", self.decode_error_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if not isinstance(self.capture_drop_counter_supported, bool):
            raise ValueError("capture_drop_counter_supported must be boolean")
        if self.continuity not in {
            "verified",
            "compromised",
            "not independently verified",
        }:
            raise ValueError("continuity must be a supported safe state")
        expected_continuity = (
            "compromised"
            if self.capture_drop_count or self.decode_error_count
            else "verified"
            if self.capture_drop_counter_supported
            else "not independently verified"
        )
        if self.continuity != expected_continuity:
            raise ValueError("continuity must match capture health counters")
        if (
            isinstance(self.window_duration_ms, bool)
            or not isinstance(self.window_duration_ms, int | float)
            or not math.isfinite(self.window_duration_ms)
            or self.window_duration_ms < 0
        ):
            raise ValueError("window_duration_ms must be finite and non-negative")
        if (
            self.alignment_uncertainty_ms is not None
            and (
                isinstance(self.alignment_uncertainty_ms, bool)
                or not isinstance(self.alignment_uncertainty_ms, int | float)
                or not math.isfinite(self.alignment_uncertainty_ms)
                or self.alignment_uncertainty_ms < 0
            )
        ):
            raise ValueError(
                "alignment_uncertainty_ms must be finite and non-negative"
            )
        if not isinstance(self.ts, datetime):
            raise ValueError("ts must be a datetime")


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


OBSERVATION_BATCH_LIMITS = {
    "metrics": 500,
    "audio_chunks": 64,
    "sip_events": 64,
    "rtp_stats": 64,
    "timeline_events": 128,
}


def _bounded_observation_batches(batch: ObservationBatch) -> tuple[ObservationBatch, ...]:
    batch_count = max(
        1,
        *(
            (len(getattr(batch, field_name)) + limit - 1) // limit
            for field_name, limit in OBSERVATION_BATCH_LIMITS.items()
        ),
    )
    batches: list[ObservationBatch] = []
    for index in range(batch_count):
        values = {
            field_name: getattr(batch, field_name)[index * limit : (index + 1) * limit]
            for field_name, limit in OBSERVATION_BATCH_LIMITS.items()
        }
        candidate = ObservationBatch(run_id=batch.run_id, **values)
        if candidate.item_count:
            batches.append(candidate)
    return tuple(batches)


def _merge_observation_batches(
    run_id: str,
    batches: tuple[ObservationBatch, ...],
) -> ObservationBatch:
    return ObservationBatch(
        run_id=run_id,
        metrics=tuple(item for batch in batches for item in batch.metrics),
        audio_chunks=tuple(item for batch in batches for item in batch.audio_chunks),
        sip_events=tuple(item for batch in batches for item in batch.sip_events),
        rtp_stats=tuple(item for batch in batches for item in batch.rtp_stats),
        timeline_events=tuple(item for batch in batches for item in batch.timeline_events),
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
        self._rtp_packet_ordinal = 0
        self._rtp_capture_health_ordinal = 0
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

        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        if channels <= 0:
            raise ValueError("channels must be positive")
        if len(output_pcm_s16le) % (2 * channels):
            raise ValueError("output PCM16LE must contain complete channel frames")
        observed_at = ts or _utc_now()
        input_rms = pcm_s16le_rms(input_pcm_s16le)
        output_rms = pcm_s16le_rms(output_pcm_s16le)
        sample_peak_dbfs, full_scale_sample_pct, silence_sample_pct = (
            pcm_s16le_quality(output_pcm_s16le)
        )
        sample_count = len(output_pcm_s16le) // 2
        frame_count = sample_count / channels
        chunk_duration_ms = frame_count / sample_rate_hz * 1000.0
        metrics = [
            MetricPoint(stage=stage, name="input_rms", value=input_rms, ts=observed_at),
            MetricPoint(stage=stage, name="output_rms", value=output_rms, ts=observed_at),
            MetricPoint(
                stage=stage,
                name="delta_db",
                value=_delta_db(input_rms, output_rms),
                ts=observed_at,
            ),
            MetricPoint(
                stage=stage,
                name="sample_peak_dbfs",
                value=sample_peak_dbfs,
                ts=observed_at,
            ),
            MetricPoint(
                stage=stage,
                name="full_scale_sample_pct",
                value=full_scale_sample_pct,
                ts=observed_at,
            ),
            MetricPoint(
                stage=stage,
                name="silence_sample_pct",
                value=silence_sample_pct,
                ts=observed_at,
            ),
            MetricPoint(
                stage=stage,
                name="audio_chunk_duration_ms",
                value=chunk_duration_ms,
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

    def observe_rtp_packet(self, packet: RtpPacket) -> None:
        """Record only safe RTP header/cadence evidence, never packet payload."""

        with self._lock:
            packet_ordinal = self._rtp_packet_ordinal
            self._rtp_packet_ordinal += 1
            self._timeline_events.append(
                TimelineEvent(
                    event_id=f"rtp-packet:{packet_ordinal}",
                    category="transport",
                    name="rtp.packet_arrived",
                    source="rtp_packet_header_observer",
                    correlation_alias=packet.stream_alias,
                    clock_domain=packet.clock_domain,
                    alignment_uncertainty_ms=packet.alignment_uncertainty_ms,
                    direction=packet.direction,
                    stream_alias=packet.stream_alias,
                    attributes={
                        "sequence_number": packet.sequence_number,
                        "rtp_timestamp": packet.rtp_timestamp,
                        "payload_type": packet.payload_type,
                        "clock_rate_hz": packet.clock_rate_hz,
                        "marker": packet.marker,
                    },
                    ts=packet.ts,
                )
            )

    def observe_rtp_capture_health(
        self,
        snapshot: RtpCaptureHealthSnapshot,
    ) -> None:
        """Record one safe capture-health window with an observer-unique event ID."""

        with self._lock:
            event_ordinal = self._rtp_capture_health_ordinal
            self._rtp_capture_health_ordinal += 1
            self._timeline_events.append(
                TimelineEvent(
                    event_id=f"rtp-capture-health:{event_ordinal}",
                    category="transport",
                    name="rtp.capture_health_reported",
                    source="rtp_packet_tap_adapter",
                    correlation_alias=snapshot.stream_alias,
                    clock_domain=snapshot.clock_domain,
                    alignment_uncertainty_ms=snapshot.alignment_uncertainty_ms,
                    direction=snapshot.direction,
                    stream_alias=snapshot.stream_alias,
                    attributes={
                        "observed_packet_count": snapshot.observed_packet_count,
                        "capture_drop_count": snapshot.capture_drop_count,
                        "decode_error_count": snapshot.decode_error_count,
                        "capture_drop_counter_supported": (
                            snapshot.capture_drop_counter_supported
                        ),
                        "capture_point_continuity": snapshot.continuity,
                        "window_duration_ms": snapshot.window_duration_ms,
                    },
                    ts=snapshot.ts,
                )
            )

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
        batches = _bounded_observation_batches(batch)
        sent_count = 0
        for index, bounded_batch in enumerate(batches):
            try:
                self.transport.send(bounded_batch)
            except Exception:
                unsent = _merge_observation_batches(self.run_id, batches[index:])
                with self._lock:
                    self._metrics[0:0] = unsent.metrics
                    self._audio_chunks[0:0] = unsent.audio_chunks
                    self._sip_events[0:0] = unsent.sip_events
                    self._rtp_stats[0:0] = unsent.rtp_stats
                    self._timeline_events[0:0] = unsent.timeline_events
                raise
            sent_count += bounded_batch.item_count
        return sent_count

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


class RtpPacketTapAdapter:
    """Project transient RTP datagrams and explicit capture health into an observer."""

    def __init__(
        self,
        observer: VoxBenchObserver,
        *,
        stream_alias: str,
        direction: RtpDirection,
        clock_rate_hz: int,
        clock_domain: str = "control_plane_wall",
        alignment_uncertainty_ms: float | None = None,
        capture_drop_counter_supported: bool = False,
        started_at: datetime | None = None,
    ) -> None:
        validation_packet = RtpPacket(
            stream_alias=stream_alias,
            direction=direction,
            sequence_number=0,
            rtp_timestamp=0,
            payload_type=0,
            clock_rate_hz=clock_rate_hz,
            clock_domain=clock_domain,
            alignment_uncertainty_ms=alignment_uncertainty_ms,
        )
        self.observer = observer
        self.stream_alias = validation_packet.stream_alias
        self.direction = validation_packet.direction
        self.clock_rate_hz = validation_packet.clock_rate_hz
        self.clock_domain = validation_packet.clock_domain
        self.alignment_uncertainty_ms = validation_packet.alignment_uncertainty_ms
        if not isinstance(capture_drop_counter_supported, bool):
            raise ValueError("capture_drop_counter_supported must be boolean")
        if started_at is not None and not isinstance(started_at, datetime):
            raise ValueError("started_at must be a datetime")
        self._capture_drop_counter_supported = capture_drop_counter_supported
        self._window_started_at = started_at or _utc_now()
        self._observed_packet_count = 0
        self._capture_drop_count = 0
        self._decode_error_count = 0
        self._lock = Lock()

    def observe_datagram(
        self,
        datagram: bytes,
        *,
        ts: datetime | None = None,
    ) -> RtpPacket:
        """Decode transient header fields and queue one safe packet observation."""

        try:
            packet = rtp_packet_from_datagram(
                datagram,
                stream_alias=self.stream_alias,
                direction=self.direction,
                clock_rate_hz=self.clock_rate_hz,
                clock_domain=self.clock_domain,
                alignment_uncertainty_ms=self.alignment_uncertainty_ms,
                ts=ts,
            )
        except ValueError:
            with self._lock:
                self._decode_error_count += 1
            raise
        self.observer.observe_rtp_packet(packet)
        with self._lock:
            self._observed_packet_count += 1
        return packet

    def record_capture_drop(self, count: int = 1) -> None:
        """Record drops reported by the owning queue, socket, or capture library."""

        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("capture drop count must be a positive integer")
        with self._lock:
            self._capture_drop_counter_supported = True
            self._capture_drop_count += count

    def report_health(self, *, ts: datetime | None = None) -> RtpCaptureHealthSnapshot:
        """Emit and reset one bounded capture-health reporting window."""

        reported_at = ts or _utc_now()
        with self._lock:
            snapshot = self._snapshot(reported_at)
            self._window_started_at = reported_at
            self._observed_packet_count = 0
            self._capture_drop_count = 0
            self._decode_error_count = 0
        self.observer.observe_rtp_capture_health(snapshot)
        return snapshot

    def snapshot(self, *, ts: datetime | None = None) -> RtpCaptureHealthSnapshot:
        """Return safe in-process counters without emitting or resetting them."""

        reported_at = ts or _utc_now()
        with self._lock:
            return self._snapshot(reported_at)

    def _snapshot(self, reported_at: datetime) -> RtpCaptureHealthSnapshot:
        if self._capture_drop_count or self._decode_error_count:
            continuity: Literal[
                "verified",
                "compromised",
                "not independently verified",
            ] = "compromised"
        elif self._capture_drop_counter_supported:
            continuity = "verified"
        else:
            continuity = "not independently verified"
        return RtpCaptureHealthSnapshot(
            stream_alias=self.stream_alias,
            direction=self.direction,
            observed_packet_count=self._observed_packet_count,
            capture_drop_count=self._capture_drop_count,
            decode_error_count=self._decode_error_count,
            capture_drop_counter_supported=self._capture_drop_counter_supported,
            continuity=continuity,
            window_duration_ms=max(
                0.0,
                (_normalized_datetime(reported_at) - _normalized_datetime(
                    self._window_started_at
                )).total_seconds()
                * 1000,
            ),
            clock_domain=self.clock_domain,
            alignment_uncertainty_ms=self.alignment_uncertainty_ms,
            ts=reported_at,
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


def _normalized_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def pcm_s16le_quality(pcm: bytes) -> tuple[float, float, float]:
    """Return sample peak dBFS, full-scale sample %, and digital-silence sample %."""

    if len(pcm) % 2:
        raise ValueError("PCM16LE data length must be divisible by two")
    sample_count = 0
    peak = 0
    full_scale_count = 0
    silence_count = 0
    for (value,) in struct.iter_unpack("<h", pcm):
        sample_count += 1
        peak = max(peak, abs(value))
        full_scale_count += value in {-32768, 32767}
        silence_count += abs(value) <= PCM16_SILENCE_AMPLITUDE
    if sample_count == 0:
        return -120.0, 0.0, 0.0
    sample_peak_dbfs = (
        20.0 * math.log10(peak / PCM16_FULL_SCALE) if peak > 0 else -120.0
    )
    return (
        sample_peak_dbfs,
        full_scale_count / sample_count * 100.0,
        silence_count / sample_count * 100.0,
    )


def _delta_db(input_rms: float, output_rms: float) -> float:
    if input_rms <= 0.0 or output_rms <= 0.0:
        return 0.0
    return 20.0 * math.log10(output_rms / input_rms)
