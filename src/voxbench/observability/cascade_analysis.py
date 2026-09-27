"""Deterministic causal timing derived from metadata-only service events."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from voxbench.engine_harness.models import TimelineEventArtifact

AnalysisStatus = Literal["observed", "unobserved", "indeterminate"]
CriticalPathStatus = Literal["complete", "incomplete", "indeterminate"]
MeasurementName = Literal[
    "stt_finalization_wait",
    "turn_coordination_wait",
    "llm_dispatch_wait",
    "llm_first_output_wait",
    "llm_first_answer_text_wait",
    "text_aggregation_wait",
    "tts_dispatch_queue_wait",
    "tts_first_audio_wait",
    "output_playback_start_wait",
    "end_to_end_local_response_wait",
]

ANALYSIS_SCHEMA_VERSION = "voxbench/cascade-analysis/v1"
MEASUREMENT_DEFINITION_VERSION = "cascade-latency/v1"


class CausalScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_alias: str
    generation_epoch: int
    operation_request_alias: str | None = None
    response_alias: str | None = None
    segment_alias: str | None = None
    request_alias: str | None = None


class ServiceLatencyMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    measurement_id: str
    name: MeasurementName
    status: AnalysisStatus
    definition_version: Literal["cascade-latency/v1"] = MEASUREMENT_DEFINITION_VERSION
    definition: str
    scope: CausalScope
    duration_ms: float | None = None
    start_event_ref: str | None = None
    end_event_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    component_ids: list[str] = Field(default_factory=list)
    clock_domain: str | None = None
    alignment_uncertainty_ms: float | None = None
    reason_alias: str | None = None


class CascadeCriticalPath(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: CriticalPathStatus
    measurement_ids: list[str]
    end_to_end_measurement_id: str
    covered_duration_ms: float
    unexplained_duration_ms: float | None
    reason_alias: str | None = None


class CascadeCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    missing_boundaries: list[str]
    explicit_unobserved_relations: list[str]
    unresolved_relations: list[str]
    remote_playout_observed: Literal[False] = False


class CascadeTurnAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["voxbench/cascade-analysis/v1"] = ANALYSIS_SCHEMA_VERSION
    turn_alias: str
    response_alias: str | None
    operation_request_alias: str | None
    generation_epoch: int
    completion_observed: bool
    measurements: list[ServiceLatencyMeasurement]
    critical_path: CascadeCriticalPath
    coverage: CascadeCoverage


_DEFINITIONS: dict[MeasurementName, str] = {
    "stt_finalization_wait": (
        "independent caller speech end to the correlated STT final boundary"
    ),
    "turn_coordination_wait": (
        "correlated STT final boundary to committed conversational turn"
    ),
    "llm_dispatch_wait": "committed turn to the correlated LLM request start",
    "llm_first_output_wait": (
        "LLM request start to first observed output of any declared output kind"
    ),
    "llm_first_answer_text_wait": (
        "LLM request start to first answer text eligible for speech"
    ),
    "text_aggregation_wait": (
        "first answer text to a correlated segment becoming ready for TTS"
    ),
    "tts_dispatch_queue_wait": (
        "correlated segment-ready boundary to the actual TTS request start"
    ),
    "tts_first_audio_wait": "TTS request start to first correlated PCM output",
    "output_playback_start_wait": (
        "first correlated TTS PCM output to first matching local frame write"
    ),
    "end_to_end_local_response_wait": (
        "independent caller speech end to first causally matching local frame write"
    ),
}


def analyze_cascade_service_events(
    events: Iterable[TimelineEventArtifact],
) -> list[CascadeTurnAnalysis]:
    """Group explicit aliases and derive timings without nearest-time matching."""

    service_events = sorted(
        (event for event in events if event.source == "service_observation"),
        key=lambda event: (event.ts, event.event_id),
    )
    llm_starts = [event for event in service_events if event.name == "llm.request_started"]
    analyses = [
        _analyze_llm_operation(service_events, llm_start)
        for llm_start in llm_starts
        if _string_attr(llm_start, "turn_alias") is not None
    ]
    return sorted(
        analyses,
        key=lambda item: (
            item.turn_alias,
            item.generation_epoch,
            item.response_alias or "",
            item.operation_request_alias or "",
        ),
    )


def _analyze_llm_operation(
    events: list[TimelineEventArtifact],
    llm_start: TimelineEventArtifact,
) -> CascadeTurnAnalysis:
    turn = _required_attr(llm_start, "turn_alias")
    llm_request = _required_attr(llm_start, "request_alias")
    response = _string_attr(llm_start, "response_alias")
    epoch = _epoch(llm_start)
    request_lineage = _request_lineage(events, llm_start, epoch=epoch)
    stt_request = next(
        (
            request
            for request in reversed(request_lineage)
            if _first(
                events,
                "stt.final_emitted",
                request_alias=request,
                generation_epoch=epoch,
            )
            is not None
        ),
        _string_attr(llm_start, "parent_request_alias"),
    )
    base_scope = CausalScope(
        turn_alias=turn,
        generation_epoch=epoch,
        operation_request_alias=llm_request,
        response_alias=response,
    )

    turn_commit = _first(
        events,
        "turn.committed",
        turn_alias=turn,
        parent_request_alias=stt_request,
        generation_epoch=epoch,
    )
    stt_final = _first(
        events,
        "stt.final_emitted",
        request_alias=stt_request,
        generation_epoch=epoch,
    )
    speech_end = _first(
        events,
        "turn.speech_ended",
        turn_alias=turn,
        request_alias=stt_request,
        generation_epoch=epoch,
    )
    first_output = _first(
        events,
        "llm.first_output",
        request_alias=llm_request,
        response_alias=response,
        generation_epoch=epoch,
    )
    first_answer = _first(
        events,
        "llm.first_answer_text",
        request_alias=llm_request,
        response_alias=response,
        generation_epoch=epoch,
    )
    llm_complete = _first(
        events,
        "llm.response_completed",
        request_alias=llm_request,
        response_alias=response,
        generation_epoch=epoch,
    )

    measurements = [
        _measurement("stt_finalization_wait", speech_end, stt_final, base_scope, "stt"),
        _measurement("turn_coordination_wait", stt_final, turn_commit, base_scope, "turn"),
        _measurement("llm_dispatch_wait", turn_commit, llm_start, base_scope, "llm"),
        _measurement("llm_first_output_wait", llm_start, first_output, base_scope, "llm"),
        _measurement(
            "llm_first_answer_text_wait", llm_start, first_answer, base_scope, "llm"
        ),
    ]

    aggregations = _matching(
        events,
        "aggregation.segment_ready",
        parent_request_alias=llm_request,
        response_alias=response,
        generation_epoch=epoch,
    )
    path_candidates: list[tuple[TimelineEventArtifact, list[str]]] = []
    tts_starts: list[TimelineEventArtifact] = []
    playback_starts: list[TimelineEventArtifact] = []
    if not aggregations:
        measurements.append(
            _measurement(
                "text_aggregation_wait", first_answer, None, base_scope, "aggregation"
            )
        )

    for aggregation in aggregations:
        segment = _string_attr(aggregation, "segment_alias")
        aggregation_request = _string_attr(aggregation, "request_alias")
        segment_scope = base_scope.model_copy(
            update={"segment_alias": segment, "request_alias": aggregation_request}
        )
        aggregation_measurement = _measurement(
            "text_aggregation_wait",
            first_answer,
            aggregation,
            segment_scope,
            "aggregation",
        )
        measurements.append(aggregation_measurement)
        segment_tts_starts = _matching(
            events,
            "tts.request_started",
            parent_request_alias=aggregation_request,
            response_alias=response,
            segment_alias=segment,
            generation_epoch=epoch,
        )
        if not segment_tts_starts:
            measurements.append(
                _measurement(
                    "tts_dispatch_queue_wait",
                    aggregation,
                    None,
                    segment_scope,
                    "tts",
                )
            )
        for tts_start in segment_tts_starts:
            tts_starts.append(tts_start)
            tts_request = _string_attr(tts_start, "request_alias")
            tts_scope = segment_scope.model_copy(update={"request_alias": tts_request})
            dispatch = _measurement(
                "tts_dispatch_queue_wait", aggregation, tts_start, tts_scope, "tts"
            )
            first_pcm = _first(
                events,
                "tts.first_pcm",
                request_alias=tts_request,
                response_alias=response,
                segment_alias=segment,
                generation_epoch=epoch,
            )
            first_audio = _measurement(
                "tts_first_audio_wait", tts_start, first_pcm, tts_scope, "tts"
            )
            playback = _first(
                events,
                "playback.write_started",
                parent_request_alias=tts_request,
                response_alias=response,
                segment_alias=segment,
                generation_epoch=epoch,
            )
            if playback is not None:
                playback_starts.append(playback)
            playback_wait = _measurement(
                "output_playback_start_wait", first_pcm, playback, tts_scope, "playback"
            )
            measurements.extend((dispatch, first_audio, playback_wait))
            if playback is not None:
                path_candidates.append(
                    (
                        playback,
                        [
                            measurements[0].measurement_id,
                            measurements[1].measurement_id,
                            measurements[2].measurement_id,
                            measurements[4].measurement_id,
                            aggregation_measurement.measurement_id,
                            dispatch.measurement_id,
                            first_audio.measurement_id,
                            playback_wait.measurement_id,
                        ],
                    )
                )

    selected_path = (
        min(path_candidates, key=lambda item: (item[0].ts, item[0].event_id))
        if path_candidates
        else None
    )
    selected_playback = selected_path[0] if selected_path is not None else None
    end_to_end = _measurement(
        "end_to_end_local_response_wait",
        speech_end,
        selected_playback,
        base_scope,
        "response",
        reason_alias="causal_path_incomplete" if selected_playback is None else None,
    )
    measurements.append(end_to_end)

    path_ids = selected_path[1] if selected_path is not None else [
        measurements[0].measurement_id,
        measurements[1].measurement_id,
        measurements[2].measurement_id,
        measurements[4].measurement_id,
    ]
    path_measurements = [
        measurement for measurement in measurements if measurement.measurement_id in path_ids
    ]
    critical_path = _critical_path(path_measurements, path_ids, end_to_end)

    relevant = _relevant_events(
        events,
        turn=turn,
        response=response,
        request_aliases={
            alias
            for alias in (
                stt_request,
                llm_request,
                *request_lineage,
                *(_string_attr(item, "request_alias") for item in aggregations),
                *(_string_attr(item, "request_alias") for item in tts_starts),
            )
            if alias is not None
        },
        epoch=epoch,
    )
    missing_boundaries = sorted(
        {
            measurement.name
            for measurement in measurements
            if measurement.status != "observed"
        }
    )
    explicit_unobserved = sorted(
        f"{event.event_id}:{relation}"
        for event in relevant
        for relation in _csv_attr(event, "unobserved_relations")
    )
    unresolved = sorted(
        f"{event.event_id}:{relation}"
        for event in relevant
        for relation in _csv_attr(event, "unresolved_relations")
    )
    tts_terminal_requests = {
        _string_attr(event, "request_alias")
        for event in relevant
        if event.name == "tts.response_completed"
    }
    playback_terminal_requests = {
        _string_attr(event, "parent_request_alias")
        for event in relevant
        if event.name in {"playback.write_completed", "playback.discarded"}
    }
    started_tts_requests = {_string_attr(event, "request_alias") for event in tts_starts}
    started_playback_requests = {
        _string_attr(event, "parent_request_alias") for event in playback_starts
    }
    completion_observed = bool(aggregations) and llm_complete is not None and (
        started_tts_requests <= tts_terminal_requests
        and started_playback_requests <= playback_terminal_requests
    )
    return CascadeTurnAnalysis(
        turn_alias=turn,
        response_alias=response,
        operation_request_alias=llm_request,
        generation_epoch=epoch,
        completion_observed=completion_observed,
        measurements=measurements,
        critical_path=critical_path,
        coverage=CascadeCoverage(
            missing_boundaries=missing_boundaries,
            explicit_unobserved_relations=explicit_unobserved,
            unresolved_relations=unresolved,
        ),
    )


def _measurement(
    name: MeasurementName,
    start: TimelineEventArtifact | None,
    end: TimelineEventArtifact | None,
    scope: CausalScope,
    identity_suffix: str,
    *,
    reason_alias: str | None = None,
) -> ServiceLatencyMeasurement:
    request = scope.request_alias or scope.operation_request_alias or "unknown"
    segment = scope.segment_alias or "none"
    measurement_id = (
        f"cascade:{scope.turn_alias}:{scope.generation_epoch}:{request}:{segment}:"
        f"{identity_suffix}:{name}"
    )
    evidence = [event.event_id for event in (start, end) if event is not None]
    components = sorted({event.stage for event in (start, end) if event and event.stage})
    if start is None or end is None:
        return ServiceLatencyMeasurement(
            measurement_id=measurement_id,
            name=name,
            status="unobserved",
            definition=_DEFINITIONS[name],
            scope=scope,
            start_event_ref=start.event_id if start else None,
            end_event_ref=end.event_id if end else None,
            evidence_refs=evidence,
            component_ids=components,
            reason_alias=reason_alias or "boundary_unobserved",
        )
    if start.clock_domain != end.clock_domain:
        return ServiceLatencyMeasurement(
            measurement_id=measurement_id,
            name=name,
            status="indeterminate",
            definition=_DEFINITIONS[name],
            scope=scope,
            start_event_ref=start.event_id,
            end_event_ref=end.event_id,
            evidence_refs=evidence,
            component_ids=components,
            reason_alias="clock_domain_mismatch",
        )
    duration_ms = (end.ts - start.ts).total_seconds() * 1000
    if duration_ms < 0:
        return ServiceLatencyMeasurement(
            measurement_id=measurement_id,
            name=name,
            status="indeterminate",
            definition=_DEFINITIONS[name],
            scope=scope,
            start_event_ref=start.event_id,
            end_event_ref=end.event_id,
            evidence_refs=evidence,
            component_ids=components,
            clock_domain=start.clock_domain,
            reason_alias="end_precedes_start",
        )
    uncertainty = (
        start.alignment_uncertainty_ms + end.alignment_uncertainty_ms
        if start.alignment_uncertainty_ms is not None
        and end.alignment_uncertainty_ms is not None
        else None
    )
    return ServiceLatencyMeasurement(
        measurement_id=measurement_id,
        name=name,
        status="observed",
        definition=_DEFINITIONS[name],
        scope=scope,
        duration_ms=duration_ms,
        start_event_ref=start.event_id,
        end_event_ref=end.event_id,
        evidence_refs=evidence,
        component_ids=components,
        clock_domain=start.clock_domain,
        alignment_uncertainty_ms=uncertainty,
    )


def _critical_path(
    path_measurements: list[ServiceLatencyMeasurement],
    path_ids: list[str],
    end_to_end: ServiceLatencyMeasurement,
) -> CascadeCriticalPath:
    ordered = {measurement.measurement_id: measurement for measurement in path_measurements}
    values = [ordered[item] for item in path_ids if item in ordered]
    if (
        any(item.status == "indeterminate" for item in values)
        or end_to_end.status == "indeterminate"
    ):
        status: CriticalPathStatus = "indeterminate"
        reason = "clock_or_order_indeterminate"
    elif (
        len(values) != len(path_ids)
        or any(item.status != "observed" for item in values)
        or end_to_end.status != "observed"
    ):
        status = "incomplete"
        reason = "causal_boundary_unobserved"
    else:
        status = "complete"
        reason = None
    covered = sum(item.duration_ms or 0.0 for item in values if item.status == "observed")
    unexplained = (
        max(0.0, (end_to_end.duration_ms or 0.0) - covered)
        if end_to_end.status == "observed"
        else None
    )
    return CascadeCriticalPath(
        status=status,
        measurement_ids=path_ids,
        end_to_end_measurement_id=end_to_end.measurement_id,
        covered_duration_ms=covered,
        unexplained_duration_ms=unexplained,
        reason_alias=reason,
    )


def _matching(
    events: list[TimelineEventArtifact],
    name: str,
    **attributes: str | int | None,
) -> list[TimelineEventArtifact]:
    return [
        event
        for event in events
        if event.name == name
        and all(
            (_epoch(event) if key == "generation_epoch" else _string_attr(event, key))
            == value
            for key, value in attributes.items()
        )
    ]


def _first(
    events: list[TimelineEventArtifact],
    name: str,
    **attributes: str | int | None,
) -> TimelineEventArtifact | None:
    matches = _matching(events, name, **attributes)
    return min(matches, key=lambda event: (event.ts, event.event_id)) if matches else None


def _relevant_events(
    events: list[TimelineEventArtifact],
    *,
    turn: str,
    response: str | None,
    request_aliases: set[str],
    epoch: int,
) -> list[TimelineEventArtifact]:
    return [
        event
        for event in events
        if _epoch(event) == epoch
        and (
            _string_attr(event, "turn_alias") == turn
            or (response is not None and _string_attr(event, "response_alias") == response)
            or _string_attr(event, "request_alias") in request_aliases
            or _string_attr(event, "parent_request_alias") in request_aliases
        )
    ]


def _request_lineage(
    events: list[TimelineEventArtifact],
    event: TimelineEventArtifact,
    *,
    epoch: int,
) -> list[str]:
    """Walk explicit request parents without inferring links from timestamp proximity."""

    lineage: list[str] = []
    current = _string_attr(event, "parent_request_alias")
    while current is not None and current not in lineage:
        lineage.append(current)
        parent_event = next(
            (
                candidate
                for candidate in events
                if _string_attr(candidate, "request_alias") == current
                and _string_attr(candidate, "parent_request_alias") is not None
                and _epoch(candidate) == epoch
            ),
            None,
        )
        current = (
            _string_attr(parent_event, "parent_request_alias")
            if parent_event is not None
            else None
        )
    return lineage


def _string_attr(event: TimelineEventArtifact, name: str) -> str | None:
    value = event.attributes.get(name)
    return value if isinstance(value, str) else None


def _required_attr(event: TimelineEventArtifact, name: str) -> str:
    value = _string_attr(event, name)
    if value is None:  # ServiceEvent validation makes this unreachable.
        raise ValueError(f"{event.event_id} is missing {name}")
    return value


def _epoch(event: TimelineEventArtifact) -> int:
    value = event.attributes.get("generation_epoch", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _csv_attr(event: TimelineEventArtifact, name: str) -> list[str]:
    value = _string_attr(event, name)
    return value.split(",") if value else []
