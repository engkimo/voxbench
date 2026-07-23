from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from voxbench.control_plane.run_api import StoredRun
from voxbench.engine_harness.models import MetricArtifact, RecordingArtifact
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
