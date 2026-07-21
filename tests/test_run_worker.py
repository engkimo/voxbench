from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from voxbench.control_plane.job_queue import PostgresRunJobQueue, RunJobQueueError
from voxbench.control_plane.models import Base
from voxbench.control_plane.run_api import PostgresRunRepository, StoredRun
from voxbench.control_plane.run_worker import (
    RunJobWorker,
    RunWorkerResult,
    RunWorkerSupervisor,
)


def _runtime(*, max_attempts: int = 2):
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    repository = PostgresRunRepository(sessions)
    run = StoredRun(
        run_id=str(uuid4()),
        config_hash="worker-config-hash",
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
    repository.save(run)
    queue = PostgresRunJobQueue(sessions, max_attempts=max_attempts)
    return queue, repository, run.run_id


def _worker(queue, repository, execute, **overrides) -> RunJobWorker:
    return RunJobWorker(
        queue=queue,
        repository=repository,
        execute=execute,
        worker_id="worker-a",
        lease_seconds=5,
        heartbeat_interval_seconds=1,
        retry_delay_seconds=0,
        **overrides,
    )


def test_worker_returns_idle_without_claimable_job() -> None:
    queue, repository, _ = _runtime()
    worker = _worker(queue, repository, lambda _run: None)

    result = worker.run_one()

    assert result.outcome == "idle"
    assert result.job_id is None
    assert result.run_id is None


def test_worker_heartbeats_and_fenced_commits_completed_result() -> None:
    queue, repository, run_id = _runtime()
    job_id = queue.enqueue(run_id)
    heartbeat_seen = Event()
    original_heartbeat = queue.heartbeat

    def capture_heartbeat(*args, **kwargs):
        active = original_heartbeat(*args, **kwargs)
        heartbeat_seen.set()
        return active

    queue.heartbeat = capture_heartbeat  # type: ignore[method-assign]

    def execute(run: StoredRun) -> None:
        assert heartbeat_seen.wait(timeout=2)
        run.status = "completed"
        run.conversation_id = "worker-result"
        run.ended_at = datetime.now(UTC)

    result = _worker(queue, repository, execute).run_one()

    assert result.outcome == "completed"
    assert result.job_id == job_id
    restored = repository.get(run_id)
    status = queue.get(job_id)
    assert restored is not None
    assert restored.status == "completed"
    assert restored.conversation_id == "worker-result"
    assert status is not None
    assert status.state == "completed"


def test_worker_retries_then_atomically_commits_final_failure() -> None:
    queue, repository, run_id = _runtime(max_attempts=2)
    job_id = queue.enqueue(run_id)

    def fail_execution(_run: StoredRun) -> None:
        raise RuntimeError("private provider detail")

    worker = _worker(queue, repository, fail_execution)
    first = worker.run_one()

    assert first.outcome == "retry_scheduled"
    after_first = repository.get(run_id)
    first_status = queue.get(job_id)
    assert after_first is not None
    assert after_first.status == "running"
    assert first_status is not None
    assert first_status.state == "queued"
    assert first_status.failure_alias == "engine-harness-error"

    second = worker.run_one()

    assert second.outcome == "failed"
    assert second.attempt == 2
    failed_run = repository.get(run_id)
    final_status = queue.get(job_id)
    assert failed_run is not None
    assert failed_run.status == "failed"
    assert failed_run.failure_alias == "engine-harness-error"
    assert failed_run.ended_at is not None
    assert final_status is not None
    assert final_status.state == "failed"
    assert final_status.failure_alias == "engine-harness-error"


@pytest.mark.parametrize("heartbeat_failure", ["rejected", "queue-error"])
def test_worker_discards_result_after_heartbeat_reports_lease_loss(
    heartbeat_failure: str,
) -> None:
    queue, repository, run_id = _runtime()
    job_id = queue.enqueue(run_id)
    heartbeat_rejected = Event()

    def reject_heartbeat(*_args, **_kwargs) -> bool:
        heartbeat_rejected.set()
        if heartbeat_failure == "queue-error":
            raise RunJobQueueError
        return False

    queue.heartbeat = reject_heartbeat  # type: ignore[method-assign]

    def execute(run: StoredRun) -> None:
        assert heartbeat_rejected.wait(timeout=2)
        run.status = "completed"
        run.conversation_id = "stale-result"
        run.ended_at = datetime.now(UTC)

    result = _worker(queue, repository, execute).run_one()

    assert result.outcome == "lease_lost"
    restored = repository.get(run_id)
    status = queue.get(job_id)
    assert restored is not None
    assert restored.status == "running"
    assert restored.conversation_id == ""
    assert status is not None
    assert status.state == "leased"


def test_worker_terminally_fails_job_when_run_is_missing() -> None:
    queue, repository, _ = _runtime()
    missing_run_id = str(uuid4())
    job_id = queue.enqueue(missing_run_id)

    result = _worker(queue, repository, lambda _run: None).run_one()

    assert result.outcome == "run_missing"
    assert result.run_id == missing_run_id
    status = queue.get(job_id)
    assert status is not None
    assert status.state == "failed"
    assert status.failure_alias == "run-not-found"


@pytest.mark.parametrize(
    "overrides",
    [
        {"worker_id": "unsafe worker"},
        {"lease_seconds": 4},
        {"lease_seconds": 301},
        {"heartbeat_interval_seconds": 0.5},
        {"heartbeat_interval_seconds": 3},
        {"retry_delay_seconds": -1},
        {"retry_delay_seconds": 3_601},
    ],
)
def test_worker_rejects_unsafe_identity_and_timing_bounds(overrides) -> None:
    queue, repository, _ = _runtime()
    arguments = {
        "queue": queue,
        "repository": repository,
        "execute": lambda _run: None,
        "worker_id": "worker-a",
        "lease_seconds": 5,
        "heartbeat_interval_seconds": 1,
        "retry_delay_seconds": 0,
    }
    arguments.update(overrides)

    with pytest.raises(ValueError):
        RunJobWorker(**arguments)


def test_supervisor_start_stop_are_idempotent_and_restartable() -> None:
    called = Event()

    class IdleWorker:
        def run_one(self) -> RunWorkerResult:
            called.set()
            return RunWorkerResult(outcome="idle")

    supervisor = RunWorkerSupervisor(
        IdleWorker(),
        idle_wait_seconds=0.05,
        error_wait_seconds=0.1,
        shutdown_timeout_seconds=0.5,
    )

    assert supervisor.start() is True
    assert supervisor.start() is False
    assert called.wait(timeout=1)
    assert supervisor.is_running is True
    assert supervisor.snapshot().running is True
    assert supervisor.stop() is True
    assert supervisor.stop() is True
    assert supervisor.is_running is False
    called.clear()
    assert supervisor.start() is True
    assert called.wait(timeout=1)
    assert supervisor.stop() is True


def test_supervisor_survives_worker_error_with_bounded_backoff() -> None:
    recovered = Event()

    class RecoveringWorker:
        calls = 0

        def run_one(self) -> RunWorkerResult:
            self.calls += 1
            if self.calls == 1:
                raise RunJobQueueError
            recovered.set()
            return RunWorkerResult(outcome="idle")

    worker = RecoveringWorker()
    supervisor = RunWorkerSupervisor(
        worker,
        idle_wait_seconds=0.05,
        error_wait_seconds=0.1,
        shutdown_timeout_seconds=0.5,
    )

    supervisor.start()
    assert recovered.wait(timeout=1)
    assert worker.calls >= 2
    assert supervisor.stop() is True
    telemetry = supervisor.snapshot()
    assert telemetry.error_total == 1
    assert telemetry.processed_total == 0
    assert telemetry.lease_lost_total == 0


def test_supervisor_counts_lease_loss_without_exposing_lease_data() -> None:
    returned = Event()

    class LeaseLostWorker:
        def run_one(self) -> RunWorkerResult:
            returned.set()
            return RunWorkerResult(
                outcome="lease_lost",
                job_id="internal-job-id",
                run_id="internal-run-id",
                attempt=2,
            )

    supervisor = RunWorkerSupervisor(
        LeaseLostWorker(),
        idle_wait_seconds=0.05,
        error_wait_seconds=0.1,
        shutdown_timeout_seconds=0.5,
    )

    supervisor.start()
    assert returned.wait(timeout=1)
    assert supervisor.stop() is True
    telemetry = supervisor.snapshot()
    assert telemetry.processed_total == 1
    assert telemetry.lease_lost_total == 1
    assert "internal-job-id" not in repr(telemetry)
    assert "internal-run-id" not in repr(telemetry)


def test_supervisor_shutdown_timeout_is_bounded() -> None:
    entered = Event()
    release = Event()

    class BlockingWorker:
        def run_one(self) -> RunWorkerResult:
            entered.set()
            release.wait(timeout=2)
            return RunWorkerResult(outcome="idle")

    supervisor = RunWorkerSupervisor(
        BlockingWorker(),
        idle_wait_seconds=0.05,
        error_wait_seconds=0.1,
        shutdown_timeout_seconds=0.1,
    )

    supervisor.start()
    assert entered.wait(timeout=1)
    assert supervisor.stop() is False
    assert supervisor.is_running is True
    release.set()
    assert supervisor.stop() is True
    assert supervisor.is_running is False


def test_supervisor_recovers_expired_lease_after_restart() -> None:
    queue, repository, run_id = _runtime(max_attempts=3)
    old_now = datetime.now(UTC) - timedelta(seconds=10)
    job_id = queue.enqueue(run_id, now=old_now)
    stale = queue.claim("old-worker", lease_seconds=5, now=old_now)
    completed = Event()
    assert stale is not None

    def execute(run: StoredRun) -> None:
        run.status = "completed"
        run.conversation_id = "restart-recovered"
        run.ended_at = datetime.now(UTC)
        completed.set()

    supervisor = RunWorkerSupervisor(
        _worker(queue, repository, execute),
        idle_wait_seconds=0.05,
        error_wait_seconds=0.1,
        shutdown_timeout_seconds=0.5,
    )

    supervisor.start()
    assert completed.wait(timeout=1)
    assert supervisor.stop() is True
    restored = repository.get(run_id)
    status = queue.get(job_id)
    assert restored is not None
    assert restored.status == "completed"
    assert restored.conversation_id == "restart-recovered"
    assert status is not None
    assert status.state == "completed"
    assert status.attempts == 2
    telemetry = supervisor.snapshot()
    assert telemetry.running is False
    assert telemetry.processed_total >= 1
    assert telemetry.error_total == 0
    assert telemetry.lease_lost_total == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"idle_wait_seconds": 0.01},
        {"idle_wait_seconds": 11},
        {"error_wait_seconds": 0.05},
        {"error_wait_seconds": 61},
        {"shutdown_timeout_seconds": 0.05},
        {"shutdown_timeout_seconds": 31},
    ],
)
def test_supervisor_rejects_unbounded_timing_configuration(overrides) -> None:
    class IdleWorker:
        def run_one(self) -> RunWorkerResult:
            return RunWorkerResult(outcome="idle")

    arguments = {
        "worker": IdleWorker(),
        "idle_wait_seconds": 0.05,
        "error_wait_seconds": 0.1,
        "shutdown_timeout_seconds": 0.5,
    }
    arguments.update(overrides)

    with pytest.raises(ValueError):
        RunWorkerSupervisor(**arguments)
