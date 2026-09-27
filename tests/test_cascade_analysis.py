from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from voxbench.control_plane.app import create_app
from voxbench.control_plane.models import Base
from voxbench.control_plane.run_api import PostgresRunRepository
from voxbench.observability import ServiceEvent
from voxbench.registry.service import load_json

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "examples/v2"


def _run_payload(*, latency_slos: list[dict] | None = None) -> dict:
    config = load_json(V2 / "configs/cascade.json")
    if latency_slos is not None:
        config["spec"]["observability"] = {"service_latency_slos": latency_slos}
    return {
        "config_name": config["meta"]["name"],
        "configs": [config],
        "manifests": [
            load_json(filename)
            for filename in sorted((V2 / "manifests").glob("*.json"))
        ],
        "call_id": "cascade-analysis",
    }


def _start(
    client: TestClient,
    *,
    latency_slos: list[dict] | None = None,
) -> tuple[str, dict]:
    response = client.post(
        "/runs/observed",
        json=_run_payload(latency_slos=latency_slos),
    )
    assert response.status_code == 200, response.text
    return response.json()["run_id"], response.json()


def _event(
    kind: str,
    component_id: str,
    role: str,
    alias: str,
    at_ms: int,
    *,
    epoch: int = 0,
    clock_domain: str = "application-monotonic-a",
    uncertainty_ms: float | None = 1.0,
    attributes: dict | None = None,
    **relations,
) -> ServiceEvent:
    return ServiceEvent(
        collector_alias="analysis-fixture",
        event_alias=alias,
        kind=kind,
        component_id=component_id,
        role=role,
        generation_epoch=epoch,
        ts=datetime(2026, 9, 27, 2, tzinfo=UTC) + timedelta(milliseconds=at_ms),
        clock_domain=clock_domain,
        alignment_uncertainty_ms=uncertainty_ms,
        attributes=attributes or {},
        **relations,
    )


def _complete_path(
    prefix: str = "one",
    *,
    epoch: int = 0,
    include_terminals: bool = True,
) -> list[ServiceEvent]:
    turn = f"turn-{prefix}"
    stt = f"stt-{prefix}"
    llm = f"llm-{prefix}"
    response = f"response-{prefix}"
    events = [
        _event(
            "turn.speech_ended",
            "application",
            "coordinator",
            f"{prefix}-speech-end",
            100,
            epoch=epoch,
            turn_alias=turn,
            request_alias=stt,
        ),
        _event(
            "stt.final_emitted",
            "stt-main",
            "stt",
            f"{prefix}-stt-final",
            140,
            epoch=epoch,
            session_alias="stt-session",
            request_alias=stt,
        ),
        _event(
            "turn.committed",
            "application",
            "coordinator",
            f"{prefix}-turn",
            150,
            epoch=epoch,
            turn_alias=turn,
            parent_request_alias=stt,
        ),
        _event(
            "llm.request_started",
            "llm-main",
            "llm",
            f"{prefix}-llm-start",
            160,
            epoch=epoch,
            turn_alias=turn,
            request_alias=llm,
            parent_request_alias=stt,
            response_alias=response,
        ),
        _event(
            "llm.first_output",
            "llm-main",
            "llm",
            f"{prefix}-llm-output",
            180,
            epoch=epoch,
            turn_alias=turn,
            request_alias=llm,
            response_alias=response,
            attributes={"output_kind": "reasoning"},
        ),
        _event(
            "llm.first_answer_text",
            "llm-main",
            "llm",
            f"{prefix}-llm-answer",
            200,
            epoch=epoch,
            turn_alias=turn,
            request_alias=llm,
            response_alias=response,
        ),
    ]
    segment_timings = ((0, 220, 230, 260, 280), (1, 240, 245, 275, 300))
    for ordinal, ready_ms, start_ms, pcm_ms, write_ms in segment_timings:
        segment = f"segment-{prefix}-{ordinal}"
        aggregation = f"aggregation-{prefix}-{ordinal}"
        tts = f"tts-{prefix}-{ordinal}"
        events.extend(
            [
                _event(
                    "aggregation.segment_ready",
                    "aggregation-main",
                    "aggregation",
                    f"{prefix}-ready-{ordinal}",
                    ready_ms,
                    epoch=epoch,
                    request_alias=aggregation,
                    parent_request_alias=llm,
                    response_alias=response,
                    segment_alias=segment,
                    attributes={"sequence_ordinal": ordinal},
                ),
                _event(
                    "tts.request_started",
                    "tts-main",
                    "tts",
                    f"{prefix}-tts-start-{ordinal}",
                    start_ms,
                    epoch=epoch,
                    request_alias=tts,
                    parent_request_alias=aggregation,
                    response_alias=response,
                    segment_alias=segment,
                ),
                _event(
                    "tts.first_pcm",
                    "tts-main",
                    "tts",
                    f"{prefix}-tts-pcm-{ordinal}",
                    pcm_ms,
                    epoch=epoch,
                    request_alias=tts,
                    response_alias=response,
                    segment_alias=segment,
                ),
                _event(
                    "playback.write_started",
                    "output-resampler",
                    "playback",
                    f"{prefix}-write-{ordinal}",
                    write_ms,
                    epoch=epoch,
                    parent_request_alias=tts,
                    response_alias=response,
                    segment_alias=segment,
                ),
            ]
        )
        if include_terminals:
            events.extend(
                [
                    _event(
                        "tts.response_completed",
                        "tts-main",
                        "tts",
                        f"{prefix}-tts-done-{ordinal}",
                        pcm_ms + 80,
                        epoch=epoch,
                        request_alias=tts,
                        response_alias=response,
                        segment_alias=segment,
                        attributes={"terminal_outcome": "completed"},
                    ),
                    _event(
                        "playback.write_completed",
                        "output-resampler",
                        "playback",
                        f"{prefix}-write-done-{ordinal}",
                        write_ms + 100,
                        epoch=epoch,
                        parent_request_alias=tts,
                        response_alias=response,
                        segment_alias=segment,
                        attributes={"audio_duration_ms": 100},
                    ),
                ]
            )
    if include_terminals:
        events.append(
            _event(
                "llm.response_completed",
                "llm-main",
                "llm",
                f"{prefix}-llm-done",
                360,
                epoch=epoch,
                turn_alias=turn,
                request_alias=llm,
                response_alias=response,
                attributes={"terminal_outcome": "completed"},
            )
        )
    return events


