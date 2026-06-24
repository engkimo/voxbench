from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.registry.service import load_json

ROOT = Path(__file__).resolve().parents[1]

MANIFESTS = [
    ROOT / "examples/manifests/engine/asterisk.json",
    ROOT / "examples/manifests/provider/gemini.json",
    ROOT / "examples/manifests/processor/resampler.json",
    ROOT / "examples/manifests/processor/agc.json",
    ROOT / "examples/manifests/processor/limiter.json",
    ROOT / "examples/manifests/processor/serializer.json",
]


def test_post_runs_creates_run_recordings_and_spans(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
        "call_id": "sip-call-id-example",
    }

    response = client.post("/runs", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["status"] == "completed"
    assert body["call_id"] == "sip-call-id-example"
    assert len(body["recordings"]) == 4
    assert len(body["spans"]) == 5
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

    run_spans = [span for span in body["spans"] if span["name"] == "voxbench.run"]
    assert len(run_spans) == 1
    assert run_spans[0]["attrs"]["voxbench.run_id"] == body["run_id"]
    assert run_spans[0]["attrs"]["voxbench.config_hash"] == body["config_hash"]
    assert run_spans[0]["attrs"]["conversation_id"] == body["conversation_id"]

    get_response = client.get(f"/runs/{body['run_id']}")
    assert get_response.status_code == 200
    assert get_response.json()["run_id"] == body["run_id"]


def test_post_runs_with_default_relative_artifact_root() -> None:
    app = create_app()
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
    }

    response = client.post("/runs", json=payload)

    assert response.status_code == 200
    assert response.json()["recordings"][0]["uri"].startswith("file://")


def test_get_run_timeline_groups_stage_metrics_and_recordings(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
    }

    response = client.post("/runs", json=payload)
    assert response.status_code == 200
    run = response.json()

    timeline_response = client.get(f"/runs/{run['run_id']}/timeline")

    assert timeline_response.status_code == 200
    timeline = timeline_response.json()
    assert timeline["run_id"] == run["run_id"]
    assert timeline["config_hash"] == run["config_hash"]
    assert timeline["t0"]
    assert timeline["lanes"]["sip_ladder"] == []
    assert timeline["lanes"]["rtp_quality"] == []
    assert timeline["lanes"]["turns"] == []
    assert timeline["lanes"]["host"] == []
    assert {stage["stage"] for stage in timeline["lanes"]["stages"]} == {
        "resampler",
        "agc",
        "limiter",
        "serializer",
    }
    assert len(timeline["lanes"]["recordings"]) == len(run["recordings"])

    serializer_stage = _stage_lane(timeline, "serializer")
    assert serializer_stage["violations"] == []
    assert {metric["name"] for metric in serializer_stage["metrics"]} >= {
        "frames_in",
        "frames_out",
        "frame_cadence_jitter_ms",
        "expected_frame_interval_ms",
    }
    assert all(metric["ts"] >= 0 for metric in serializer_stage["metrics"])


def _stage_lane(timeline: dict[str, object], stage_name: str) -> dict[str, object]:
    stages = timeline["lanes"]["stages"]
    for stage in stages:
        if stage["stage"] == stage_name:
            return stage
    raise AssertionError(f"unknown stage lane: {stage_name}")
