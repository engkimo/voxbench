"""Engine harness value objects."""

from __future__ import annotations

from dataclasses import dataclass
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
class HarnessResult:
    run_id: str
    conversation_id: str
    recordings: list[RecordingArtifact]
    spans: list[SpanArtifact]

