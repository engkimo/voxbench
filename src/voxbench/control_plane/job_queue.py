"""Postgres-backed lease queue for persistent run execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from voxbench.control_plane.models import RunJob as RunJobRow

RunJobState = Literal["queued", "leased", "completed", "failed"]

_MIN_LEASE_SECONDS = 5
_MAX_LEASE_SECONDS = 300
_MAX_RETRY_DELAY_SECONDS = 3_600
_MAX_ATTEMPTS_LIMIT = 100


class RunJobQueueError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("run job queue is unavailable")


@dataclass(frozen=True)
class RunJobLease:
    job_id: str
    run_id: str
    lease_token: str
    attempt: int
    lease_expires_at: datetime


@dataclass(frozen=True)
class RunJobStatus:
    job_id: str
    run_id: str
    state: RunJobState
    attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    failure_alias: str | None


class RunJobQueue(Protocol):
    def enqueue(self, run_id: str, *, now: datetime | None = None) -> str: ...

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> RunJobLease | None: ...

    def heartbeat(
        self,
        lease: RunJobLease,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> bool: ...

    def complete(
        self,
        lease: RunJobLease,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> bool: ...

    def fail(
        self,
        lease: RunJobLease,
        worker_id: str,
        *,
        failure_alias: str,
        retryable: bool,
        retry_delay_seconds: int = 0,
        now: datetime | None = None,
    ) -> bool: ...

    def get(self, job_id: str) -> RunJobStatus | None: ...


@dataclass
class PostgresRunJobQueue:
    session_factory: sessionmaker[Session] = field(repr=False)
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= _MAX_ATTEMPTS_LIMIT:
            raise ValueError("run-job-max-attempts-invalid")

    def enqueue(self, run_id: str, *, now: datetime | None = None) -> str:
        run_uuid = _parse_uuid(run_id, reason_alias="run-job-run-id-invalid")
        available_at = _normalize_now(now)
        try:
            with self.session_factory.begin() as session:
                existing = session.scalar(
                    select(RunJobRow).where(RunJobRow.run_id == run_uuid)
                )
                if existing is not None:
                    return str(existing.id)
                job = RunJobRow(
                    run_id=run_uuid,
                    state="queued",
                    available_at=available_at,
                    attempts=0,
                )
                try:
                    with session.begin_nested():
                        session.add(job)
                        session.flush()
                except IntegrityError:
                    existing = session.scalar(
                        select(RunJobRow).where(RunJobRow.run_id == run_uuid)
                    )
                    if existing is None:
                        raise
                    return str(existing.id)
                return str(job.id)
        except SQLAlchemyError as exc:
            raise RunJobQueueError from exc

    def claim(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> RunJobLease | None:
        _validate_alias(worker_id, reason_alias="run-job-worker-id-invalid")
        _validate_lease_seconds(lease_seconds)
        claimed_at = _normalize_now(now)
        lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
        try:
            with self.session_factory.begin() as session:
                session.execute(
                    _expire_exhausted_jobs_statement(claimed_at, self.max_attempts)
                )
                row = session.scalar(_claim_statement(claimed_at, self.max_attempts))
                if row is None:
                    return None
                lease_token = uuid4()
                row.state = "leased"
                row.lease_owner = worker_id
                row.lease_token = lease_token
                row.lease_expires_at = lease_expires_at
                row.attempts += 1
                row.failure_alias = None
                session.flush()
                return RunJobLease(
                    job_id=str(row.id),
                    run_id=str(row.run_id),
                    lease_token=str(lease_token),
                    attempt=row.attempts,
                    lease_expires_at=lease_expires_at,
                )
        except SQLAlchemyError as exc:
            raise RunJobQueueError from exc

    def heartbeat(
        self,
        lease: RunJobLease,
        worker_id: str,
        *,
        lease_seconds: int = 60,
        now: datetime | None = None,
    ) -> bool:
        _validate_alias(worker_id, reason_alias="run-job-worker-id-invalid")
        _validate_lease_seconds(lease_seconds)
        heartbeat_at = _normalize_now(now)
        try:
            with self.session_factory.begin() as session:
                result = session.execute(
                    update(RunJobRow)
                    .where(*_active_lease_conditions(lease, worker_id, heartbeat_at))
                    .values(
                        lease_expires_at=heartbeat_at + timedelta(seconds=lease_seconds)
                    )
                )
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise RunJobQueueError from exc

    def complete(
        self,
        lease: RunJobLease,
        worker_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        _validate_alias(worker_id, reason_alias="run-job-worker-id-invalid")
        completed_at = _normalize_now(now)
        try:
            with self.session_factory.begin() as session:
                result = session.execute(
                    update(RunJobRow)
                    .where(*_active_lease_conditions(lease, worker_id, completed_at))
                    .values(
                        state="completed",
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        failure_alias=None,
                    )
                )
                return result.rowcount == 1
        except SQLAlchemyError as exc:
            raise RunJobQueueError from exc

    def fail(
        self,
        lease: RunJobLease,
        worker_id: str,
        *,
        failure_alias: str,
        retryable: bool,
        retry_delay_seconds: int = 0,
        now: datetime | None = None,
    ) -> bool:
        _validate_alias(worker_id, reason_alias="run-job-worker-id-invalid")
        _validate_alias(failure_alias, reason_alias="run-job-failure-alias-invalid")
        if not 0 <= retry_delay_seconds <= _MAX_RETRY_DELAY_SECONDS:
            raise ValueError("run-job-retry-delay-invalid")
        failed_at = _normalize_now(now)
        try:
            with self.session_factory.begin() as session:
                row = session.scalar(
                    select(RunJobRow)
                    .where(*_active_lease_conditions(lease, worker_id, failed_at))
                    .with_for_update()
                )
                if row is None:
                    return False
                should_retry = retryable and row.attempts < self.max_attempts
                row.state = "queued" if should_retry else "failed"
                row.available_at = failed_at + timedelta(seconds=retry_delay_seconds)
                row.lease_owner = None
                row.lease_token = None
                row.lease_expires_at = None
                row.failure_alias = failure_alias
                return True
        except SQLAlchemyError as exc:
            raise RunJobQueueError from exc

    def get(self, job_id: str) -> RunJobStatus | None:
        job_uuid = _parse_uuid(job_id, reason_alias="run-job-id-invalid")
        try:
            with self.session_factory() as session:
                row = session.get(RunJobRow, job_uuid)
                return _to_status(row) if row is not None else None
        except SQLAlchemyError as exc:
            raise RunJobQueueError from exc


def _claim_statement(now: datetime, max_attempts: int) -> Select[tuple[RunJobRow]]:
    return (
        select(RunJobRow)
        .where(
            RunJobRow.attempts < max_attempts,
            or_(
                and_(RunJobRow.state == "queued", RunJobRow.available_at <= now),
                and_(
                    RunJobRow.state == "leased",
                    RunJobRow.lease_expires_at <= now,
                ),
            ),
        )
        .order_by(RunJobRow.available_at, RunJobRow.created_at, RunJobRow.id)
        .limit(1)
        .with_for_update(skip_locked=True, of=RunJobRow)
    )


def _expire_exhausted_jobs_statement(now: datetime, max_attempts: int):
    return (
        update(RunJobRow)
        .where(
            RunJobRow.attempts >= max_attempts,
            or_(
                RunJobRow.state == "queued",
                and_(
                    RunJobRow.state == "leased",
                    RunJobRow.lease_expires_at <= now,
                ),
            ),
        )
        .values(
            state="failed",
            lease_owner=None,
            lease_token=None,
            lease_expires_at=None,
            failure_alias="attempt-limit-exhausted",
        )
    )


def _active_lease_conditions(
    lease: RunJobLease,
    worker_id: str,
    now: datetime,
) -> tuple:
    return (
        RunJobRow.id == _parse_uuid(lease.job_id, reason_alias="run-job-id-invalid"),
        RunJobRow.state == "leased",
        RunJobRow.lease_owner == worker_id,
        RunJobRow.lease_token
        == _parse_uuid(lease.lease_token, reason_alias="run-job-lease-token-invalid"),
        RunJobRow.lease_expires_at > now,
    )


def _to_status(row: RunJobRow) -> RunJobStatus:
    return RunJobStatus(
        job_id=str(row.id),
        run_id=str(row.run_id),
        state=row.state,
        attempts=row.attempts,
        available_at=_as_utc(row.available_at),
        lease_owner=row.lease_owner,
        lease_expires_at=(
            _as_utc(row.lease_expires_at) if row.lease_expires_at is not None else None
        ),
        failure_alias=row.failure_alias,
    )


def _parse_uuid(value: str, *, reason_alias: str) -> UUID:
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ValueError(reason_alias) from None


def _validate_alias(value: str, *, reason_alias: str) -> None:
    if (
        not 1 <= len(value) <= 128
        or not value.isascii()
        or any(not (character.isalnum() or character in "-_.") for character in value)
    ):
        raise ValueError(reason_alias)


def _validate_lease_seconds(value: int) -> None:
    if not _MIN_LEASE_SECONDS <= value <= _MAX_LEASE_SECONDS:
        raise ValueError("run-job-lease-seconds-invalid")


def _normalize_now(value: datetime | None) -> datetime:
    now = value or datetime.now(UTC)
    return _as_utc(now)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
