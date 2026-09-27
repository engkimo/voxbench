"""Closed, metadata-only lifecycle observations for voice AI services."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ServiceEventKind = Literal[
    "service.session_started",
    "service.session_ended",
    "stt.partial_emitted",
    "stt.final_emitted",
    "turn.speech_ended",
    "turn.committed",
    "llm.request_started",
    "llm.first_output",
    "llm.first_answer_text",
    "llm.response_completed",
    "aggregation.segment_ready",
    "tts.request_started",
    "tts.first_pcm",
    "tts.response_completed",
    "realtime.request_started",
    "realtime.first_audio",
    "realtime.response_completed",
    "playback.write_started",
    "playback.write_completed",
    "playback.discarded",
    "cancel.requested",
    "cancel.acknowledged",
    "service.failed",
]
ServiceEventRole = Literal[
    "stt", "llm", "tts", "realtime", "aggregation", "coordinator", "playback"
]
RelationName = Literal[
    "session", "turn", "request", "parent_request", "response", "segment"
]
ServiceAttributeName = Literal[
    "attempt_ordinal",
    "sequence_ordinal",
    "duration_ms",
    "audio_duration_ms",
    "input_units",
    "output_units",
    "cached_input_units",
    "tool_call_count",
    "queue_depth",
    "sample_rate_hz",
    "channels",
    "encoding",
    "retry",
    "truncated",
    "terminal_outcome",
    "failure_alias",
    "cancel_scope",
    "observed_boundary",
    "output_kind",
]
Scalar = str | int | float | bool | None

_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_EXPECTED_RELATIONS: dict[str, tuple[RelationName, ...]] = {
    "service.session_started": ("session",),
    "service.session_ended": ("session",),
    "stt.partial_emitted": ("session",),
    "stt.final_emitted": ("session", "request"),
    "turn.speech_ended": ("turn", "request"),
    "turn.committed": ("turn", "parent_request"),
    "llm.request_started": ("turn", "request", "parent_request", "response"),
    "llm.first_output": ("turn", "request", "response"),
    "llm.first_answer_text": ("turn", "request", "response"),
    "llm.response_completed": ("turn", "request", "response"),
    "aggregation.segment_ready": ("request", "response", "segment", "parent_request"),
    "tts.request_started": ("request", "parent_request", "response", "segment"),
    "tts.first_pcm": ("request", "response", "segment"),
    "tts.response_completed": ("request", "response", "segment"),
    "realtime.request_started": ("turn", "request", "response"),
    "realtime.first_audio": ("request", "response"),
    "realtime.response_completed": ("request", "response"),
    "playback.write_started": ("parent_request", "response", "segment"),
    "playback.write_completed": ("parent_request", "response", "segment"),
    "playback.discarded": ("parent_request", "response", "segment"),
    "cancel.requested": ("request",),
    "cancel.acknowledged": ("request",),
    "service.failed": ("request",),
}
_KIND_ROLES: dict[str, frozenset[str]] = {
    **{kind: frozenset({"stt"}) for kind in _EXPECTED_RELATIONS if kind.startswith("stt.")},
    **{kind: frozenset({"llm"}) for kind in _EXPECTED_RELATIONS if kind.startswith("llm.")},
    **{kind: frozenset({"tts"}) for kind in _EXPECTED_RELATIONS if kind.startswith("tts.")},
    **{
        kind: frozenset({"realtime"})
        for kind in _EXPECTED_RELATIONS
        if kind.startswith("realtime.")
    },
    "turn.committed": frozenset({"coordinator"}),
    "turn.speech_ended": frozenset({"coordinator"}),
    "aggregation.segment_ready": frozenset({"aggregation"}),
    "playback.write_started": frozenset({"playback"}),
    "playback.write_completed": frozenset({"playback"}),
    "playback.discarded": frozenset({"playback"}),
    "service.session_started": frozenset({"stt", "llm", "tts", "realtime"}),
    "service.session_ended": frozenset({"stt", "llm", "tts", "realtime"}),
    "cancel.requested": frozenset({"stt", "llm", "tts", "realtime", "playback"}),
    "cancel.acknowledged": frozenset({"stt", "llm", "tts", "realtime", "playback"}),
    "service.failed": frozenset(
        {"stt", "llm", "tts", "realtime", "aggregation", "playback"}
    ),
}


class ServiceEvent(BaseModel):
    """One observed boundary with local aliases and no conversation content."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    schema_version: Literal["voxbench/service-event/v1"] = "voxbench/service-event/v1"
    collector_alias: Annotated[str, Field(min_length=1, max_length=48)]
    event_alias: Annotated[str, Field(min_length=1, max_length=48)]
    kind: ServiceEventKind
    component_id: Annotated[str, Field(min_length=1, max_length=64)]
    role: ServiceEventRole
    generation_epoch: int = Field(default=0, ge=0)
    session_alias: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    turn_alias: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    request_alias: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    parent_request_alias: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    response_alias: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    segment_alias: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    unobserved_relations: list[RelationName] = Field(default_factory=list, max_length=6)
    attributes: dict[ServiceAttributeName, Scalar] = Field(default_factory=dict)
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    clock_domain: Annotated[str, Field(min_length=1, max_length=64)] = "application_wall"
    alignment_uncertainty_ms: float | None = Field(default=None, ge=0)

    @field_validator(
        "collector_alias", "event_alias", "component_id", "session_alias", "turn_alias",
        "request_alias", "parent_request_alias", "response_alias", "segment_alias",
        "clock_domain",
    )
    @classmethod
    def validate_alias(cls, value: str | None, info) -> str | None:
        if value is not None and not _ALIAS.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a local safe alias")
        return value

    @field_validator("unobserved_relations")
    @classmethod
    def validate_missing_relations(cls, value: list[RelationName]) -> list[RelationName]:
        if len(value) != len(set(value)):
            raise ValueError("unobserved_relations must be unique")
        return value

    @field_validator("attributes")
    @classmethod
    def validate_attributes(
        cls, value: dict[ServiceAttributeName, Scalar],
    ) -> dict[ServiceAttributeName, Scalar]:
        result: dict[ServiceAttributeName, Scalar] = {}
        integer_keys = {
            "attempt_ordinal", "sequence_ordinal", "input_units", "output_units",
            "cached_input_units", "tool_call_count", "queue_depth", "sample_rate_hz",
            "channels",
        }
        number_keys = {"duration_ms", "audio_duration_ms"}
        boolean_keys = {"retry", "truncated"}
        for key, item in value.items():
            if item is None:
                raise ValueError(f"attribute '{key}' must not be null")
            if key in integer_keys and (
                isinstance(item, bool) or not isinstance(item, int) or item < 0
            ):
                raise ValueError(f"attribute '{key}' must be a non-negative integer")
            if key in number_keys and (
                isinstance(item, bool)
                or not isinstance(item, int | float)
                or not math.isfinite(item)
                or item < 0
            ):
                raise ValueError(f"attribute '{key}' must be finite and non-negative")
            if key in boolean_keys and not isinstance(item, bool):
                raise ValueError(f"attribute '{key}' must be boolean")
            if key not in integer_keys | number_keys | boolean_keys and not isinstance(item, str):
                raise ValueError(f"attribute '{key}' must be a safe alias string")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError(f"attribute '{key}' must be finite")
            if isinstance(item, str) and (
                not item or len(item) > 128 or not _ALIAS.fullmatch(item)
            ):
                raise ValueError(f"attribute '{key}' must be a local safe alias")
            if key == "output_kind" and item not in {
                "answer", "reasoning", "tool_call", "other"
            }:
                raise ValueError(
                    "attribute 'output_kind' must be answer, reasoning, tool_call, or other"
                )
            result[key] = item
        return result

    @field_validator("ts")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ts must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_relationships_and_budget(self) -> ServiceEvent:
        if self.role not in _KIND_ROLES[self.kind]:
            raise ValueError(f"{self.kind} is not compatible with role '{self.role}'")
        relations = {
            "session": self.session_alias,
            "turn": self.turn_alias,
            "request": self.request_alias,
            "parent_request": self.parent_request_alias,
            "response": self.response_alias,
            "segment": self.segment_alias,
        }
        missing = set(self.unobserved_relations)
        for relation in _EXPECTED_RELATIONS[self.kind]:
            if relations[relation] is None and relation not in missing:
                raise ValueError(
                    f"{self.kind} requires {relation}_alias or an explicit "
                    f"unobserved_relations entry"
                )
            if relations[relation] is not None and relation in missing:
                raise ValueError(f"{relation} cannot be both linked and unobserved")
        normalized_scalar_count = (
            2
            + sum(item is not None for item in relations.values())
            + bool(self.unobserved_relations)
            + len(self.attributes)
        )
        if normalized_scalar_count > 15:
            raise ValueError("service event reserves one of at most 16 attributes for coverage")
        return self

    @property
    def normalized_event_id(self) -> str:
        return f"service:{self.collector_alias}:{self.event_alias}"

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


SERVICE_EVENT_KINDS = frozenset(_EXPECTED_RELATIONS)