def _ingest(client: TestClient, run_id: str, events: list[ServiceEvent]) -> None:
    response = client.post(
        "/v1/observations",
        json={
            "run_id": run_id,
            "service_events": [event.model_dump(mode="json") for event in events],
        },
    )
    assert response.status_code == 200, response.text


def _measurements(turn: dict) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for measurement in turn["measurements"]:
        result.setdefault(measurement["name"], []).append(measurement)
    return result


def test_overlapping_multisegment_path_uses_correlated_boundaries_without_double_count(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id, _ = _start(client)
    _ingest(client, run_id, _complete_path())

    timeline = client.get(f"/runs/{run_id}/timeline").json()
    assert len(timeline["lanes"]["turns"]) == 1
    turn = timeline["lanes"]["turns"][0]
    measurements = _measurements(turn)
    assert measurements["llm_first_output_wait"][0]["duration_ms"] == 20
    assert measurements["llm_first_answer_text_wait"][0]["duration_ms"] == 40
    assert [item["duration_ms"] for item in measurements["tts_first_audio_wait"]] == [30, 30]
    end_to_end = measurements["end_to_end_local_response_wait"][0]
    assert end_to_end["duration_ms"] == 180
    assert end_to_end["end_event_ref"] == "service:analysis-fixture:one-write-0"
    assert turn["critical_path"] == {
        "status": "complete",
        "measurement_ids": turn["critical_path"]["measurement_ids"],
        "end_to_end_measurement_id": end_to_end["measurement_id"],
        "covered_duration_ms": 180.0,
        "unexplained_duration_ms": 0.0,
        "reason_alias": None,
    }
    assert turn["completion_observed"] is True
    assert all(
        item["definition_version"] == "cascade-latency/v1"
        for item in turn["measurements"]
    )
    assert all(
        item["evidence_refs"]
        for item in turn["measurements"]
        if item["status"] == "observed"
    )

    derived = [
        interval
        for interval in timeline["lanes"]["intervals"]
        if interval["source"] == "cascade_causal_analysis_v1"
    ]
    assert any(item["name"] == "cascade.end_to_end_local_response_wait" for item in derived)
    assert not any("response_completed" in item["name"] for item in derived)
    assert not any(
        item["rule_id"].startswith("cascade_latency")
        for item in timeline["lanes"]["incidents"]
    )


def test_missing_speech_end_and_unknown_parent_are_explicitly_unobserved(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id, _ = _start(client)
    events = [event for event in _complete_path() if event.kind != "turn.speech_ended"]
    llm_index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "llm.request_started"
    )
    events[llm_index] = events[llm_index].model_copy(
        update={"parent_request_alias": "stt-parent-never-observed"}
    )
    _ingest(client, run_id, events)

    turn = client.get(f"/runs/{run_id}/timeline").json()["lanes"]["turns"][0]
    measurements = _measurements(turn)
    assert measurements["stt_finalization_wait"][0]["status"] == "unobserved"
    assert measurements["end_to_end_local_response_wait"][0]["status"] == "unobserved"
    assert turn["critical_path"]["status"] == "incomplete"
    assert any("parent_request" in item for item in turn["coverage"]["unresolved_relations"])
    assert turn["coverage"]["remote_playout_observed"] is False


def test_uncalibrated_cross_domain_latency_is_indeterminate(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id, _ = _start(client)
    events = _complete_path()
    index = next(
        index
        for index, event in enumerate(events)
        if event.kind == "llm.first_answer_text"
    )
    events[index] = events[index].model_copy(update={"clock_domain": "provider-wall-b"})
    _ingest(client, run_id, events)

    turn = client.get(f"/runs/{run_id}/timeline").json()["lanes"]["turns"][0]
    answer = _measurements(turn)["llm_first_answer_text_wait"][0]
    assert answer["status"] == "indeterminate"
    assert answer["duration_ms"] is None
    assert answer["reason_alias"] == "clock_domain_mismatch"
    assert answer["evidence_refs"] == [
        "service:analysis-fixture:one-llm-start",
        "service:analysis-fixture:one-llm-answer",
    ]
    assert turn["critical_path"]["status"] == "indeterminate"


def test_negative_endpoint_order_is_indeterminate_not_zero(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id, _ = _start(client)
    events = _complete_path()
    index = next(index for index, event in enumerate(events) if event.kind == "tts.first_pcm")
    events[index] = events[index].model_copy(
        update={"ts": datetime(2026, 9, 27, 2, tzinfo=UTC) + timedelta(milliseconds=225)}
    )
    _ingest(client, run_id, events)

    turn = client.get(f"/runs/{run_id}/timeline").json()["lanes"]["turns"][0]
    first_audio = _measurements(turn)["tts_first_audio_wait"][0]
    assert first_audio["status"] == "indeterminate"
    assert first_audio["reason_alias"] == "end_precedes_start"
    assert first_audio["duration_ms"] is None


def test_new_epoch_does_not_reuse_old_response_playback(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id, _ = _start(client)
    old_events = _complete_path("old", epoch=0)
    new_events = [
        event
        for event in _complete_path("new", epoch=1, include_terminals=False)
        if not event.kind.startswith("aggregation.")
        and not event.kind.startswith("tts.")
        and not event.kind.startswith("playback.")
    ]
    _ingest(client, run_id, old_events + new_events)

    turns = client.get(f"/runs/{run_id}/timeline").json()["lanes"]["turns"]
    new_turn = next(item for item in turns if item["generation_epoch"] == 1)
    end_to_end = _measurements(new_turn)["end_to_end_local_response_wait"][0]
    assert end_to_end["status"] == "unobserved"
    assert end_to_end["end_event_ref"] is None
    assert new_turn["critical_path"]["status"] == "incomplete"


def test_tool_continuation_is_a_separate_operation_with_explicit_parent_chain(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id, _ = _start(client)
    first_operation = _complete_path("main")
    continuation: list[ServiceEvent] = []
    for event in _complete_path("continuation"):
        if event.kind in {"turn.speech_ended", "stt.final_emitted", "turn.committed"}:
            continue
        update: dict = {"ts": event.ts + timedelta(milliseconds=500)}
        if event.turn_alias == "turn-continuation":
            update["turn_alias"] = "turn-main"
        if event.kind == "llm.request_started":
            update["parent_request_alias"] = "llm-main"
        continuation.append(event.model_copy(update=update))
    _ingest(client, run_id, first_operation + continuation)

    turns = client.get(f"/runs/{run_id}/timeline").json()["lanes"]["turns"]
    assert [item["operation_request_alias"] for item in turns] == [
        "llm-continuation",
        "llm-main",
    ]
    continued = next(
        item for item in turns if item["operation_request_alias"] == "llm-continuation"
    )
    end_to_end = _measurements(continued)["end_to_end_local_response_wait"][0]
    assert end_to_end["status"] == "observed"
    assert end_to_end["end_event_ref"] == (
        "service:analysis-fixture:continuation-write-0"
    )
    assert end_to_end["duration_ms"] == 680


def test_safe_ai_projection_is_consistent_and_excludes_component_params(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id, created = _start(client)
    expected_roles = ["stt", "llm", "aggregation", "tts"]
    assert created["ai_mode"] == "cascade"
    assert [item["role"] for item in created["ai_components"]] == expected_roles
    encoded = json.dumps(created["ai_components"])
    assert "params" not in encoded
    assert "system_prompt_ref" not in encoded
    assert "tools" not in encoded

    recent = client.get("/runs").json()[0]
    live = client.get("/runs/live-preview").json()[0]
    timeline = client.get(f"/runs/{run_id}/timeline").json()
    for projection in (recent, live, timeline):
        assert projection["ai_mode"] == "cascade"
        assert projection["ai_components"] == created["ai_components"]


def test_latency_incident_requires_and_cites_explicit_slo(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id, _ = _start(
        client,
        latency_slos=[{
            "id": "local-response",
            "measurement": "end_to_end_local_response_wait",
            "max_ms": 150,
        }],
    )
    _ingest(client, run_id, _complete_path())
    timeline = client.get(f"/runs/{run_id}/timeline").json()
    incident = next(
        item
        for item in timeline["lanes"]["incidents"]
        if item["rule_id"] == "cascade_latency_slo_v1"
    )
    assert incident["observed"]["duration_ms"] == 180
    assert incident["expected"] == {
        "service_latency_slo_id": "local-response",
        "duration_ms_at_or_below": 150,
    }
    assert incident["evidence_refs"] == [
        "service:analysis-fixture:one-speech-end",
        "service:analysis-fixture:one-write-0",
    ]
    interval = next(
        item
        for item in timeline["lanes"]["intervals"]
        if item["name"] == "cascade.end_to_end_local_response_wait"
    )
    assert interval["attributes"]["latency_threshold_status"] == "exceeded"
    assert interval["attributes"]["latency_slo_id"] == "local-response"


def test_sql_repository_reload_reconstructs_identical_causal_analysis(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    repository = PostgresRunRepository(sessions)
    first = TestClient(create_app(artifact_root=tmp_path, repository=repository))
    run_id, _ = _start(first)
    _ingest(first, run_id, _complete_path())
    before = first.get(f"/runs/{run_id}/timeline").json()["lanes"]["turns"]

    restored = PostgresRunRepository(sessions).get(run_id)
    assert restored is not None
    after = [item.model_dump(mode="json") for item in restored.to_timeline().lanes.turns]
    assert after == before


def test_output_kind_is_closed_metadata_and_not_answer_content() -> None:
    valid = _event(
        "llm.first_output",
        "llm-main",
        "llm",
        "reasoning",
        10,
        turn_alias="turn",
        request_alias="request",
        response_alias="response",
        attributes={"output_kind": "tool_call"},
    )
    assert valid.attributes == {"output_kind": "tool_call"}
    try:
        _event(
            "llm.first_output",
            "llm-main",
            "llm",
            "invalid",
            10,
            turn_alias="turn",
            request_alias="request",
            response_alias="response",
            attributes={"output_kind": "private words"},
        )
    except ValueError as exc:
        assert "output_kind" in str(exc)
    else:
        raise AssertionError("invalid output_kind was accepted")
