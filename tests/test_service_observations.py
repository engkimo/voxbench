from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import voxbench.control_plane.run_api as run_api_module
from voxbench.control_plane.app import create_app
from voxbench.control_plane.models import Base
from voxbench.control_plane.run_api import PostgresRunRepository
from voxbench.observability import ObservationBatch, ServiceEvent, VoxBenchObserver
from voxbench.registry.service import load_json
from voxbench.schemas_export import export_schemas

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "examples/v2"


def _v2_payload(mode: str = "cascade") -> dict:
    config = load_json(V2 / f"configs/{mode}.json")
    return {
        "config_name": config["meta"]["name"],
        "configs": [config],
        "manifests": [load_json(path) for path in sorted((V2 / "manifests").glob("*.json"))],
        "call_id": f"service-observation-{mode}",
    }


class ApiTransport:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def send(self, batch: ObservationBatch) -> None:
        response = self.client.post("/v1/observations", json=batch.to_payload())
        response.raise_for_status()


class RecordingTransport:
    def __init__(self) -> None:
        self.batches: list[ObservationBatch] = []

    def send(self, batch: ObservationBatch) -> None:
        self.batches.append(batch)


def _start(client: TestClient, mode: str = "cascade") -> str:
    response = client.post("/runs/observed", json=_v2_payload(mode))
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def _event(kind: str, component_id: str, role: str, alias: str, **relations) -> ServiceEvent:
    return ServiceEvent(
        collector_alias="application-a",
        event_alias=alias,
        kind=kind,
        component_id=component_id,
        role=role,
        ts=datetime(2026, 9, 27, 1, 2, tzinfo=UTC) + timedelta(milliseconds=len(alias)),
        **relations,
    )


def _turn_events(prefix: str, *, segment_count: int = 2) -> list[ServiceEvent]:
    turn = f"turn-{prefix}"
    stt_request = f"stt-request-{prefix}"
    response = f"response-{prefix}"
    llm_request = f"llm-request-{prefix}"
    events = [
        _event(
            "stt.final_emitted", "stt-main", "stt", f"{prefix}-stt-final",
            session_alias="stt-session", request_alias=stt_request,
            segment_alias=f"stt-segment-{prefix}",
        ),
        _event(
            "turn.committed", "application", "coordinator", f"{prefix}-turn",
            turn_alias=turn, parent_request_alias=stt_request,
        ),
        _event(
            "llm.request_started", "llm-main", "llm", f"{prefix}-llm-start",
            turn_alias=turn, request_alias=llm_request,
            parent_request_alias=stt_request, response_alias=response,
        ),
        _event(
            "llm.first_answer_text", "llm-main", "llm", f"{prefix}-llm-first",
            turn_alias=turn, request_alias=llm_request, response_alias=response,
        ),
    ]
    for ordinal in range(segment_count):
        segment = f"segment-{prefix}-{ordinal}"
        aggregation_request = f"aggregation-request-{prefix}-{ordinal}"
        tts_request = f"tts-request-{prefix}-{ordinal}"
        events.extend(
            [
                _event(
                    "aggregation.segment_ready", "aggregation-main", "aggregation",
                    f"{prefix}-aggregation-{ordinal}", request_alias=aggregation_request,
                    parent_request_alias=llm_request, response_alias=response,
                    segment_alias=segment, attributes={"sequence_ordinal": ordinal},
                ),
                _event(
                    "tts.request_started", "tts-main", "tts", f"{prefix}-tts-start-{ordinal}",
                    request_alias=tts_request, parent_request_alias=aggregation_request,
                    response_alias=response, segment_alias=segment,
                ),
                _event(
                    "tts.first_pcm", "tts-main", "tts", f"{prefix}-tts-first-{ordinal}",
                    request_alias=tts_request, response_alias=response, segment_alias=segment,
                    attributes={"observed_boundary": "first-pcm-frame"},
                ),
                _event(
                    "tts.response_completed", "tts-main", "tts",
                    f"{prefix}-tts-complete-{ordinal}", request_alias=tts_request,
                    response_alias=response, segment_alias=segment,
                    attributes={"terminal_outcome": "completed"},
                ),
                _event(
                    "playback.write_started", "output-resampler", "playback",
                    f"{prefix}-playback-{ordinal}", parent_request_alias=tts_request,
                    response_alias=response, segment_alias=segment,
                    attributes={"observed_boundary": "local-write"},
                ),
            ]
        )
    events.append(
        _event(
            "llm.response_completed", "llm-main", "llm", f"{prefix}-llm-complete",
            turn_alias=turn, request_alias=llm_request, response_alias=response,
            attributes={"terminal_outcome": "completed"},
        )
    )
    return events


