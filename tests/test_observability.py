from __future__ import annotations

import json
import struct
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.observability import (
    ObservationBatch,
    ObservationTransport,
    RtpCaptureHealthSnapshot,
    RtpPacket,
    RtpPacketTapAdapter,
    RtpStats,
    SipEvent,
    VoxBenchObserver,
    rtp_packet_from_datagram,
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


class RecordingTransport(ObservationTransport):
    def __init__(self) -> None:
        self.batches: list[ObservationBatch] = []

    def send(self, batch: ObservationBatch) -> None:
        self.batches.append(batch)


def _pcm(value: int, frame_count: int = 160) -> bytes:
    return struct.pack("<h", value) * frame_count


def _rtp_datagram(
    sequence_number: int,
    rtp_timestamp: int,
    *,
    payload: bytes = b"transient-media",
) -> bytes:
    return (
        bytes((0x80, 0x00))
        + sequence_number.to_bytes(2, byteorder="big")
        + rtp_timestamp.to_bytes(4, byteorder="big")
        + b"\xde\xad\xbe\xef"
        + payload
    )


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

    assert observer.flush() == 11
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
    assert agc_metrics["sample_peak_dbfs"] == pytest.approx(-24.2884, rel=1e-4)
    assert agc_metrics["full_scale_sample_pct"] == 0.0
    assert agc_metrics["silence_sample_pct"] == 0.0
    assert agc_metrics["audio_chunk_duration_ms"] == 10.0
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


def test_pcm_quality_projects_clipping_suspicion_and_silence_window(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = client.post("/runs/observed", json=_observed_run_payload()).json()["run_id"]
    t0 = datetime.fromisoformat(client.get(f"/runs/{run_id}/timeline").json()["t0"])
    observer = VoxBenchObserver(run_id, ApiTestTransport(client))
    full_scale = _pcm(32767)
    observer.observe_stage_audio(
        stage="resampler",
        input_pcm_s16le=full_scale,
        output_pcm_s16le=full_scale,
        sample_rate_hz=8_000,
        ts=t0 + timedelta(milliseconds=100),
    )
    silence = _pcm(0)
    for index in range(10):
        observer.observe_stage_audio(
            stage="agc",
            input_pcm_s16le=silence,
            output_pcm_s16le=silence,
            sample_rate_hz=8_000,
            record_output=False,
            ts=t0 + timedelta(seconds=1, milliseconds=index * 20),
        )

    assert observer.flush() == 78
    lanes = client.get(f"/runs/{run_id}/timeline").json()["lanes"]
    full_scale_event = next(
        event
        for event in lanes["events"]
        if event["name"] == "stage.full_scale_samples_detected"
    )
    assert full_scale_event["stage"] == "resampler"
    assert full_scale_event["attributes"]["full_scale_sample_pct"] == 100.0
    assert full_scale_event["attributes"]["sample_peak_dbfs"] == pytest.approx(
        -0.000265,
        rel=1e-3,
    )
    clipping_incident = next(
        incident
        for incident in lanes["incidents"]
        if incident["rule_id"] == "pcm_full_scale_samples_v1"
    )
    assert clipping_incident["title"] == "Clipping suspected at resampler"
    assert clipping_incident["confidence"] == "medium"
    assert clipping_incident["evidence_refs"] == [
        "recording:0",
        "stage-full-scale:resampler",
    ]

    silence_event = next(
        event
        for event in lanes["events"]
        if event["name"] == "stage.digital_silence_started"
    )
    silence_interval = next(
        interval
        for interval in lanes["intervals"]
        if interval["name"] == "digital_silence"
    )
    assert silence_event["stage"] == "agc"
    assert silence_event["t_rel_ms"] == pytest.approx(1000)
    assert silence_interval["start_ms"] == pytest.approx(1000)
    assert silence_interval["end_ms"] == pytest.approx(1200)
    assert silence_interval["attributes"] == {
        "duration_ms": 200.0,
        "observation_count": 10,
        "peak_silence_sample_pct": 100.0,
        "incident_status": "evidence_only_without_speech_context",
    }
    assert all(
        incident["rule_id"] != "digital_silence"
        for incident in lanes["incidents"]
    )


def test_rtp_degradation_projects_directional_evidence_windows(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = client.post("/runs/observed", json=_observed_run_payload()).json()["run_id"]
    t0 = datetime.fromisoformat(client.get(f"/runs/{run_id}/timeline").json()["t0"])
    observer = VoxBenchObserver(run_id, ApiTestTransport(client))
    observer.observe_rtp_stats(
        RtpStats(
            jitter_ms=12,
            loss_pct=2,
            mos=4.0,
            direction="received",
            ts=t0 + timedelta(seconds=1),
        )
    )
    observer.observe_rtp_stats(
        RtpStats(
            jitter_ms=35,
            loss_pct=3,
            mos=3.2,
            direction="received",
            ts=t0 + timedelta(seconds=2),
        )
    )
    observer.observe_rtp_stats(
        RtpStats(
            jitter_ms=45,
            loss_pct=0.1,
            mos=4.1,
            direction="sent",
            ts=t0 + timedelta(seconds=1, milliseconds=500),
        )
    )
    observer.observe_rtp_stats(
        RtpStats(
            jitter_ms=5,
            loss_pct=0,
            mos=4.4,
            direction="received",
            ts=t0 + timedelta(seconds=8),
        )
    )

    assert observer.flush() == 4
    lanes = client.get(f"/runs/{run_id}/timeline").json()["lanes"]
    transport_events = [
        event for event in lanes["events"] if event["category"] == "transport"
    ]
    assert [event["name"] for event in transport_events] == [
        "rtp.loss_elevated",
        "rtp.jitter_elevated",
        "rtp.jitter_elevated",
        "rtp.loss_elevated",
        "rtp.mos_degraded",
    ]
    assert {event["direction"] for event in transport_events} == {"received", "sent"}
    received_aliases = {
        event["correlation_alias"]
        for event in transport_events
        if event["direction"] == "received"
    }
    assert len(received_aliases) == 1

    transport_intervals = [
        interval
        for interval in lanes["intervals"]
        if interval["category"] == "transport"
    ]
    assert len(transport_intervals) == 2
    assert transport_intervals[0]["start_ms"] == pytest.approx(1000)
    assert transport_intervals[0]["end_ms"] == pytest.approx(2000)

    transport_incidents = [
        incident
        for incident in lanes["incidents"]
        if incident["category"] == "transport"
    ]
    received_incident = next(
        incident
        for incident in transport_incidents
        if incident["direction"] == "received"
    )
    sent_incident = next(
        incident for incident in transport_incidents if incident["direction"] == "sent"
    )
    assert received_incident["title"] == "RTP loss burst suspected"
    assert received_incident["confidence"] == "medium"
    assert received_incident["observed"] == {
        "sample_count": 2,
        "triggers": ["jitter", "loss", "mos"],
        "peak_loss_pct": 3.0,
        "peak_jitter_ms": 35.0,
        "minimum_mos": 3.2,
        "loss_burst_suspected": True,
    }
    assert received_incident["evidence_refs"] == [
        "rtp:0:loss",
        "rtp:1:loss",
        "rtp:1:jitter",
        "rtp:1:mos",
    ]
    assert sent_incident["title"] == "RTP jitter elevated"
    assert sent_incident["observed"]["loss_burst_suspected"] is False


def test_rtp_packet_headers_project_sequence_gap_and_arrival_stall(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = client.post("/runs/observed", json=_observed_run_payload()).json()["run_id"]
    t0 = datetime.fromisoformat(client.get(f"/runs/{run_id}/timeline").json()["t0"])
    observer = VoxBenchObserver(run_id, ApiTestTransport(client))
    for sequence_number, rtp_timestamp, offset_ms in (
        (65_534, 1_000, 100),
        (65_535, 1_160, 120),
        (1, 1_480, 160),
        (2, 1_640, 300),
    ):
        observer.observe_rtp_packet(
            RtpPacket(
                stream_alias="caller-audio",
                direction="received",
                sequence_number=sequence_number,
                rtp_timestamp=rtp_timestamp,
                payload_type=0,
                clock_rate_hz=8_000,
                ts=t0 + timedelta(milliseconds=offset_ms),
            )
        )

    assert observer.flush() == 4
    lanes = client.get(f"/runs/{run_id}/timeline").json()["lanes"]
    packet_events = [
        event
        for event in lanes["events"]
        if event["source"] == "rtp_packet_cadence_rule_v1"
    ]
    assert [event["name"] for event in packet_events] == [
        "rtp.sequence_gap_observed",
        "rtp.arrival_stall_observed",
    ]
    assert not any(
        event["name"] == "rtp.packet_arrived" for event in lanes["events"]
    )
    sequence_event = packet_events[0]
    assert sequence_event["direction"] == "received"
    assert sequence_event["stream_alias"] == "caller-audio"
    assert sequence_event["attributes"] == {
        "previous_sequence_number": 65_535,
        "current_sequence_number": 1,
        "sequence_delta": 2,
        "missing_packet_count": 1,
        "arrival_gap_ms": 40,
        "media_advance_ms": 40,
        "excess_arrival_delay_ms": 0,
        "payload_type": 0,
        "clock_rate_hz": 8_000,
        "capture_point_continuity": "not independently verified",
    }

    packet_intervals = [
        interval
        for interval in lanes["intervals"]
        if interval["source"] == "rtp_packet_cadence_rule_v1"
    ]
    assert [interval["name"] for interval in packet_intervals] == [
        "rtp_sequence_gap",
        "rtp_arrival_stall",
    ]
    assert packet_intervals[0]["start_ms"] == pytest.approx(120)
    assert packet_intervals[0]["end_ms"] == pytest.approx(160)
    assert packet_intervals[1]["start_ms"] == pytest.approx(160)
    assert packet_intervals[1]["end_ms"] == pytest.approx(300)

    packet_incidents = [
        incident
        for incident in lanes["incidents"]
        if incident["rule_id"]
        in {"rtp_sequence_gap_v1", "rtp_arrival_stall_v1"}
    ]
    assert [incident["title"] for incident in packet_incidents] == [
        "RTP sequence gap observed",
        "RTP arrival stall suspected",
    ]
    sequence_gap, arrival_stall = packet_incidents
    assert sequence_gap["confidence"] == "high"
    assert sequence_gap["summary"] == (
        "1 RTP packet absent between observed sequence numbers"
    )
    assert sequence_gap["observed"]["missing_packet_count"] == 1
    assert sequence_gap["expected"]["capture_point_continuity"] == (
        "not independently verified"
    )
    assert sequence_gap["evidence_refs"] == [sequence_event["event_id"]]
    assert arrival_stall["confidence"] == "medium"
    assert arrival_stall["observed"]["sequence_delta"] == 1
    assert arrival_stall["observed"]["arrival_gap_ms"] == pytest.approx(140)
    assert arrival_stall["observed"]["media_advance_ms"] == pytest.approx(20)
    assert arrival_stall["observed"]["excess_arrival_delay_ms"] == pytest.approx(
        120
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sequence_number", 65_536, "16 bits"),
        ("rtp_timestamp", -1, "32 bits"),
        ("payload_type", 128, "7 bits"),
        ("clock_rate_hz", 0, "between 1 and 384000"),
        ("direction", "sideways", "received or sent"),
        ("clock_domain", "https://clock.invalid", "safe alias"),
        ("alignment_uncertainty_ms", float("nan"), "finite and non-negative"),
    ],
)
def test_rtp_packet_rejects_invalid_header_values(
    field: str,
    value: object,
    message: str,
) -> None:
    values: dict[str, object] = {
        "stream_alias": "caller-audio",
        "direction": "received",
        "sequence_number": 1,
        "rtp_timestamp": 160,
        "payload_type": 0,
        "clock_rate_hz": 8_000,
    }
    values[field] = value
    with pytest.raises(ValueError, match=message):
        RtpPacket(**values)  # type: ignore[arg-type]


def test_rtp_datagram_decoder_discards_payload_and_ssrc() -> None:
    datagram = (
        bytes((0x80, 0x80))
        + (321).to_bytes(2, byteorder="big")
        + (654_321).to_bytes(4, byteorder="big")
        + b"\xde\xad\xbe\xef"
        + b"raw-media-must-not-be-stored"
    )

    packet = rtp_packet_from_datagram(
        datagram,
        stream_alias="caller-audio",
        direction="received",
        clock_rate_hz=8_000,
    )

    assert packet.sequence_number == 321
    assert packet.rtp_timestamp == 654_321
    assert packet.payload_type == 0
    assert packet.marker is True
    assert "raw-media" not in repr(packet)
    assert "deadbeef" not in repr(packet).lower()

    with pytest.raises(ValueError, match="12-byte fixed header"):
        rtp_packet_from_datagram(
            b"\x80\x00",
            stream_alias="caller-audio",
            direction="received",
            clock_rate_hz=8_000,
        )
    with pytest.raises(ValueError, match="version must be 2"):
        rtp_packet_from_datagram(
            b"\x40" + (b"\x00" * 11),
            stream_alias="caller-audio",
            direction="received",
            clock_rate_hz=8_000,
        )


def test_rtp_packet_tap_adapter_reports_bounded_capture_health() -> None:
    transport = RecordingTransport()
    observer = VoxBenchObserver("run-1", transport)
    t0 = datetime.fromisoformat("2026-07-25T12:00:00+00:00")
    tap = RtpPacketTapAdapter(
        observer,
        stream_alias="caller-audio",
        direction="received",
        clock_rate_hz=8_000,
        alignment_uncertainty_ms=2.5,
        capture_drop_counter_supported=True,
        started_at=t0,
    )

    verified = tap.snapshot(ts=t0 + timedelta(milliseconds=20))
    assert isinstance(verified, RtpCaptureHealthSnapshot)
    assert verified.continuity == "verified"
    packet = tap.observe_datagram(
        _rtp_datagram(10, 1_000),
        ts=t0 + timedelta(milliseconds=40),
    )
    assert packet.sequence_number == 10
    assert packet.alignment_uncertainty_ms == 2.5
    with pytest.raises(ValueError, match="version must be 2"):
        tap.observe_datagram(
            b"\x40" + (b"\x00" * 11),
            ts=t0 + timedelta(milliseconds=50),
        )
    tap.record_capture_drop(2)
    health = tap.report_health(ts=t0 + timedelta(milliseconds=100))

    assert health.observed_packet_count == 1
    assert health.capture_drop_count == 2
    assert health.decode_error_count == 1
    assert health.capture_drop_counter_supported is True
    assert health.continuity == "compromised"
    assert health.window_duration_ms == 100
    assert observer.flush() == 2

    packet_event, health_event = transport.batches[0].timeline_events
    assert packet_event.name == "rtp.packet_arrived"
    assert packet_event.alignment_uncertainty_ms == 2.5
    assert health_event.name == "rtp.capture_health_reported"
    assert health_event.attributes == {
        "observed_packet_count": 1,
        "capture_drop_count": 2,
        "decode_error_count": 1,
        "capture_drop_counter_supported": True,
        "capture_point_continuity": "compromised",
        "window_duration_ms": 100,
    }
    assert "transient-media" not in repr(transport.batches[0])
    assert tap.snapshot(ts=t0 + timedelta(milliseconds=120)).observed_packet_count == 0
    sent_tap = RtpPacketTapAdapter(
        observer,
        stream_alias="assistant-audio",
        direction="sent",
        clock_rate_hz=8_000,
        started_at=t0,
    )
    sent_tap.report_health(ts=t0 + timedelta(milliseconds=120))
    assert observer.flush() == 1
    assert transport.batches[1].timeline_events[0].event_id == "rtp-capture-health:1"


def test_capture_drop_downgrades_rtp_gap_claim_on_common_timeline(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = client.post("/runs/observed", json=_observed_run_payload()).json()["run_id"]
    t0 = datetime.fromisoformat(client.get(f"/runs/{run_id}/timeline").json()["t0"])
    observer = VoxBenchObserver(run_id, ApiTestTransport(client))
    tap = RtpPacketTapAdapter(
        observer,
        stream_alias="caller-audio",
        direction="received",
        clock_rate_hz=8_000,
        alignment_uncertainty_ms=3.0,
        capture_drop_counter_supported=True,
        started_at=t0,
    )
    tap.observe_datagram(
        _rtp_datagram(10, 1_000),
        ts=t0 + timedelta(milliseconds=100),
    )
    tap.observe_datagram(
        _rtp_datagram(12, 1_320),
        ts=t0 + timedelta(milliseconds=140),
    )
    tap.record_capture_drop()
    health = tap.report_health(ts=t0 + timedelta(milliseconds=200))

    assert health.continuity == "compromised"
    assert observer.flush() == 3
    lanes = client.get(f"/runs/{run_id}/timeline").json()["lanes"]
    health_event = next(
        event
        for event in lanes["events"]
        if event["name"] == "rtp.capture_health_reported"
    )
    assert health_event["alignment_uncertainty_ms"] == 3
    sequence_event = next(
        event
        for event in lanes["events"]
        if event["name"] == "rtp.sequence_gap_observed"
    )
    assert sequence_event["attributes"]["capture_point_continuity"] == "compromised"
    assert sequence_event["attributes"]["capture_drop_count"] == 1
    assert sequence_event["alignment_uncertainty_ms"] == 3

    incident = next(
        incident
        for incident in lanes["incidents"]
        if incident["rule_id"] == "rtp_sequence_gap_capture_ambiguous_v1"
    )
    assert incident["title"] == "RTP sequence gap may be capture loss"
    assert incident["confidence"] == "low"
    assert incident["observed"]["capture_decode_error_count"] == 0
    assert incident["expected"]["capture_point_continuity"] == "compromised"
    assert incident["expected"]["network_loss_confirmation"] == (
        "restore capture continuity before attributing the gap to the network"
    )
    assert incident["evidence_refs"] == [
        sequence_event["event_id"],
        health_event["event_id"],
    ]


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
