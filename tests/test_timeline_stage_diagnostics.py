from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voxbench.control_plane.run_api import StoredRun
from voxbench.engine_harness.models import (
    MetricArtifact,
    RecordingArtifact,
    TimelineEventArtifact,
)
from voxbench.verification import VerificationResult


def test_timeline_locates_media_contraction_and_stage_level_change() -> None:
    t0 = datetime(2026, 7, 23, tzinfo=UTC)
    run = StoredRun(
        run_id="timeline-stage-diagnostics",
        config_hash="config-hash",
        call_id=None,
        conversation_id="conversation-alias",
        provider="provider-alias",
        engine="engine-alias",
        status="completed",
        started_at=t0,
        ended_at=t0 + timedelta(seconds=1),
        resolved_config={
            "spec": {
                "media": {
                    "pipeline": [
                        {"type": "resampler"},
                        {"type": "serializer"},
                    ]
                }
            }
        },
        recordings=[
            RecordingArtifact(
                stage="resampler",
                uri="recording-ref-resampler",
                format={"encoding": "pcm16", "rate": 8000, "channels": 1},
                duration_ms=1000,
            ),
            RecordingArtifact(
                stage="serializer",
                uri="recording-ref-serializer",
                format={"encoding": "pcm16", "rate": 8000, "channels": 1},
                duration_ms=750,
            ),
        ],
        spans=[],
        metrics=[
            MetricArtifact(
                stage="serializer",
                name="input_rms",
                value=1000,
                ts=t0 + timedelta(milliseconds=100),
            ),
            MetricArtifact(
                stage="serializer",
                name="output_rms",
                value=500,
                ts=t0 + timedelta(milliseconds=100),
            ),
            MetricArtifact(
                stage="serializer",
                name="delta_db",
                value=-6.0206,
                ts=t0 + timedelta(milliseconds=100),
            ),
            MetricArtifact(
                stage="serializer",
                name="gain_applied",
                value=0.5,
                ts=t0 + timedelta(milliseconds=100),
            ),
        ],
        verifications=[
            VerificationResult(
                stage="serializer",
                invariant="duration_preserving",
                passed=False,
                observed={
                    "input_duration_ms": 1000,
                    "output_duration_ms": 750,
                    "duration_ratio": 0.75,
                },
                expected={"duration_ratio_min": 0.98, "duration_ratio_max": 1.02},
                detail="duration ratio outside tolerance",
            ),
            VerificationResult(
                stage="serializer",
                invariant="level_preserving",
                passed=False,
                observed={
                    "input_rms": 1000,
                    "output_rms": 500,
                    "delta_db": -6.0206,
                },
                expected={"delta_db_min": -3, "delta_db_max": 3},
                detail="RMS delta outside tolerance",
            ),
        ],
    )

    lanes = run.to_timeline().lanes
    signal_event = next(
        event for event in lanes.events if event.event_id.startswith("stage-signal")
    )
    contraction_event = next(
        event for event in lanes.events if event.name == "stage.media_time_contracted"
    )
    assert signal_event.name == "stage.level_decreased"
    assert signal_event.t_rel_ms == pytest.approx(100)
    assert signal_event.attributes == {
        "input_rms": 1000,
        "output_rms": 500,
        "delta_db": -6.0206,
        "gain_applied": 0.5,
        "sample_count": 1,
        "event_threshold_db": 1.0,
    }
    assert contraction_event.t_rel_ms == 750
    assert contraction_event.clock_domain == "recording_media_time"
    assert contraction_event.attributes["missing_duration_ms"] == 250

    missing_interval = next(
        interval for interval in lanes.intervals if interval.name == "media_time_missing"
    )
    assert missing_interval.start_ms == 750
    assert missing_interval.end_ms == 1000
    assert missing_interval.stage == "serializer"

    duration_incident = next(
        incident for incident in lanes.incidents if incident.rule_id == "duration_preserving"
    )
    assert duration_incident.title == "Media time contracted at serializer"
    assert duration_incident.summary == "250 ms missing between resampler and serializer"
    assert duration_incident.start_ms == 750
    assert duration_incident.end_ms == 1000
    assert duration_incident.evidence_refs == [
        "recording:0",
        "recording:1",
        "duration-contraction:0",
    ]

    level_incident = next(
        incident for incident in lanes.incidents if incident.rule_id == "level_preserving"
    )
    assert level_incident.title == "Signal level contract failed at serializer"
    assert level_incident.summary == "Stage changed RMS level by -6.02 dB"
    assert level_incident.evidence_refs == [
        "recording:0",
        "recording:1",
        "stage-signal:serializer",
    ]


