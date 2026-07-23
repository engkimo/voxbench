"""Engine harness value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class StagePlan:
    stage: str
    plugin: str
    format: dict[str, Any]


@dataclass(frozen=True)
class RecordingArtifact:
    stage: str
    uri: str
    format: dict[str, Any]
    duration_ms: float


@dataclass(frozen=True)
class SpanArtifact:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    start_ns: int
    end_ns: int
    attrs: dict[str, Any]


@dataclass(frozen=True)
class MetricArtifact:
    stage: str | None
    name: str
    value: float
    ts: datetime


@dataclass(frozen=True)
class TimelineEventArtifact:
    event_id: str
    category: str
    name: str
    ts: datetime
    clock_domain: str = "control_plane_wall"
    alignment_uncertainty_ms: float | None = None
    direction: str | None = None
    stage: str | None = None
    stream_alias: str | None = None
    source: str = "observer"
    correlation_alias: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessResult:
    run_id: str
    conversation_id: str
    recordings: list[RecordingArtifact]
    spans: list[SpanArtifact]
    metrics: list[MetricArtifact]
