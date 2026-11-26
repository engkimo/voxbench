"""Deterministic diagnostic sessions and replayable UI-command events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from voxbench.control_plane.ui_commands import (
    ResolvedUiCommand,
    UiCommandRequest,
    resolve_ui_command,
)

if TYPE_CHECKING:
    from voxbench.control_plane.run_api import TimelineResponse


DiagnosticEventType = Literal[
    "status",
    "evidence_found",
    "answer_delta",
    "ui_command_proposed",
    "ui_command",
    "ui_command_result",
    "completed",
]
UiCommandOutcome = Literal["applied", "rejected", "blocked"]


class DiagnosticSessionCreateRequest(BaseModel):
    """A bounded local request; this slice never sends the message externally."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        default="Show me the strongest evidence in this run.",
        min_length=1,
        max_length=500,
    )

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("diagnostic message must not be blank")
        return normalized


class DiagnosticSessionEvent(BaseModel):
    session_id: UUID
    ordinal: int = Field(ge=1)
    type: DiagnosticEventType
    created_at: datetime
    payload: dict[str, Any]


class UiCommandResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: UUID
    outcome: UiCommandOutcome
    detail: str = Field(min_length=1, max_length=500)

    @field_validator("detail")
    @classmethod
    def normalize_detail(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("UI command result detail must not be blank")
        return normalized


class UiCommandResultResponse(BaseModel):
    session_id: UUID
    command_id: UUID
    outcome: UiCommandOutcome
    detail: str
    acknowledged_at: datetime


class DiagnosticSessionResponse(BaseModel):
    session_id: UUID
    run_id: str
    status: Literal["completed"]
    adapter: Literal["deterministic-local"]
    message: str
    created_at: datetime
    latest_ordinal: int
    command_count: int
    acknowledged_command_count: int


@dataclass
class _DiagnosticSession:
    session_id: UUID
    run_id: str
    message: str
    created_at: datetime
    events: list[DiagnosticSessionEvent] = field(default_factory=list)
    commands: dict[UUID, ResolvedUiCommand] = field(default_factory=dict)
    results: dict[UUID, UiCommandResultResponse] = field(default_factory=dict)

    def append_event(
        self,
        event_type: DiagnosticEventType,
        payload: dict[str, Any],
    ) -> DiagnosticSessionEvent:
        event = DiagnosticSessionEvent(
            session_id=self.session_id,
            ordinal=len(self.events) + 1,
            type=event_type,
            created_at=datetime.now(UTC),
            payload=payload,
        )
        self.events.append(event)
        return event

    def to_response(self) -> DiagnosticSessionResponse:
        return DiagnosticSessionResponse(
            session_id=self.session_id,
            run_id=self.run_id,
            status="completed",
            adapter="deterministic-local",
            message=self.message,
            created_at=self.created_at,
            latest_ordinal=len(self.events),
            command_count=len(self.commands),
            acknowledged_command_count=len(self.results),
        )


class DiagnosticSessionNotFoundError(LookupError):
    """The requested diagnostic session does not exist."""


class DiagnosticCommandNotFoundError(LookupError):
    """The acknowledgement does not belong to the diagnostic session."""


class DiagnosticCommandResultConflictError(ValueError):
    """A command was already acknowledged with a different result."""


class DiagnosticSessionStore:
    """Thread-safe in-memory store for the deterministic adapter slice."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, _DiagnosticSession] = {}
        self._lock = RLock()

    def create(
        self,
        *,
        timeline: TimelineResponse,
        message: str,
    ) -> DiagnosticSessionResponse:
        session = _DiagnosticSession(
            session_id=uuid4(),
            run_id=timeline.run_id,
            message=message,
            created_at=datetime.now(UTC),
        )
        command_request, evidence_payload, answer = _plan_deterministic_command(timeline)
        command = resolve_ui_command(command_request, timeline)
        session.commands[command.command_id] = command
        session.append_event(
            "status",
            {
                "phase": "analyzing",
                "detail": (
                    "Inspecting typed timeline evidence with the local deterministic adapter."
                ),
            },
        )
        session.append_event("evidence_found", evidence_payload)
        session.append_event("answer_delta", {"text": answer})
        session.append_event(
            "ui_command_proposed",
            {
                "command_id": str(command_request.command_id),
                "type": command_request.type,
                "reason": "Guide the operator to the cited evidence.",
            },
        )
        session.append_event(
            "ui_command",
            {"command": command.model_dump(mode="json")},
        )
        session.append_event(
            "completed",
            {"detail": "The deterministic investigation completed."},
        )
        with self._lock:
            self._sessions[session.session_id] = session
        return session.to_response()

    def get(self, session_id: UUID) -> DiagnosticSessionResponse:
        with self._lock:
            return self._require_session(session_id).to_response()

    def events_after(
        self,
        session_id: UUID,
        ordinal: int,
    ) -> list[DiagnosticSessionEvent]:
        with self._lock:
            session = self._require_session(session_id)
            return [
                event.model_copy(deep=True) for event in session.events if event.ordinal > ordinal
            ]

    def acknowledge(
        self,
        session_id: UUID,
        result: UiCommandResultRequest,
    ) -> UiCommandResultResponse:
        with self._lock:
            session = self._require_session(session_id)
            if result.command_id not in session.commands:
                raise DiagnosticCommandNotFoundError
            existing = session.results.get(result.command_id)
            if existing is not None:
                if existing.outcome == result.outcome and existing.detail == result.detail:
                    return existing.model_copy(deep=True)
                raise DiagnosticCommandResultConflictError
            acknowledged = UiCommandResultResponse(
                session_id=session_id,
                command_id=result.command_id,
                outcome=result.outcome,
                detail=result.detail,
                acknowledged_at=datetime.now(UTC),
            )
            session.results[result.command_id] = acknowledged
            session.append_event(
                "ui_command_result",
                acknowledged.model_dump(mode="json"),
            )
            return acknowledged.model_copy(deep=True)

    def _require_session(self, session_id: UUID) -> _DiagnosticSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise DiagnosticSessionNotFoundError
        return session


def _plan_deterministic_command(
    timeline: TimelineResponse,
) -> tuple[UiCommandRequest, dict[str, Any], str]:
    command_id = uuid4()
    incidents = sorted(
        timeline.lanes.incidents,
        key=lambda item: (
            {"error": 0, "warning": 1, "info": 2}.get(item.severity, 3),
            item.start_ms,
            item.incident_id,
        ),
    )
    if incidents:
        incident = incidents[0]
        return (
            UiCommandRequest(
                command_id=command_id,
                type="select_incident",
                run_id=timeline.run_id,
                incident_id=incident.incident_id,
            ),
            {
                "classification": "observed",
                "incident_id": incident.incident_id,
                "evidence_refs": incident.evidence_refs,
                "summary": incident.summary,
            },
            (
                f"The strongest detected incident is {incident.title}. "
                "I will select its correlated evidence."
            ),
        )
    if timeline.lanes.events:
        event = min(
            timeline.lanes.events,
            key=lambda item: (item.t_rel_ms, item.event_id),
        )
        return (
            UiCommandRequest(
                command_id=command_id,
                type="focus_evidence",
                run_id=timeline.run_id,
                evidence_ref=event.event_id,
            ),
            {
                "classification": "observed",
                "evidence_refs": [event.event_id],
                "summary": f"First typed event: {event.name}.",
            },
            f"No incident rule fired. I will show the first observed event, {event.name}.",
        )
    panel = "recordings" if timeline.lanes.recordings else "stages"
    return (
        UiCommandRequest(
            command_id=command_id,
            type="open_panel",
            run_id=timeline.run_id,
            panel=panel,
        ),
        {
            "classification": "unknown",
            "evidence_refs": [],
            "summary": "No typed incident or event evidence is available.",
        },
        "No typed incident or event evidence is available. I will open the nearest observed panel.",
    )
