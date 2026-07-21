from __future__ import annotations

import base64
import json
import struct
import time
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from voxbench.control_plane.app import create_app, create_app_from_env
from voxbench.control_plane.models import Base
from voxbench.control_plane.repository_config import (
    RepositoryConfigurationError,
    RepositoryReadiness,
    build_run_repository_from_env,
)
from voxbench.control_plane.run_api import PostgresRunRepository

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


def test_repository_environment_defaults_to_memory_and_exposes_safe_readiness() -> None:
    client = TestClient(create_app_from_env(environ={}))

    response = client.get("/repository/readiness")

    assert response.status_code == 200
    assert response.json() == {
        "mode": "memory",
        "state": "ready",
        "reason_alias": None,
        "job_queue_enabled": False,
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
    }
    serialized = response.text + repr(app.state.voxbench)
    assert "private-password" not in serialized
    assert "db.internal" not in serialized


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


def test_opt_in_postgres_probe_reports_ready_only_at_expected_migration_head() -> None:
    engine = _sqlite_probe_engine("0008_run_job_leases")

    runtime = build_run_repository_from_env(
        _postgres_environment(),
        engine_factory=lambda _: engine,
    )

    assert runtime.readiness == RepositoryReadiness(
        mode="postgres",
        state="ready",
        job_queue_enabled=True,
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
