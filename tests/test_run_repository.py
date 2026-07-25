from __future__ import annotations

import base64
import json
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import voxbench.control_plane.repository_config as repository_config_module
from voxbench.control_plane.app import create_app, create_app_from_env
from voxbench.control_plane.job_queue import PostgresRunJobQueue
from voxbench.control_plane.models import Base
from voxbench.control_plane.models import Run as RunRow
from voxbench.control_plane.models import RunJob as RunJobRow
from voxbench.control_plane.repository_config import (
    RepositoryConfigurationError,
    RepositoryReadiness,
    build_run_repository_from_env,
)
from voxbench.control_plane.run_api import (
    PostgresRunRepository,
    RunRepositoryError,
    StoredRun,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATHS = (
    "examples/manifests/engine/asterisk.json",
    "examples/manifests/provider/openai-realtime.json",
    "examples/manifests/processor/resampler.json",
    "examples/manifests/processor/agc.json",
    "examples/manifests/processor/limiter.json",
    "examples/manifests/processor/serializer.json",
)


def _json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def _run_payload() -> dict:
    config = _json("examples/configs/live-demo-openai-realtime.json")
    return {
        "config_name": config["meta"]["name"],
        "configs": [config],
        "manifests": [_json(path) for path in MANIFEST_PATHS],
        "call_id": "postgres-repository-test",
        "environment": {
            "environment_profile": "integration",
            "server_alias": "integration-host-a",
            "tags": ["postgres-test"],
        },
    }


def _sqlite_repository():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, sessions, PostgresRunRepository(sessions)


def _queued_run() -> StoredRun:
    return StoredRun(
        run_id=str(uuid4()),
        config_hash="queued-config-hash",
        call_id=None,
        conversation_id="",
        provider="provider",
        engine="engine",
        status="running",
        started_at=datetime.now(UTC),
        ended_at=None,
        resolved_config={},
        recordings=[],
        spans=[],
        metrics=[],
    )


def test_postgres_repository_atomically_saves_run_and_enqueues_job() -> None:
    _, sessions, repository = _sqlite_repository()
    run = _queued_run()

    job_id = repository.save_queued_run(run)
    duplicate_job_id = repository.save_queued_run(run)

    restored = repository.get(run.run_id)
    with sessions() as session:
        job = session.get(RunJobRow, UUID(job_id))
    assert duplicate_job_id == job_id
    assert restored is not None
    assert restored.status == "running"
    assert job is not None
    assert str(job.run_id) == run.run_id
    assert job.state == "queued"
    assert job.attempts == 0


def test_postgres_repository_rolls_back_run_when_atomic_enqueue_fails() -> None:
    _, sessions, repository = _sqlite_repository()
    run = _queued_run()

    def fail_job_flush(session, _context, _instances) -> None:
        if any(isinstance(row, RunJobRow) for row in session.new):
            raise IntegrityError("enqueue failed", {}, RuntimeError("private"))

    event.listen(sessions.class_, "before_flush", fail_job_flush)
    try:
        with pytest.raises(RunRepositoryError, match="run repository is unavailable"):
            repository.save_queued_run(run)
    finally:
        event.remove(sessions.class_, "before_flush", fail_job_flush)

    assert repository.get(run.run_id) is None
    with sessions() as session:
        assert session.scalar(select(RunJobRow)) is None


def test_postgres_repository_restores_completed_run_after_app_restart(tmp_path: Path) -> None:
    _, sessions, repository = _sqlite_repository()
    readiness = RepositoryReadiness(
        mode="postgres",
        state="configured",
        reason_alias="connectivity-and-migrations-not-checked",
    )
    first_client = TestClient(
        create_app(
            artifact_root=tmp_path / "recordings",
            repository=repository,
            repository_readiness=readiness,
        )
    )

    created = first_client.post("/runs", json=_run_payload())

    assert created.status_code == 200
    created_body = created.json()
    assert created_body["status"] == "completed"
    assert len(created_body["recordings"]) == 4
    assert len(created_body["spans"]) == 5
    restarted_client = TestClient(
        create_app(
            artifact_root=tmp_path / "recordings",
            repository=PostgresRunRepository(sessions),
            repository_readiness=readiness,
        )
    )
    restored = restarted_client.get(f"/runs/{created_body['run_id']}")
    timeline = restarted_client.get(f"/runs/{created_body['run_id']}/timeline")

    assert restored.status_code == 200
    restored_body = restored.json()
    assert restored_body["run_id"] == created_body["run_id"]
    assert restored_body["config_hash"] == created_body["config_hash"]
    assert restored_body["recordings"] == created_body["recordings"]
    assert restored_body["spans"] == created_body["spans"]
    assert restored_body["metrics"] == created_body["metrics"]
    assert restored_body["verifications"] == created_body["verifications"]
    assert restarted_client.get("/runs").json()[0]["run_id"] == created_body["run_id"]
    assert timeline.status_code == 200
    assert [item["stage"] for item in timeline.json()["lanes"]["recordings"]] == [
        "resampler",
        "agc",
        "limiter",
        "serializer",
    ]


def test_postgres_repository_persists_observation_mutations_and_failure(tmp_path: Path) -> None:
    _, sessions, repository = _sqlite_repository()
    first_client = TestClient(
        create_app(artifact_root=tmp_path / "recordings", repository=repository)
    )
    run_id = first_client.post("/runs/observed", json=_run_payload()).json()["run_id"]
    pcm = base64.b64encode(struct.pack("<h", 1200) * 160).decode("ascii")

    observed = first_client.post(
        "/v1/observations",
        json={
            "run_id": run_id,
            "metrics": [{"stage": "agc", "name": "input_rms", "value": 1200}],
            "audio_chunks": [
                {
                    "stage": "agc",
                    "pcm_s16le_base64": pcm,
                    "sample_rate_hz": 16000,
                    "channels": 1,
                }
            ],
            "sip_events": [
                {
                    "call_id": "postgres-repository-test",
                    "method": "INVITE",
                    "direction": "in",
                    "summary_alias": "test-invite",
                }
            ],
            "rtp_stats": [
                {
                    "jitter_ms": 1.2,
                    "loss_pct": 0.1,
                    "mos": 4.3,
                    "direction": "received",
                    "rtt_ms": 8.5,
                }
            ],
            "timeline_events": [
                {
                    "event_id": "barge-in-1:1",
                    "category": "conversation",
                    "name": "provider_input_speech_started",
                    "source": "audiosocket_bridge",
                    "correlation_alias": "barge-in-1",
                },
                {
                    "event_id": "barge-in-1:2",
                    "category": "buffer",
                    "name": "playback_queue_cleared",
                    "source": "audiosocket_bridge",
                    "correlation_alias": "barge-in-1",
                    "attributes": {
                        "dropped_frames": 2,
                        "discarded_audio_ms": 40,
                    },
                },
                {
                    "event_id": "barge-in-1:3",
                    "category": "conversation",
                    "name": "barge_in_completed",
                    "source": "audiosocket_bridge",
                    "correlation_alias": "barge-in-1",
                    "attributes": {
                        "interrupt_path": "provider-request",
                        "played_audio_end_ms": 60,
                    },
                },
                {
                    "event_id": "rtp-packet-test:0",
                    "category": "transport",
                    "name": "rtp.packet_arrived",
                    "source": "rtp_packet_header_observer",
                    "correlation_alias": "test-rtp-stream",
                    "stream_alias": "test-rtp-stream",
                    "direction": "received",
                    "attributes": {
                        "sequence_number": 10,
                        "rtp_timestamp": 1600,
                        "payload_type": 0,
                        "clock_rate_hz": 8000,
                        "marker": False,
                    },
                },
                {
                    "event_id": "rtp-packet-test:1",
                    "category": "transport",
                    "name": "rtp.packet_arrived",
                    "source": "rtp_packet_header_observer",
                    "correlation_alias": "test-rtp-stream",
                    "stream_alias": "test-rtp-stream",
                    "direction": "received",
                    "attributes": {
                        "sequence_number": 12,
                        "rtp_timestamp": 1920,
                        "payload_type": 0,
                        "clock_rate_hz": 8000,
                        "marker": False,
                    },
                },
            ],
        },
    )
    failed = first_client.post(
        f"/runs/{run_id}/fail",
        json={"failure_alias": "provider-session-error"},
    )

    assert observed.status_code == 200
    assert failed.status_code == 200
    restarted_client = TestClient(
        create_app(
            artifact_root=tmp_path / "recordings",
            repository=PostgresRunRepository(sessions),
        )
    )
    restored = restarted_client.get(f"/runs/{run_id}").json()
    timeline = restarted_client.get(f"/runs/{run_id}/timeline").json()

    assert restored["status"] == "failed"
    assert restored["failure_alias"] == "provider-session-error"
    assert {item["name"] for item in restored["metrics"]} == {"input_rms", "run_failed"}
    assert timeline["lanes"]["sip_ladder"][0]["method"] == "INVITE"
    assert timeline["lanes"]["rtp_quality"][0]["rtt_ms"] == 8.5
    assert timeline["lanes"]["recordings"][0]["stage"] == "agc"
    assert [
        event["event_id"]
        for event in timeline["lanes"]["events"]
        if (event["correlation_alias"] or "").startswith("barge-in")
    ] == [
        "barge-in-1:1",
        "barge-in-1:2",
        "barge-in-1:3",
    ]
    barge_in = next(
        incident
        for incident in timeline["lanes"]["incidents"]
        if incident["rule_id"] == "barge_in_sequence"
    )
    assert barge_in["observed"]["discarded_audio_ms"] == 40.0
    assert barge_in["evidence_refs"] == [
        "barge-in-1:1",
        "barge-in-1:2",
        "barge-in-1:3",
    ]
    sequence_gap = next(
        incident
        for incident in timeline["lanes"]["incidents"]
        if incident["rule_id"] == "rtp_sequence_gap_v1"
    )
    assert sequence_gap["observed"]["missing_packet_count"] == 1
    assert sequence_gap["direction"] == "received"


def test_postgres_repository_commits_result_and_job_completion_atomically(
    tmp_path: Path,
) -> None:
    _, sessions, repository = _sqlite_repository()
    client = TestClient(
        create_app(artifact_root=tmp_path / "recordings", repository=repository)
    )
    run_id = client.post("/runs/observed", json=_run_payload()).json()["run_id"]
    queue = PostgresRunJobQueue(sessions)
    t0 = datetime(2026, 7, 21, tzinfo=UTC)
    job_id = queue.enqueue(run_id, now=t0)
    lease = queue.claim("worker-a", lease_seconds=30, now=t0)
    run = repository.get(run_id)
    assert lease is not None
    assert run is not None
    run.status = "completed"
    run.conversation_id = "fenced-result"
    run.ended_at = t0 + timedelta(seconds=1)

    committed = repository.commit_leased_result(
        run,
        lease,
        "worker-a",
        now=t0 + timedelta(seconds=1),
    )

    assert committed is True
    restored = repository.get(run_id)
    status = queue.get(job_id)
    assert restored is not None
    assert restored.status == "completed"
    assert restored.conversation_id == "fenced-result"
    assert status is not None
    assert status.state == "completed"
    assert status.lease_owner is None
    assert status.lease_expires_at is None


def test_postgres_repository_rejects_expired_stale_lease_without_overwrite(
    tmp_path: Path,
) -> None:
    _, sessions, repository = _sqlite_repository()
    client = TestClient(
        create_app(artifact_root=tmp_path / "recordings", repository=repository)
    )
    run_id = client.post("/runs/observed", json=_run_payload()).json()["run_id"]
    queue = PostgresRunJobQueue(sessions)
    t0 = datetime(2026, 7, 21, tzinfo=UTC)
    job_id = queue.enqueue(run_id, now=t0)
    stale_lease = queue.claim("worker-a", lease_seconds=5, now=t0)
    current_lease = queue.claim("worker-b", lease_seconds=5, now=t0 + timedelta(seconds=6))
    stale_result = repository.get(run_id)
    assert stale_lease is not None
    assert current_lease is not None
    assert stale_result is not None
    original_conversation_id = stale_result.conversation_id
    stale_result.status = "completed"
    stale_result.conversation_id = "must-not-overwrite"
    stale_result.ended_at = t0 + timedelta(seconds=7)

    assert (
        repository.commit_leased_result(
            stale_result,
            stale_lease,
            "worker-a",
            now=t0 + timedelta(seconds=7),
        )
        is False
    )
    unchanged = repository.get(run_id)
    assert unchanged is not None
    assert unchanged.status == "running"
    assert unchanged.conversation_id == original_conversation_id

    current_result = repository.get(run_id)
    assert current_result is not None
    current_result.status = "failed"
    current_result.failure_alias = "engine-harness-error"
    current_result.ended_at = t0 + timedelta(seconds=8)
    assert repository.commit_leased_result(
        current_result,
        current_lease,
        "worker-b",
        now=t0 + timedelta(seconds=8),
    )
    status = queue.get(job_id)
    assert status is not None
    assert status.state == "failed"
    assert status.failure_alias == "engine-harness-error"


def test_postgres_repository_rolls_back_result_when_fenced_commit_fails(
    tmp_path: Path,
) -> None:
    _, sessions, repository = _sqlite_repository()
    client = TestClient(
        create_app(artifact_root=tmp_path / "recordings", repository=repository)
    )
    run_id = client.post("/runs/observed", json=_run_payload()).json()["run_id"]
    queue = PostgresRunJobQueue(sessions)
    t0 = datetime(2026, 7, 21, tzinfo=UTC)
    job_id = queue.enqueue(run_id, now=t0)
    lease = queue.claim("worker-a", lease_seconds=30, now=t0)
    run = repository.get(run_id)
    assert lease is not None
    assert run is not None
    run.status = "completed"
    run.conversation_id = "must-roll-back"
    run.ended_at = t0 + timedelta(seconds=1)

    def fail_completed_run_flush(session, _context, _instances) -> None:
        if any(
            isinstance(row, RunRow) and row.status == "completed"
            for row in session.dirty
        ):
            raise IntegrityError("fenced commit failed", {}, RuntimeError("private"))

    event.listen(sessions.class_, "before_flush", fail_completed_run_flush)
    try:
        with pytest.raises(RunRepositoryError, match="run repository is unavailable"):
            repository.commit_leased_result(
                run,
                lease,
                "worker-a",
                now=t0 + timedelta(seconds=1),
            )
    finally:
        event.remove(sessions.class_, "before_flush", fail_completed_run_flush)

    restored = repository.get(run_id)
    status = queue.get(job_id)
    assert restored is not None
    assert restored.status == "running"
    assert restored.conversation_id != "must-roll-back"
    assert status is not None
    assert status.state == "leased"
    assert status.lease_owner == "worker-a"


def test_repository_environment_defaults_to_memory_and_exposes_safe_readiness() -> None:
    client = TestClient(create_app_from_env(environ={}))

    response = client.get("/repository/readiness")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "memory",
        "state": "ready",
        "reason_alias": None,
        "job_queue_enabled": False,
        "statement_timeout_ms": None,
        "worker_enabled": False,
        "worker_running": False,
        "worker_processed_total": 0,
        "worker_error_total": 0,
        "worker_lease_lost_total": 0,
    }


