"""Run API models and router."""

import asyncio
import base64
import binascii
import hmac
import json
import wave
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
from voxbench.engine_harness.models import MetricArtifact, RecordingArtifact, SpanArtifact
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

    @model_validator(mode="after")
    def require_observation(self) -> "ObservationBatchRequest":
        if not (self.metrics or self.audio_chunks or self.sip_events or self.rtp_stats):
            raise ValueError("at least one observation is required")
        return self


class ObservationBatchResponse(BaseModel):
    run_id: str
    metric_count: int
    audio_chunk_count: int
    sip_event_count: int
    rtp_stat_count: int
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


class TimelineLanes(BaseModel):
    sip_ladder: list[TimelineSipEvent]
    rtp_quality: list[TimelineRtpStat]
    stages: list[TimelineStageLane]
    turns: list[dict[str, Any]]
    host: list[TimelineMetricPoint]
    recordings: list[TimelineRecording]


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


def _execute_stored_run(stored: StoredRun, api_state: RunApiState) -> None:
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
        return RepositoryReadinessResponse(**api_state.repository_readiness.__dict__)

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
        } | {chunk.stage for chunk in request.audio_chunks}
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
        api_state.repository.save(stored)
        return ObservationBatchResponse(
            run_id=stored.run_id,
            metric_count=len(request.metrics),
            audio_chunk_count=len(request.audio_chunks),
            sip_event_count=len(request.sip_events),
            rtp_stat_count=len(request.rtp_stats),
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
