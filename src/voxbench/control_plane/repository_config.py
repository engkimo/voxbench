"""Safe process configuration for the run repository."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from queue import Empty, Queue
from threading import Thread
from typing import Any, Literal

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError
from sqlalchemy.orm import sessionmaker

RUN_REPOSITORY_ENV = "VOXBENCH_RUN_REPOSITORY"
DATABASE_URL_ENV = "VOXBENCH_DATABASE_URL"
POSTGRES_PROBE_ENV = "VOXBENCH_POSTGRES_PROBE"
POSTGRES_PROBE_TIMEOUT_MS_ENV = "VOXBENCH_POSTGRES_PROBE_TIMEOUT_MS"
POSTGRES_STATEMENT_TIMEOUT_MS_ENV = "VOXBENCH_POSTGRES_STATEMENT_TIMEOUT_MS"
EXPECTED_ALEMBIC_HEAD = "0008_run_job_leases"

_DEFAULT_PROBE_TIMEOUT_MS = 2_000
_MIN_PROBE_TIMEOUT_MS = 10
_MAX_PROBE_TIMEOUT_MS = 10_000
_DEFAULT_STATEMENT_TIMEOUT_MS = 5_000
_MIN_STATEMENT_TIMEOUT_MS = 100
_MAX_STATEMENT_TIMEOUT_MS = 30_000

RepositoryMode = Literal["memory", "postgres"]
RepositoryState = Literal["ready", "configured", "unavailable"]
EngineFactory = Callable[[str], Engine]


class RepositoryConfigurationError(RuntimeError):
    def __init__(
        self,
        reason_alias: str,
        *,
        missing_env_names: tuple[str, ...] = (),
    ) -> None:
        self.reason_alias = reason_alias
        self.missing_env_names = missing_env_names
        message = f"run repository configuration failed: {reason_alias}"
        if missing_env_names:
            message += f" ({', '.join(missing_env_names)})"
        super().__init__(message)


@dataclass(frozen=True)
class RepositoryReadiness:
    mode: RepositoryMode
    state: RepositoryState
    reason_alias: str | None = None
    job_queue_enabled: bool = False
    statement_timeout_ms: int | None = None


@dataclass(frozen=True)
class RunRepositoryRuntime:
    repository: Any = field(repr=False)
    readiness: RepositoryReadiness
    engine: Engine | None = field(default=None, repr=False)
    job_queue: Any | None = field(default=None, repr=False)


def memory_repository_readiness() -> RepositoryReadiness:
    return RepositoryReadiness(mode="memory", state="ready")


def build_run_repository_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    engine_factory: EngineFactory | None = None,
) -> RunRepositoryRuntime:
    from voxbench.control_plane.job_queue import PostgresRunJobQueue
    from voxbench.control_plane.run_api import (
        InMemoryRunRepository,
        PostgresRunRepository,
    )

    values = os.environ if environ is None else environ
    mode = values.get(RUN_REPOSITORY_ENV, "memory").strip().lower()
    if mode == "memory":
        return RunRepositoryRuntime(
            repository=InMemoryRunRepository(),
            readiness=memory_repository_readiness(),
        )
    if mode != "postgres":
        raise RepositoryConfigurationError("run-repository-mode-invalid")

    if DATABASE_URL_ENV not in values:
        raise RepositoryConfigurationError(
            "database-url-missing",
            missing_env_names=(DATABASE_URL_ENV,),
        )
    database_url = values[DATABASE_URL_ENV]
    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        raise RepositoryConfigurationError("database-url-invalid") from None
    if parsed_url.drivername != "postgresql+psycopg" or not parsed_url.database:
        raise RepositoryConfigurationError("database-url-invalid")
    probe_enabled = _parse_boolean(
        values.get(POSTGRES_PROBE_ENV, "false"),
        reason_alias="postgres-probe-flag-invalid",
    )
    probe_timeout_ms = _parse_bounded_integer(
        values.get(POSTGRES_PROBE_TIMEOUT_MS_ENV, str(_DEFAULT_PROBE_TIMEOUT_MS)),
        minimum=_MIN_PROBE_TIMEOUT_MS,
        maximum=_MAX_PROBE_TIMEOUT_MS,
        reason_alias="postgres-probe-timeout-invalid",
    )
    statement_timeout_ms = _parse_bounded_integer(
        values.get(
            POSTGRES_STATEMENT_TIMEOUT_MS_ENV,
            str(_DEFAULT_STATEMENT_TIMEOUT_MS),
        ),
        minimum=_MIN_STATEMENT_TIMEOUT_MS,
        maximum=_MAX_STATEMENT_TIMEOUT_MS,
        reason_alias="postgres-statement-timeout-invalid",
    )

    try:
        engine = (
            engine_factory(database_url)
            if engine_factory is not None
            else create_engine(
                database_url,
                hide_parameters=True,
                pool_pre_ping=True,
                pool_size=5,
                max_overflow=5,
                pool_timeout=5,
                connect_args={
                    "options": f"-c statement_timeout={statement_timeout_ms}"
                },
            )
        )
    except (ImportError, NoSuchModuleError):
        raise RepositoryConfigurationError("postgres-driver-unavailable") from None
    except Exception:
        raise RepositoryConfigurationError("database-engine-configuration-failed") from None

    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    readiness = (
        _probe_postgres(engine, timeout_ms=probe_timeout_ms)
        if probe_enabled
        else RepositoryReadiness(
            mode="postgres",
            state="configured",
            reason_alias="connectivity-and-migrations-not-checked",
            job_queue_enabled=True,
        )
    )
    readiness = replace(
        readiness,
        job_queue_enabled=True,
        statement_timeout_ms=statement_timeout_ms,
    )
    return RunRepositoryRuntime(
        repository=PostgresRunRepository(sessions),
        readiness=readiness,
        engine=engine,
        job_queue=PostgresRunJobQueue(sessions),
    )


def _probe_postgres(engine: Engine, *, timeout_ms: int) -> RepositoryReadiness:
    result: Queue[RepositoryReadiness] = Queue(maxsize=1)

    def target() -> None:
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1").scalar_one()
                migration_heads = set(
                    connection.exec_driver_sql(
                        "SELECT version_num FROM alembic_version"
                    ).scalars()
                )
        except Exception:
            result.put(
                RepositoryReadiness(
                    mode="postgres",
                    state="unavailable",
                    reason_alias="postgres-probe-failed",
                )
            )
            return
        if migration_heads != {EXPECTED_ALEMBIC_HEAD}:
            result.put(
                RepositoryReadiness(
                    mode="postgres",
                    state="unavailable",
                    reason_alias="migration-head-mismatch",
                )
            )
            return
        result.put(
            RepositoryReadiness(
                mode="postgres",
                state="ready",
                job_queue_enabled=True,
            )
        )

    thread = Thread(target=target, name="voxbench-postgres-readiness", daemon=True)
    thread.start()
    thread.join(timeout_ms / 1_000)
    if thread.is_alive():
        return RepositoryReadiness(
            mode="postgres",
            state="unavailable",
            reason_alias="postgres-probe-timeout",
        )
    try:
        return result.get_nowait()
    except Empty:
        return RepositoryReadiness(
            mode="postgres",
            state="unavailable",
            reason_alias="postgres-probe-failed",
        )


def _parse_boolean(value: str, *, reason_alias: str) -> bool:
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise RepositoryConfigurationError(reason_alias)


def _parse_bounded_integer(
    value: str,
    *,
    minimum: int,
    maximum: int,
    reason_alias: str,
) -> int:
    normalized = value.strip()
    if not normalized.isdigit():
        raise RepositoryConfigurationError(reason_alias)
    parsed = int(normalized)
    if not minimum <= parsed <= maximum:
        raise RepositoryConfigurationError(reason_alias)
    return parsed
