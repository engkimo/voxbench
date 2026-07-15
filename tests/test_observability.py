from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.observability import (
    ObservationBatch,
    ObservationTransport,
    RtpStats,
    SipEvent,
    VoxBenchObserver,
)

ROOT = Path(__file__).resolve().parents[1]


def _json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def _observed_run_payload() -> dict:
    config = _json("examples/configs/live-demo-openai-realtime.json")
    return {
        "config_name": config["meta"]["name"],
        "configs": [config],
        "manifests": [
            _json("examples/manifests/engine/asterisk.json"),
            _json("examples/manifests/provider/openai-realtime.json"),
            _json("examples/manifests/processor/resampler.json"),
            _json("examples/manifests/processor/agc.json"),
            _json("examples/manifests/processor/limiter.json"),
            _json("examples/manifests/processor/serializer.json"),
        ],
        "call_id": "library-integration-test",
    }


class ApiTestTransport(ObservationTransport):
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def send(self, batch: ObservationBatch) -> None:
        response = self.client.post("/v1/observations", json=batch.to_payload())
        response.raise_for_status()


class FailingTransport(ObservationTransport):
    def send(self, batch: ObservationBatch) -> None:
        raise RuntimeError("transport unavailable")


def _pcm(value: int, frame_count: int = 160) -> bytes:
    return struct.pack("<h", value) * frame_count


def test_observer_streams_audio_metrics_sip_and_rtp_to_control_plane(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    start_response = client.post("/runs/observed", json=_observed_run_payload())
    assert start_response.status_code == 200
    run_id = start_response.json()["run_id"]
    assert start_response.json()["status"] == "running"

    observer = VoxBenchObserver(run_id, ApiTestTransport(client))
    observer.observe_stage_audio(
        stage="agc",
        input_pcm_s16le=_pcm(1000),
        output_pcm_s16le=_pcm(2000),
        sample_rate_hz=16_000,
        gain_applied=2.0,
    )
    observer.observe_sip_event(
        SipEvent(
            call_id="library-integration-test",
            method="INVITE",
            direction="in",
            summary_alias="local-softphone-invite",
        )
    )
    observer.observe_rtp_stats(RtpStats(jitter_ms=1.2, loss_pct=0.1, mos=4.3))

    assert observer.flush() == 7
    assert observer.pending_count == 0

    timeline = client.get(f"/runs/{run_id}/timeline").json()
    agc_metrics = {
        point["name"]: point["value"]
        for lane in timeline["lanes"]["stages"]
        if lane["stage"] == "agc"
        for point in lane["metrics"]
    }
    assert agc_metrics["input_rms"] == pytest.approx(1000.0)
    assert agc_metrics["output_rms"] == pytest.approx(2000.0)
    assert agc_metrics["delta_db"] == pytest.approx(6.0206, rel=1e-4)
    assert agc_metrics["gain_applied"] == pytest.approx(2.0)
    assert len(timeline["lanes"]["sip_ladder"]) == 1
    assert len(timeline["lanes"]["rtp_quality"]) == 1
    assert timeline["lanes"]["recordings"][0]["stage"] == "agc"

    audio_response = client.get(f"/runs/{run_id}/recordings/agc/audio")
    assert audio_response.status_code == 200
    assert audio_response.content.startswith(b"RIFF")

    complete_response = client.post(f"/runs/{run_id}/complete", json={})
    assert complete_response.status_code == 200
    assert complete_response.json()["status"] == "completed"

    rejected = client.post(
        "/v1/observations",
        json={
            "run_id": run_id,
            "metrics": [{"stage": "agc", "name": "input_rms", "value": 1.0}],
        },
    )
    assert rejected.status_code == 409


def test_observation_batch_rejects_unknown_stage_and_raw_sip_fields(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = client.post("/runs/observed", json=_observed_run_payload()).json()["run_id"]

    unknown_stage = client.post(
        "/v1/observations",
        json={
            "run_id": run_id,
            "metrics": [{"stage": "not-configured", "name": "input_rms", "value": 1.0}],
        },
    )
    assert unknown_stage.status_code == 400

    raw_sip = client.post(
        "/v1/observations",
        json={
            "run_id": run_id,
            "sip_events": [
                {
                    "method": "INVITE",
                    "direction": "in",
                    "raw_sip_body": "INVITE sip:demo@example.invalid SIP/2.0",
                }
            ],
        },
    )
    assert raw_sip.status_code == 422


def test_observer_restores_pending_items_when_transport_fails() -> None:
    observer = VoxBenchObserver("run-1", FailingTransport())
    observer.observe_metric("cpu_pct", 12.5)

    with pytest.raises(RuntimeError, match="transport unavailable"):
        observer.flush()

    assert observer.pending_count == 1


def test_observed_run_can_fail_with_safe_alias(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = client.post("/runs/observed", json=_observed_run_payload()).json()["run_id"]

    failed = client.post(
        f"/runs/{run_id}/fail",
        json={"failure_alias": "provider-session-error"},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["failure_alias"] == "provider-session-error"
    assert any(metric["name"] == "run_failed" for metric in failed.json()["metrics"])

    live = client.get("/runs/live-preview").json()
    assert live[0]["failure_alias"] == "provider-session-error"
    assert client.post(f"/runs/{run_id}/complete", json={}).status_code == 409

    unsafe = client.post(
        f"/runs/{run_id}/fail",
        json={"failure_alias": "https://provider.example/error"},
    )
    assert unsafe.status_code == 422
