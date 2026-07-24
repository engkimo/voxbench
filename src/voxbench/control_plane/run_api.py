"""Run API models and router."""

import asyncio
import base64
import binascii
import hmac
import json
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from threading import Thread
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import quote, unquote, urlparse
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, status
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.websockets import WebSocketDisconnect
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from voxbench.control_plane.audio_session import (
    REMOTE_AUDIO_SESSION_COOKIE,
    AudioSessionLoginError,
    RemoteAudioSessionAuth,
)
from voxbench.control_plane.job_queue import RunJobLease, RunJobQueue
from voxbench.control_plane.models import (
    Metric as MetricRow,
)
from voxbench.control_plane.models import (
    Recording as RecordingRow,
)
from voxbench.control_plane.models import (
    RtpStat as RtpStatRow,
)
from voxbench.control_plane.models import (
    Run as RunRow,
)
from voxbench.control_plane.models import (
    RunJob as RunJobRow,
)
from voxbench.control_plane.models import (
    SipEvent as SipEventRow,
)
from voxbench.control_plane.models import (
    Span as SpanRow,
)
from voxbench.control_plane.models import (
    TimelineEvent as TimelineEventRow,
)
from voxbench.control_plane.models import (
    Verification as VerificationRow,
)
from voxbench.control_plane.repository_config import (
    RepositoryReadiness,
    memory_repository_readiness,
)
from voxbench.control_plane.storage_config import (
    StorageReadiness,
    injected_storage_readiness,
    local_storage_readiness,
)
from voxbench.engine_harness.harness import EngineHarness
from voxbench.engine_harness.models import (
    MetricArtifact,
    RecordingArtifact,
    SpanArtifact,
    TimelineEventArtifact,
)
from voxbench.engine_harness.storage import (
    LocalRecordingSink,
    RecordingSink,
    RemoteRecordingBusyError,
    RemoteRecordingIdentityError,
    RemoteRecordingInvalidContentError,
    RemoteRecordingReader,
    RemoteRecordingTimeoutError,
    RemoteRecordingTooLargeError,
    RemoteRecordingUnavailableError,
)
from voxbench.live_demo.simulated_bridge import run_simulated_live_bridge
from voxbench.realtime_providers import GeminiLiveProvider, OpenAIRealtimeProvider
from voxbench.registry.errors import RegistryError
from voxbench.registry.service import RegistryService
from voxbench.verification import VerificationResult, verify_recordings

EnvironmentProfile = Literal["local", "dev", "demo", "integration", "staging"]
ReadinessStatus = Literal["pass", "fail", "unknown"]
SipDirection = Literal["in", "out"]
RtpDirection = Literal["received", "sent"]
LiveDemoProvider = Literal["gemini-live", "openai-realtime"]
ProviderConnectionState = Literal[
    "not_applicable",
    "pending",
    "connected",
    "exhausted",
    "unobserved",
]
RtpCollectorState = Literal["inactive", "connected", "collecting", "failed"]
CrossSessionTrendState = Literal["insufficient", "stable", "increasing"]
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
TimelineSeverity = Literal["info", "warning", "error"]
TimelineConfidence = Literal["certain", "high", "medium", "low"]

CROSS_SESSION_METRIC_NAMES = ("active_tasks", "memory_rss_bytes")
CROSS_SESSION_MIN_SAMPLES = 3

DEFAULT_READINESS_ITEMS: tuple[tuple[str, str], ...] = (
    ("ai_phone_setup_complete", "AI phone setup complete"),
    (
        "intermediate_db_environment_registration_complete",
        "Intermediate DB/environment registration complete",
    ),
    ("connection_route_verified", "Connection route verified"),
    (
        "expected_codec_sample_rate_cadence_declared",
        "Expected codec/sample rate/cadence declared",
    ),
    ("recording_taps_enabled", "Recording taps enabled"),
    ("host_metrics_enabled", "Host metrics enabled"),
    ("secret_references_present", "Secret references present"),
)

SENSITIVE_TEXT_MARKERS = ("http://", "https://", "<@", "slack://")
EXAMPLE_ROOT = Path(__file__).resolve().parents[3] / "examples"
EXAMPLE_CONFIG_PATH = EXAMPLE_ROOT / "configs" / "valid-baseline.json"
EXAMPLE_MANIFEST_PATHS = (
    EXAMPLE_ROOT / "manifests" / "engine" / "asterisk.json",
    EXAMPLE_ROOT / "manifests" / "provider" / "gemini.json",
    EXAMPLE_ROOT / "manifests" / "processor" / "resampler.json",
    EXAMPLE_ROOT / "manifests" / "processor" / "agc.json",
    EXAMPLE_ROOT / "manifests" / "processor" / "limiter.json",
    EXAMPLE_ROOT / "manifests" / "processor" / "serializer.json",
)
LIVE_DEMO_CONFIG_PATHS = {
    "gemini-live": EXAMPLE_ROOT / "configs" / "live-demo-gemini-live.json",
    "openai-realtime": EXAMPLE_ROOT / "configs" / "live-demo-openai-realtime.json",
}
LIVE_DEMO_PROVIDER_MANIFEST_PATHS = {
    "gemini-live": EXAMPLE_ROOT / "manifests" / "provider" / "gemini-live.json",
    "openai-realtime": EXAMPLE_ROOT / "manifests" / "provider" / "openai-realtime.json",
}
LIVE_DEMO_PROCESSOR_MANIFEST_PATHS = (
    EXAMPLE_ROOT / "manifests" / "engine" / "asterisk.json",
    EXAMPLE_ROOT / "manifests" / "processor" / "resampler.json",
    EXAMPLE_ROOT / "manifests" / "processor" / "agc.json",
    EXAMPLE_ROOT / "manifests" / "processor" / "limiter.json",
    EXAMPLE_ROOT / "manifests" / "processor" / "serializer.json",
)


def _validate_reference_text(value: str, field_name: str) -> str:
    stripped = value.strip()
    lowered = stripped.lower()
    if any(marker in lowered for marker in SENSITIVE_TEXT_MARKERS):
        raise ValueError(f"{field_name} must use an alias/reference, not a URL or Slack ID")
    return stripped


def _default_readiness_checklist() -> list["ReadinessChecklistItem"]:
    return [
        ReadinessChecklistItem(item_id=item_id, label=label)
        for item_id, label in DEFAULT_READINESS_ITEMS
    ]


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_name: str
    configs: list[dict[str, Any]]
    manifests: list[dict[str, Any]]
    call_id: str | None = None
    environment: "RunEnvironmentMetadata" = Field(default_factory=lambda: RunEnvironmentMetadata())
    readiness_checklist: list["ReadinessChecklistItem"] = Field(
        default_factory=_default_readiness_checklist,
    )

    @model_validator(mode="after")
    def require_unique_checklist_items(self) -> "RunCreateRequest":
        item_ids = [item.item_id for item in self.readiness_checklist]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("readiness_checklist item_id values must be unique")
        return self


class LiveDemoRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: LiveDemoProvider = "gemini-live"
    call_id: str | None = "local-softphone-simulated"
    dry_run: bool = True
    duration_ms: int = Field(default=1200, ge=100, le=30_000)
    input_rms: float = Field(default=2600.0, gt=0.0, le=32_000.0)
    target_rms: int | None = Field(default=None, gt=0)
    max_gain: float | None = Field(default=None, gt=0.0)
    noise_floor: int | None = Field(default=None, ge=0)

    @field_validator("call_id")
    @classmethod
    def validate_call_id(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_reference_text(value, info.field_name)


class RunFailureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_alias: str = Field(min_length=1, max_length=128)

    @field_validator("failure_alias")
    @classmethod
    def validate_failure_alias(cls, value: str, info) -> str:
        return _validate_reference_text(value, info.field_name)


class RunEnvironmentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_profile: EnvironmentProfile = "local"
    server_alias: str | None = None
    integration_target_alias: str | None = None
    environment_snapshot_hash: str | None = None
    started_from: str | None = None
    operator_note: str | None = None
    manual_blockers: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    related_internal_ref: str | None = None
    secret_ref_names: list[str] = Field(default_factory=list)

    @field_validator(
        "server_alias",
        "integration_target_alias",
        "environment_snapshot_hash",
        "started_from",
        "operator_note",
        "related_internal_ref",
    )
    @classmethod
    def validate_optional_reference_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_reference_text(value, info.field_name)

    @field_validator("manual_blockers", "tags", "secret_ref_names")
    @classmethod
    def validate_reference_lists(cls, values: list[str], info) -> list[str]:
        return [_validate_reference_text(value, info.field_name) for value in values]


class ReadinessChecklistItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    label: str
    status: ReadinessStatus = "unknown"
    note: str | None = None

    @field_validator("item_id", "label", "note")
    @classmethod
    def validate_reference_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_reference_text(value, info.field_name)


class RecordingResponse(BaseModel):
    stage: str
    uri: str
    format: dict[str, Any]
    duration_ms: float


class SpanResponse(BaseModel):
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    start_ns: int
    end_ns: int
    attrs: dict[str, Any]


class VerificationResponse(BaseModel):
    stage: str
    invariant: str
    passed: bool
    observed: dict[str, Any]
    expected: dict[str, Any]
    detail: str


class MetricResponse(BaseModel):
    stage: str | None
    name: str
    value: float
    ts: datetime


class TimelineMetricPoint(BaseModel):
    ts: float
    name: str
    value: float


class TimelineViolation(BaseModel):
    invariant: str
    passed: bool
    detail: str
    observed: dict[str, Any]
    expected: dict[str, Any]


class TimelineStageLane(BaseModel):
    stage: str
    metrics: list[TimelineMetricPoint]
    violations: list[TimelineViolation]


class TimelineRecording(BaseModel):
    stage: str
    uri: str
    format: dict[str, Any]
    duration_ms: float


class TimelineTypedEvent(BaseModel):
    event_id: str
    category: TimelineCategory
    name: str
    t_rel_ms: float
    clock_domain: str
    alignment_uncertainty_ms: float | None = None
    direction: str | None = None
    stage: str | None = None
    stream_alias: str | None = None
    source: str
    correlation_alias: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TimelineTypedInterval(BaseModel):
    interval_id: str
    category: TimelineCategory
    name: str
    start_ms: float
    end_ms: float
    clock_domain: str
    alignment_uncertainty_ms: float | None = None
    direction: str | None = None
    stage: str | None = None
    stream_alias: str | None = None
    source: str
    correlation_alias: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class TimelineSeriesPoint(BaseModel):
    t_rel_ms: float
    value: float


class TimelineTypedSeries(BaseModel):
    series_id: str
    category: TimelineCategory
    name: str
    unit: str | None = None
    clock_domain: str
    alignment_uncertainty_ms: float | None = None
    direction: str | None = None
    stage: str | None = None
    stream_alias: str | None = None
    source: str
    points: list[TimelineSeriesPoint]


class TimelineTypedArtifact(BaseModel):
    artifact_id: str
    category: TimelineCategory
    name: str
    kind: Literal["audio", "trace", "report", "capture", "config"]
    start_ms: float
    duration_ms: float | None = None
    stage: str | None = None
    direction: str | None = None
    artifact_ref: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class TimelineIncident(BaseModel):
    incident_id: str
    rule_id: str
    category: TimelineCategory
    severity: TimelineSeverity
    title: str
    summary: str
    start_ms: float
    end_ms: float
    confidence: TimelineConfidence
    stage: str | None = None
    direction: str | None = None
    observed: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class SipEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    call_id: str | None = None
    method: str
    direction: SipDirection
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status_code: int | None = None
    summary_alias: str | None = None

    @field_validator("call_id", "method", "summary_alias")
    @classmethod
    def validate_reference_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_reference_text(value, info.field_name)


class SipEventResponse(BaseModel):
    call_id: str | None
    method: str
    direction: SipDirection
    ts: datetime
    status_code: int | None
    summary_alias: str | None


class TimelineSipEvent(BaseModel):
    ts: float
    call_id: str | None
    method: str
    direction: SipDirection
    status_code: int | None
    summary_alias: str | None


class RtpStatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    run_id: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    jitter_ms: float | None = Field(default=None, ge=0)
    loss_pct: float | None = Field(default=None, ge=0, le=100)
    mos: float | None = Field(default=None, ge=0, le=5)
    direction: RtpDirection | None = None
    rtt_ms: float | None = Field(default=None, ge=0)


class RtpStatResponse(BaseModel):
    ts: datetime
    jitter_ms: float | None
    loss_pct: float | None
    mos: float | None
    direction: RtpDirection | None
    rtt_ms: float | None


class MetricObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    stage: str | None = None
    name: str
    value: float
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("stage", "name")
    @classmethod
    def validate_reference_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_reference_text(value, info.field_name)


class AudioChunkObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    pcm_s16le_base64: str = Field(max_length=1_400_000)
    sample_rate_hz: int = Field(ge=8_000, le=48_000)
    channels: int = Field(default=1, ge=1, le=2)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("stage")
    @classmethod
    def validate_stage(cls, value: str, info) -> str:
        return _validate_reference_text(value, info.field_name)


class SipEventObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str | None = None
    method: str
    direction: SipDirection
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status_code: int | None = None
    summary_alias: str | None = None

    @field_validator("call_id", "method", "summary_alias")
    @classmethod
    def validate_reference_text(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_reference_text(value, info.field_name)


class RtpStatObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    jitter_ms: float | None = Field(default=None, ge=0)
    loss_pct: float | None = Field(default=None, ge=0, le=100)
    mos: float | None = Field(default=None, ge=0, le=5)
    direction: RtpDirection | None = None
    rtt_ms: float | None = Field(default=None, ge=0)


class TimelineEventObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    event_id: str = Field(min_length=1, max_length=128)
    category: TimelineCategory
    name: str = Field(min_length=1, max_length=128)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    clock_domain: str = Field(default="control_plane_wall", min_length=1, max_length=64)
    alignment_uncertainty_ms: float | None = Field(default=None, ge=0)
    direction: str | None = Field(default=None, min_length=1, max_length=32)
    stage: str | None = Field(default=None, min_length=1, max_length=255)
    stream_alias: str | None = Field(default=None, min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=128)
    correlation_alias: str | None = Field(default=None, min_length=1, max_length=128)
    attributes: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=16,
    )

    @field_validator(
        "event_id",
        "name",
        "clock_domain",
        "direction",
        "stage",
        "stream_alias",
        "source",
        "correlation_alias",
    )
    @classmethod
    def validate_reference_fields(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_reference_text(value, info.field_name)

    @field_validator("attributes")
    @classmethod
    def validate_attributes(
        cls,
        value: dict[str, str | int | float | bool | None],
    ) -> dict[str, str | int | float | bool | None]:
        result: dict[str, str | int | float | bool | None] = {}
        for key, item in value.items():
            safe_key = _validate_reference_text(key, "attributes key")
            if not safe_key or len(safe_key) > 64:
                raise ValueError("attribute keys must contain 1 to 64 characters")
            if isinstance(item, str):
                item = _validate_reference_text(item, f"attribute '{safe_key}'")
                if len(item) > 256:
                    raise ValueError("string attribute values must not exceed 256 characters")
            result[safe_key] = item
        return result


class ObservationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    metrics: list[MetricObservationRequest] = Field(default_factory=list, max_length=500)
    audio_chunks: list[AudioChunkObservationRequest] = Field(
        default_factory=list,
        max_length=64,
    )
    sip_events: list[SipEventObservationRequest] = Field(default_factory=list, max_length=64)
    rtp_stats: list[RtpStatObservationRequest] = Field(default_factory=list, max_length=64)
    timeline_events: list[TimelineEventObservationRequest] = Field(
        default_factory=list,
        max_length=128,
    )

    @model_validator(mode="after")
    def require_observation(self) -> "ObservationBatchRequest":
        if not (
            self.metrics
            or self.audio_chunks
            or self.sip_events
            or self.rtp_stats
            or self.timeline_events
        ):
            raise ValueError("at least one observation is required")
        event_ids = [event.event_id for event in self.timeline_events]
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("timeline event_id values must be unique within a batch")
        return self


class ObservationBatchResponse(BaseModel):
    run_id: str
    metric_count: int
    audio_chunk_count: int
    sip_event_count: int
    rtp_stat_count: int
    timeline_event_count: int
    recording_count: int


class TimelineRtpStat(BaseModel):
    ts: float
    jitter_ms: float | None
    loss_pct: float | None
    mos: float | None
    direction: RtpDirection | None
    rtt_ms: float | None


class ReadinessSummaryResponse(BaseModel):
    passed_count: int
    failed_count: int
    unknown_count: int
    manual_blocker_count: int
    incomplete_count: int


class StorageReadinessResponse(BaseModel):
    mode: Literal["local", "minio", "injected"]
    state: Literal["ready", "configured", "unavailable"]
    bucket_alias: str | None = None
    prefix_alias: str | None = None
    secure: bool | None = None
    reason_alias: str | None = None
    remote_audio_proxy_enabled: bool = False
    web_audio_session_enabled: bool = False
    web_audio_cookie_secure: bool | None = None
    web_audio_session_ttl_seconds: int | None = None


class AudioSessionStatusResponse(BaseModel):
    enabled: bool
    authenticated: bool
    expires_in_seconds: int | None = None


class RepositoryReadinessResponse(BaseModel):
    mode: Literal["memory", "postgres"]
    state: Literal["ready", "configured", "unavailable"]
    reason_alias: str | None = None
    job_queue_enabled: bool = False
    statement_timeout_ms: int | None = None
    worker_enabled: bool = False
    worker_running: bool = False
    worker_processed_total: int = 0
    worker_error_total: int = 0
    worker_lease_lost_total: int = 0


class TimelineLanes(BaseModel):
    sip_ladder: list[TimelineSipEvent]
    rtp_quality: list[TimelineRtpStat]
    stages: list[TimelineStageLane]
    turns: list[dict[str, Any]]
    host: list[TimelineMetricPoint]
    recordings: list[TimelineRecording]
    events: list[TimelineTypedEvent]
    intervals: list[TimelineTypedInterval]
    series: list[TimelineTypedSeries]
    artifacts: list[TimelineTypedArtifact]
    incidents: list[TimelineIncident]


class TimelineResponse(BaseModel):
    run_id: str
    t0: datetime
    config_hash: str
    environment: RunEnvironmentMetadata
    readiness_checklist: list[ReadinessChecklistItem]
    readiness_summary: ReadinessSummaryResponse
    lanes: TimelineLanes


class RunResponse(BaseModel):
    run_id: str
    config_hash: str
    call_id: str | None
    conversation_id: str
    provider: str
    engine: str
    status: str
    failure_alias: str | None
    recordings: list[RecordingResponse]
    spans: list[SpanResponse]
    metrics: list[MetricResponse]
    verifications: list[VerificationResponse]
    environment: RunEnvironmentMetadata
    readiness_checklist: list[ReadinessChecklistItem]
    readiness_summary: ReadinessSummaryResponse


class RunSummaryResponse(BaseModel):
    run_id: str
    config_hash: str
    provider: str
    engine: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    recording_count: int
    violation_count: int
    environment_profile: EnvironmentProfile
    server_alias: str | None
    integration_target_alias: str | None
    readiness_failed_count: int
    readiness_unknown_count: int
    manual_blocker_count: int
    tags: list[str]


class HostMetricSnapshotResponse(BaseModel):
    name: str
    value: float
    ts: datetime


class ProviderConnectionStatusResponse(BaseModel):
    state: ProviderConnectionState
    attempts: int = 0
    retries: int = 0
    failures: int = 0
    exhausted: bool = False


class RtpCollectorStatusResponse(BaseModel):
    state: RtpCollectorState
    events_collected: int = 0
    failures: int = 0


class CrossSessionTrendPoint(BaseModel):
    run_id: str
    started_at: datetime
    value: float


class CrossSessionTrendResponse(BaseModel):
    metric: str
    environment_profile: EnvironmentProfile
    server_alias: str
    state: CrossSessionTrendState
    sample_count: int
    first_value: float
    latest_value: float
    total_delta: float
    points: list[CrossSessionTrendPoint]


class LiveRunStatusResponse(BaseModel):
    run_id: str
    status: str
    failure_alias: str | None
    started_at: datetime
    ended_at: datetime | None
    environment_profile: EnvironmentProfile
    server_alias: str | None
    integration_target_alias: str | None
    readiness_summary: ReadinessSummaryResponse
    manual_blockers: list[str]
    latest_host_metrics: list[HostMetricSnapshotResponse]
    provider_connection: ProviderConnectionStatusResponse
    rtp_collector: RtpCollectorStatusResponse
    violation_count: int
    tags: list[str]


@dataclass
class StoredRun:
    run_id: str
    config_hash: str
    call_id: str | None
    conversation_id: str
    provider: str
    engine: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    resolved_config: dict[str, Any]
    recordings: list[RecordingArtifact]
    spans: list[SpanArtifact]
    metrics: list[MetricArtifact]
    failure_alias: str | None = None
    verifications: list[VerificationResult] = field(default_factory=list)
    sip_events: list[SipEventResponse] = field(default_factory=list)
    rtp_stats: list[RtpStatResponse] = field(default_factory=list)
    timeline_events: list[TimelineEventArtifact] = field(default_factory=list)
    environment: RunEnvironmentMetadata = field(default_factory=RunEnvironmentMetadata)
    readiness_checklist: list[ReadinessChecklistItem] = field(
        default_factory=_default_readiness_checklist,
    )

    def to_response(self) -> RunResponse:
        return RunResponse(
            run_id=self.run_id,
            config_hash=self.config_hash,
            call_id=self.call_id,
            conversation_id=self.conversation_id,
            provider=self.provider,
            engine=self.engine,
            status=self.status,
            failure_alias=self.failure_alias,
            recordings=[RecordingResponse(**recording.__dict__) for recording in self.recordings],
            spans=[SpanResponse(**span.__dict__) for span in self.spans],
            metrics=[MetricResponse(**metric.__dict__) for metric in self.metrics],
            verifications=[
                VerificationResponse(**verification.__dict__)
                for verification in self.verifications
            ],
            environment=self.environment,
            readiness_checklist=self.readiness_checklist,
            readiness_summary=self.readiness_summary(),
        )

    def to_summary(self) -> RunSummaryResponse:
        readiness_summary = self.readiness_summary()
        return RunSummaryResponse(
            run_id=self.run_id,
            config_hash=self.config_hash,
            provider=self.provider,
            engine=self.engine,
            status=self.status,
            started_at=self.started_at,
            ended_at=self.ended_at,
            recording_count=len(self.recordings),
            violation_count=sum(
                1 for verification in self.verifications if not verification.passed
            ),
            environment_profile=self.environment.environment_profile,
            server_alias=self.environment.server_alias,
            integration_target_alias=self.environment.integration_target_alias,
            readiness_failed_count=readiness_summary.failed_count,
            readiness_unknown_count=readiness_summary.unknown_count,
            manual_blocker_count=readiness_summary.manual_blocker_count,
            tags=self.environment.tags,
        )

    def to_live_status(self) -> LiveRunStatusResponse:
        return LiveRunStatusResponse(
            run_id=self.run_id,
            status=self.status,
            failure_alias=self.failure_alias,
            started_at=self.started_at,
            ended_at=self.ended_at,
            environment_profile=self.environment.environment_profile,
            server_alias=self.environment.server_alias,
            integration_target_alias=self.environment.integration_target_alias,
            readiness_summary=self.readiness_summary(),
            manual_blockers=self.environment.manual_blockers,
            latest_host_metrics=self.latest_host_metrics(),
            provider_connection=self.provider_connection_status(),
            rtp_collector=self.rtp_collector_status(),
            violation_count=sum(
                1 for verification in self.verifications if not verification.passed
            ),
            tags=self.environment.tags,
        )

    def to_timeline(self) -> TimelineResponse:
        stage_names = [
            stage["type"]
            for stage in self.resolved_config["spec"]["media"]["pipeline"]
        ]
        metrics_by_stage = {
            stage: [
                TimelineMetricPoint(
                    ts=_relative_seconds(metric.ts, self.started_at),
                    name=metric.name,
                    value=metric.value,
                )
                for metric in self.metrics
                if metric.stage == stage
            ]
            for stage in stage_names
        }
        violations_by_stage = {
            stage: [
                TimelineViolation(
                    invariant=verification.invariant,
                    passed=verification.passed,
                    detail=verification.detail,
                    observed=verification.observed,
                    expected=verification.expected,
                )
                for verification in self.verifications
                if verification.stage == stage and not verification.passed
            ]
            for stage in stage_names
        }
        host_metrics = [
            TimelineMetricPoint(
                ts=_relative_seconds(metric.ts, self.started_at),
                name=metric.name,
                value=metric.value,
            )
            for metric in self.metrics
            if metric.stage is None
        ]
        sip_ladder = [
            TimelineSipEvent(
                ts=_relative_seconds(event.ts, self.started_at),
                call_id=event.call_id,
                method=event.method,
                direction=event.direction,
                status_code=event.status_code,
                summary_alias=event.summary_alias,
            )
            for event in self.sip_events
        ]
        rtp_quality = [
            TimelineRtpStat(
                ts=_relative_seconds(stat.ts, self.started_at),
                jitter_ms=stat.jitter_ms,
                loss_pct=stat.loss_pct,
                mos=stat.mos,
                direction=stat.direction,
                rtt_ms=stat.rtt_ms,
            )
            for stat in self.rtp_stats
        ]
        typed_events = _typed_timeline_events(self)
        typed_intervals = _typed_timeline_intervals(self)
        typed_series = _typed_timeline_series(self)
        typed_artifacts = _typed_timeline_artifacts(self)
        typed_incidents = _typed_timeline_incidents(self, typed_artifacts)
        return TimelineResponse(
            run_id=self.run_id,
            t0=self.started_at,
            config_hash=self.config_hash,
            environment=self.environment,
            readiness_checklist=self.readiness_checklist,
            readiness_summary=self.readiness_summary(),
            lanes=TimelineLanes(
                sip_ladder=sip_ladder,
                rtp_quality=rtp_quality,
                stages=[
                    TimelineStageLane(
                        stage=stage,
                        metrics=metrics_by_stage[stage],
                        violations=violations_by_stage[stage],
                    )
                    for stage in stage_names
                ],
                turns=[],
                host=host_metrics,
                recordings=[
                    TimelineRecording(**recording.__dict__)
                    for recording in self.recordings
                ],
                events=typed_events,
                intervals=typed_intervals,
                series=typed_series,
                artifacts=typed_artifacts,
                incidents=typed_incidents,
            ),
        )

    def readiness_summary(self) -> ReadinessSummaryResponse:
        passed_count = sum(1 for item in self.readiness_checklist if item.status == "pass")
        failed_count = sum(1 for item in self.readiness_checklist if item.status == "fail")
        unknown_count = sum(1 for item in self.readiness_checklist if item.status == "unknown")
        manual_blocker_count = len(self.environment.manual_blockers)
        return ReadinessSummaryResponse(
            passed_count=passed_count,
            failed_count=failed_count,
            unknown_count=unknown_count,
            manual_blocker_count=manual_blocker_count,
            incomplete_count=failed_count + unknown_count + manual_blocker_count,
        )

    def latest_host_metrics(self) -> list[HostMetricSnapshotResponse]:
        by_name: dict[str, MetricArtifact] = {}
        for metric in self.metrics:
            if metric.stage is not None:
                continue
            current = by_name.get(metric.name)
            if current is None or metric.ts >= current.ts:
                by_name[metric.name] = metric
        return [
            HostMetricSnapshotResponse(name=metric.name, value=metric.value, ts=metric.ts)
            for metric in sorted(by_name.values(), key=lambda metric: metric.name)
        ]

    def provider_connection_status(self) -> ProviderConnectionStatusResponse:
        values = {
            metric.name: metric.value
            for metric in self.latest_host_metrics()
            if metric.name.startswith("provider_connect_")
        }
        attempts = max(0, int(values.get("provider_connect_attempts", 0)))
        retries = max(0, int(values.get("provider_connect_retries", 0)))
        failures = max(0, int(values.get("provider_connect_failures", 0)))
        exhausted = (
            values.get("provider_connect_exhausted", 0) > 0
            or self.failure_alias == "provider-connect-error"
        )
        if exhausted:
            state: ProviderConnectionState = "exhausted"
        elif attempts > 0:
            state = "connected"
        elif "provider" not in self.environment.tags:
            state = "not_applicable"
        elif self.status == "running":
            state = "pending"
        else:
            state = "unobserved"
        return ProviderConnectionStatusResponse(
            state=state,
            attempts=attempts,
            retries=retries,
            failures=failures,
            exhausted=exhausted,
        )

    def rtp_collector_status(self) -> RtpCollectorStatusResponse:
        events_collected = sum(
            max(0, int(metric.value))
            for metric in self.metrics
            if metric.stage is None and metric.name == "asterisk_ami_rtcp_events"
        )
        failures = sum(
            max(0, int(metric.value))
            for metric in self.metrics
            if metric.stage is None and metric.name == "asterisk_ami_rtcp_failures"
        )
        status_metric_names = {
            "asterisk_ami_rtcp_connected",
            "asterisk_ami_rtcp_events",
            "asterisk_ami_rtcp_failures",
        }
        latest_status_metric: MetricArtifact | None = None
        for metric in self.metrics:
            if metric.stage is not None or metric.name not in status_metric_names:
                continue
            if latest_status_metric is None or metric.ts >= latest_status_metric.ts:
                latest_status_metric = metric
        latest_status_name = latest_status_metric.name if latest_status_metric else None
        if latest_status_name == "asterisk_ami_rtcp_failures":
            state: RtpCollectorState = "failed"
        elif latest_status_name == "asterisk_ami_rtcp_events":
            state = "collecting"
        elif latest_status_name == "asterisk_ami_rtcp_connected":
            state = "connected"
        else:
            state = "inactive"
        return RtpCollectorStatusResponse(
            state=state,
            events_collected=events_collected,
            failures=failures,
        )


_TIMELINE_EVENT_METRICS: dict[str, TimelineCategory] = {
    "barge_in_events": "conversation",
    "dtmf_events": "conversation",
    "output_frames_dropped": "buffer",
    "provider_auto_interrupts": "provider",
    "provider_input_speech_started": "conversation",
    "provider_interrupt_requests": "provider",
    "provider_interrupted": "provider",
    "provider_response_done": "provider",
    "provider_response_started": "provider",
    "provider_stream_ended": "provider",
    "provider_stream_errors": "provider",
    "provider_truncate_requests": "provider",
}

_TYPED_BARGE_IN_METRICS = {
    "barge_in_events",
    "output_frames_dropped",
    "provider_auto_interrupts",
    "provider_input_speech_started",
    "provider_interrupt_requests",
    "provider_interrupted",
    "provider_truncate_requests",
}

_PROVIDER_RESPONSE_METRICS = {
    "provider_response_done",
    "provider_response_started",
}

_TIMELINE_UNITS = {
    "active_tasks": "count",
    "audio_chunk_duration_ms": "ms",
    "cpu": "percent",
    "delta_db": "dB",
    "frame_cadence_jitter_ms": "ms",
    "frames_in": "count",
    "frames_out": "count",
    "gain_applied": "ratio",
    "full_scale_sample_pct": "percent",
    "input_rms": "pcm-rms",
    "jitter_ms": "ms",
    "loop_lag": "ms",
    "loss_pct": "percent",
    "mos": "score",
    "output_frames_dropped": "count",
    "output_rms": "pcm-rms",
    "provider_input_rms": "pcm-rms",
    "rtt_ms": "ms",
    "sample_peak_dbfs": "dBFS",
    "silence_sample_pct": "percent",
}

RTP_LOSS_WARNING_PCT = 1.0
RTP_JITTER_WARNING_MS = 30.0
RTP_MOS_WARNING_SCORE = 3.5
RTP_DEGRADATION_WINDOW_GAP_MS = 5_000.0
STAGE_LEVEL_EVENT_THRESHOLD_DB = 1.0
STAGE_SILENCE_THRESHOLD_DBFS = -60.0
STAGE_SILENCE_SAMPLE_THRESHOLD_PCT = 98.0
STAGE_SILENCE_MIN_WINDOW_MS = 200.0
STAGE_SILENCE_MAX_OBSERVATION_GAP_MS = 100.0
ASSISTANT_OUTPUT_DEAD_AIR_MIN_OVERLAP_MS = 200.0


@dataclass(frozen=True)
class _RtpQualityEvidence:
    stat_index: int
    direction_ordinal: int
    direction: RtpDirection | None
    t_rel_ms: float
    loss_pct: float | None
    jitter_ms: float | None
    mos: float | None
    triggers: tuple[str, ...]

    @property
    def evidence_refs(self) -> list[str]:
        return [f"rtp:{self.stat_index}:{trigger}" for trigger in self.triggers]


@dataclass(frozen=True)
class _StageSignalEvidence:
    stage: str
    t_rel_ms: float
    input_rms: float | None
    output_rms: float | None
    delta_db: float
    gain_applied: float | None
    sample_count: int

    @property
    def event_id(self) -> str:
        return f"stage-signal:{self.stage}"


@dataclass(frozen=True)
class _DurationContractionEvidence:
    verification_index: int
    previous_stage: str | None
    stage: str
    input_duration_ms: float
    output_duration_ms: float
    duration_ratio: float

    @property
    def missing_duration_ms(self) -> float:
        return self.input_duration_ms - self.output_duration_ms

    @property
    def event_id(self) -> str:
        return f"duration-contraction:{self.verification_index}"

    @property
    def correlation_alias(self) -> str:
        return f"media-time:{self.stage}:{self.verification_index}"


@dataclass(frozen=True)
class _StageFullScaleEvidence:
    stage: str
    previous_stage: str | None
    t_rel_ms: float
    full_scale_sample_pct: float
    sample_peak_dbfs: float | None
    sample_count: int

    @property
    def event_id(self) -> str:
        return f"stage-full-scale:{self.stage}"


@dataclass(frozen=True)
class _StageSilenceWindow:
    stage: str
    window_index: int
    start_ms: float
    end_ms: float
    observation_count: int
    peak_silence_sample_pct: float

    @property
    def correlation_alias(self) -> str:
        return f"stage-silence:{self.stage}:{self.window_index}"


@dataclass(frozen=True)
class _ProviderResponseEvidence:
    response_index: int
    start_observation_index: int
    done_observation_index: int | None
    start_ms: float
    end_ms: float
    completed: bool

    @property
    def correlation_alias(self) -> str:
        return f"provider-response:{self.response_index}"

    @property
    def start_event_id(self) -> str:
        return f"{self.correlation_alias}:start"

    @property
    def done_event_id(self) -> str | None:
        if not self.completed:
            return None
        return f"{self.correlation_alias}:done"


@dataclass(frozen=True)
class _AssistantOutputDeadAirEvidence:
    silence: _StageSilenceWindow
    provider_response: _ProviderResponseEvidence
    start_ms: float
    end_ms: float

    @property
    def overlap_ms(self) -> float:
        return self.end_ms - self.start_ms


def _provider_response_evidence(run: StoredRun) -> list[_ProviderResponseEvidence]:
    observations = sorted(
        (
            (metric.ts, metric_index, metric.name)
            for metric_index, metric in enumerate(run.metrics)
            if metric.name in _PROVIDER_RESPONSE_METRICS and metric.value > 0
        ),
        key=lambda item: (item[0], item[1]),
    )
    responses: list[_ProviderResponseEvidence] = []
    active: tuple[int, int, float] | None = None
    next_response_index = 0
    for ts, observation_index, name in observations:
        t_rel_ms = max(0.0, _relative_seconds(ts, run.started_at) * 1000)
        if name == "provider_response_started":
            if active is not None:
                response_index, start_observation_index, start_ms = active
                responses.append(
                    _ProviderResponseEvidence(
                        response_index=response_index,
                        start_observation_index=start_observation_index,
                        done_observation_index=None,
                        start_ms=start_ms,
                        end_ms=max(start_ms, t_rel_ms),
                        completed=False,
                    )
                )
            active = (next_response_index, observation_index, t_rel_ms)
            next_response_index += 1
            continue
        if active is None:
            continue
        response_index, start_observation_index, start_ms = active
        responses.append(
            _ProviderResponseEvidence(
                response_index=response_index,
                start_observation_index=start_observation_index,
                done_observation_index=observation_index,
                start_ms=start_ms,
                end_ms=max(start_ms, t_rel_ms),
                completed=True,
            )
        )
        active = None
    if active is not None:
        response_index, start_observation_index, start_ms = active
        responses.append(
            _ProviderResponseEvidence(
                response_index=response_index,
                start_observation_index=start_observation_index,
                done_observation_index=None,
                start_ms=start_ms,
                end_ms=max(start_ms, _run_timeline_duration_ms(run)),
                completed=False,
            )
        )
    return sorted(responses, key=lambda item: (item.start_ms, item.response_index))


def _assistant_output_dead_air_evidence(
    run: StoredRun,
) -> list[_AssistantOutputDeadAirEvidence]:
    stage_names = _pipeline_stage_names(run)
    if not stage_names:
        return []
    final_stage = stage_names[-1]
    silence_windows = [
        window
        for window in _stage_silence_windows(run)
        if window.stage == final_stage
    ]
    evidence: list[_AssistantOutputDeadAirEvidence] = []
    for silence in silence_windows:
        for response in _provider_response_evidence(run):
            start_ms = max(silence.start_ms, response.start_ms)
            end_ms = min(silence.end_ms, response.end_ms)
            if end_ms - start_ms < ASSISTANT_OUTPUT_DEAD_AIR_MIN_OVERLAP_MS:
                continue
            evidence.append(
                _AssistantOutputDeadAirEvidence(
                    silence=silence,
                    provider_response=response,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            )
    return evidence


def _stage_signal_evidence(run: StoredRun) -> list[_StageSignalEvidence]:
    metric_names = {"input_rms", "output_rms", "delta_db", "gain_applied"}
    bundles: dict[tuple[str, datetime], dict[str, float]] = {}
    for metric in run.metrics:
        if metric.stage is None or metric.name not in metric_names:
            continue
        bundles.setdefault((metric.stage, metric.ts), {})[metric.name] = metric.value

    stage_order = _pipeline_stage_names(run)
    by_stage: dict[str, list[tuple[datetime, dict[str, float]]]] = {}
    for (stage, ts), values in bundles.items():
        if "delta_db" in values:
            by_stage.setdefault(stage, []).append((ts, values))

    evidence: list[_StageSignalEvidence] = []
    for stage in stage_order:
        candidates = by_stage.get(stage, [])
        if not candidates:
            continue
        ts, values = max(
            candidates,
            key=lambda item: (abs(item[1]["delta_db"]), item[0]),
        )
        delta_db = values["delta_db"]
        if abs(delta_db) < STAGE_LEVEL_EVENT_THRESHOLD_DB:
            continue
        evidence.append(
            _StageSignalEvidence(
                stage=stage,
                t_rel_ms=_relative_seconds(ts, run.started_at) * 1000,
                input_rms=values.get("input_rms"),
                output_rms=values.get("output_rms"),
                delta_db=delta_db,
                gain_applied=values.get("gain_applied"),
                sample_count=len(candidates),
            )
        )
    return evidence


def _stage_quality_bundles(
    run: StoredRun,
) -> dict[str, list[tuple[datetime, dict[str, float]]]]:
    quality_metric_names = {
        "audio_chunk_duration_ms",
        "full_scale_sample_pct",
        "sample_peak_dbfs",
        "silence_sample_pct",
    }
    bundles: dict[tuple[str, datetime], dict[str, float]] = {}
    for metric in run.metrics:
        if metric.stage is None or metric.name not in quality_metric_names:
            continue
        bundles.setdefault((metric.stage, metric.ts), {})[metric.name] = metric.value
    by_stage: dict[str, list[tuple[datetime, dict[str, float]]]] = {}
    for (stage, ts), values in bundles.items():
        by_stage.setdefault(stage, []).append((ts, values))
    return {
        stage: sorted(values, key=lambda item: item[0])
        for stage, values in by_stage.items()
    }


def _stage_full_scale_evidence(run: StoredRun) -> list[_StageFullScaleEvidence]:
    bundles_by_stage = _stage_quality_bundles(run)
    evidence: list[_StageFullScaleEvidence] = []
    previous_stage: str | None = None
    previous_peak_pct: float | None = None
    for stage in _pipeline_stage_names(run):
        bundles = bundles_by_stage.get(stage, [])
        candidates = [
            (ts, values)
            for ts, values in bundles
            if values.get("full_scale_sample_pct", 0.0) > 0.0
        ]
        current_peak_pct = max(
            (values.get("full_scale_sample_pct", 0.0) for _, values in bundles),
            default=0.0,
        )
        if candidates and (previous_peak_pct is None or previous_peak_pct <= 0.0):
            ts, values = max(
                candidates,
                key=lambda item: (item[1]["full_scale_sample_pct"], item[0]),
            )
            evidence.append(
                _StageFullScaleEvidence(
                    stage=stage,
                    previous_stage=previous_stage,
                    t_rel_ms=_relative_seconds(ts, run.started_at) * 1000,
                    full_scale_sample_pct=values["full_scale_sample_pct"],
                    sample_peak_dbfs=values.get("sample_peak_dbfs"),
                    sample_count=len(bundles),
                )
            )
        previous_stage = stage
        previous_peak_pct = current_peak_pct if bundles else None
    return evidence


def _stage_silence_windows(run: StoredRun) -> list[_StageSilenceWindow]:
    bundles_by_stage = _stage_quality_bundles(run)
    windows: list[_StageSilenceWindow] = []
    for stage in _pipeline_stage_names(run):
        current: list[tuple[float, float, float]] = []

        def finish_window(stage_name: str = stage) -> None:
            nonlocal current
            duration_ms = sum(item[1] for item in current)
            if current and duration_ms >= STAGE_SILENCE_MIN_WINDOW_MS:
                windows.append(
                    _StageSilenceWindow(
                        stage=stage_name,
                        window_index=sum(
                            window.stage == stage_name for window in windows
                        ),
                        start_ms=current[0][0],
                        end_ms=current[0][0] + duration_ms,
                        observation_count=len(current),
                        peak_silence_sample_pct=max(item[2] for item in current),
                    )
                )
            current = []

        for ts, values in bundles_by_stage.get(stage, []):
            silence_pct = values.get("silence_sample_pct")
            duration_ms = values.get("audio_chunk_duration_ms")
            if (
                silence_pct is None
                or duration_ms is None
                or duration_ms <= 0
                or silence_pct < STAGE_SILENCE_SAMPLE_THRESHOLD_PCT
            ):
                finish_window()
                continue
            start_ms = _relative_seconds(ts, run.started_at) * 1000
            if current:
                previous_start_ms, previous_duration_ms, _ = current[-1]
                allowed_gap_ms = max(
                    STAGE_SILENCE_MAX_OBSERVATION_GAP_MS,
                    previous_duration_ms * 2,
                )
                if start_ms - previous_start_ms > allowed_gap_ms:
                    finish_window()
            current.append((start_ms, duration_ms, silence_pct))
        finish_window()
    return windows


def _duration_contraction_evidence(
    run: StoredRun,
) -> list[_DurationContractionEvidence]:
    stage_names = _pipeline_stage_names(run)
    previous_by_stage = {
        stage: stage_names[index - 1] if index > 0 else None
        for index, stage in enumerate(stage_names)
    }
    evidence: list[_DurationContractionEvidence] = []
    for verification_index, verification in enumerate(run.verifications):
        if (
            verification.passed
            or verification.invariant != "duration_preserving"
        ):
            continue
        input_duration_ms = _plain_number(
            verification.observed.get("input_duration_ms")
        )
        output_duration_ms = _plain_number(
            verification.observed.get("output_duration_ms")
        )
        duration_ratio = _plain_number(verification.observed.get("duration_ratio"))
        if (
            input_duration_ms is None
            or output_duration_ms is None
            or duration_ratio is None
            or input_duration_ms <= output_duration_ms
        ):
            continue
        evidence.append(
            _DurationContractionEvidence(
                verification_index=verification_index,
                previous_stage=previous_by_stage.get(verification.stage),
                stage=verification.stage,
                input_duration_ms=input_duration_ms,
                output_duration_ms=output_duration_ms,
                duration_ratio=duration_ratio,
            )
        )
    return evidence


def _pipeline_stage_names(run: StoredRun) -> list[str]:
    return [
        stage["type"] for stage in run.resolved_config["spec"]["media"]["pipeline"]
    ]


def _plain_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _rtp_quality_evidence(run: StoredRun) -> list[_RtpQualityEvidence]:
    grouped: dict[RtpDirection | None, list[tuple[int, RtpStatResponse]]] = {}
    for stat_index, stat in enumerate(run.rtp_stats):
        grouped.setdefault(stat.direction, []).append((stat_index, stat))

    evidence: list[_RtpQualityEvidence] = []
    for direction, values in grouped.items():
        ordered = sorted(values, key=lambda item: (item[1].ts, item[0]))
        for direction_ordinal, (stat_index, stat) in enumerate(ordered):
            triggers: list[str] = []
            if stat.loss_pct is not None and stat.loss_pct >= RTP_LOSS_WARNING_PCT:
                triggers.append("loss")
            if stat.jitter_ms is not None and stat.jitter_ms >= RTP_JITTER_WARNING_MS:
                triggers.append("jitter")
            if stat.mos is not None and stat.mos <= RTP_MOS_WARNING_SCORE:
                triggers.append("mos")
            if not triggers:
                continue
            evidence.append(
                _RtpQualityEvidence(
                    stat_index=stat_index,
                    direction_ordinal=direction_ordinal,
                    direction=direction,
                    t_rel_ms=_relative_seconds(stat.ts, run.started_at) * 1000,
                    loss_pct=stat.loss_pct,
                    jitter_ms=stat.jitter_ms,
                    mos=stat.mos,
                    triggers=tuple(triggers),
                )
            )
    return sorted(
        evidence,
        key=lambda item: (item.t_rel_ms, item.direction or "", item.stat_index),
    )


def _rtp_quality_windows(run: StoredRun) -> list[list[_RtpQualityEvidence]]:
    by_direction: dict[RtpDirection | None, list[_RtpQualityEvidence]] = {}
    for item in _rtp_quality_evidence(run):
        by_direction.setdefault(item.direction, []).append(item)

    windows: list[list[_RtpQualityEvidence]] = []
    for values in by_direction.values():
        current: list[_RtpQualityEvidence] = []
        for item in sorted(values, key=lambda value: value.t_rel_ms):
            if current:
                previous = current[-1]
                is_consecutive = item.direction_ordinal == previous.direction_ordinal + 1
                is_nearby = (
                    item.t_rel_ms - previous.t_rel_ms <= RTP_DEGRADATION_WINDOW_GAP_MS
                )
                if not (is_consecutive and is_nearby):
                    windows.append(current)
                    current = []
            current.append(item)
        if current:
            windows.append(current)
    return sorted(
        windows,
        key=lambda window: (window[0].t_rel_ms, window[0].direction or ""),
    )


def _typed_timeline_events(run: StoredRun) -> list[TimelineTypedEvent]:
    events: list[TimelineTypedEvent] = []
    for index, event in enumerate(run.sip_events):
        attributes: dict[str, Any] = {"method": event.method}
        if event.status_code is not None:
            attributes["status_code"] = event.status_code
        if event.summary_alias is not None:
            attributes["summary_alias"] = event.summary_alias
        events.append(
            TimelineTypedEvent(
                event_id=f"sip:{index}",
                category="signaling",
                name=f"sip.{event.method.lower()}",
                t_rel_ms=_relative_seconds(event.ts, run.started_at) * 1000,
                clock_domain="control_plane_wall",
                direction=event.direction,
                source="sip_event",
                attributes=attributes,
            )
        )

    persisted_names = {event.name for event in run.timeline_events}
    events.extend(
        TimelineTypedEvent(
            event_id=event.event_id,
            category=event.category,
            name=event.name,
            t_rel_ms=_relative_seconds(event.ts, run.started_at) * 1000,
            clock_domain=event.clock_domain,
            alignment_uncertainty_ms=event.alignment_uncertainty_ms,
            direction=event.direction,
            stage=event.stage,
            stream_alias=event.stream_alias,
            source=event.source,
            correlation_alias=event.correlation_alias,
            attributes=event.attributes,
        )
        for event in run.timeline_events
    )

    provider_response_observation_indices = {
        observation_index
        for response in _provider_response_evidence(run)
        for observation_index in (
            response.start_observation_index,
            response.done_observation_index,
        )
        if observation_index is not None
    }
    metric_index = 0
    for observation_index, metric in enumerate(run.metrics):
        category = _TIMELINE_EVENT_METRICS.get(metric.name)
        if (
            category is None
            or metric.value <= 0
            or observation_index in provider_response_observation_indices
            or metric.name in persisted_names
            or (run.timeline_events and metric.name in _TYPED_BARGE_IN_METRICS)
        ):
            continue
        events.append(
            TimelineTypedEvent(
                event_id=f"metric-event:{metric_index}",
                category=category,
                name=metric.name,
                t_rel_ms=_relative_seconds(metric.ts, run.started_at) * 1000,
                clock_domain="control_plane_wall",
                stage=metric.stage,
                source="metric_observation",
                attributes={"value": metric.value},
            )
        )
        metric_index += 1

    for item in _provider_response_evidence(run):
        events.append(
            TimelineTypedEvent(
                event_id=item.start_event_id,
                category="provider",
                name="provider.response_started",
                t_rel_ms=item.start_ms,
                clock_domain="control_plane_wall",
                direction="assistant_to_caller",
                source="provider_lifecycle_metrics",
                correlation_alias=item.correlation_alias,
                attributes={"completion_observed": item.completed},
            )
        )
        if item.done_event_id is not None:
            events.append(
                TimelineTypedEvent(
                    event_id=item.done_event_id,
                    category="provider",
                    name="provider.response_done",
                    t_rel_ms=item.end_ms,
                    clock_domain="control_plane_wall",
                    direction="assistant_to_caller",
                    source="provider_lifecycle_metrics",
                    correlation_alias=item.correlation_alias,
                    attributes={
                        "completion_observed": True,
                        "duration_ms": item.end_ms - item.start_ms,
                    },
                )
            )

    for item in _stage_signal_evidence(run):
        events.append(
            TimelineTypedEvent(
                event_id=item.event_id,
                category="pipeline",
                name=(
                    "stage.level_increased"
                    if item.delta_db > 0
                    else "stage.level_decreased"
                ),
                t_rel_ms=item.t_rel_ms,
                clock_domain="control_plane_wall",
                stage=item.stage,
                source="stage_signal_metrics",
                correlation_alias=item.event_id,
                attributes={
                    "input_rms": item.input_rms,
                    "output_rms": item.output_rms,
                    "delta_db": item.delta_db,
                    "gain_applied": item.gain_applied,
                    "sample_count": item.sample_count,
                    "event_threshold_db": STAGE_LEVEL_EVENT_THRESHOLD_DB,
                },
            )
        )

    for item in _stage_full_scale_evidence(run):
        events.append(
            TimelineTypedEvent(
                event_id=item.event_id,
                category="pipeline",
                name="stage.full_scale_samples_detected",
                t_rel_ms=item.t_rel_ms,
                clock_domain="control_plane_wall",
                stage=item.stage,
                source="pcm_sample_quality",
                correlation_alias=item.event_id,
                attributes={
                    "previous_stage": item.previous_stage,
                    "full_scale_sample_pct": item.full_scale_sample_pct,
                    "sample_peak_dbfs": item.sample_peak_dbfs,
                    "sample_count": item.sample_count,
                    "clipping_status": "suspected",
                },
            )
        )

    for item in _stage_silence_windows(run):
        events.append(
            TimelineTypedEvent(
                event_id=f"{item.correlation_alias}:start",
                category="pipeline",
                name="stage.digital_silence_started",
                t_rel_ms=item.start_ms,
                clock_domain="control_plane_wall",
                stage=item.stage,
                source="pcm_sample_quality",
                correlation_alias=item.correlation_alias,
                attributes={
                    "duration_ms": item.end_ms - item.start_ms,
                    "observation_count": item.observation_count,
                    "peak_silence_sample_pct": item.peak_silence_sample_pct,
                    "silence_threshold_dbfs": STAGE_SILENCE_THRESHOLD_DBFS,
                },
            )
        )

    for item in _duration_contraction_evidence(run):
        events.append(
            TimelineTypedEvent(
                event_id=item.event_id,
                category="pipeline",
                name="stage.media_time_contracted",
                t_rel_ms=item.output_duration_ms,
                clock_domain="recording_media_time",
                stage=item.stage,
                source="duration_verification",
                correlation_alias=item.correlation_alias,
                attributes={
                    "previous_stage": item.previous_stage,
                    "input_duration_ms": item.input_duration_ms,
                    "output_duration_ms": item.output_duration_ms,
                    "missing_duration_ms": item.missing_duration_ms,
                    "duration_ratio": item.duration_ratio,
                },
            )
        )

    for window_index, window in enumerate(_rtp_quality_windows(run)):
        correlation_alias = _rtp_window_alias(window_index, window)
        for item in window:
            for trigger in item.triggers:
                if trigger == "loss":
                    name = "rtp.loss_elevated"
                    attributes = {
                        "loss_pct": item.loss_pct,
                        "warning_threshold_pct": RTP_LOSS_WARNING_PCT,
                    }
                elif trigger == "jitter":
                    name = "rtp.jitter_elevated"
                    attributes = {
                        "jitter_ms": item.jitter_ms,
                        "warning_threshold_ms": RTP_JITTER_WARNING_MS,
                    }
                else:
                    name = "rtp.mos_degraded"
                    attributes = {
                        "mos": item.mos,
                        "warning_below_or_equal": RTP_MOS_WARNING_SCORE,
                    }
                events.append(
                    TimelineTypedEvent(
                        event_id=f"rtp:{item.stat_index}:{trigger}",
                        category="transport",
                        name=name,
                        t_rel_ms=item.t_rel_ms,
                        clock_domain="control_plane_wall",
                        direction=item.direction,
                        stream_alias=f"rtp-{item.direction or 'unknown'}",
                        source="rtp_quality_rule_v1",
                        correlation_alias=correlation_alias,
                        attributes=attributes,
                    )
                )

    if run.failure_alias is not None:
        events.append(
            TimelineTypedEvent(
                event_id="run:failure",
                category="session",
                name="run_failed",
                t_rel_ms=_relative_seconds(
                    run.ended_at or run.started_at,
                    run.started_at,
                )
                * 1000,
                clock_domain="control_plane_wall",
                source="run_state",
                attributes={"failure_alias": run.failure_alias},
            )
        )
    return sorted(events, key=lambda event: (event.t_rel_ms, event.event_id))


def _typed_timeline_intervals(run: StoredRun) -> list[TimelineTypedInterval]:
    intervals: list[TimelineTypedInterval] = []
    origin_ns = int(run.started_at.timestamp() * 1_000_000_000)
    for index, span in enumerate(run.spans):
        stage_value = span.attrs.get("voxbench.stage")
        stage = stage_value if isinstance(stage_value, str) else None
        start_ms = max(0.0, (span.start_ns - origin_ns) / 1_000_000)
        end_ms = max(start_ms, (span.end_ns - origin_ns) / 1_000_000)
        intervals.append(
            TimelineTypedInterval(
                interval_id=f"span:{index}",
                category="pipeline" if stage is not None else "session",
                name=span.name,
                start_ms=start_ms,
                end_ms=end_ms,
                clock_domain="otel_epoch_ns",
                stage=stage,
                source="otel_span",
                attributes={"duration_ms": end_ms - start_ms},
            )
        )
    for item in _provider_response_evidence(run):
        intervals.append(
            TimelineTypedInterval(
                interval_id=item.correlation_alias,
                category="provider",
                name="provider_response",
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                clock_domain="control_plane_wall",
                direction="assistant_to_caller",
                source="provider_lifecycle_metrics",
                correlation_alias=item.correlation_alias,
                attributes={
                    "duration_ms": item.end_ms - item.start_ms,
                    "completion_observed": item.completed,
                },
            )
        )
    dead_air_silence_aliases = {
        item.silence.correlation_alias
        for item in _assistant_output_dead_air_evidence(run)
    }
    for item in _stage_silence_windows(run):
        intervals.append(
            TimelineTypedInterval(
                interval_id=item.correlation_alias,
                category="pipeline",
                name="digital_silence",
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                clock_domain="control_plane_wall",
                stage=item.stage,
                source="pcm_sample_quality",
                correlation_alias=item.correlation_alias,
                attributes={
                    "duration_ms": item.end_ms - item.start_ms,
                    "observation_count": item.observation_count,
                    "peak_silence_sample_pct": item.peak_silence_sample_pct,
                    "incident_status": (
                        "assistant_output_dead_air_suspected"
                        if item.correlation_alias in dead_air_silence_aliases
                        else "evidence_only_without_speech_context"
                    ),
                },
            )
        )
    for correlation_alias, events in _correlated_barge_in_events(run).items():
        start = min(events, key=lambda event: event.ts)
        completed = next(
            (event for event in reversed(events) if event.name == "barge_in_completed"),
            None,
        )
        if completed is None:
            continue
        start_ms = _relative_seconds(start.ts, run.started_at) * 1000
        end_ms = max(start_ms, _relative_seconds(completed.ts, run.started_at) * 1000)
        intervals.append(
            TimelineTypedInterval(
                interval_id=f"barge-in:{correlation_alias}",
                category="conversation",
                name="barge_in",
                start_ms=start_ms,
                end_ms=end_ms,
                clock_domain="control_plane_wall",
                direction="caller_to_assistant",
                source="audiosocket_bridge",
                correlation_alias=correlation_alias,
                attributes={"duration_ms": end_ms - start_ms},
            )
        )
    for window_index, window in enumerate(_rtp_quality_windows(run)):
        start_ms = window[0].t_rel_ms
        end_ms = max(start_ms, window[-1].t_rel_ms)
        triggers = sorted({trigger for item in window for trigger in item.triggers})
        correlation_alias = _rtp_window_alias(window_index, window)
        intervals.append(
            TimelineTypedInterval(
                interval_id=correlation_alias,
                category="transport",
                name="rtp_quality_degradation",
                start_ms=start_ms,
                end_ms=end_ms,
                clock_domain="control_plane_wall",
                direction=window[0].direction,
                stream_alias=f"rtp-{window[0].direction or 'unknown'}",
                source="rtp_quality_rule_v1",
                correlation_alias=correlation_alias,
                attributes={
                    "duration_ms": end_ms - start_ms,
                    "sample_count": len(window),
                    "triggers": triggers,
                },
            )
        )
    for item in _duration_contraction_evidence(run):
        intervals.append(
            TimelineTypedInterval(
                interval_id=f"media-time-missing:{item.verification_index}",
                category="pipeline",
                name="media_time_missing",
                start_ms=item.output_duration_ms,
                end_ms=item.input_duration_ms,
                clock_domain="recording_media_time",
                stage=item.stage,
                source="duration_verification",
                correlation_alias=item.correlation_alias,
                attributes={
                    "previous_stage": item.previous_stage,
                    "missing_duration_ms": item.missing_duration_ms,
                    "duration_ratio": item.duration_ratio,
                },
            )
        )
    return sorted(intervals, key=lambda interval: (interval.start_ms, interval.interval_id))


def _typed_timeline_series(run: StoredRun) -> list[TimelineTypedSeries]:
    metric_groups: dict[
        tuple[TimelineCategory, str | None, str],
        list[TimelineSeriesPoint],
    ] = {}
    for metric in run.metrics:
        category = _metric_timeline_category(metric)
        key = (category, metric.stage, metric.name)
        metric_groups.setdefault(key, []).append(
            TimelineSeriesPoint(
                t_rel_ms=_relative_seconds(metric.ts, run.started_at) * 1000,
                value=metric.value,
            )
        )

    series: list[TimelineTypedSeries] = []
    for index, ((category, stage, name), points) in enumerate(
        sorted(metric_groups.items(), key=lambda item: tuple(str(part) for part in item[0]))
    ):
        series.append(
            TimelineTypedSeries(
                series_id=f"metric:{index}",
                category=category,
                name=name,
                unit=_timeline_unit(name),
                clock_domain="control_plane_wall",
                stage=stage,
                source="metric_observation",
                points=sorted(points, key=lambda point: point.t_rel_ms),
            )
        )

    rtp_values = (
        ("jitter_ms", "jitter_ms"),
        ("loss_pct", "loss_pct"),
        ("mos", "mos"),
        ("rtt_ms", "rtt_ms"),
    )
    rtp_groups: dict[tuple[str | None, str], list[TimelineSeriesPoint]] = {}
    for stat in run.rtp_stats:
        for attribute, name in rtp_values:
            value = getattr(stat, attribute)
            if value is None:
                continue
            rtp_groups.setdefault((stat.direction, name), []).append(
                TimelineSeriesPoint(
                    t_rel_ms=_relative_seconds(stat.ts, run.started_at) * 1000,
                    value=value,
                )
            )
    for index, ((direction, name), points) in enumerate(
        sorted(rtp_groups.items(), key=lambda item: tuple(str(part) for part in item[0]))
    ):
        series.append(
            TimelineTypedSeries(
                series_id=f"rtp:{index}",
                category="transport",
                name=name,
                unit=_timeline_unit(name),
                clock_domain="control_plane_wall",
                direction=direction,
                stream_alias=f"rtp-{direction or 'unknown'}",
                source="rtp_stats",
                points=sorted(points, key=lambda point: point.t_rel_ms),
            )
        )
    return series


def _typed_timeline_artifacts(run: StoredRun) -> list[TimelineTypedArtifact]:
    return [
        TimelineTypedArtifact(
            artifact_id=f"recording:{index}",
            category="pipeline",
            name=f"{recording.stage} recording",
            kind="audio",
            start_ms=0,
            duration_ms=recording.duration_ms,
            stage=recording.stage,
            artifact_ref=f"recording:{recording.stage}",
            metadata={"format": recording.format},
        )
        for index, recording in enumerate(run.recordings)
    ]


def _typed_timeline_incidents(
    run: StoredRun,
    artifacts: list[TimelineTypedArtifact],
) -> list[TimelineIncident]:
    duration_ms = _run_timeline_duration_ms(run)
    artifact_by_stage = {
        artifact.stage: artifact.artifact_id
        for artifact in artifacts
        if artifact.stage is not None
    }
    stage_names = _pipeline_stage_names(run)
    previous_by_stage = {
        stage: stage_names[index - 1] if index > 0 else None
        for index, stage in enumerate(stage_names)
    }
    duration_evidence_by_verification = {
        item.verification_index: item
        for item in _duration_contraction_evidence(run)
    }
    signal_evidence_by_stage = {
        item.stage: item for item in _stage_signal_evidence(run)
    }
    incidents: list[TimelineIncident] = []
    failure_index = 0
    for verification_index, verification in enumerate(run.verifications):
        if verification.passed:
            continue
        evidence_refs = []
        artifact_id = artifact_by_stage.get(verification.stage)
        if artifact_id is not None:
            evidence_refs.append(artifact_id)
        previous_stage = previous_by_stage.get(verification.stage)
        previous_artifact_id = artifact_by_stage.get(previous_stage)
        if previous_artifact_id is not None:
            evidence_refs.insert(0, previous_artifact_id)
        start_ms = 0.0
        end_ms = duration_ms
        title = f"{verification.stage}: {verification.invariant}"
        summary = verification.detail
        duration_evidence = duration_evidence_by_verification.get(verification_index)
        signal_evidence = signal_evidence_by_stage.get(verification.stage)
        if duration_evidence is not None:
            start_ms = duration_evidence.output_duration_ms
            end_ms = duration_evidence.input_duration_ms
            title = f"Media time contracted at {verification.stage}"
            summary = (
                f"{duration_evidence.missing_duration_ms:g} ms missing between "
                f"{duration_evidence.previous_stage or 'upstream'} and "
                f"{verification.stage}"
            )
            evidence_refs.append(duration_evidence.event_id)
        elif verification.invariant == "level_preserving":
            title = f"Signal level contract failed at {verification.stage}"
            delta_db = _plain_number(verification.observed.get("delta_db"))
            if delta_db is not None:
                summary = f"Stage changed RMS level by {delta_db:+.2f} dB"
            if signal_evidence is not None:
                evidence_refs.append(signal_evidence.event_id)
        incidents.append(
            TimelineIncident(
                incident_id=f"verification:{failure_index}",
                rule_id=verification.invariant,
                category="pipeline",
                severity="error",
                title=title,
                summary=summary,
                start_ms=start_ms,
                end_ms=end_ms,
                confidence="certain",
                stage=verification.stage,
                observed=verification.observed,
                expected=verification.expected,
                evidence_refs=evidence_refs,
            )
        )
        failure_index += 1
    for item in _stage_full_scale_evidence(run):
        evidence_refs = []
        previous_artifact_id = artifact_by_stage.get(item.previous_stage)
        if previous_artifact_id is not None:
            evidence_refs.append(previous_artifact_id)
        artifact_id = artifact_by_stage.get(item.stage)
        if artifact_id is not None:
            evidence_refs.append(artifact_id)
        evidence_refs.append(item.event_id)
        incidents.append(
            TimelineIncident(
                incident_id=f"full-scale:{item.stage}",
                rule_id="pcm_full_scale_samples_v1",
                category="pipeline",
                severity="warning",
                title=f"Clipping suspected at {item.stage}",
                summary=(
                    f"{item.full_scale_sample_pct:.3g}% of observed samples reached "
                    "a PCM16 full-scale endpoint"
                ),
                start_ms=item.t_rel_ms,
                end_ms=item.t_rel_ms,
                confidence="medium",
                stage=item.stage,
                observed={
                    "full_scale_sample_pct": item.full_scale_sample_pct,
                    "sample_peak_dbfs": item.sample_peak_dbfs,
                    "sample_count": item.sample_count,
                    "first_observed_after_stage": item.previous_stage,
                },
                expected={
                    "full_scale_sample_pct": 0.0,
                    "clipping_confirmation": (
                        "waveform plateau or oversampled true-peak evidence required"
                    ),
                },
                evidence_refs=evidence_refs,
            )
        )
    for item in _assistant_output_dead_air_evidence(run):
        response = item.provider_response
        evidence_refs = []
        artifact_id = artifact_by_stage.get(item.silence.stage)
        if artifact_id is not None:
            evidence_refs.append(artifact_id)
        evidence_refs.extend(
            [
                f"{item.silence.correlation_alias}:start",
                response.start_event_id,
            ]
        )
        if response.done_event_id is not None:
            evidence_refs.append(response.done_event_id)
        incidents.append(
            TimelineIncident(
                incident_id=(
                    f"assistant-output-dead-air:{item.silence.stage}:"
                    f"{item.silence.window_index}:{response.response_index}"
                ),
                rule_id="assistant_output_dead_air_v1",
                category="conversation",
                severity="warning",
                title="Assistant output dead air suspected",
                summary=(
                    f"{item.overlap_ms:g} ms digital silence at "
                    f"{item.silence.stage} while provider response was active"
                ),
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                confidence="medium",
                stage=item.silence.stage,
                direction="assistant_to_caller",
                observed={
                    "digital_silence_while_provider_active_ms": item.overlap_ms,
                    "silence_window_duration_ms": (
                        item.silence.end_ms - item.silence.start_ms
                    ),
                    "provider_response_duration_ms": (
                        response.end_ms - response.start_ms
                    ),
                    "provider_completion_observed": response.completed,
                    "remote_playout_observed": False,
                },
                expected={
                    "digital_silence_while_provider_active_ms_below": (
                        ASSISTANT_OUTPUT_DEAD_AIR_MIN_OVERLAP_MS
                    ),
                    "remote_playout": "not observed",
                },
                evidence_refs=evidence_refs,
            )
        )
    if run.failure_alias is not None:
        incidents.append(
            TimelineIncident(
                incident_id="run:failure",
                rule_id="run_failure",
                category="session",
                severity="error",
                title="Run failed",
                summary=run.failure_alias,
                start_ms=duration_ms,
                end_ms=duration_ms,
                confidence="certain",
                evidence_refs=["run:failure"],
            )
        )
    for correlation_alias, events in _correlated_barge_in_events(run).items():
        completed = next(
            (event for event in reversed(events) if event.name == "barge_in_completed"),
            None,
        )
        if completed is None:
            continue
        start = min(events, key=lambda event: event.ts)
        start_ms = _relative_seconds(start.ts, run.started_at) * 1000
        end_ms = max(start_ms, _relative_seconds(completed.ts, run.started_at) * 1000)
        cleared = next(
            (event for event in events if event.name == "playback_queue_cleared"),
            completed,
        )
        dropped_frames = _numeric_attribute(cleared.attributes, "dropped_frames")
        discarded_ms = _numeric_attribute(cleared.attributes, "discarded_audio_ms")
        played_audio_end_ms = _numeric_attribute(
            completed.attributes,
            "played_audio_end_ms",
        )
        interrupt_path = str(completed.attributes.get("interrupt_path", "unobserved"))
        summary = "Playback queue cleared"
        if discarded_ms is not None:
            summary += f"; {discarded_ms:g} ms queued audio discarded"
        incidents.append(
            TimelineIncident(
                incident_id=f"barge-in:{correlation_alias}",
                rule_id="barge_in_sequence",
                category="conversation",
                severity="info",
                title="Barge-in handled",
                summary=summary,
                start_ms=start_ms,
                end_ms=end_ms,
                confidence="high",
                direction="caller_to_assistant",
                observed={
                    "handling_duration_ms": end_ms - start_ms,
                    "interrupt_path": interrupt_path,
                    "played_audio_end_ms": played_audio_end_ms,
                    "dropped_frames": dropped_frames,
                    "discarded_audio_ms": discarded_ms,
                },
                expected={
                    "interrupt_path": "provider-auto-or-request",
                    "playback_queue_cleared": True,
                    "truncate_when_position_available": True,
                },
                evidence_refs=[event.event_id for event in events],
            )
        )
    for window_index, window in enumerate(_rtp_quality_windows(run)):
        loss_samples = [item.loss_pct for item in window if "loss" in item.triggers]
        jitter_samples = [
            item.jitter_ms for item in window if "jitter" in item.triggers
        ]
        mos_samples = [item.mos for item in window if "mos" in item.triggers]
        triggers = sorted({trigger for item in window for trigger in item.triggers})
        loss_burst_suspected = any(
            "loss" in previous.triggers and "loss" in current.triggers
            for previous, current in pairwise(window)
        )
        if loss_burst_suspected:
            title = "RTP loss burst suspected"
        elif triggers == ["loss"]:
            title = "RTP packet loss elevated"
        elif triggers == ["jitter"]:
            title = "RTP jitter elevated"
        elif triggers == ["mos"]:
            title = "RTP quality degraded"
        else:
            title = "RTP quality degradation"
        direction = window[0].direction
        trigger_summary = ", ".join(triggers)
        summary = (
            f"{direction or 'unknown-direction'} RTP: {trigger_summary} across "
            f"{len(window)} observation{'s' if len(window) != 1 else ''}"
        )
        incidents.append(
            TimelineIncident(
                incident_id=_rtp_window_alias(window_index, window),
                rule_id="rtp_quality_degradation_v1",
                category="transport",
                severity="warning",
                title=title,
                summary=summary,
                start_ms=window[0].t_rel_ms,
                end_ms=max(window[0].t_rel_ms, window[-1].t_rel_ms),
                confidence="medium",
                direction=direction,
                observed={
                    "sample_count": len(window),
                    "triggers": triggers,
                    "peak_loss_pct": max(loss_samples) if loss_samples else None,
                    "peak_jitter_ms": max(jitter_samples) if jitter_samples else None,
                    "minimum_mos": min(mos_samples) if mos_samples else None,
                    "loss_burst_suspected": loss_burst_suspected,
                },
                expected={
                    "loss_pct_below": RTP_LOSS_WARNING_PCT,
                    "jitter_ms_below": RTP_JITTER_WARNING_MS,
                    "mos_above": RTP_MOS_WARNING_SCORE,
                    "packet_gap_confirmation": "RTP sequence or arrival cadence required",
                },
                evidence_refs=[
                    evidence_ref
                    for item in window
                    for evidence_ref in item.evidence_refs
                ],
            )
        )
    return incidents


def _rtp_window_alias(
    window_index: int,
    window: list[_RtpQualityEvidence],
) -> str:
    return f"rtp-degradation:{window[0].direction or 'unknown'}:{window_index}"


def _correlated_barge_in_events(
    run: StoredRun,
) -> dict[str, list[TimelineEventArtifact]]:
    groups: dict[str, list[TimelineEventArtifact]] = {}
    for event in run.timeline_events:
        if event.correlation_alias is None:
            continue
        if event.name not in {
            "provider_input_speech_started",
            "provider_interrupted",
            "provider_auto_interrupt_confirmed",
            "provider_interrupt_requested",
            "provider_truncate_requested",
            "playback_queue_cleared",
            "barge_in_completed",
        }:
            continue
        groups.setdefault(event.correlation_alias, []).append(event)
    return {
        alias: sorted(events, key=lambda event: (event.ts, event.event_id))
        for alias, events in groups.items()
    }


def _numeric_attribute(attributes: Mapping[str, Any], name: str) -> float | None:
    value = attributes.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _metric_timeline_category(metric: MetricArtifact) -> TimelineCategory:
    if metric.stage is not None:
        return "pipeline"
    if metric.name in {"barge_in_events", "dtmf_events"}:
        return "conversation"
    if metric.name == "output_frames_dropped":
        return "buffer"
    if metric.name.startswith("provider_"):
        return "provider"
    return "runtime"


def _timeline_unit(name: str) -> str | None:
    if name in _TIMELINE_UNITS:
        return _TIMELINE_UNITS[name]
    if name.endswith("_ms"):
        return "ms"
    if name.endswith("_pct"):
        return "percent"
    if name.endswith("_events") or name.endswith("_failures"):
        return "count"
    return None


def _run_timeline_duration_ms(run: StoredRun) -> float:
    recording_duration = max(
        (recording.duration_ms for recording in run.recordings),
        default=0.0,
    )
    ended_duration = (
        max(0.0, (run.ended_at - run.started_at).total_seconds() * 1000)
        if run.ended_at is not None
        else 0.0
    )
    return max(recording_duration, ended_duration)


class RunRepository(Protocol):
    def save(self, run: StoredRun) -> None: ...

    def get(self, run_id: str) -> StoredRun | None: ...

    def list_recent(self) -> list[StoredRun]: ...

    def list_all(self) -> list[StoredRun]: ...


class RunRepositoryError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("run repository is unavailable")


@dataclass
class InMemoryRunRepository:
    runs: dict[str, StoredRun] = field(default_factory=dict)

    def save(self, run: StoredRun) -> None:
        self.runs[run.run_id] = run

    def get(self, run_id: str) -> StoredRun | None:
        return self.runs.get(run_id)

    def list_recent(self) -> list[StoredRun]:
        return sorted(self.runs.values(), key=lambda run: run.started_at, reverse=True)

    def list_all(self) -> list[StoredRun]:
        return sorted(self.runs.values(), key=lambda run: run.started_at)


@dataclass
class PostgresRunRepository:
    session_factory: sessionmaker[Session] = field(repr=False)

    def save(self, run: StoredRun) -> None:
        try:
            self._save(run)
        except SQLAlchemyError as exc:
            raise RunRepositoryError from exc

    def _save(self, run: StoredRun) -> None:
        run_uuid = UUID(run.run_id)
        with self.session_factory.begin() as session:
            self._save_in_session(session, run, run_uuid)

    def save_queued_run(
        self,
        run: StoredRun,
        *,
        now: datetime | None = None,
    ) -> str:
        if run.status != "running" or run.ended_at is not None:
            raise ValueError("queued-run-not-running")
        run_uuid = UUID(run.run_id)
        available_at = _as_utc(now or datetime.now(UTC))
        try:
            with self.session_factory.begin() as session:
                existing = session.scalar(
                    select(RunJobRow).where(RunJobRow.run_id == run_uuid)
                )
                if existing is not None:
                    return str(existing.id)
                self._save_in_session(session, run, run_uuid)
                job = RunJobRow(
                    run_id=run_uuid,
                    state="queued",
                    available_at=available_at,
                    attempts=0,
                )
                session.add(job)
                session.flush()
                return str(job.id)
        except SQLAlchemyError as exc:
            raise RunRepositoryError from exc

    def commit_leased_result(
        self,
        run: StoredRun,
        lease: RunJobLease,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        if run.status not in {"completed", "failed"} or run.ended_at is None:
            raise ValueError("leased-run-result-not-terminal")
        try:
            run_uuid = UUID(run.run_id)
            if UUID(lease.run_id) != run_uuid:
                return False
            job_uuid = UUID(lease.job_id)
            lease_token = UUID(lease.lease_token)
        except (AttributeError, TypeError, ValueError):
            return False
        committed_at = _as_utc(now or datetime.now(UTC))
        try:
            with self.session_factory.begin() as session:
                job = session.scalar(
                    select(RunJobRow)
                    .where(
                        RunJobRow.id == job_uuid,
                        RunJobRow.run_id == run_uuid,
                        RunJobRow.state == "leased",
                        RunJobRow.lease_owner == worker_id,
                        RunJobRow.lease_token == lease_token,
                        RunJobRow.lease_expires_at > committed_at,
                    )
                    .with_for_update()
                )
                if job is None:
                    return False
                self._save_in_session(session, run, run_uuid)
                job.state = "completed" if run.status == "completed" else "failed"
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None
                job.failure_alias = run.failure_alias if run.status == "failed" else None
                session.flush()
            return True
        except SQLAlchemyError as exc:
            raise RunRepositoryError from exc

    def _save_in_session(
        self,
        session: Session,
        run: StoredRun,
        run_uuid: UUID,
    ) -> None:
        row = session.get(RunRow, run_uuid)
        values = {
            "config_hash": run.config_hash,
            "call_id": run.call_id,
            "conversation_id": run.conversation_id,
            "provider": run.provider,
            "engine": run.engine,
            "status": run.status,
            "failure_alias": run.failure_alias,
            "resolved_config": run.resolved_config,
            "environment_metadata": run.environment.model_dump(mode="json"),
            "readiness_checklist": [
                item.model_dump(mode="json") for item in run.readiness_checklist
            ],
            "started_at": run.started_at,
            "ended_at": run.ended_at,
        }
        if row is None:
            row = RunRow(id=run_uuid, **values)
            session.add(row)
        else:
            for name, value in values.items():
                setattr(row, name, value)
        session.flush()

        child_models = (
            RecordingRow,
            SpanRow,
            MetricRow,
            VerificationRow,
            SipEventRow,
            RtpStatRow,
            TimelineEventRow,
        )
        for child_model in child_models:
            session.execute(delete(child_model).where(child_model.run_id == run_uuid))

        session.add_all(
            [
                RecordingRow(
                    run_id=run_uuid,
                    ordinal=ordinal,
                    stage=item.stage,
                    uri=item.uri,
                    format=item.format,
                    duration_ms=item.duration_ms,
                )
                for ordinal, item in enumerate(run.recordings)
            ]
            + [
                SpanRow(
                    run_id=run_uuid,
                    ordinal=ordinal,
                    trace_id=item.trace_id,
                    span_id=item.span_id,
                    parent_id=item.parent_id,
                    name=item.name,
                    start_ns=item.start_ns,
                    end_ns=item.end_ns,
                    attrs=item.attrs,
                )
                for ordinal, item in enumerate(run.spans)
            ]
            + [
                MetricRow(
                    run_id=run_uuid,
                    ordinal=ordinal,
                    stage=item.stage,
                    name=item.name,
                    value=item.value,
                    ts=item.ts,
                )
                for ordinal, item in enumerate(run.metrics)
            ]
            + [
                VerificationRow(
                    run_id=run_uuid,
                    ordinal=ordinal,
                    stage=item.stage,
                    invariant=item.invariant,
                    passed=item.passed,
                    observed=item.observed,
                    expected=item.expected,
                    detail=item.detail,
                )
                for ordinal, item in enumerate(run.verifications)
            ]
            + [
                SipEventRow(
                    run_id=run_uuid,
                    ordinal=ordinal,
                    call_id=item.call_id,
                    method=item.method,
                    direction=item.direction,
                    status_code=item.status_code,
                    summary_alias=item.summary_alias,
                    ts=item.ts,
                )
                for ordinal, item in enumerate(run.sip_events)
            ]
            + [
                RtpStatRow(
                    run_id=run_uuid,
                    ordinal=ordinal,
                    ts=item.ts,
                    jitter_ms=item.jitter_ms,
                    loss_pct=item.loss_pct,
                    mos=item.mos,
                    direction=item.direction,
                    rtt_ms=item.rtt_ms,
                )
                for ordinal, item in enumerate(run.rtp_stats)
            ]
            + [
                TimelineEventRow(
                    run_id=run_uuid,
                    ordinal=ordinal,
                    event_id=item.event_id,
                    category=item.category,
                    name=item.name,
                    ts=item.ts,
                    clock_domain=item.clock_domain,
                    alignment_uncertainty_ms=item.alignment_uncertainty_ms,
                    direction=item.direction,
                    stage=item.stage,
                    stream_alias=item.stream_alias,
                    source=item.source,
                    correlation_alias=item.correlation_alias,
                    attributes=item.attributes,
                )
                for ordinal, item in enumerate(run.timeline_events)
            ]
        )

    def get(self, run_id: str) -> StoredRun | None:
        try:
            run_uuid = UUID(run_id)
        except ValueError:
            return None
        try:
            with self.session_factory() as session:
                row = session.get(RunRow, run_uuid)
                return self._restore(session, row) if row is not None else None
        except (SQLAlchemyError, ValidationError, KeyError, TypeError, AttributeError) as exc:
            raise RunRepositoryError from exc

    def list_recent(self) -> list[StoredRun]:
        try:
            with self.session_factory() as session:
                rows = session.scalars(
                    select(RunRow).order_by(RunRow.started_at.desc())
                ).all()
                return [self._restore(session, row) for row in rows]
        except (SQLAlchemyError, ValidationError, KeyError, TypeError, AttributeError) as exc:
            raise RunRepositoryError from exc

    def list_all(self) -> list[StoredRun]:
        try:
            with self.session_factory() as session:
                rows = session.scalars(select(RunRow).order_by(RunRow.started_at)).all()
                return [self._restore(session, row) for row in rows]
        except (SQLAlchemyError, ValidationError, KeyError, TypeError, AttributeError) as exc:
            raise RunRepositoryError from exc

    def _restore(self, session: Session, row: RunRow) -> StoredRun:
        run_uuid = row.id
        recordings = session.scalars(
            select(RecordingRow)
            .where(RecordingRow.run_id == run_uuid)
            .order_by(RecordingRow.ordinal, RecordingRow.id)
        ).all()
        spans = session.scalars(
            select(SpanRow)
            .where(SpanRow.run_id == run_uuid)
            .order_by(SpanRow.ordinal, SpanRow.id)
        ).all()
        metrics = session.scalars(
            select(MetricRow)
            .where(MetricRow.run_id == run_uuid)
            .order_by(MetricRow.ordinal, MetricRow.id)
        ).all()
        verifications = session.scalars(
            select(VerificationRow)
            .where(VerificationRow.run_id == run_uuid)
            .order_by(VerificationRow.ordinal, VerificationRow.id)
        ).all()
        sip_events = session.scalars(
            select(SipEventRow)
            .where(SipEventRow.run_id == run_uuid)
            .order_by(SipEventRow.ordinal, SipEventRow.id)
        ).all()
        rtp_stats = session.scalars(
            select(RtpStatRow)
            .where(RtpStatRow.run_id == run_uuid)
            .order_by(RtpStatRow.ordinal, RtpStatRow.id)
        ).all()
        timeline_events = session.scalars(
            select(TimelineEventRow)
            .where(TimelineEventRow.run_id == run_uuid)
            .order_by(TimelineEventRow.ordinal, TimelineEventRow.id)
        ).all()
        restored_recordings = [
            RecordingArtifact(
                stage=item.stage,
                uri=item.uri,
                format=item.format,
                duration_ms=item.duration_ms,
            )
            for item in recordings
        ]
        return StoredRun(
            run_id=str(row.id),
            config_hash=row.config_hash,
            call_id=row.call_id,
            conversation_id=row.conversation_id,
            provider=row.provider,
            engine=row.engine,
            status=row.status,
            failure_alias=row.failure_alias,
            started_at=_as_utc(row.started_at),
            ended_at=_as_utc(row.ended_at) if row.ended_at is not None else None,
            resolved_config=row.resolved_config,
            recordings=restored_recordings,
            spans=[
                SpanArtifact(
                    trace_id=item.trace_id,
                    span_id=item.span_id,
                    parent_id=item.parent_id,
                    name=item.name,
                    start_ns=item.start_ns,
                    end_ns=item.end_ns,
                    attrs=item.attrs,
                )
                for item in spans
            ],
            metrics=[
                MetricArtifact(
                    stage=item.stage,
                    name=item.name,
                    value=item.value,
                    ts=_as_utc(item.ts),
                )
                for item in metrics
            ],
            verifications=[
                VerificationResult(
                    stage=item.stage,
                    invariant=item.invariant,
                    passed=item.passed,
                    observed=item.observed,
                    expected=item.expected,
                    detail=item.detail,
                )
                for item in verifications
            ],
            sip_events=[
                SipEventResponse(
                    call_id=item.call_id,
                    method=item.method,
                    direction=item.direction,
                    status_code=item.status_code,
                    summary_alias=item.summary_alias,
                    ts=_as_utc(item.ts),
                )
                for item in sip_events
            ],
            rtp_stats=[
                RtpStatResponse(
                    ts=_as_utc(item.ts),
                    jitter_ms=item.jitter_ms,
                    loss_pct=item.loss_pct,
                    mos=item.mos,
                    direction=item.direction,
                    rtt_ms=item.rtt_ms,
                )
                for item in rtp_stats
            ],
            timeline_events=[
                TimelineEventArtifact(
                    event_id=item.event_id,
                    category=item.category,
                    name=item.name,
                    ts=_as_utc(item.ts),
                    clock_domain=item.clock_domain,
                    alignment_uncertainty_ms=item.alignment_uncertainty_ms,
                    direction=item.direction,
                    stage=item.stage,
                    stream_alias=item.stream_alias,
                    source=item.source,
                    correlation_alias=item.correlation_alias,
                    attributes=item.attributes,
                )
                for item in timeline_events
            ],
            environment=RunEnvironmentMetadata.model_validate(row.environment_metadata),
            readiness_checklist=[
                ReadinessChecklistItem.model_validate(item) for item in row.readiness_checklist
            ],
        )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass
class RunAudioBuffer:
    sample_rate_hz: int
    channels: int
    pcm_s16le: bytearray = field(default_factory=bytearray)


@dataclass
class RunApiState:
    artifact_root: Path = Path("artifacts/recordings")
    recording_sink: RecordingSink | None = None
    storage_readiness: StorageReadiness | None = None
    remote_recording_reader: RemoteRecordingReader | None = None
    remote_audio_access_token: str | None = field(default=None, repr=False)
    remote_audio_session_auth: RemoteAudioSessionAuth | None = None
    repository: RunRepository = field(default_factory=InMemoryRunRepository)
    repository_readiness: RepositoryReadiness = field(
        default_factory=memory_repository_readiness
    )
    job_queue: RunJobQueue | None = None
    persistent_run_submitter: Callable[[StoredRun], str] | None = field(
        default=None,
        repr=False,
    )
    worker_telemetry_provider: Callable[[], Mapping[str, bool | int]] | None = field(
        default=None,
        repr=False,
    )
    audio_buffers: dict[tuple[str, str], RunAudioBuffer] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.remote_recording_reader is None) != (
            self.remote_audio_access_token is None
        ):
            raise ValueError("remote recording reader and access token must be configured together")
        if self.remote_audio_access_token is not None and (
            not 32 <= len(self.remote_audio_access_token) <= 256
            or not self.remote_audio_access_token.isascii()
            or any(character.isspace() for character in self.remote_audio_access_token)
        ):
            raise ValueError("remote audio access token must be a safe 32..256 character value")
        if self.remote_audio_session_auth is not None and self.remote_recording_reader is None:
            raise ValueError("remote audio session requires a remote recording reader")
        if self.storage_readiness is None:
            self.storage_readiness = (
                injected_storage_readiness()
                if self.recording_sink is not None
                else local_storage_readiness()
            )

    def create_harness(self) -> EngineHarness:
        sink = self.recording_sink or LocalRecordingSink(self.artifact_root)
        return EngineHarness(recording_sink=sink)


def get_run_api_state(request: Request) -> RunApiState:
    return request.app.state.voxbench


RunApiStateDependency = Annotated[RunApiState, Depends(get_run_api_state)]


def _resolve_run_request(request: RunCreateRequest):
    registry = RegistryService()
    try:
        for manifest in request.manifests:
            registry.register_manifest(manifest)
        for config in request.configs:
            registry.register_config(config)
        return registry.resolve_config(request.config_name)
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _create_running_run(request: RunCreateRequest, resolved: Any) -> StoredRun:
    run_id = str(uuid4())
    return StoredRun(
        run_id=run_id,
        config_hash=resolved.hash,
        call_id=request.call_id,
        conversation_id="",
        provider=resolved.resolved["spec"]["ai"]["provider"],
        engine=resolved.resolved["spec"]["engine"]["kind"],
        status="running",
        started_at=datetime.now(UTC),
        ended_at=None,
        resolved_config=resolved.resolved,
        recordings=[],
        spans=[],
        metrics=[],
        verifications=[],
        environment=request.environment,
        readiness_checklist=request.readiness_checklist,
    )


def _populate_stored_run_result(stored: StoredRun, api_state: RunApiState) -> None:
    try:
        harness_result = api_state.create_harness().run_once(
            run_id=stored.run_id,
            resolved_config=stored.resolved_config,
            config_hash=stored.config_hash,
        )
    except Exception:
        stored.status = "failed"
        stored.failure_alias = "engine-harness-error"
        stored.ended_at = datetime.now(UTC)
        raise
    else:
        stored.conversation_id = harness_result.conversation_id
        stored.status = "completed"
        stored.ended_at = datetime.now(UTC)
        stored.recordings = harness_result.recordings
        stored.spans = harness_result.spans
        stored.metrics = harness_result.metrics
        stored.verifications = verify_recordings(
            resolved_config=stored.resolved_config,
            recordings=harness_result.recordings,
            metrics=harness_result.metrics,
        )


def _execute_stored_run(stored: StoredRun, api_state: RunApiState) -> None:
    try:
        _populate_stored_run_result(stored, api_state)
    finally:
        api_state.repository.save(stored)


def _start_background_run(stored: StoredRun, api_state: RunApiState) -> None:
    def target() -> None:
        try:
            _execute_stored_run(stored, api_state)
        except Exception:
            # The run status carries the failure; a future persistent runner can store detail.
            return

    Thread(target=target, name=f"voxbench-run-{stored.run_id}", daemon=True).start()


def _live_preview(api_state: RunApiState) -> list[LiveRunStatusResponse]:
    return [run.to_live_status() for run in api_state.repository.list_recent()]


def _cross_session_trends(api_state: RunApiState) -> list[CrossSessionTrendResponse]:
    groups: dict[
        tuple[EnvironmentProfile, str, str],
        list[CrossSessionTrendPoint],
    ] = {}
    for run in api_state.repository.list_all():
        if run.status not in {"completed", "failed"} or run.ended_at is None:
            continue
        server_alias = run.environment.server_alias
        if server_alias is None:
            continue
        latest_metrics = {metric.name: metric for metric in run.latest_host_metrics()}
        for metric_name in CROSS_SESSION_METRIC_NAMES:
            metric = latest_metrics.get(metric_name)
            if metric is None:
                continue
            key = (
                run.environment.environment_profile,
                server_alias,
                metric_name,
            )
            groups.setdefault(key, []).append(
                CrossSessionTrendPoint(
                    run_id=run.run_id,
                    started_at=run.started_at,
                    value=metric.value,
                )
            )

    trends: list[CrossSessionTrendResponse] = []
    sorted_groups = sorted(
        groups.items(),
        key=lambda item: item[0],
    )
    for (profile, server_alias, metric_name), points in sorted_groups:
        values = [point.value for point in points]
        total_delta = values[-1] - values[0]
        if len(points) < CROSS_SESSION_MIN_SAMPLES:
            state: CrossSessionTrendState = "insufficient"
        elif total_delta > 0 and all(
            current >= previous for previous, current in pairwise(values)
        ):
            state = "increasing"
        else:
            state = "stable"
        trends.append(
            CrossSessionTrendResponse(
                metric=metric_name,
                environment_profile=profile,
                server_alias=server_alias,
                state=state,
                sample_count=len(points),
                first_value=values[0],
                latest_value=values[-1],
                total_delta=total_delta,
                points=points,
            )
        )
    return trends


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"example payload source is unavailable: {path.name}",
        ) from exc


def _example_run_payload(environment_profile: EnvironmentProfile) -> RunCreateRequest:
    return RunCreateRequest(
        config_name="baseline",
        configs=[_load_json_file(EXAMPLE_CONFIG_PATH)],
        manifests=[_load_json_file(path) for path in EXAMPLE_MANIFEST_PATHS],
        environment=RunEnvironmentMetadata(
            environment_profile=environment_profile,
            server_alias=f"{environment_profile}-host-a",
            integration_target_alias="integration-target-a",
            environment_snapshot_hash="example-snapshot-ref",
            started_from="web-async-run-example",
            operator_note="Example aliases only; replace before demo or integration use.",
            manual_blockers=[],
            tags=["phase4", "example"],
            related_internal_ref="example-run-ref",
            secret_ref_names=["example-provider-api-key-ref"],
        ),
        readiness_checklist=[
            ReadinessChecklistItem(
                item_id=item_id,
                label=label,
                status="unknown",
            )
            for item_id, label in DEFAULT_READINESS_ITEMS
        ],
    )


def _live_demo_run_payload(request: LiveDemoRunRequest) -> RunCreateRequest:
    config = _load_json_file(LIVE_DEMO_CONFIG_PATHS[request.provider])
    _apply_agc_overrides(config, request)
    readiness = _provider_readiness(request)
    missing_dependency = readiness.missing_optional_dependency
    provider_note = (
        "Simulated live bridge; provider network connection is not opened in this slice."
    )
    if missing_dependency:
        provider_note = f"{provider_note} Optional dependency missing: {missing_dependency}."

    return RunCreateRequest(
        config_name=config["meta"]["name"],
        configs=[config],
        manifests=[
            _load_json_file(LIVE_DEMO_PROVIDER_MANIFEST_PATHS[request.provider]),
            *[_load_json_file(path) for path in LIVE_DEMO_PROCESSOR_MANIFEST_PATHS],
        ],
        call_id=request.call_id,
        environment=RunEnvironmentMetadata(
            environment_profile="demo",
            server_alias="local-softphone-demo-host",
            integration_target_alias=f"{request.provider}-live-api",
            started_from="live-demo-simulated-bridge",
            operator_note=provider_note,
            manual_blockers=[],
            tags=["live-demo", "simulated-audio", request.provider],
            related_internal_ref="live-softphone-demo-plan",
            secret_ref_names=[readiness.env_var],
        ),
        readiness_checklist=[
            ReadinessChecklistItem(
                item_id="ai_phone_setup_complete",
                label="AI phone setup complete",
                status="unknown",
                note="Configure macOS softphone and Asterisk before real RTP bridge.",
            ),
            ReadinessChecklistItem(
                item_id="intermediate_db_environment_registration_complete",
                label="Intermediate DB/environment registration complete",
                status="pass",
            ),
            ReadinessChecklistItem(
                item_id="connection_route_verified",
                label="Connection route verified",
                status="unknown",
                note="Structured SIP/RTP events are simulated in this slice.",
            ),
            ReadinessChecklistItem(
                item_id="expected_codec_sample_rate_cadence_declared",
                label="Expected codec/sample rate/cadence declared",
                status="pass",
            ),
            ReadinessChecklistItem(
                item_id="recording_taps_enabled",
                label="Recording taps enabled",
                status="pass",
            ),
            ReadinessChecklistItem(
                item_id="host_metrics_enabled",
                label="Host metrics enabled",
                status="unknown",
            ),
            ReadinessChecklistItem(
                item_id="secret_references_present",
                label="Secret references present",
                status="pass" if request.dry_run or readiness.has_api_key else "fail",
                note=f"Uses env var alias {readiness.env_var}; value is not stored.",
            ),
        ],
    )


def _apply_agc_overrides(config: dict[str, Any], request: LiveDemoRunRequest) -> None:
    agc_params = None
    for stage in config.get("spec", {}).get("media", {}).get("pipeline", []):
        if isinstance(stage, dict) and stage.get("type") == "agc":
            agc_params = stage.setdefault("params", {})
            break
    if not isinstance(agc_params, dict):
        return
    if request.target_rms is not None:
        agc_params["target_rms"] = request.target_rms
    if request.max_gain is not None:
        agc_params["max_gain"] = request.max_gain
    if request.noise_floor is not None:
        agc_params["noise_floor"] = request.noise_floor


def _provider_readiness(request: LiveDemoRunRequest):
    if request.provider == "openai-realtime":
        return OpenAIRealtimeProvider().readiness(dry_run=request.dry_run)
    return GeminiLiveProvider().readiness(dry_run=request.dry_run)


def _configured_stage_names(stored: StoredRun) -> set[str]:
    return {
        stage["type"]
        for stage in stored.resolved_config["spec"]["media"]["pipeline"]
        if isinstance(stage, dict) and isinstance(stage.get("type"), str)
    }


def _decode_audio_chunk(chunk: AudioChunkObservationRequest) -> bytes:
    try:
        pcm = base64.b64decode(chunk.pcm_s16le_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="pcm_s16le_base64 is not valid base64") from exc
    if not pcm:
        raise HTTPException(status_code=400, detail="audio chunks must not be empty")
    if len(pcm) % (2 * chunk.channels):
        raise HTTPException(
            status_code=400,
            detail="PCM16LE audio length must contain complete channel frames",
        )
    return pcm


def _write_observed_audio(
    *,
    api_state: RunApiState,
    stored: StoredRun,
    chunk: AudioChunkObservationRequest,
    pcm: bytes,
) -> None:
    key = (stored.run_id, chunk.stage)
    buffer = api_state.audio_buffers.get(key)
    if buffer is None:
        buffer = RunAudioBuffer(
            sample_rate_hz=chunk.sample_rate_hz,
            channels=chunk.channels,
        )
        api_state.audio_buffers[key] = buffer
    elif (buffer.sample_rate_hz, buffer.channels) != (
        chunk.sample_rate_hz,
        chunk.channels,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"audio format changed within stage '{chunk.stage}'",
        )

    buffer.pcm_s16le.extend(pcm)
    stage_filename = quote(chunk.stage, safe="")
    path = api_state.artifact_root.resolve() / stored.run_id / f"{stage_filename}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(buffer.channels)
        wav.setsampwidth(2)
        wav.setframerate(buffer.sample_rate_hz)
        wav.writeframes(buffer.pcm_s16le)

    duration_ms = (
        len(buffer.pcm_s16le) / (2 * buffer.channels * buffer.sample_rate_hz) * 1000.0
    )
    recording = RecordingArtifact(
        stage=chunk.stage,
        uri=path.as_uri(),
        format={
            "encoding": "linear16",
            "rate": buffer.sample_rate_hz,
            "channels": buffer.channels,
        },
        duration_ms=duration_ms,
    )
    existing_index = next(
        (
            index
            for index, existing in enumerate(stored.recordings)
            if existing.stage == chunk.stage
        ),
        None,
    )
    if existing_index is None:
        stored.recordings.append(recording)
    else:
        stored.recordings[existing_index] = recording


def create_runs_router() -> APIRouter:
    router = APIRouter()

    @router.get(
        "/repository/readiness",
        response_model=RepositoryReadinessResponse,
    )
    async def get_repository_readiness(
        api_state: RunApiStateDependency,
    ) -> RepositoryReadinessResponse:
        provider = api_state.worker_telemetry_provider
        telemetry = provider() if provider is not None else {}
        return RepositoryReadinessResponse(
            **api_state.repository_readiness.__dict__,
            worker_enabled=provider is not None,
            worker_running=bool(telemetry.get("running", False)),
            worker_processed_total=int(telemetry.get("processed_total", 0)),
            worker_error_total=int(telemetry.get("error_total", 0)),
            worker_lease_lost_total=int(telemetry.get("lease_lost_total", 0)),
        )

    @router.get("/storage/readiness", response_model=StorageReadinessResponse)
    async def get_storage_readiness(
        api_state: RunApiStateDependency,
    ) -> StorageReadinessResponse:
        assert api_state.storage_readiness is not None
        session_auth = api_state.remote_audio_session_auth
        return StorageReadinessResponse(
            **api_state.storage_readiness.__dict__,
            web_audio_session_enabled=session_auth is not None,
            web_audio_cookie_secure=(
                session_auth.cookie_secure if session_auth is not None else None
            ),
            web_audio_session_ttl_seconds=(
                session_auth.ttl_seconds if session_auth is not None else None
            ),
        )

    @router.get(
        "/auth/remote-audio/session",
        response_model=AudioSessionStatusResponse,
    )
    async def get_remote_audio_session_status(
        request: Request,
        api_state: RunApiStateDependency,
    ) -> AudioSessionStatusResponse:
        session_auth = api_state.remote_audio_session_auth
        if session_auth is None:
            return AudioSessionStatusResponse(enabled=False, authenticated=False)
        remaining = session_auth.remaining_seconds(
            request.cookies.get(REMOTE_AUDIO_SESSION_COOKIE)
        )
        return AudioSessionStatusResponse(
            enabled=True,
            authenticated=remaining is not None,
            expires_in_seconds=remaining,
        )

    @router.post(
        "/auth/remote-audio/session",
        response_model=AudioSessionStatusResponse,
    )
    async def create_remote_audio_session(
        request: Request,
        api_state: RunApiStateDependency,
    ) -> Response:
        session_auth = api_state.remote_audio_session_auth
        if session_auth is None:
            raise HTTPException(status_code=404, detail="web audio session is disabled")
        login_token = await _read_audio_session_login_token(request)
        try:
            cookie = session_auth.issue_cookie(login_token)
        except AudioSessionLoginError as exc:
            raise HTTPException(
                status_code=401,
                detail="web audio session login rejected",
            ) from exc
        response = JSONResponse(
            AudioSessionStatusResponse(
                enabled=True,
                authenticated=True,
                expires_in_seconds=session_auth.ttl_seconds,
            ).model_dump()
        )
        response.set_cookie(
            REMOTE_AUDIO_SESSION_COOKIE,
            cookie,
            max_age=session_auth.ttl_seconds,
            path="/",
            secure=session_auth.cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @router.delete(
        "/auth/remote-audio/session",
        response_model=AudioSessionStatusResponse,
    )
    async def delete_remote_audio_session(
        api_state: RunApiStateDependency,
    ) -> Response:
        session_auth = api_state.remote_audio_session_auth
        response = JSONResponse(
            AudioSessionStatusResponse(
                enabled=session_auth is not None,
                authenticated=False,
            ).model_dump()
        )
        response.delete_cookie(REMOTE_AUDIO_SESSION_COOKIE, path="/")
        return response

    @router.post("/runs", response_model=RunResponse)
    async def create_run(
        request: RunCreateRequest,
        api_state: RunApiStateDependency,
    ) -> RunResponse:
        resolved = _resolve_run_request(request)
        stored = _create_running_run(request, resolved)
        api_state.repository.save(stored)
        _execute_stored_run(stored, api_state)
        return stored.to_response()

    @router.post(
        "/runs/async",
        response_model=LiveRunStatusResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_run_async(
        request: RunCreateRequest,
        api_state: RunApiStateDependency,
    ) -> LiveRunStatusResponse:
        resolved = _resolve_run_request(request)
        stored = _create_running_run(request, resolved)
        if api_state.persistent_run_submitter is not None:
            api_state.persistent_run_submitter(stored)
        else:
            api_state.repository.save(stored)
            _start_background_run(stored, api_state)
        return stored.to_live_status()

    @router.post("/runs/observed", response_model=RunResponse)
    async def create_observed_run(
        request: RunCreateRequest,
        api_state: RunApiStateDependency,
    ) -> RunResponse:
        resolved = _resolve_run_request(request)
        stored = _create_running_run(request, resolved)
        stored.conversation_id = f"observed-{stored.run_id}"
        api_state.repository.save(stored)
        return stored.to_response()

    @router.get("/runs", response_model=list[RunSummaryResponse])
    async def list_runs(api_state: RunApiStateDependency) -> list[RunSummaryResponse]:
        return [run.to_summary() for run in api_state.repository.list_recent()]

    @router.get("/runs/live-preview", response_model=list[LiveRunStatusResponse])
    async def list_live_preview(
        api_state: RunApiStateDependency,
    ) -> list[LiveRunStatusResponse]:
        return _live_preview(api_state)

    @router.get(
        "/runs/cross-session-trends",
        response_model=list[CrossSessionTrendResponse],
    )
    async def list_cross_session_trends(
        api_state: RunApiStateDependency,
    ) -> list[CrossSessionTrendResponse]:
        return _cross_session_trends(api_state)

    @router.post("/runs/live-demo/simulated", response_model=RunResponse)
    async def create_simulated_live_demo_run(
        request: LiveDemoRunRequest,
        api_state: RunApiStateDependency,
    ) -> RunResponse:
        readiness = _provider_readiness(request)
        if not request.dry_run and not readiness.ready:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{request.provider} is not ready for a non-dry-run demo; "
                    "set the API key env var and install optional provider deps"
                ),
            )

        run_request = _live_demo_run_payload(request)
        resolved = _resolve_run_request(run_request)
        stored = _create_running_run(run_request, resolved)
        api_state.repository.save(stored)
        try:
            bridge_result = run_simulated_live_bridge(
                run_id=stored.run_id,
                call_id=stored.call_id,
                resolved_config=stored.resolved_config,
                artifact_root=api_state.artifact_root,
                started_at=stored.started_at,
                input_rms=request.input_rms,
                duration_ms=request.duration_ms,
            )
        except Exception:
            stored.status = "failed"
            stored.failure_alias = "simulated-live-bridge-error"
            stored.ended_at = datetime.now(UTC)
            api_state.repository.save(stored)
            raise

        stored.conversation_id = f"simulated-{stored.run_id}"
        stored.recordings = bridge_result.recordings
        stored.metrics = bridge_result.metrics
        stored.sip_events = [
            SipEventResponse(**event.__dict__) for event in bridge_result.sip_events
        ]
        stored.rtp_stats = [RtpStatResponse(**stat.__dict__) for stat in bridge_result.rtp_stats]
        stored.verifications = verify_recordings(
            resolved_config=stored.resolved_config,
            recordings=stored.recordings,
            metrics=stored.metrics,
        )
        stored.status = "completed"
        stored.ended_at = datetime.now(UTC)
        api_state.repository.save(stored)
        return stored.to_response()

    @router.post("/v1/sip-events", response_model=SipEventResponse)
    async def ingest_sip_event(
        request: SipEventRequest,
        api_state: RunApiStateDependency,
    ) -> SipEventResponse:
        stored = api_state.repository.get(request.run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{request.run_id}'")
        event = SipEventResponse(
            call_id=request.call_id,
            method=request.method,
            direction=request.direction,
            ts=request.ts,
            status_code=request.status_code,
            summary_alias=request.summary_alias,
        )
        stored.sip_events.append(event)
        api_state.repository.save(stored)
        return event

    @router.post("/v1/rtp-stats", response_model=RtpStatResponse)
    async def ingest_rtp_stat(
        request: RtpStatRequest,
        api_state: RunApiStateDependency,
    ) -> RtpStatResponse:
        stored = api_state.repository.get(request.run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{request.run_id}'")
        stat = RtpStatResponse(
            ts=request.ts,
            jitter_ms=request.jitter_ms,
            loss_pct=request.loss_pct,
            mos=request.mos,
            direction=request.direction,
            rtt_ms=request.rtt_ms,
        )
        stored.rtp_stats.append(stat)
        api_state.repository.save(stored)
        return stat

    @router.post("/v1/observations", response_model=ObservationBatchResponse)
    async def ingest_observation_batch(
        request: ObservationBatchRequest,
        api_state: RunApiStateDependency,
    ) -> ObservationBatchResponse:
        stored = api_state.repository.get(request.run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{request.run_id}'")
        if stored.status != "running":
            raise HTTPException(
                status_code=409,
                detail=f"run '{request.run_id}' is not accepting observations",
            )

        configured_stages = _configured_stage_names(stored)
        referenced_stages = {
            metric.stage for metric in request.metrics if metric.stage is not None
        } | {chunk.stage for chunk in request.audio_chunks} | {
            event.stage for event in request.timeline_events if event.stage is not None
        }
        unknown_stages = sorted(referenced_stages - configured_stages)
        if unknown_stages:
            raise HTTPException(
                status_code=400,
                detail=f"unknown pipeline stages: {', '.join(unknown_stages)}",
            )

        decoded_audio = [(chunk, _decode_audio_chunk(chunk)) for chunk in request.audio_chunks]
        formats_by_stage: dict[str, set[tuple[int, int]]] = {}
        for chunk, _ in decoded_audio:
            formats_by_stage.setdefault(chunk.stage, set()).add(
                (chunk.sample_rate_hz, chunk.channels)
            )
        changed_stage = next(
            (stage for stage, formats in formats_by_stage.items() if len(formats) > 1),
            None,
        )
        if changed_stage is not None:
            raise HTTPException(
                status_code=409,
                detail=f"audio format changed within stage '{changed_stage}'",
            )
        for stage, formats in formats_by_stage.items():
            existing = api_state.audio_buffers.get((stored.run_id, stage))
            if existing is not None and (existing.sample_rate_hz, existing.channels) not in formats:
                raise HTTPException(
                    status_code=409,
                    detail=f"audio format changed within stage '{stage}'",
                )

        for chunk, pcm in decoded_audio:
            _write_observed_audio(
                api_state=api_state,
                stored=stored,
                chunk=chunk,
                pcm=pcm,
            )
        stored.metrics.extend(
            MetricArtifact(
                stage=metric.stage,
                name=metric.name,
                value=metric.value,
                ts=metric.ts,
            )
            for metric in request.metrics
        )
        stored.sip_events.extend(
            SipEventResponse(
                call_id=event.call_id,
                method=event.method,
                direction=event.direction,
                ts=event.ts,
                status_code=event.status_code,
                summary_alias=event.summary_alias,
            )
            for event in request.sip_events
        )
        stored.rtp_stats.extend(
            RtpStatResponse(
                ts=stat.ts,
                jitter_ms=stat.jitter_ms,
                loss_pct=stat.loss_pct,
                mos=stat.mos,
                direction=stat.direction,
                rtt_ms=stat.rtt_ms,
            )
            for stat in request.rtp_stats
        )
        existing_event_ids = {event.event_id for event in stored.timeline_events}
        stored.timeline_events.extend(
            TimelineEventArtifact(
                event_id=event.event_id,
                category=event.category,
                name=event.name,
                ts=event.ts,
                clock_domain=event.clock_domain,
                alignment_uncertainty_ms=event.alignment_uncertainty_ms,
                direction=event.direction,
                stage=event.stage,
                stream_alias=event.stream_alias,
                source=event.source,
                correlation_alias=event.correlation_alias,
                attributes=event.attributes,
            )
            for event in request.timeline_events
            if event.event_id not in existing_event_ids
        )
        api_state.repository.save(stored)
        return ObservationBatchResponse(
            run_id=stored.run_id,
            metric_count=len(request.metrics),
            audio_chunk_count=len(request.audio_chunks),
            sip_event_count=len(request.sip_events),
            rtp_stat_count=len(request.rtp_stats),
            timeline_event_count=len(request.timeline_events),
            recording_count=len(stored.recordings),
        )

    @router.post("/runs/{run_id}/complete", response_model=RunResponse)
    async def complete_observed_run(
        run_id: str,
        api_state: RunApiStateDependency,
    ) -> RunResponse:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        if stored.status == "completed":
            return stored.to_response()
        if stored.status != "running":
            raise HTTPException(status_code=409, detail=f"run '{run_id}' cannot be completed")

        stored.verifications = verify_recordings(
            resolved_config=stored.resolved_config,
            recordings=stored.recordings,
            metrics=stored.metrics,
        )
        stored.status = "completed"
        stored.ended_at = datetime.now(UTC)
        for key in [key for key in api_state.audio_buffers if key[0] == run_id]:
            del api_state.audio_buffers[key]
        api_state.repository.save(stored)
        return stored.to_response()

    @router.post("/runs/{run_id}/fail", response_model=RunResponse)
    async def fail_observed_run(
        run_id: str,
        request: RunFailureRequest,
        api_state: RunApiStateDependency,
    ) -> RunResponse:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        if stored.status == "failed":
            return stored.to_response()
        if stored.status != "running":
            raise HTTPException(status_code=409, detail=f"run '{run_id}' cannot be failed")

        stored.status = "failed"
        stored.failure_alias = request.failure_alias
        stored.ended_at = datetime.now(UTC)
        stored.metrics.append(
            MetricArtifact(
                stage=None,
                name="run_failed",
                value=1.0,
                ts=stored.ended_at,
            )
        )
        for key in [key for key in api_state.audio_buffers if key[0] == run_id]:
            del api_state.audio_buffers[key]
        api_state.repository.save(stored)
        return stored.to_response()

    @router.get("/runs/example-payload", response_model=RunCreateRequest)
    async def get_example_run_payload(
        environment_profile: EnvironmentProfile = "demo",
    ) -> RunCreateRequest:
        return _example_run_payload(environment_profile)

    @router.websocket("/live")
    async def live_websocket(
        websocket: WebSocket,
        interval_ms: Annotated[int, Query(ge=100, le=10_000)] = 1_000,
    ) -> None:
        await websocket.accept()
        api_state = get_run_api_state(websocket)
        try:
            while True:
                await websocket.send_json(
                    [
                        status.model_dump(mode="json")
                        for status in _live_preview(api_state)
                    ]
                )
                await asyncio.sleep(interval_ms / 1000.0)
        except WebSocketDisconnect:
            return

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(
        run_id: str,
        api_state: RunApiStateDependency,
    ) -> RunResponse:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        return stored.to_response()

    @router.get("/runs/{run_id}/verifications", response_model=list[VerificationResponse])
    async def get_run_verifications(
        run_id: str,
        api_state: RunApiStateDependency,
    ) -> list[VerificationResponse]:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        return [
            VerificationResponse(**verification.__dict__)
            for verification in stored.verifications
        ]

    @router.get("/runs/{run_id}/timeline", response_model=TimelineResponse)
    async def get_run_timeline(
        run_id: str,
        api_state: RunApiStateDependency,
    ) -> TimelineResponse:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        return stored.to_timeline()

    @router.get("/runs/{run_id}/recordings/{stage}/audio")
    async def get_run_recording_audio(
        run_id: str,
        stage: str,
        request: Request,
        api_state: RunApiStateDependency,
    ) -> Response:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")

        recording = next(
            (recording for recording in stored.recordings if recording.stage == stage),
            None,
        )
        if recording is None:
            raise HTTPException(
                status_code=404,
                detail=f"unknown recording stage '{stage}' for run '{run_id}'",
            )

        path = _local_recording_path(recording.uri)
        if path is not None and path.exists():
            return FileResponse(path, media_type="audio/wav")

        remote_reader = api_state.remote_recording_reader
        access_token = api_state.remote_audio_access_token
        if remote_reader is None or access_token is None:
            raise HTTPException(
                status_code=404,
                detail=f"recording audio is not available locally for stage '{stage}'",
            )
        _authorize_remote_audio(
            request,
            access_token,
            api_state.remote_audio_session_auth,
        )

        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(
                    remote_reader.read_wav,
                    uri=recording.uri,
                    run_id=run_id,
                    stage=stage,
                ),
                timeout=remote_reader.timeout_seconds,
            )
        except TimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="remote recording read timed out",
            ) from exc
        except RemoteRecordingIdentityError as exc:
            raise HTTPException(
                status_code=404,
                detail="remote recording identity is not available",
            ) from exc
        except RemoteRecordingTooLargeError as exc:
            raise HTTPException(
                status_code=413,
                detail="remote recording exceeds the configured byte limit",
            ) from exc
        except RemoteRecordingBusyError as exc:
            raise HTTPException(
                status_code=503,
                detail="remote recording reader is busy",
                headers={"Retry-After": "1"},
            ) from exc
        except RemoteRecordingTimeoutError as exc:
            raise HTTPException(
                status_code=504,
                detail="remote recording read timed out",
            ) from exc
        except RemoteRecordingInvalidContentError as exc:
            raise HTTPException(
                status_code=502,
                detail="remote recording is not a valid WAV object",
            ) from exc
        except RemoteRecordingUnavailableError as exc:
            raise HTTPException(
                status_code=502,
                detail="remote recording read failed",
            ) from exc

        return Response(
            content=payload,
            media_type="audio/wav",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/runs/{run_id}/metrics", response_model=list[MetricResponse])
    async def get_run_metrics(
        run_id: str,
        api_state: RunApiStateDependency,
    ) -> list[MetricResponse]:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        return [MetricResponse(**metric.__dict__) for metric in stored.metrics]

    return router


def _relative_seconds(ts: datetime, t0: datetime) -> float:
    return max(0.0, (ts - t0).total_seconds())


def _local_recording_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def _authorize_remote_audio(
    request: Request,
    expected_token: str,
    session_auth: RemoteAudioSessionAuth | None,
) -> None:
    authorization = request.headers.get("authorization", "")
    scheme, separator, supplied_token = authorization.partition(" ")
    authorized = (
        bool(separator)
        and scheme.lower() == "bearer"
        and bool(supplied_token)
        and supplied_token.isascii()
        and " " not in supplied_token
        and hmac.compare_digest(supplied_token, expected_token)
    )
    if not authorized and session_auth is not None:
        authorized = session_auth.remaining_seconds(
            request.cookies.get(REMOTE_AUDIO_SESSION_COOKIE)
        ) is not None
    if not authorized:
        raise HTTPException(
            status_code=401,
            detail="remote recording authorization required",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def _read_audio_session_login_token(request: Request) -> str:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="web audio login requires JSON")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 1_024:
            raise HTTPException(status_code=413, detail="web audio login request is too large")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="web audio login request is invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"login_token"}
        or not isinstance(payload["login_token"], str)
    ):
        raise HTTPException(status_code=400, detail="web audio login request is invalid")
    login_token = payload["login_token"]
    if (
        not 32 <= len(login_token) <= 256
        or not login_token.isascii()
        or any(character.isspace() for character in login_token)
    ):
        raise HTTPException(status_code=400, detail="web audio login request is invalid")
    return login_token
