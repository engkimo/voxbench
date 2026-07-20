"""Safe process configuration for the run repository."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, NoSuchModuleError
from sqlalchemy.orm import sessionmaker

RUN_REPOSITORY_ENV = "VOXBENCH_RUN_REPOSITORY"
DATABASE_URL_ENV = "VOXBENCH_DATABASE_URL"

RepositoryMode = Literal["memory", "postgres"]
RepositoryState = Literal["ready", "configured"]
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


@dataclass(frozen=True)
class RunRepositoryRuntime:
    repository: Any = field(repr=False)
    readiness: RepositoryReadiness
    engine: Engine | None = field(default=None, repr=False)


def memory_repository_readiness() -> RepositoryReadiness:
    return RepositoryReadiness(mode="memory", state="ready")


def build_run_repository_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    engine_factory: EngineFactory | None = None,
) -> RunRepositoryRuntime:
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
            )
        )
    except (ImportError, NoSuchModuleError):
        raise RepositoryConfigurationError("postgres-driver-unavailable") from None
    except Exception:
        raise RepositoryConfigurationError("database-engine-configuration-failed") from None

    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    return RunRepositoryRuntime(
        repository=PostgresRunRepository(sessions),
        readiness=RepositoryReadiness(
            mode="postgres",
            state="configured",
            reason_alias="connectivity-and-migrations-not-checked",
        ),
        engine=engine,
    )
