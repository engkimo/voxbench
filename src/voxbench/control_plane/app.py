"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from voxbench.control_plane.run_api import RunApiState, create_runs_router
from voxbench.control_plane.storage_config import (
    MinioClientFactory,
    StorageReadiness,
    build_recording_storage_from_env,
)
from voxbench.engine_harness.storage import RecordingSink, RemoteRecordingReader


def create_app(
    *,
    artifact_root: Path | None = None,
    recording_sink: RecordingSink | None = None,
    storage_readiness: StorageReadiness | None = None,
    remote_recording_reader: RemoteRecordingReader | None = None,
    remote_audio_access_token: str | None = None,
) -> FastAPI:
    app = FastAPI(title="VoxBench Control Plane")
    state = RunApiState(
        artifact_root=artifact_root or Path("artifacts/recordings"),
        recording_sink=recording_sink,
        storage_readiness=storage_readiness,
        remote_recording_reader=remote_recording_reader,
        remote_audio_access_token=remote_audio_access_token,
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
    runtime = build_recording_storage_from_env(
        environ,
        client_factory=client_factory,
    )
    return create_app(
        artifact_root=artifact_root,
        recording_sink=runtime.recording_sink,
        storage_readiness=runtime.readiness,
        remote_recording_reader=runtime.remote_recording_reader,
        remote_audio_access_token=runtime.remote_audio_access_token,
    )


app = create_app_from_env()
