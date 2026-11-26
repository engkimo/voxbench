"""Read-only inspector commands resolved against a selected run's evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from voxbench.control_plane.run_api import TimelineResponse

UiCommandType = Literal["select_incident", "focus_evidence", "set_time_window", "open_panel"]
UiPanel = Literal["transport", "recordings", "stages"]
UiTimelineCategory = Literal[
    "conversation",
    "signaling",
    "transport",
    "buffer",
    "pipeline",
    "provider",
    "runtime",
    "session",
]


class UiCommandRequest(BaseModel):
    """Untrusted command proposal. The server derives all target times."""

    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    type: UiCommandType
    run_id: str = Field(min_length=1, max_length=128)
    incident_id: str | None = Field(default=None, min_length=1, max_length=256)
    evidence_ref: str | None = Field(default=None, min_length=1, max_length=256)
    panel: UiPanel | None = None
    padding_ms: float = Field(default=1_000.0, ge=0, le=30_000)
    highlight_ms: int = Field(default=3_000, ge=0, le=10_000)

    @field_validator("run_id", "incident_id", "evidence_ref")
    @classmethod
    def validate_safe_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped or any(
            marker in stripped.lower() for marker in ("http://", "https://", "javascript:")
        ):
            raise ValueError("UI command references must be safe aliases")
        return stripped

    @model_validator(mode="after")
    def validate_command_shape(self) -> UiCommandRequest:
        required = {
            "select_incident": "incident_id",
            "focus_evidence": "evidence_ref",
            "set_time_window": "evidence_ref",
            "open_panel": "panel",
        }[self.type]
        if getattr(self, required) is None:
            raise ValueError(f"{self.type} requires {required}")
        allowed = {"command_id", "run_id", "type", required, "highlight_ms"}
        if self.type == "set_time_window":
            allowed.add("padding_ms")
        if self.model_fields_set - allowed:
            raise ValueError(f"unexpected field for {self.type}")
        return self


class UiCommandTarget(BaseModel):
    """Canonical inspector target computed from typed timeline evidence."""

    run_id: str
    incident_id: str | None = None
    evidence_ref: str | None = None
    category: UiTimelineCategory | None = None
    panel: UiPanel | None = None
    stage: str | None = None
    direction: str | None = None
    artifact_id: str | None = None
    start_ms: float | None = None
    end_ms: float | None = None
    highlight_ms: int = 0


class ResolvedUiCommand(BaseModel):
    command_id: UUID
    type: UiCommandType
    status: Literal["resolved"] = "resolved"
    target: UiCommandTarget


class UiCommandResolutionError(ValueError):
    """A command referenced evidence outside the selected run."""


def resolve_ui_command(
    command: UiCommandRequest,
    timeline: TimelineResponse,
) -> ResolvedUiCommand:
    if command.run_id != timeline.run_id:
        raise UiCommandResolutionError("UI command run_id must match the timeline run_id")

    if command.type == "open_panel":
        target = UiCommandTarget(
            run_id=timeline.run_id,
            panel=command.panel,
            category="transport" if command.panel == "transport" else None,
        )
    else:
        target = _resolve_evidence(timeline, command.incident_id or command.evidence_ref or "")
        if command.type == "select_incident" and target.incident_id is None:
            raise UiCommandResolutionError("incident is outside the command run")
        if command.type == "set_time_window":
            target.start_ms = max(0.0, target.start_ms - command.padding_ms)
            target.end_ms = min(_timeline_duration_ms(timeline), target.end_ms + command.padding_ms)

    target.highlight_ms = command.highlight_ms
    return ResolvedUiCommand(command_id=command.command_id, type=command.type, target=target)


def _resolve_evidence(timeline: TimelineResponse, evidence_ref: str) -> UiCommandTarget:
    run_id = timeline.run_id
    for item in timeline.lanes.incidents:
        if item.incident_id == evidence_ref:
            return UiCommandTarget(
                run_id=run_id,
                incident_id=item.incident_id,
                evidence_ref=evidence_ref,
                category=item.category,
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                stage=item.stage,
                direction=item.direction,
            )
    for event in timeline.lanes.events:
        if event.event_id == evidence_ref:
            return UiCommandTarget(
                run_id=run_id,
                evidence_ref=evidence_ref,
                category=event.category,
                start_ms=event.t_rel_ms,
                end_ms=event.t_rel_ms,
                stage=event.stage,
                direction=event.direction,
            )
    for interval in timeline.lanes.intervals:
        if interval.interval_id == evidence_ref:
            return UiCommandTarget(
                run_id=run_id,
                evidence_ref=evidence_ref,
                category=interval.category,
                start_ms=interval.start_ms,
                end_ms=interval.end_ms,
                stage=interval.stage,
                direction=interval.direction,
            )
    for series in timeline.lanes.series:
        if series.series_id == evidence_ref and series.points:
            return UiCommandTarget(
                run_id=run_id,
                evidence_ref=evidence_ref,
                category=series.category,
                start_ms=series.points[0].t_rel_ms,
                end_ms=series.points[-1].t_rel_ms,
                stage=series.stage,
                direction=series.direction,
            )
    for artifact in timeline.lanes.artifacts:
        if evidence_ref in {artifact.artifact_id, artifact.artifact_ref}:
            return UiCommandTarget(
                run_id=run_id,
                evidence_ref=evidence_ref,
                category=artifact.category,
                start_ms=artifact.start_ms,
                end_ms=artifact.start_ms + (artifact.duration_ms or 0),
                stage=artifact.stage,
                direction=artifact.direction,
                artifact_id=artifact.artifact_id,
            )
    raise UiCommandResolutionError("evidence reference is outside the command run")


def _timeline_duration_ms(timeline: TimelineResponse) -> float:
    ends = [0.0]
    ends.extend(item.t_rel_ms for item in timeline.lanes.events)
    ends.extend(item.end_ms for item in timeline.lanes.intervals)
    ends.extend(item.end_ms for item in timeline.lanes.incidents)
    ends.extend(item.points[-1].t_rel_ms for item in timeline.lanes.series if item.points)
    ends.extend(item.start_ms + (item.duration_ms or 0.0) for item in timeline.lanes.artifacts)
    ends.extend(item.duration_ms for item in timeline.lanes.recordings)
    return max(ends)
