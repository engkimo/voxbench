from __future__ import annotations

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
