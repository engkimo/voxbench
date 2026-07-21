from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from voxbench.control_plane.job_queue import (
    PostgresRunJobQueue,
    RunJobLease,
    RunJobQueueError,
    _claim_statement,
)
from voxbench.control_plane.models import Base, Run


def _queue(*, max_attempts: int = 3):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    run_id = uuid4()
    with sessions.begin() as session:
        session.add(
            Run(
                id=run_id,
                config_hash="config-hash",
                call_id=None,
                conversation_id="",
                provider="provider",
                engine="engine",
                status="running",
                failure_alias=None,
                resolved_config={},
                environment_metadata={},
                readiness_checklist=[],
                started_at=datetime.now(UTC),
                ended_at=None,
            )
        )
    return engine, PostgresRunJobQueue(sessions, max_attempts=max_attempts), str(run_id)


def test_job_queue_claim_heartbeat_expiry_reclaim_and_complete() -> None:
    _, queue, run_id = _queue()
    t0 = datetime(2026, 7, 21, tzinfo=UTC)

    job_id = queue.enqueue(run_id, now=t0)
    assert queue.enqueue(run_id, now=t0 + timedelta(seconds=1)) == job_id
    first = queue.claim("worker-a", lease_seconds=5, now=t0)

    assert first is not None
    assert first.run_id == run_id
    assert first.attempt == 1
    assert queue.claim("worker-b", lease_seconds=5, now=t0 + timedelta(seconds=1)) is None
    assert queue.heartbeat(first, "wrong-worker", lease_seconds=5, now=t0) is False
    assert queue.heartbeat(first, "worker-a", lease_seconds=5, now=t0 + timedelta(seconds=2))
    assert queue.claim("worker-b", lease_seconds=5, now=t0 + timedelta(seconds=6)) is None

    second = queue.claim("worker-b", lease_seconds=5, now=t0 + timedelta(seconds=8))

    assert second is not None
    assert second.attempt == 2
    assert second.lease_token != first.lease_token
    assert queue.complete(first, "worker-a", now=t0 + timedelta(seconds=8)) is False
    assert queue.complete(second, "worker-b", now=t0 + timedelta(seconds=9)) is True
    status = queue.get(job_id)
    assert status is not None
    assert status.state == "completed"
    assert status.attempts == 2
    assert status.lease_owner is None
    assert status.lease_expires_at is None


def test_job_queue_retry_delay_and_attempt_limit_are_deterministic() -> None:
    _, queue, run_id = _queue(max_attempts=2)
    t0 = datetime(2026, 7, 21, tzinfo=UTC)
    job_id = queue.enqueue(run_id, now=t0)
    first = queue.claim("worker-a", lease_seconds=5, now=t0)
    assert first is not None

    assert queue.fail(
        first,
        "worker-a",
        failure_alias="engine-harness-error",
        retryable=True,
        retry_delay_seconds=10,
        now=t0 + timedelta(seconds=1),
    )
    assert queue.claim("worker-b", lease_seconds=5, now=t0 + timedelta(seconds=10)) is None
    second = queue.claim("worker-b", lease_seconds=5, now=t0 + timedelta(seconds=11))

    assert second is not None
    assert second.attempt == 2
    assert queue.fail(
        second,
        "worker-b",
        failure_alias="engine-harness-error",
        retryable=True,
        now=t0 + timedelta(seconds=12),
    )
    status = queue.get(job_id)
    assert status is not None
    assert status.state == "failed"
    assert status.attempts == 2
    assert status.failure_alias == "engine-harness-error"
    assert queue.claim("worker-c", lease_seconds=5, now=t0 + timedelta(seconds=30)) is None


def test_expired_final_attempt_is_failed_before_another_claim() -> None:
    _, queue, run_id = _queue(max_attempts=1)
    t0 = datetime(2026, 7, 21, tzinfo=UTC)
    job_id = queue.enqueue(run_id, now=t0)
    lease = queue.claim("worker-a", lease_seconds=5, now=t0)
    assert lease is not None

    reclaimed = queue.claim("worker-b", lease_seconds=5, now=t0 + timedelta(seconds=6))

    assert reclaimed is None
    status = queue.get(job_id)
    assert status is not None
    assert status.state == "failed"
    assert status.failure_alias == "attempt-limit-exhausted"


def test_claim_statement_compiles_to_postgres_skip_locked() -> None:
    statement = _claim_statement(datetime(2026, 7, 21, tzinfo=UTC), 3)

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE OF run_jobs SKIP LOCKED" in sql
    assert "LIMIT" in sql


@pytest.mark.parametrize(
    "operation",
    [
        lambda queue, run_id, now: queue.enqueue("not-a-uuid", now=now),
        lambda queue, run_id, now: queue.claim("unsafe worker", now=now),
        lambda queue, run_id, now: queue.claim("worker", lease_seconds=4, now=now),
        lambda queue, run_id, now: queue.claim("worker", lease_seconds=301, now=now),
        lambda queue, run_id, now: queue.fail(
            RunJobLease(str(uuid4()), run_id, str(uuid4()), 1, now),
            "worker",
            failure_alias="https://private.invalid",
            retryable=False,
            now=now,
        ),
    ],
)
def test_job_queue_rejects_unsafe_identity_and_bounds(operation) -> None:
    _, queue, run_id = _queue()
    now = datetime(2026, 7, 21, tzinfo=UTC)

    with pytest.raises(ValueError):
        operation(queue, run_id, now)


def test_job_queue_database_error_is_fixed_and_safe() -> None:
    engine, queue, _ = _queue()
    engine.dispose()

    with pytest.raises(RunJobQueueError) as error:
        queue.get(str(uuid4()))

    assert str(error.value) == "run job queue is unavailable"
    assert "no such table" not in str(error.value).lower()