def test_fake_multi_turn_multi_segment_cascade_ingests_without_content(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "audio"))
    run_id = _start(client)
    observer = VoxBenchObserver(run_id, ApiTransport(client))
    observer.observe_service_event(
        _event(
            "service.session_started", "stt-main", "stt", "stt-session-start",
            session_alias="stt-session",
        )
    )
    events = _turn_events("one") + _turn_events("two")
    for event in events:
        observer.observe_service_event(event)
    assert observer.flush() == len(events) + 1
    assert observer.pending_count == 0

    completed = client.post(f"/runs/{run_id}/complete", json={})
    assert completed.status_code == 200
    assert completed.json()["recordings"] == []
    timeline = client.get(f"/runs/{run_id}/timeline").json()
    service_events = [
        event for event in timeline["lanes"]["events"]
        if event["source"] == "service_observation"
    ]
    assert len(service_events) == len(events) + 1
    assert sum(event["name"] == "tts.first_pcm" for event in service_events) == 4
    assert sum(event["name"] == "turn.committed" for event in service_events) == 2
    assert all(len(event["attributes"]) <= 16 for event in service_events)
    encoded = json.dumps(service_events).lower()
    for forbidden in ("transcript", "prompt", "generated_text", "content_hash", "provider_id"):
        assert forbidden not in encoded
    first_pcm = next(event for event in service_events if event["name"] == "tts.first_pcm")
    assert first_pcm["attributes"]["observed_boundary"] == "first-pcm-frame"
    assert "local-write" not in first_pcm["attributes"].values()
    playback = next(event for event in service_events if event["name"] == "playback.write_started")
    assert playback["attributes"]["observed_boundary"] == "local-write"


@pytest.mark.parametrize(
    "injected",
    [
        {"transcript": "private words"},
        {"prompt": "private words"},
        {"generated_text": "private words"},
        {"content_hash": "abc"},
        {"provider_id": "raw-123"},
        {"url": "https://provider.example/item"},
        {"api_key": "secret"},
        {"raw_payload": {}},
        {"run_id": "other-run"},
    ],
)
def test_service_event_contract_rejects_content_raw_ids_and_cross_run_fields(
    injected: dict,
) -> None:
    payload = _event(
        "stt.final_emitted", "stt-main", "stt", "strict",
        session_alias="session", request_alias="request",
    ).to_payload()
    payload.update(injected)
    with pytest.raises(ValueError):
        ServiceEvent.model_validate(payload)


def test_service_attributes_are_allowlisted_scalar_safe_and_bounded() -> None:
    base = _event(
        "stt.final_emitted", "stt-main", "stt", "attrs",
        session_alias="session", request_alias="request",
    ).to_payload()
    for attributes in (
        {"unknown": 1},
        {"failure_alias": "https://provider.example/error"},
        {"duration_ms": float("inf")},
        {"duration_ms": -1},
        {"input_units": True},
        {"retry": 1},
        {"failure_alias": None},
        {"terminal_outcome": {"nested": "payload"}},
    ):
        with pytest.raises(ValueError):
            ServiceEvent.model_validate({**base, "attributes": attributes})
    too_many = {
        "attempt_ordinal": 1, "sequence_ordinal": 1, "duration_ms": 1,
        "audio_duration_ms": 1, "input_units": 1, "output_units": 1,
        "cached_input_units": 1, "tool_call_count": 1, "queue_depth": 1,
        "sample_rate_hz": 1, "channels": 1, "encoding": "pcm16",
    }
    with pytest.raises(ValueError, match="at most 16"):
        ServiceEvent.model_validate({**base, "attributes": too_many})


def test_service_timestamp_must_include_offset_and_is_normalized_to_utc() -> None:
    payload = _event(
        "stt.final_emitted", "stt-main", "stt", "timestamp",
        session_alias="session", request_alias="request",
    ).to_payload()
    with pytest.raises(ValueError, match="UTC offset"):
        ServiceEvent.model_validate({**payload, "ts": "2026-09-27T10:00:00"})
    normalized = ServiceEvent.model_validate({**payload, "ts": "2026-09-27T10:00:00+09:00"})
    assert normalized.ts.isoformat() == "2026-09-27T01:00:00+00:00"