def test_timeline_correlates_final_stage_silence_with_provider_response() -> None:
    t0 = datetime(2026, 7, 24, tzinfo=UTC)
    metrics = [
        MetricArtifact(
            stage=None,
            name="provider_response_started",
            value=1,
            ts=t0,
        ),
        MetricArtifact(
            stage=None,
            name="provider_response_done",
            value=1,
            ts=t0 + timedelta(milliseconds=500),
        ),
    ]
    for window_start_ms in (100, 700):
        for chunk_index in range(15):
            ts = t0 + timedelta(
                milliseconds=window_start_ms + (chunk_index * 20)
            )
            metrics.extend(
                [
                    MetricArtifact(
                        stage="serializer",
                        name="silence_sample_pct",
                        value=100,
                        ts=ts,
                    ),
                    MetricArtifact(
                        stage="serializer",
                        name="audio_chunk_duration_ms",
                        value=20,
                        ts=ts,
                    ),
                ]
            )
    for chunk_index in range(15):
        ts = t0 + timedelta(milliseconds=100 + (chunk_index * 20))
        metrics.extend(
            [
                MetricArtifact(
                    stage="resampler",
                    name="silence_sample_pct",
                    value=100,
                    ts=ts,
                ),
                MetricArtifact(
                    stage="resampler",
                    name="audio_chunk_duration_ms",
                    value=20,
                    ts=ts,
                ),
            ]
        )
    run = StoredRun(
        run_id="timeline-provider-dead-air",
        config_hash="config-hash",
        call_id=None,
        conversation_id="conversation-alias",
        provider="provider-alias",
        engine="engine-alias",
        status="completed",
        started_at=t0,
        ended_at=t0 + timedelta(seconds=1),
        resolved_config={
            "spec": {
                "media": {
                    "pipeline": [
                        {"type": "resampler"},
                        {"type": "serializer"},
                    ]
                }
            }
        },
        recordings=[
            RecordingArtifact(
                stage="serializer",
                uri="recording-ref-serializer",
                format={"encoding": "pcm16", "rate": 8000, "channels": 1},
                duration_ms=1000,
            )
        ],
        spans=[],
        metrics=metrics,
        verifications=[],
    )

    lanes = run.to_timeline().lanes
    provider_events = [
        event
        for event in lanes.events
        if event.name in {"provider.response_started", "provider.response_done"}
    ]
    assert [event.t_rel_ms for event in provider_events] == [0, 500]
    assert {
        event.correlation_alias for event in provider_events
    } == {"provider-response:0"}
    assert not any(
        event.name in {"provider_response_started", "provider_response_done"}
        for event in lanes.events
    )

    provider_interval = next(
        interval
        for interval in lanes.intervals
        if interval.name == "provider_response"
    )
    assert provider_interval.start_ms == 0
    assert provider_interval.end_ms == 500
    assert provider_interval.direction == "assistant_to_caller"
    assert provider_interval.attributes["completion_observed"] is True

    silence_intervals = [
        interval
        for interval in lanes.intervals
        if interval.name == "digital_silence"
    ]
    assert [
        (interval.stage, interval.start_ms, interval.end_ms)
        for interval in silence_intervals
    ] == [
        ("resampler", 100, 400),
        ("serializer", 100, 400),
        ("serializer", 700, 1000),
    ]
    assert silence_intervals[0].attributes["incident_status"] == (
        "evidence_only_without_speech_context"
    )
    assert silence_intervals[1].attributes["incident_status"] == (
        "assistant_output_dead_air_suspected"
    )
    assert silence_intervals[2].attributes["incident_status"] == (
        "evidence_only_without_speech_context"
    )

    incidents = [
        incident
        for incident in lanes.incidents
        if incident.rule_id == "assistant_output_dead_air_v1"
    ]
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident.title == "Assistant output dead air suspected"
    assert incident.summary == (
        "300 ms digital silence at serializer while provider response was active"
    )
    assert incident.start_ms == 100
    assert incident.end_ms == 400
    assert incident.severity == "warning"
    assert incident.confidence == "medium"
    assert incident.stage == "serializer"
    assert incident.direction == "assistant_to_caller"
    assert incident.observed == {
        "digital_silence_while_provider_active_ms": 300,
        "silence_window_duration_ms": 300,
        "provider_response_duration_ms": 500,
        "provider_completion_observed": True,
        "assistant_playback_observed": False,
        "assistant_playback_written_audio_ms": None,
        "remote_playout_observed": False,
    }
    assert incident.expected == {
        "digital_silence_while_provider_active_ms_below": 200,
        "remote_playout": "not observed",
    }
    assert incident.evidence_refs == [
        "recording:0",
        "stage-silence:serializer:0:start",
        "provider-response:0:start",
        "provider-response:0:done",
    ]


