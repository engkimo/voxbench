from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from voxbench.control_plane.job_queue import (
    PostgresRunJobQueue,
    _claim_statement,
)
from voxbench.control_plane.run_api import PostgresRunRepository, StoredRun
from voxbench.engine_harness.models import SpanArtifact

TEST_POSTGRES_URL_ENV = "VOXBENCH_TEST_POSTGRES_URL"

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(
        TEST_POSTGRES_URL_ENV not in os.environ,
        reason=f"{TEST_POSTGRES_URL_ENV} is not configured",
    ),
]


@dataclass(frozen=True)
class PostgresRuntime:
    sessions: sessionmaker[Session]
    repository: PostgresRunRepository
    queue: PostgresRunJobQueue


@pytest.fixture(scope="module")
def postgres_runtime() -> PostgresRuntime:
    database_url = os.environ[TEST_POSTGRES_URL_ENV]
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+psycopg" or not parsed.database:
        pytest.fail(f"{TEST_POSTGRES_URL_ENV} must use postgresql+psycopg")
    schema = f"voxbench_test_{uuid4().hex}"
    migration_url = parsed.update_query_dict(
        {"options": f"-c search_path={schema} -c statement_timeout=5000"}
    ).render_as_string(hide_password=False)
    admin_engine = create_engine(database_url, hide_parameters=True, pool_pre_ping=True)
    test_engine = create_engine(
        database_url,
        hide_parameters=True,
        pool_pre_ping=True,
        connect_args={
            "options": f"-c search_path={schema} -c statement_timeout=5000"
        },
    )
    try:
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        alembic_config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
        with patch.dict(os.environ, {"VOXBENCH_DATABASE_URL": migration_url}):
            command.upgrade(alembic_config, "head")
        with test_engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one()
                == "0009_timeline_events"
            )
        sessions = sessionmaker(bind=test_engine, expire_on_commit=False)
        yield PostgresRuntime(
            sessions=sessions,
            repository=PostgresRunRepository(sessions),
            queue=PostgresRunJobQueue(sessions, max_attempts=3),
        )
    finally:
        test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin_engine.dispose()


def _run() -> StoredRun:
    return StoredRun(
        run_id=str(uuid4()),
        config_hash="postgres-integration-config",
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


def test_postgres_round_trips_epoch_nanosecond_spans(
    postgres_runtime: PostgresRuntime,
) -> None:
    run = _run()
    start_ns = 1_800_000_000_000_000_000
    end_ns = start_ns + 1_000_000
    run.spans = [
        SpanArtifact(
            trace_id="postgres-integration-trace",
            span_id="postgres-integration-span",
            parent_id=None,
            name="postgres-integration",
            start_ns=start_ns,
            end_ns=end_ns,
            attrs={},
        )
    ]

    postgres_runtime.repository.save(run)

    restored = postgres_runtime.repository.get(run.run_id)
    assert restored is not None
    assert [(span.start_ns, span.end_ns) for span in restored.spans] == [
        (start_ns, end_ns)
    ]


def test_postgres_skip_locked_claims_next_job_without_waiting(
    postgres_runtime: PostgresRuntime,
) -> None:
    first_run = _run()
    second_run = _run()
    available_at = datetime.now(UTC) - timedelta(seconds=1)
    postgres_runtime.repository.save_queued_run(first_run, now=available_at)
    postgres_runtime.repository.save_queued_run(second_run, now=available_at)

    with postgres_runtime.sessions.begin() as locking_session:
        locked = locking_session.scalar(_claim_statement(datetime.now(UTC), 3))
        assert locked is not None
        claimed_while_locked = postgres_runtime.queue.claim(
            "worker-b",
            lease_seconds=5,
        )
        assert claimed_while_locked is not None
        assert claimed_while_locked.run_id != str(locked.run_id)

    claimed_after_unlock = postgres_runtime.queue.claim(
        "worker-a",
        lease_seconds=5,
    )
    assert claimed_after_unlock is not None
    assert {claimed_while_locked.run_id, claimed_after_unlock.run_id} == {
        first_run.run_id,
        second_run.run_id,
    }
    assert postgres_runtime.queue.complete(claimed_while_locked, "worker-b")
    assert postgres_runtime.queue.complete(claimed_after_unlock, "worker-a")


def test_postgres_expired_lease_recovery_rejects_stale_result(
    postgres_runtime: PostgresRuntime,
) -> None:
    run = _run()
    old_now = datetime.now(UTC) - timedelta(seconds=10)
    job_id = postgres_runtime.repository.save_queued_run(run, now=old_now)
    stale = postgres_runtime.queue.claim(
        "old-worker",
        lease_seconds=5,
        now=old_now,
    )
    current = postgres_runtime.queue.claim("new-worker", lease_seconds=5)
    assert stale is not None
    assert current is not None
    assert current.attempt == 2

    stale_result = postgres_runtime.repository.get(run.run_id)
    assert stale_result is not None
    stale_result.status = "completed"
    stale_result.conversation_id = "stale-result"
    stale_result.ended_at = datetime.now(UTC)
    assert not postgres_runtime.repository.commit_leased_result(
        stale_result,
        stale,
        "old-worker",
    )

    current_result = postgres_runtime.repository.get(run.run_id)
    assert current_result is not None
    current_result.status = "completed"
    current_result.conversation_id = "current-result"
    current_result.ended_at = datetime.now(UTC)
    assert postgres_runtime.repository.commit_leased_result(
        current_result,
        current,
        "new-worker",
    )
    restored = postgres_runtime.repository.get(run.run_id)
    status = postgres_runtime.queue.get(job_id)
    assert restored is not None
    assert restored.conversation_id == "current-result"
    assert status is not None
    assert status.state == "completed"
    assert status.attempts == 2