def test_repository_environment_wires_postgres_without_exposing_database_url() -> None:
    database_url = "postgresql+psycopg://voxbench:private-password@db.internal/voxbench"
    engine, _, _ = _sqlite_repository()
    captured_urls: list[str] = []

    def engine_factory(url: str):
        captured_urls.append(url)
        return engine

    app = create_app_from_env(
        environ={
            "VOXBENCH_RUN_REPOSITORY": "postgres",
            "VOXBENCH_DATABASE_URL": database_url,
        },
        repository_engine_factory=engine_factory,
    )
    response = TestClient(app).get("/repository/readiness")

    assert captured_urls == [database_url]
    assert app.state.voxbench.job_queue is not None
    assert response.json() == {
        "mode": "postgres",
        "state": "configured",
        "reason_alias": "connectivity-and-migrations-not-checked",
        "job_queue_enabled": True,
        "statement_timeout_ms": 5_000,
        "worker_enabled": True,
        "worker_running": False,
        "worker_processed_total": 0,
        "worker_error_total": 0,
        "worker_lease_lost_total": 0,
    }
    serialized = response.text + repr(app.state.voxbench)
    assert "private-password" not in serialized
    assert "db.internal" not in serialized
    assert "worker-" not in response.text
    assert "lease_token" not in response.text


