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
    observer.observe_rtp_stats(
        RtpStats(
            jitter_ms=1.2,
            loss_pct=0.1,
            mos=4.3,
            direction="received",
            rtt_ms=8.5,
        )
    )

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
    assert timeline["lanes"]["rtp_quality"][0]["direction"] == "received"
    assert timeline["lanes"]["rtp_quality"][0]["rtt_ms"] == 8.5
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

    unsafe_event = client.post(
        "/v1/observations",
        json={
            "run_id": run_id,
            "timeline_events": [
                {
                    "event_id": "unsafe-event",
                    "category": "provider",
                    "name": "provider_event",
                    "source": "integration-test",
                    "attributes": {"provider_ref": "https://provider.example/item"},
                }
            ],
        },
    )
    assert unsafe_event.status_code == 422


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


def test_live_preview_projects_provider_connection_metrics(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    pending_payload = _observed_run_payload()
    pending_payload["environment"] = {"tags": ["live-demo", "provider"]}
    pending_run_id = client.post(
        "/runs/observed",
        json=pending_payload,
    ).json()["run_id"]
    pending_preview = client.get("/runs/live-preview").json()[0]
    assert pending_preview["run_id"] == pending_run_id
    assert pending_preview["provider_connection"] == {
        "state": "pending",
        "attempts": 0,
        "retries": 0,
        "failures": 0,
        "exhausted": False,
    }

    run_id = client.post("/runs/observed", json=_observed_run_payload()).json()["run_id"]
    observer = VoxBenchObserver(run_id, ApiTestTransport(client))
    observer.observe_metric("provider_connect_attempts", 3)
    observer.observe_metric("provider_connect_retries", 2)
    observer.observe_metric("provider_connect_failures", 2)
    assert observer.flush() == 3

    preview = client.get("/runs/live-preview").json()[0]
    assert preview["provider_connection"] == {
        "state": "connected",
        "attempts": 3,
        "retries": 2,
        "failures": 2,
        "exhausted": False,
    }

    exhausted_run_id = client.post(
        "/runs/observed",
        json=_observed_run_payload(),
    ).json()["run_id"]
    exhausted_observer = VoxBenchObserver(exhausted_run_id, ApiTestTransport(client))
    exhausted_observer.observe_metric("provider_connect_attempts", 3)
    exhausted_observer.observe_metric("provider_connect_retries", 2)
    exhausted_observer.observe_metric("provider_connect_failures", 3)
    exhausted_observer.observe_metric("provider_connect_exhausted", 1)
    assert exhausted_observer.flush() == 4
    failed = client.post(
        f"/runs/{exhausted_run_id}/fail",
        json={"failure_alias": "provider-connect-error"},
    )
    assert failed.status_code == 200

    exhausted_preview = client.get("/runs/live-preview").json()[0]
    assert exhausted_preview["run_id"] == exhausted_run_id
    assert exhausted_preview["provider_connection"] == {
        "state": "exhausted",
        "attempts": 3,
        "retries": 2,
        "failures": 3,
        "exhausted": True,
    }


def test_live_preview_projects_asterisk_rtp_collector_metrics(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = client.post("/runs/observed", json=_observed_run_payload()).json()["run_id"]
    observer = VoxBenchObserver(run_id, ApiTestTransport(client))

    inactive = client.get("/runs/live-preview").json()[0]["rtp_collector"]
    assert inactive == {"state": "inactive", "events_collected": 0, "failures": 0}

    observer.observe_metric("asterisk_ami_rtcp_connected", 1)
    assert observer.flush() == 1
    connected = client.get("/runs/live-preview").json()[0]["rtp_collector"]
    assert connected == {"state": "connected", "events_collected": 0, "failures": 0}

    observer.observe_metric("asterisk_ami_rtcp_events", 1)
    observer.observe_metric("asterisk_ami_rtcp_events", 1)
    assert observer.flush() == 2
    collecting = client.get("/runs/live-preview").json()[0]["rtp_collector"]
    assert collecting == {"state": "collecting", "events_collected": 2, "failures": 0}

    observer.observe_metric("asterisk_ami_rtcp_failures", 1)
    assert observer.flush() == 1
    failed = client.get("/runs/live-preview").json()[0]["rtp_collector"]
    assert failed == {"state": "failed", "events_collected": 2, "failures": 1}

    observer.observe_metric("asterisk_ami_rtcp_connected", 1)
    assert observer.flush() == 1
    reconnected = client.get("/runs/live-preview").json()[0]["rtp_collector"]
    assert reconnected == {"state": "connected", "events_collected": 2, "failures": 1}
