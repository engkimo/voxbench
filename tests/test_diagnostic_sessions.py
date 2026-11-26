from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.observability import (
    ObservationBatch,
    ObservationTransport,
    RtpPacket,
    VoxBenchObserver,
)

ROOT = Path(__file__).resolve().parents[1]


class _ApiTestTransport(ObservationTransport):
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def send(self, batch: ObservationBatch) -> None:
        response = self.client.post("/v1/observations", json=batch.to_payload())
        response.raise_for_status()


def _json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def _new_timing_run(client: TestClient) -> tuple[str, VoxBenchObserver, datetime]:
    config = _json("examples/configs/live-demo-openai-realtime.json")
    response = client.post(
        "/runs/observed",
        json={
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
            "call_id": "diagnostic-timing-test",
        },
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    t0 = datetime.fromisoformat(client.get(f"/runs/{run_id}/timeline").json()["t0"])
    return run_id, VoxBenchObserver(run_id, _ApiTestTransport(client)), t0


def _create_timing_run(
    client: TestClient,
    *,
    send_clock_domain: str = "control_plane_wall",
    packet_offsets_ms: tuple[int, ...] = (100, 120, 520),
    loop_delays: tuple[tuple[int, int], ...] = ((550, 400),),
) -> str:
    run_id, observer, t0 = _new_timing_run(client)
    _append_timing_observations(
        observer,
        t0,
        stream_alias="handset",
        packet_offsets_ms=packet_offsets_ms,
        loop_delays=loop_delays,
        send_clock_domain=send_clock_domain,
    )
    return run_id


def _append_timing_observations(
    observer: VoxBenchObserver,
    t0: datetime,
    *,
    stream_alias: str,
    packet_offsets_ms: tuple[int, ...],
    loop_delays: tuple[tuple[int, int], ...] = (),
    send_clock_domain: str = "control_plane_wall",
    first_sequence: int = 1,
) -> None:
    for sequence, offset_ms in enumerate(packet_offsets_ms, start=first_sequence):
        observer.observe_rtp_packet(
            RtpPacket(
                stream_alias=stream_alias,
                direction="sent",
                sequence_number=sequence,
                rtp_timestamp=offset_ms * 8,
                payload_type=0,
                clock_rate_hz=8_000,
                clock_domain=send_clock_domain,
                alignment_uncertainty_ms=2,
                ts=t0 + timedelta(milliseconds=offset_ms),
            )
        )
    for end_ms, delay_ms in loop_delays:
        observer.observe_metric(
            "loop_lag",
            delay_ms,
            ts=t0 + timedelta(milliseconds=end_ms),
        )
    assert observer.flush() == len(packet_offsets_ms) + len(loop_delays)


def _create_run(client: TestClient) -> str:
    response = client.post(
        "/runs/live-demo/simulated",
        json={
            "provider": "gemini-live",
            "scenario": "rtp-gap",
            "duration_ms": 3000,
        },
    )
    assert response.status_code == 200
    return response.json()["run_id"]


def _sse_payloads(response_text: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def test_deterministic_diagnostic_session_streams_resolved_ui_command(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_run(client)

    created = client.post(
        f"/runs/{run_id}/diagnostic-sessions",
        json={"message": "Guide me to the strongest evidence."},
    )

    assert created.status_code == 201
    session = created.json()
    assert session["run_id"] == run_id
    assert session["adapter"] == "deterministic-local"
    assert session["command_count"] == 1
    assert session["acknowledged_command_count"] == 0

    events_response = client.get(f"/diagnostic-sessions/{session['session_id']}/events")
    assert events_response.status_code == 200
    assert events_response.headers["content-type"].startswith("text/event-stream")
    payloads = _sse_payloads(events_response.text)
    assert [payload["type"] for payload in payloads] == [
        "status",
        "evidence_found",
        "answer_delta",
        "ui_command_proposed",
        "ui_command",
        "completed",
    ]
    command = next(
        payload["payload"]["command"] for payload in payloads if payload["type"] == "ui_command"
    )
    assert command["status"] == "resolved"
    assert command["target"]["run_id"] == run_id
    assert command["type"] == "select_incident"


def test_diagnostic_session_guides_rtp_send_loop_overlap(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_timing_run(client)
    timeline = client.get(f"/runs/{run_id}/timeline").json()
    candidate = next(
        item
        for item in timeline["lanes"]["incidents"]
        if item["rule_id"] == "rtp_send_loop_overlap_v1"
    )

    created = client.post(
        f"/runs/{run_id}/diagnostic-sessions",
        json={"message": "携帯向けRTPの送出タイミングと音切れ候補を見せて"},
    )
    payloads = _sse_payloads(
        client.get(f"/diagnostic-sessions/{created.json()['session_id']}/events").text
    )
    evidence = next(item["payload"] for item in payloads if item["type"] == "evidence_found")
    answer = next(item["payload"]["text"] for item in payloads if item["type"] == "answer_delta")
    command = next(item["payload"]["command"] for item in payloads if item["type"] == "ui_command")

    assert created.status_code == 201
    assert candidate["direction"] == "sent"
    assert candidate["observed"] == {
        "clock_domain": "control_plane_wall",
        "stream_alias": "handset",
        "send_series_id": evidence["send"]["series_id"],
        "loop_series_id": evidence["loop_delay"]["series_id"],
        "send_start_ms": 120,
        "send_end_ms": 520,
        "send_interval_ms": 400,
        "loop_start_ms": 150,
        "loop_end_ms": 550,
        "loop_delay_ms": 400,
        "overlap_start_ms": 150,
        "overlap_end_ms": 520,
        "overlap_ms": 370,
        "long_gap_count": 1,
        "overlapping_gap_count": 1,
        "remote_playout_observed": False,
    }
    assert evidence["classification"] == "derived"
    assert evidence["evidence_refs"] == candidate["evidence_refs"]
    assert evidence["evidence_refs"] == [
        evidence["send"]["series_id"],
        evidence["loop_delay"]["series_id"],
    ]
    assert all(
        ref in {series["series_id"] for series in timeline["lanes"]["series"]}
        for ref in evidence["evidence_refs"]
    )
    assert evidence["scope"] == "server-side timing candidate"
    assert evidence["remote_audibility"] == "not observed"
    assert "0.120-0.520 s" in answer
    assert "0.150-0.550 s" in answer
    assert "370.00 ms" in answer
    assert command["type"] == "set_time_window"
    assert command["target"]["run_id"] == run_id
    assert command["target"]["evidence_ref"] == candidate["incident_id"]
    assert command["target"]["category"] == "transport"
    assert command["target"]["direction"] == "sent"
    assert command["target"]["focus_ms"] == 120
    assert command["target"]["start_ms"] == 0
    assert command["target"]["end_ms"] == 550

    for unrelated_message in (
        "受信側のRTP到着停滞を見せて",
        "受信側RTPの音切れを調べて",
        "Show inbound RTP stutter.",
        "Show the strongest RTP evidence.",
    ):
        unrelated = client.post(
            f"/runs/{run_id}/diagnostic-sessions",
            json={"message": unrelated_message},
        )
        unrelated_payloads = _sse_payloads(
            client.get(f"/diagnostic-sessions/{unrelated.json()['session_id']}/events").text
        )
        unrelated_command = next(
            item["payload"]["command"]
            for item in unrelated_payloads
            if item["type"] == "ui_command"
        )
        assert unrelated_command["target"]["evidence_ref"] != candidate["incident_id"]

    dropout = client.post(
        f"/runs/{run_id}/diagnostic-sessions",
        json={"message": "音が途切れてプツプツした箇所を見せて"},
    )
    dropout_payloads = _sse_payloads(
        client.get(f"/diagnostic-sessions/{dropout.json()['session_id']}/events").text
    )
    dropout_command = next(
        item["payload"]["command"] for item in dropout_payloads if item["type"] == "ui_command"
    )
    assert dropout_command["target"]["evidence_ref"] == candidate["incident_id"]


def test_diagnostic_overlap_checks_later_delay_with_earlier_start(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_timing_run(
        client,
        packet_offsets_ms=(1_000, 1_800),
        loop_delays=((2_000, 100), (2_100, 1_000)),
    )
    candidate = next(
        item
        for item in client.get(f"/runs/{run_id}/timeline").json()["lanes"]["incidents"]
        if item["rule_id"] == "rtp_send_loop_overlap_v1"
    )

    assert candidate["observed"]["send_start_ms"] == 1_000
    assert candidate["observed"]["send_end_ms"] == 1_800
    assert candidate["observed"]["loop_start_ms"] == 1_100
    assert candidate["observed"]["loop_end_ms"] == 2_100
    assert candidate["observed"]["overlap_ms"] == 700


def test_timing_reference_identifies_one_candidate_as_observations_arrive(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id, observer, t0 = _new_timing_run(client)
    _append_timing_observations(
        observer,
        t0,
        stream_alias="handset",
        packet_offsets_ms=(100, 120, 520),
        loop_delays=((550, 400),),
    )

    def candidate_ids() -> dict[str, str]:
        lanes = client.get(f"/runs/{run_id}/timeline").json()["lanes"]
        return {
            item["observed"]["stream_alias"]: item["incident_id"]
            for item in lanes["incidents"]
            if item["rule_id"] == "rtp_send_loop_overlap_v1"
        }

    original_ref = candidate_ids()["handset"]
    command = {
        "type": "set_time_window",
        "run_id": run_id,
        "command_id": str(uuid4()),
        "evidence_ref": original_ref,
    }
    resolve_url = f"/runs/{run_id}/ui-commands/resolve"
    original = client.post(resolve_url, json=command).json()["target"]

    # The new stream sorts first and produces an equal-ranked candidate.
    _append_timing_observations(observer, t0, stream_alias="desk", packet_offsets_ms=(120, 520))
    ids = candidate_ids()
    assert ids["handset"] == original_ref
    assert ids["desk"] != original_ref
    assert client.post(resolve_url, json=command).json()["target"] == original
    session = client.post(
        f"/runs/{run_id}/diagnostic-sessions", json={"message": "Show outbound RTP gaps"}
    ).json()
    events = _sse_payloads(client.get(f"/diagnostic-sessions/{session['session_id']}/events").text)
    selected = next(
        event["payload"]["command"] for event in events if event["type"] == "ui_command"
    )
    assert selected["target"]["incident_id"] == original_ref

    # Replacing the handset's strongest window must invalidate the old target.
    _append_timing_observations(
        observer,
        t0,
        stream_alias="handset",
        packet_offsets_ms=(1200,),
        loop_delays=((1210, 640),),
        first_sequence=4,
    )
    updated = candidate_ids()
    assert updated["desk"] == ids["desk"]
    assert updated["handset"] != original_ref
    assert client.post(resolve_url, json=command).status_code == 422


def test_diagnostic_selects_longest_matching_gap_and_counts_each_gap_once(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_timing_run(
        client,
        packet_offsets_ms=(100, 400, 900, 1020),
        loop_delays=((390, 230), (440, 80), (920, 160), (1050, 90)),
    )
    candidates = [
        item
        for item in client.get(f"/runs/{run_id}/timeline").json()["lanes"]["incidents"]
        if item["rule_id"] == "rtp_send_loop_overlap_v1"
    ]
    assert len(candidates) == 1
    observed = candidates[0]["observed"]
    assert observed["long_gap_count"] == observed["overlapping_gap_count"] == 3
    assert observed["send_interval_ms"] == 500
    assert observed["overlap_ms"] == 140


def test_diagnostic_window_clamps_focus_to_run_start(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_timing_run(client, packet_offsets_ms=(10, 110), loop_delays=((120, 135),))
    session = client.post(
        f"/runs/{run_id}/diagnostic-sessions", json={"message": "Show downstream RTP gaps"}
    ).json()
    events = _sse_payloads(client.get(f"/diagnostic-sessions/{session['session_id']}/events").text)
    evidence = next(event["payload"] for event in events if event["type"] == "evidence_found")
    command = next(event["payload"]["command"] for event in events if event["type"] == "ui_command")
    assert evidence["loop_delay"]["start_ms"] == -15
    assert command["type"] == "set_time_window"
    assert command["target"]["start_ms"] == command["target"]["focus_ms"] == 0
    assert command["target"]["end_ms"] == 120


@pytest.mark.parametrize(
    ("offsets", "delays"),
    [
        ((80, 179), ((179, 80),)),  # short send interval
        ((80, 180), ((180, 49),)),  # short loop delay
        ((80, 180), ((211, 50),)),  # less than the required overlap
    ],
)
def test_diagnostic_reports_unknown_for_subthreshold_evidence(
    tmp_path: Path,
    offsets: tuple[int, ...],
    delays: tuple[tuple[int, int], ...],
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_timing_run(client, packet_offsets_ms=offsets, loop_delays=delays)
    session = client.post(
        f"/runs/{run_id}/diagnostic-sessions", json={"message": "Show outbound RTP gaps"}
    ).json()
    events = _sse_payloads(client.get(f"/diagnostic-sessions/{session['session_id']}/events").text)
    evidence = next(event["payload"] for event in events if event["type"] == "evidence_found")
    command = next(event["payload"]["command"] for event in events if event["type"] == "ui_command")
    assert evidence["classification"] == "unknown"
    assert evidence["evidence_refs"] == []
    assert command["target"]["panel"] == "transport"


def test_diagnostic_session_does_not_correlate_different_clocks(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_timing_run(client, send_clock_domain="separate_clock")
    timeline = client.get(f"/runs/{run_id}/timeline").json()

    created = client.post(
        f"/runs/{run_id}/diagnostic-sessions",
        json={"message": "Show the outbound RTP packet timing gap."},
    )
    payloads = _sse_payloads(
        client.get(f"/diagnostic-sessions/{created.json()['session_id']}/events").text
    )
    evidence = next(item["payload"] for item in payloads if item["type"] == "evidence_found")
    command = next(item["payload"]["command"] for item in payloads if item["type"] == "ui_command")

    assert not any(
        item["rule_id"] == "rtp_send_loop_overlap_v1" for item in timeline["lanes"]["incidents"]
    )
    assert evidence["classification"] == "unknown"
    assert evidence["evidence_refs"] == []
    assert command["type"] == "open_panel"
    assert command["target"]["panel"] == "transport"


def test_diagnostic_command_acknowledgement_is_idempotent_and_replayable(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_run(client)
    session = client.post(
        f"/runs/{run_id}/diagnostic-sessions",
        json={},
    ).json()
    initial_events = _sse_payloads(
        client.get(f"/diagnostic-sessions/{session['session_id']}/events").text
    )
    command = next(
        payload["payload"]["command"]
        for payload in initial_events
        if payload["type"] == "ui_command"
    )
    result = {
        "command_id": command["command_id"],
        "outcome": "applied",
        "detail": "Selected the incident.",
    }

    acknowledged = client.post(
        f"/diagnostic-sessions/{session['session_id']}/ui-command-results",
        json=result,
    )
    duplicate = client.post(
        f"/diagnostic-sessions/{session['session_id']}/ui-command-results",
        json=result,
    )
    conflict = client.post(
        f"/diagnostic-sessions/{session['session_id']}/ui-command-results",
        json={**result, "outcome": "blocked"},
    )

    assert acknowledged.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json() == acknowledged.json()
    assert conflict.status_code == 409
    snapshot = client.get(f"/diagnostic-sessions/{session['session_id']}").json()
    assert snapshot["acknowledged_command_count"] == 1
    replay = _sse_payloads(
        client.get(f"/diagnostic-sessions/{session['session_id']}/events?after=6").text
    )
    assert [payload["type"] for payload in replay] == ["ui_command_result"]
    assert replay[0]["payload"]["command_id"] == command["command_id"]


def test_diagnostic_session_rejects_unknown_scope_and_bad_resume(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_run(client)
    session = client.post(
        f"/runs/{run_id}/diagnostic-sessions",
        json={},
    ).json()

    unknown_run = client.post(
        f"/runs/{uuid4()}/diagnostic-sessions",
        json={},
    )
    unknown_command = client.post(
        f"/diagnostic-sessions/{session['session_id']}/ui-command-results",
        json={
            "command_id": str(uuid4()),
            "outcome": "applied",
            "detail": "Not part of this session.",
        },
    )
    bad_resume = client.get(
        f"/diagnostic-sessions/{session['session_id']}/events",
        headers={"Last-Event-ID": "not-an-ordinal"},
    )

    assert unknown_run.status_code == 404
    assert unknown_command.status_code == 422
    assert bad_resume.status_code == 400
    negative_resume = client.get(
        f"/diagnostic-sessions/{session['session_id']}/events",
        headers={"Last-Event-ID": "-1"},
    )
    assert negative_resume.status_code == 400