def test_postgres_async_run_uses_persistent_worker_lifespan_and_atomic_job(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'worker.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    app = create_app_from_env(
        environ={
            "VOXBENCH_RUN_REPOSITORY": "postgres",
            "VOXBENCH_DATABASE_URL": (
                "postgresql+psycopg://voxbench:private-password@db.internal/voxbench"
            ),
        },
        repository_engine_factory=lambda _url: engine,
        artifact_root=tmp_path / "recordings",
    )
    supervisor = app.state.run_worker_supervisor
    assert supervisor is not None
    assert supervisor.is_running is False

    with TestClient(app) as client:
        assert supervisor.is_running is True
        readiness = client.get("/repository/readiness").json()
        assert readiness["worker_enabled"] is True
        assert readiness["worker_running"] is True
        assert readiness["statement_timeout_ms"] == 5_000
        accepted = client.post("/runs/async", json=_run_payload())
        assert accepted.status_code == 202
        run_id = accepted.json()["run_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            restored = client.get(f"/runs/{run_id}").json()
            if restored["status"] == "completed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("persistent worker did not complete queued run")

        assert restored["recordings"]
        with sessions() as session:
            job = session.scalar(select(RunJobRow).where(RunJobRow.run_id == UUID(run_id)))
        assert job is not None
        assert job.state == "completed"
        assert job.attempts == 1
        assert job.lease_owner is None
        assert job.lease_token is None
        telemetry = client.get("/repository/readiness").json()
        assert telemetry["worker_processed_total"] >= 1
        assert telemetry["worker_error_total"] == 0
        assert telemetry["worker_lease_lost_total"] == 0

    assert supervisor.is_running is False


@pytest.mark.parametrize(
    ("environment", "reason_alias"),
    [
        ({"VOXBENCH_RUN_REPOSITORY": "unknown"}, "run-repository-mode-invalid"),
        ({"VOXBENCH_RUN_REPOSITORY": "postgres"}, "database-url-missing"),
        (
            {
                "VOXBENCH_RUN_REPOSITORY": "postgres",
                "VOXBENCH_DATABASE_URL": "mysql://private-user:private-password@db/private",
            },
            "database-url-invalid",
        ),
    ],
)
def test_repository_environment_rejects_invalid_configuration_safely(
    environment: dict[str, str],
    reason_alias: str,
) -> None:
    with pytest.raises(RepositoryConfigurationError) as error:
        build_run_repository_from_env(environment)

    assert error.value.reason_alias == reason_alias
    assert "private-user" not in str(error.value)
    assert "private-password" not in str(error.value)


def test_repository_engine_factory_failure_discards_raw_error() -> None:
    database_url = "postgresql+psycopg://private-user:private-password@db/private"

    def failing_factory(_: str):
        raise RuntimeError("private-password db.internal")

    with pytest.raises(RepositoryConfigurationError) as error:
        build_run_repository_from_env(
            {
                "VOXBENCH_RUN_REPOSITORY": "postgres",
                "VOXBENCH_DATABASE_URL": database_url,
            },
            engine_factory=failing_factory,
        )

    assert error.value.reason_alias == "database-engine-configuration-failed"
    assert "private-password" not in str(error.value)
    assert "db.internal" not in str(error.value)


def _postgres_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "VOXBENCH_RUN_REPOSITORY": "postgres",
        "VOXBENCH_DATABASE_URL": (
            "postgresql+psycopg://private-user:private-password@db.internal/private"
        ),
        "VOXBENCH_POSTGRES_PROBE": "true",
        "VOXBENCH_POSTGRES_PROBE_TIMEOUT_MS": "1000",
    }
    environment.update(overrides)
    return environment


