"""Single-job execution unit for the persistent run lease queue."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Literal, Protocol

from voxbench.control_plane.job_queue import (
    RunJobLease,
    RunJobQueue,
    RunJobQueueError,
)
from voxbench.control_plane.run_api import StoredRun

RunWorkerOutcome = Literal[
    "idle",
    "completed",
    "retry_scheduled",
    "failed",
    "run_missing",
    "lease_lost",
]
RunExecutor = Callable[[StoredRun], None]


class FencedRunRepository(Protocol):
    def get(self, run_id: str) -> StoredRun | None: ...

    def commit_leased_result(
        self,
        run: StoredRun,
        lease: RunJobLease,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> bool: ...


class SingleJobWorker(Protocol):
    def run_one(self) -> RunWorkerResult: ...


@dataclass(frozen=True)
class RunWorkerResult:
    outcome: RunWorkerOutcome
    job_id: str | None = None
    run_id: str | None = None
    attempt: int | None = None


@dataclass
class RunJobWorker:
    queue: RunJobQueue = field(repr=False)
    repository: FencedRunRepository = field(repr=False)
    execute: RunExecutor = field(repr=False)
    worker_id: str
    lease_seconds: int = 60
    heartbeat_interval_seconds: float = 20.0
    retry_delay_seconds: int = 5

    def __post_init__(self) -> None:
        _validate_alias(self.worker_id, reason_alias="run-worker-id-invalid")
        if not 5 <= self.lease_seconds <= 300:
            raise ValueError("run-worker-lease-seconds-invalid")
        if not 1.0 <= self.heartbeat_interval_seconds <= self.lease_seconds / 2:
            raise ValueError("run-worker-heartbeat-interval-invalid")
        if not 0 <= self.retry_delay_seconds <= 3_600:
            raise ValueError("run-worker-retry-delay-invalid")

    def run_one(self) -> RunWorkerResult:
        lease = self.queue.claim(
            self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if lease is None:
            return RunWorkerResult(outcome="idle")
        result = RunWorkerResult(
            outcome="lease_lost",
            job_id=lease.job_id,
            run_id=lease.run_id,
            attempt=lease.attempt,
        )
        run = self.repository.get(lease.run_id)
        if run is None:
            failed = self.queue.fail(
                lease,
                self.worker_id,
                failure_alias="run-not-found",
                retryable=False,
            )
            return RunWorkerResult(
                outcome="run_missing" if failed else "lease_lost",
                job_id=result.job_id,
                run_id=result.run_id,
                attempt=result.attempt,
            )

        heartbeat = _LeaseHeartbeat(
            queue=self.queue,
            lease=lease,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            interval_seconds=self.heartbeat_interval_seconds,
        )
        heartbeat.start()
        failure_alias: str | None = None
        try:
            self.execute(run)
        except Exception:
            failure_alias = "engine-harness-error"
        finally:
            lease_active = heartbeat.stop()
        if not lease_active:
            return result

        if failure_alias is None and run.status == "completed" and run.ended_at is not None:
            committed = self.repository.commit_leased_result(
                run,
                lease,
                self.worker_id,
            )
            return RunWorkerResult(
                outcome="completed" if committed else "lease_lost",
                job_id=result.job_id,
                run_id=result.run_id,
                attempt=result.attempt,
            )

        if failure_alias is None:
            failure_alias = (
                run.failure_alias
                if run.status == "failed" and _is_safe_alias(run.failure_alias)
                else "run-result-invalid"
            )
        return self._record_failure(run, lease, failure_alias)

    def _record_failure(
        self,
        run: StoredRun,
        lease: RunJobLease,
        failure_alias: str,
    ) -> RunWorkerResult:
        if lease.final_attempt:
            run.status = "failed"
            run.failure_alias = failure_alias
            run.ended_at = datetime.now(UTC)
            committed = self.repository.commit_leased_result(
                run,
                lease,
                self.worker_id,
            )
            outcome: RunWorkerOutcome = "failed" if committed else "lease_lost"
        else:
            retry_scheduled = self.queue.fail(
                lease,
                self.worker_id,
                failure_alias=failure_alias,
                retryable=True,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            outcome = "retry_scheduled" if retry_scheduled else "lease_lost"
        return RunWorkerResult(
            outcome=outcome,
            job_id=lease.job_id,
            run_id=lease.run_id,
            attempt=lease.attempt,
        )


@dataclass
class RunWorkerSupervisor:
    worker: SingleJobWorker = field(repr=False)
    idle_wait_seconds: float = 0.25
    error_wait_seconds: float = 1.0
    shutdown_timeout_seconds: float = 5.0
    _stop: Event = field(default_factory=Event, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _thread: Thread | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not 0.05 <= self.idle_wait_seconds <= 10:
            raise ValueError("run-worker-idle-wait-invalid")
        if not 0.1 <= self.error_wait_seconds <= 60:
            raise ValueError("run-worker-error-wait-invalid")
        if not 0.1 <= self.shutdown_timeout_seconds <= 30:
            raise ValueError("run-worker-shutdown-timeout-invalid")

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = Thread(
                target=self._run,
                name="voxbench-run-worker",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> bool:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=self.shutdown_timeout_seconds)
        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.worker.run_one()
            except Exception:
                if self._stop.wait(self.error_wait_seconds):
                    return
                continue
            if result.outcome == "idle":
                if self._stop.wait(self.idle_wait_seconds):
                    return
            elif result.outcome == "lease_lost" and self._stop.wait(
                self.error_wait_seconds
            ):
                return


@dataclass
class _LeaseHeartbeat:
    queue: RunJobQueue = field(repr=False)
    lease: RunJobLease = field(repr=False)
    worker_id: str
    lease_seconds: int
    interval_seconds: float
    _stop: Event = field(default_factory=Event, init=False, repr=False)
    _lost: Event = field(default_factory=Event, init=False, repr=False)
    _thread: Thread | None = field(default=None, init=False, repr=False)

    def start(self) -> None:
        self._thread = Thread(
            target=self._run,
            name=f"voxbench-heartbeat-{self.lease.job_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            if self._thread.is_alive():
                self._lost.set()
        return not self._lost.is_set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                active = self.queue.heartbeat(
                    self.lease,
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except RunJobQueueError:
                active = False
            if not active:
                self._lost.set()
                return


def _is_safe_alias(value: str | None) -> bool:
    if value is None:
        return False
    return (
        1 <= len(value) <= 128
        and value.isascii()
        and all(character.isalnum() or character in "-_." for character in value)
    )


def _validate_alias(value: str, *, reason_alias: str) -> None:
    if not _is_safe_alias(value):
        raise ValueError(reason_alias)