def test_timeline_projects_speech_playback_and_narrows_dead_air() -> None:
    t0 = datetime(2026, 7, 24, tzinfo=UTC)
    metrics = [
        MetricArtifact(
            stage=None,
            name="provider_response_started",
            value=1,
            ts=t0,
        ),
        MetricArtifact(
            stage=None,
            name="provider_response_done",
            value=1,
            ts=t0 + timedelta(seconds=1),
        ),
    ]
    for chunk_index in range(40):
        ts = t0 + timedelta(milliseconds=chunk_index * 20)
        metrics.extend(
            [
                MetricArtifact(
                    stage="serializer",
                    name="silence_sample_pct",
                    value=100,
                    ts=ts,
                ),
                MetricArtifact(
                    stage="serializer",
                    name="audio_chunk_duration_ms",
                    value=20,
                    ts=ts,
                ),
            ]
        )
    run = StoredRun(
        run_id="timeline-speech-playback",
        config_hash="config-hash",
        call_id=None,
        conversation_id="conversation-alias",
        provider="provider-alias",
        engine="engine-alias",
        status="completed",
        started_at=t0,
        ended_at=t0 + timedelta(seconds=1),
        resolved_config={
            "spec": {
                "media": {
                    "pipeline": [
                        {"type": "serializer"},
                    ]
                }
            }
        },
        recordings=[
            RecordingArtifact(
                stage="serializer",
                uri="recording-ref-serializer",
                format={"encoding": "pcm16", "rate": 8000, "channels": 1},
                duration_ms=1000,
            )
        ],
        spans=[],
        metrics=metrics,
        verifications=[],
        timeline_events=[
            TimelineEventArtifact(
                event_id="caller-turn-1:start",
                category="conversation",
                name="provider_input_speech_started",
                ts=t0 + timedelta(milliseconds=100),
                source="audiosocket_bridge",
                correlation_alias="caller-turn-1",
                direction="caller_to_assistant",
            ),
            TimelineEventArtifact(
                event_id="caller-turn-1:stop",
                category="conversation",
                name="provider_input_speech_stopped",
                ts=t0 + timedelta(milliseconds=450),
                source="audiosocket_bridge",
                correlation_alias="caller-turn-1",
                direction="caller_to_assistant",
                attributes={
                    "completion_observed": True,
                    "stop_reason": "provider_speech_stopped",
                },
            ),
            TimelineEventArtifact(
                event_id="assistant-playback-1:start",
                category="conversation",
                name="assistant_playback_started",
                ts=t0 + timedelta(milliseconds=300),
                source="audiosocket_bridge",
                correlation_alias="assistant-playback-1",
                direction="assistant_to_caller",
                attributes={"frame_duration_ms": 20},
            ),
            TimelineEventArtifact(
                event_id="assistant-playback-1:stop",
                category="conversation",
                name="assistant_playback_stopped",
                ts=t0 + timedelta(milliseconds=650),
                source="audiosocket_bridge",
                correlation_alias="assistant-playback-1",
                direction="assistant_to_caller",
                attributes={
                    "written_audio_ms": 340,
                    "stop_reason": "stream_ended",
                },
            ),
        ],
    )

    lanes = run.to_timeline().lanes
    caller_speech = next(
        interval for interval in lanes.intervals if interval.name == "caller_speech"
    )
    assert caller_speech.start_ms == 100
    assert caller_speech.end_ms == 450
    assert caller_speech.direction == "caller_to_assistant"
    assert caller_speech.attributes == {
        "duration_ms": 350,
        "completion_observed": True,
        "stop_reason": "provider_speech_stopped",
        "observation_boundary": "provider_input_vad",
    }

    playback = next(
        interval
        for interval in lanes.intervals
        if interval.name == "assistant_playback"
    )
    assert playback.start_ms == 300
    assert playback.end_ms == 650
    assert playback.direction == "assistant_to_caller"
    assert playback.attributes == {
        "duration_ms": 350,
        "completion_observed": True,
        "written_audio_ms": 340,
        "stop_reason": "stream_ended",
        "observation_boundary": "audiosocket_frame_write",
        "remote_playout_observed": False,
    }

    dead_air = next(
        incident
        for incident in lanes.incidents
        if incident.rule_id == "assistant_output_dead_air_v1"
    )
    assert dead_air.start_ms == 300
    assert dead_air.end_ms == 650
    assert dead_air.summary == (
        "350 ms digital silence at serializer during observed assistant playback"
    )
    assert dead_air.observed["assistant_playback_observed"] is True
    assert dead_air.observed["assistant_playback_written_audio_ms"] == 340
    assert dead_air.evidence_refs[-2:] == [
        "assistant-playback-1:start",
        "assistant-playback-1:stop",
    ]