def _sqlite_probe_engine(migration_head: str):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(64))")
        connection.exec_driver_sql(
            "INSERT INTO alembic_version (version_num) VALUES (?)",
            (migration_head,),
        )
    return engine


def test_postgres_engine_applies_bounded_session_statement_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}
    engine = create_engine("sqlite+pysqlite://")

    def capture_engine(url: str, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return engine

    monkeypatch.setattr(repository_config_module, "create_engine", capture_engine)

    runtime = build_run_repository_from_env(
        _postgres_environment(
            VOXBENCH_POSTGRES_PROBE="false",
            VOXBENCH_POSTGRES_STATEMENT_TIMEOUT_MS="1234",
        )
    )

    assert captured["connect_args"] == {"options": "-c statement_timeout=1234"}
    assert runtime.readiness.statement_timeout_ms == 1_234


def test_opt_in_postgres_probe_reports_ready_only_at_expected_migration_head() -> None:
    engine = _sqlite_probe_engine("0009_timeline_events")

    runtime = build_run_repository_from_env(
        _postgres_environment(),
        engine_factory=lambda _: engine,
    )

    assert runtime.readiness == RepositoryReadiness(
        mode="postgres",
        state="ready",
        job_queue_enabled=True,
        statement_timeout_ms=5_000,
    )


def test_postgres_probe_reports_safe_migration_head_mismatch() -> None:
    engine = _sqlite_probe_engine("0006_phase4_rtp_rtt_direction")

    runtime = build_run_repository_from_env(
        _postgres_environment(),
        engine_factory=lambda _: engine,
    )

    assert runtime.readiness == RepositoryReadiness(
        mode="postgres",
        state="unavailable",
        reason_alias="migration-head-mismatch",
        job_queue_enabled=True,
        statement_timeout_ms=5_000,
    )


def test_postgres_probe_failure_discards_raw_database_error() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def fail_connection(*_args) -> None:
        raise RuntimeError("private-password db.internal")

    runtime = build_run_repository_from_env(
        _postgres_environment(),
        engine_factory=lambda _: engine,
    )

    assert runtime.readiness == RepositoryReadiness(
        mode="postgres",
        state="unavailable",
        reason_alias="postgres-probe-failed",
        job_queue_enabled=True,
        statement_timeout_ms=5_000,
    )
    serialized = repr(runtime) + repr(runtime.repository)
    assert "private-password" not in serialized
    assert "db.internal" not in serialized


def test_postgres_probe_timeout_is_bounded_and_safe() -> None:
    release = Event()
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def block_connection(*_args) -> None:
        release.wait(1)

    started = time.monotonic()
    runtime = build_run_repository_from_env(
        _postgres_environment(VOXBENCH_POSTGRES_PROBE_TIMEOUT_MS="10"),
        engine_factory=lambda _: engine,
    )
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.5
    assert runtime.readiness == RepositoryReadiness(
        mode="postgres",
        state="unavailable",
        reason_alias="postgres-probe-timeout",
        job_queue_enabled=True,
        statement_timeout_ms=5_000,
    )


@pytest.mark.parametrize(
    ("name", "value", "reason_alias"),
    [
        ("VOXBENCH_POSTGRES_PROBE", "sometimes", "postgres-probe-flag-invalid"),
        (
            "VOXBENCH_POSTGRES_PROBE_TIMEOUT_MS",
            "private-timeout",
            "postgres-probe-timeout-invalid",
        ),
        (
            "VOXBENCH_POSTGRES_PROBE_TIMEOUT_MS",
            "9",
            "postgres-probe-timeout-invalid",
        ),
        (
            "VOXBENCH_POSTGRES_PROBE_TIMEOUT_MS",
            "10001",
            "postgres-probe-timeout-invalid",
        ),
        (
            "VOXBENCH_POSTGRES_STATEMENT_TIMEOUT_MS",
            "private-timeout",
            "postgres-statement-timeout-invalid",
        ),
        (
            "VOXBENCH_POSTGRES_STATEMENT_TIMEOUT_MS",
            "99",
            "postgres-statement-timeout-invalid",
        ),
        (
            "VOXBENCH_POSTGRES_STATEMENT_TIMEOUT_MS",
            "30001",
            "postgres-statement-timeout-invalid",
        ),
    ],
)
def test_postgres_probe_rejects_invalid_configuration_safely(
    name: str,
    value: str,
    reason_alias: str,
) -> None:
    with pytest.raises(RepositoryConfigurationError) as error:
        build_run_repository_from_env(_postgres_environment(**{name: value}))

    assert error.value.reason_alias == reason_alias
    assert value not in str(error.value)


def test_repository_database_error_maps_to_fixed_503_response(tmp_path: Path) -> None:
    engine, _, repository = _sqlite_repository()
    client = TestClient(
        create_app(artifact_root=tmp_path / "recordings", repository=repository)
    )
    engine.dispose()

    response = client.get("/runs")

    assert response.status_code == 503
    assert response.json() == {"detail": "run repository is unavailable"}
    assert response.headers["retry-after"] == "1"
    assert "no such table" not in response.text.lower()


def test_repository_corrupt_json_maps_to_fixed_503_without_reflection(tmp_path: Path) -> None:
    engine, _, repository = _sqlite_repository()
    client = TestClient(
        create_app(artifact_root=tmp_path / "recordings", repository=repository)
    )
    run_id = client.post("/runs/observed", json=_run_payload()).json()["run_id"]
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE runs SET environment_metadata = ? WHERE id = ?",
            ('"private-corrupt-value"', run_id.replace("-", "")),
        )

    response = client.get(f"/runs/{run_id}")

    assert response.status_code == 503
    assert response.json() == {"detail": "run repository is unavailable"}
    assert "private-corrupt-value" not in response.text
