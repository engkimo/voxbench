"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from voxbench.control_plane.run_api import RunApiState, create_runs_router
from voxbench.control_plane.storage_config import (
    MinioClientFactory,
    StorageReadiness,
    build_recording_sink_from_env,
)
from voxbench.engine_harness.storage import RecordingSink


def create_app(
    *,
    artifact_root: Path | None = None,
    recording_sink: RecordingSink | None = None,
    storage_readiness: StorageReadiness | None = None,
) -> FastAPI:
    app = FastAPI(title="VoxBench Control Plane")
    state = RunApiState(
        artifact_root=artifact_root or Path("artifacts/recordings"),
        recording_sink=recording_sink,
        storage_readiness=storage_readiness,
    )
    app.state.voxbench = state
    app.include_router(create_runs_router())
    return app


def create_app_from_env(
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: MinioClientFactory | None = None,
    artifact_root: Path | None = None,
) -> FastAPI:
    recording_sink, storage_readiness = build_recording_sink_from_env(
        environ,
        client_factory=client_factory,
    )
    return create_app(
        artifact_root=artifact_root,
        recording_sink=recording_sink,
        storage_readiness=storage_readiness,
    )


app = create_app_from_env()