def test_component_role_and_turn_authority_are_checked_against_run_config(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id = _start(client)
    wrong_role = _event(
        "tts.first_pcm", "stt-main", "tts", "wrong-role",
        request_alias="request", response_alias="response", segment_alias="segment",
    )
    response = client.post(
        "/v1/observations",
        json={"run_id": run_id, "service_events": [wrong_role.to_payload()]},
    )
    assert response.status_code == 400
    assert "not configured for service role" in response.json()["detail"]

    wrong_authority = _event(
        "turn.committed", "engine", "coordinator", "wrong-authority",
        turn_alias="turn", parent_request_alias="stt-request",
    )
    response = client.post(
        "/v1/observations",
        json={"run_id": run_id, "service_events": [wrong_authority.to_payload()]},
    )
    assert response.status_code == 400
    assert "end_of_turn authority" in response.json()["detail"]


def test_retry_is_idempotent_changed_payload_conflicts_and_collectors_do_not_collide(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id = _start(client)
    event = _event(
        "stt.final_emitted", "stt-main", "stt", "same-event",
        session_alias="session", request_alias="request",
        attributes={"sequence_ordinal": 1},
    )
    payload = {"run_id": run_id, "service_events": [event.to_payload()]}
    assert client.post("/v1/observations", json=payload).status_code == 200
    assert client.post("/v1/observations", json=payload).status_code == 200

    conflict = event.model_copy(update={"attributes": {"sequence_ordinal": 2}})
    response = client.post(
        "/v1/observations",
        json={"run_id": run_id, "service_events": [conflict.to_payload()]},
    )
    assert response.status_code == 409

    other_collector = event.model_copy(update={"collector_alias": "application-b"})
    assert client.post(
        "/v1/observations",
        json={"run_id": run_id, "service_events": [other_collector.to_payload()]},
    ).status_code == 200
    timeline = client.get(f"/runs/{run_id}/timeline").json()
    matching = [
        event
        for event in timeline["lanes"]["events"]
        if event["name"] == "stt.final_emitted"
    ]
    assert {event["event_id"] for event in matching} == {
        "service:application-a:same-event", "service:application-b:same-event",
    }


def test_unknown_parent_coverage_resolves_when_parent_arrives_out_of_order(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id = _start(client)
    child = _event(
        "aggregation.segment_ready", "aggregation-main", "aggregation", "child",
        request_alias="aggregation-request", parent_request_alias="late-request",
        response_alias="response", segment_alias="segment",
    )
    assert client.post(
        "/v1/observations", json={"run_id": run_id, "service_events": [child.to_payload()]},
    ).status_code == 200
    child_event = next(
        event for event in client.get(f"/runs/{run_id}/timeline").json()["lanes"]["events"]
        if event["event_id"].endswith(":child")
    )
    assert child_event["attributes"]["unresolved_relations"] == "parent_request"

    parent = _event(
        "llm.request_started", "llm-main", "llm", "late-parent",
        turn_alias="turn", request_alias="late-request", response_alias="response",
        unobserved_relations=["parent_request"],
    )
    assert client.post(
        "/v1/observations", json={"run_id": run_id, "service_events": [parent.to_payload()]},
    ).status_code == 200
    child_event = next(
        event for event in client.get(f"/runs/{run_id}/timeline").json()["lanes"]["events"]
        if event["event_id"].endswith(":child")
    )
    assert "unresolved_relations" not in child_event["attributes"]
    parent_event = next(
        event for event in client.get(f"/runs/{run_id}/timeline").json()["lanes"]["events"]
        if event["event_id"].endswith(":late-parent")
    )
    assert parent_event["attributes"]["unobserved_relations"] == "parent_request"


def test_cancellation_requested_acknowledged_and_missing_terminal_remain_distinct(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id = _start(client)
    requested = _event(
        "cancel.requested", "tts-main", "tts", "cancel-request",
        request_alias="tts-request", generation_epoch=2,
        attributes={"cancel_scope": "current-generation"},
    )
    started_without_terminal = _event(
        "tts.request_started", "tts-main", "tts", "unfinished",
        request_alias="unfinished-request", parent_request_alias="aggregation-request",
        response_alias="response", segment_alias="segment",
    )
    assert client.post(
        "/v1/observations",
        json={
            "run_id": run_id,
            "service_events": [requested.to_payload(), started_without_terminal.to_payload()],
        },
    ).status_code == 200
    names = [
        event["name"] for event in client.get(f"/runs/{run_id}/timeline").json()["lanes"]["events"]
        if event["source"] == "service_observation"
    ]
    assert "cancel.requested" in names
    assert "cancel.acknowledged" not in names
    assert "tts.response_completed" not in names

    acknowledged = _event(
        "cancel.acknowledged", "tts-main", "tts", "cancel-ack",
        request_alias="tts-request", generation_epoch=2,
        attributes={"terminal_outcome": "cancelled"},
    )
    assert client.post(
        "/v1/observations",
        json={"run_id": run_id, "service_events": [acknowledged.to_payload()]},
    ).status_code == 200


def test_generic_path_cannot_bypass_reserved_service_event_validation(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id = _start(client)
    response = client.post(
        "/v1/observations",
        json={
            "run_id": run_id,
            "timeline_events": [{
                "event_id": "bypass", "category": "provider", "name": "stt.final_emitted",
                "source": "custom", "attributes": {"transcript": "private"},
            }],
        },
    )
    assert response.status_code == 422
    for key, value in (
        ("event_id", "service:collector:event"),
        ("source", "service_observation"),
    ):
        generic = {
            "event_id": "generic", "category": "provider", "name": "custom",
            "source": "custom", key: value,
        }
        response = client.post(
            "/v1/observations",
            json={"run_id": run_id, "timeline_events": [generic]},
        )
        assert response.status_code == 422


def test_generic_timeline_retries_are_idempotent_and_changed_payload_conflicts(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id = _start(client)
    event = {
        "event_id": "generic-retry", "category": "provider", "name": "custom.boundary",
        "source": "custom", "ts": "2026-09-27T01:00:00+00:00",
    }
    payload = {"run_id": run_id, "timeline_events": [event]}
    assert client.post("/v1/observations", json=payload).status_code == 200
    assert client.post("/v1/observations", json=payload).status_code == 200
    conflict = {**event, "name": "custom.changed"}
    response = client.post(
        "/v1/observations", json={"run_id": run_id, "timeline_events": [conflict]},
    )
    assert response.status_code == 409
    timeline = client.get(f"/runs/{run_id}/timeline").json()["lanes"]["events"]
    assert sum(event["event_id"] == "generic-retry" for event in timeline) == 1


def test_combined_event_batch_limit_and_observer_splitting() -> None:
    transport = RecordingTransport()
    observer = VoxBenchObserver("run", transport)
    for ordinal in range(129):
        observer.observe_service_event(
            _event(
                "stt.final_emitted", "stt-main", "stt", f"event-{ordinal}",
                session_alias="session", request_alias=f"request-{ordinal}",
            )
        )
    assert observer.flush() == 129
    assert [len(batch.service_events) for batch in transport.batches] == [128, 1]

    payload = {
        "run_id": "run",
        "timeline_events": [
            {"event_id": f"generic-{n}", "category": "provider", "name": "custom",
             "source": "custom"}
            for n in range(64)
        ],
        "service_events": [
            _event(
                "stt.final_emitted", "stt-main", "stt", f"combined-{n}",
                session_alias="session", request_alias=f"request-{n}",
            ).to_payload()
            for n in range(65)
        ],
    }
    from voxbench.control_plane.run_api import ObservationBatchRequest

    with pytest.raises(ValueError, match="128 combined"):
        ObservationBatchRequest.model_validate(payload)


def test_observer_pending_budget_exposes_drops_without_blocking_flush() -> None:
    transport = RecordingTransport()
    observer = VoxBenchObserver("run", transport, max_pending_items=2)
    for ordinal in range(3):
        observer.observe_service_event(
            _event(
                "stt.final_emitted", "stt-main", "stt", f"bounded-{ordinal}",
                session_alias="session", request_alias=f"request-{ordinal}",
            )
        )
    assert observer.pending_count == 2
    assert observer.observation_drop_count == 1
    assert observer.flush() == 2
    assert observer.observation_drop_count == 1


def test_failed_flush_restores_admitted_event_and_drops_newer_concurrent_arrival() -> None:
    class ArrivingFailureTransport:
        observer: VoxBenchObserver

        def send(self, _batch: ObservationBatch) -> None:
            self.observer.observe_service_event(
                _event(
                    "stt.final_emitted", "stt-main", "stt", "newer",
                    session_alias="session", request_alias="newer-request",
                )
            )
            raise RuntimeError("send failed")

    transport = ArrivingFailureTransport()
    observer = VoxBenchObserver("run", transport, max_pending_items=1)
    transport.observer = observer
    original = _event(
        "stt.final_emitted", "stt-main", "stt", "original",
        session_alias="session", request_alias="original-request",
    )
    observer.observe_service_event(original)
    with pytest.raises(RuntimeError, match="send failed"):
        observer.flush()
    assert observer.pending_count == 1
    assert observer.observation_drop_count == 1
    recording = RecordingTransport()
    observer.transport = recording
    assert observer.flush() == 1
    assert recording.batches[0].service_events[0].event_alias == "original"


def test_per_run_event_budget_rejects_without_partial_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_api_module, "MAX_TIMELINE_EVENTS_PER_RUN", 2)
    client = TestClient(create_app(artifact_root=tmp_path))
    run_id = _start(client)
    first = _turn_events("budget", segment_count=0)[:2]
    assert client.post(
        "/v1/observations",
        json={"run_id": run_id, "service_events": [event.to_payload() for event in first]},
    ).status_code == 200
    rejected = _event(
        "stt.final_emitted", "stt-main", "stt", "over-budget",
        session_alias="session", request_alias="request",
    )
    response = client.post(
        "/v1/observations",
        json={"run_id": run_id, "service_events": [rejected.to_payload()]},
    )
    assert response.status_code == 429
    events = client.get(f"/runs/{run_id}/timeline").json()["lanes"]["events"]
    assert all(not event["event_id"].endswith(":over-budget") for event in events)


def test_service_events_persist_and_reconstruct_from_sql_repository(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    repository = PostgresRunRepository(sessions)
    first = TestClient(create_app(artifact_root=tmp_path, repository=repository))
    run_id = _start(first)
    events = _turn_events("persist", segment_count=1)
    response = first.post(
        "/v1/observations",
        json={"run_id": run_id, "service_events": [event.to_payload() for event in events]},
    )
    assert response.status_code == 200
    assert response.json()["service_event_count"] == len(events)

    restarted = TestClient(
        create_app(
            artifact_root=tmp_path,
            repository=PostgresRunRepository(sessions),
        )
    )
    persisted = [
        event for event in restarted.get(f"/runs/{run_id}/timeline").json()["lanes"]["events"]
        if event["source"] == "service_observation"
    ]
    assert len(persisted) == len(events)
    assert {event["name"] for event in persisted} >= {
        "stt.final_emitted", "llm.request_started", "tts.first_pcm", "playback.write_started",
    }
    engine.dispose()


def test_exported_service_event_schema_validates_plain_http_payload(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    schema = load_json(tmp_path / "service-event.v1.schema.json")
    Draft202012Validator.check_schema(schema)
    payload = _event(
        "stt.final_emitted", "stt-main", "stt", "schema",
        session_alias="session", request_alias="request",
    ).to_payload()
    Draft202012Validator(schema).validate(payload)
    assert (tmp_path / "service-event.v1.schema.json").read_bytes() == (
        ROOT / "schemas/service-event.v1.schema.json"
    ).read_bytes()


def test_v1_realtime_run_can_use_same_service_contract(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path))
    config = load_json(ROOT / "examples/configs/live-demo-openai-realtime.json")
    payload = {
        "config_name": config["meta"]["name"],
        "configs": [config],
        "manifests": [
            load_json(ROOT / relative)
            for relative in (
                "examples/manifests/engine/asterisk.json",
                "examples/manifests/provider/openai-realtime.json",
                "examples/manifests/processor/resampler.json",
                "examples/manifests/processor/agc.json",
                "examples/manifests/processor/limiter.json",
                "examples/manifests/processor/serializer.json",
            )
        ],
    }
    run_id = client.post("/runs/observed", json=payload).json()["run_id"]
    event = _event(
        "realtime.first_audio", "ai", "realtime", "v1-realtime",
        request_alias="request", response_alias="response",
    )
    response = client.post(
        "/v1/observations",
        json={"run_id": run_id, "service_events": [event.to_payload()]},
    )
    assert response.status_code == 200
    assert response.json()["service_event_count"] == 1
