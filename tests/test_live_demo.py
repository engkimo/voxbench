from __future__ import annotations

import wave
from pathlib import Path

from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.realtime_providers import GeminiLiveProvider, OpenAIRealtimeProvider


def test_provider_readiness_is_dry_run_friendly_without_keys(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    openai = OpenAIRealtimeProvider().readiness(dry_run=True)
    gemini = GeminiLiveProvider().readiness(dry_run=True)

    assert openai.ready
    assert gemini.ready
    assert gemini.model == "gemini-3.1-flash-live-preview"
    assert openai.has_api_key is False
    assert gemini.has_api_key is False


def test_simulated_live_demo_run_writes_timeline_audio_and_gain_metrics(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)

    response = client.post(
        "/runs/live-demo/simulated",
        json={
            "provider": "gemini-live",
            "call_id": "local-demo-call",
            "input_rms": 1000,
            "target_rms": 4000,
            "max_gain": 3.0,
            "noise_floor": 100,
            "duration_ms": 3000,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "gemini-live"
    assert body["status"] == "completed"
    assert body["call_id"] == "local-demo-call"
    assert {recording["stage"] for recording in body["recordings"]} == {
        "resampler",
        "agc",
        "limiter",
        "serializer",
    }
    for recording in body["recordings"]:
        path = Path(recording["uri"].removeprefix("file://"))
        assert path.exists()
        assert path.read_bytes().startswith(b"RIFF")
        assert recording["duration_ms"] == 3000.0

    reference_path = Path(
        next(
            recording["uri"]
            for recording in body["recordings"]
            if recording["stage"] == "resampler"
        ).removeprefix("file://")
    )
    with wave.open(str(reference_path), "rb") as reference:
        assert reference.getnframes() / reference.getframerate() == 3.0
        assert any(reference.readframes(reference.getnframes()))

    agc_metrics = [
        metric for metric in body["metrics"] if metric["stage"] == "agc"
    ]
    assert {metric["name"] for metric in agc_metrics} >= {
        "input_rms",
        "output_rms",
        "delta_db",
        "gain_applied",
    }
    assert next(metric for metric in agc_metrics if metric["name"] == "gain_applied")[
        "value"
    ] == 3.0

    timeline_response = client.get(f"/runs/{body['run_id']}/timeline")
    assert timeline_response.status_code == 200
    timeline = timeline_response.json()
    assert len(timeline["lanes"]["sip_ladder"]) >= 4
    assert len(timeline["lanes"]["rtp_quality"]) == 3
    agc_lane = next(stage for stage in timeline["lanes"]["stages"] if stage["stage"] == "agc")
    assert {metric["name"] for metric in agc_lane["metrics"]} >= {
        "input_rms",
        "output_rms",
        "delta_db",
        "gain_applied",
    }
    assert {event["category"] for event in timeline["lanes"]["events"]} >= {
        "signaling",
    }
    agc_level_event = next(
        event
        for event in timeline["lanes"]["events"]
        if event["name"] == "stage.level_increased" and event["stage"] == "agc"
    )
    assert agc_level_event["attributes"]["gain_applied"] == 3.0
    assert agc_level_event["attributes"]["delta_db"] > 9.0
    assert {series["category"] for series in timeline["lanes"]["series"]} >= {
        "pipeline",
        "transport",
    }
    agc_incident = next(
        incident
        for incident in timeline["lanes"]["incidents"]
        if incident["stage"] == "agc"
    )
    assert agc_incident["rule_id"] == "level_preserving"
    assert agc_incident["title"] == "Signal level contract failed at agc"
    assert agc_incident["severity"] == "error"
    assert agc_incident["confidence"] == "certain"
    assert "stage-signal:agc" in agc_incident["evidence_refs"]
    assert not {
        "rtp_sequence_gap_v1",
        "rtp_arrival_stall_v1",
    } & {incident["rule_id"] for incident in timeline["lanes"]["incidents"]}


def test_simulated_live_demo_exposes_rtp_gap_and_stall_on_common_timeline(
    tmp_path: Path,
) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)

    response = client.post(
        "/runs/live-demo/simulated",
        json={
            "provider": "gemini-live",
            "scenario": "rtp-gap",
            "duration_ms": 3000,
        },
    )

    assert response.status_code == 200
    run_id = response.json()["run_id"]
    timeline = client.get(f"/runs/{run_id}/timeline").json()

    event_names = {event["name"] for event in timeline["lanes"]["events"]}
    assert "rtp.packet_arrived" not in event_names
    assert {
        "rtp.sequence_gap_observed",
        "rtp.arrival_stall_observed",
    } <= event_names

    intervals = {
        interval["name"]: interval
        for interval in timeline["lanes"]["intervals"]
    }
    assert intervals["rtp_sequence_gap"]["stream_alias"] == "simulated-caller-audio"
    assert intervals["rtp_arrival_stall"]["direction"] == "received"

    incidents = {
        incident["rule_id"]: incident
        for incident in timeline["lanes"]["incidents"]
    }
    assert incidents["rtp_sequence_gap_v1"]["observed"]["missing_packet_count"] == 1
    assert (
        incidents["rtp_sequence_gap_v1"]["expected"][
            "capture_point_continuity"
        ]
        == "verified"
    )
    assert incidents["rtp_sequence_gap_v1"]["observed"]["alignment_uncertainty_ms"] == 0.5
    assert incidents["rtp_sequence_gap_v1"]["evidence_refs"][-1] == (
        "rtp-capture-health:0"
    )
    assert incidents["rtp_arrival_stall_v1"]["observed"]["excess_arrival_delay_ms"] == 140


def test_simulated_live_demo_rejects_non_dry_run_when_provider_not_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)

    response = client.post(
        "/runs/live-demo/simulated",
        json={"provider": "openai-realtime", "dry_run": False},
    )

    assert response.status_code == 400
