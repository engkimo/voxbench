"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from voxbench.control_plane.audio_session import (
    RemoteAudioSessionAuth,
    build_remote_audio_session_from_env,
)
from voxbench.control_plane.job_queue import RunJobQueue
from voxbench.control_plane.repository_config import (
    EngineFactory,
    RepositoryReadiness,
    build_run_repository_from_env,
)
from voxbench.control_plane.run_api import (
    RunApiState,
    RunRepository,
    RunRepositoryError,
    create_runs_router,
)
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
    remote_audio_session_auth: RemoteAudioSessionAuth | None = None,
    repository: RunRepository | None = None,
    repository_readiness: RepositoryReadiness | None = None,
    job_queue: RunJobQueue | None = None,
) -> FastAPI:
    app = FastAPI(title="VoxBench Control Plane")
    state = RunApiState(
        artifact_root=artifact_root or Path("artifacts/recordings"),
        recording_sink=recording_sink,
        storage_readiness=storage_readiness,
        remote_recording_reader=remote_recording_reader,
        remote_audio_access_token=remote_audio_access_token,
        remote_audio_session_auth=remote_audio_session_auth,
        **({"repository": repository} if repository is not None else {}),
        **(
            {"repository_readiness": repository_readiness}
            if repository_readiness is not None
            else {}
        ),
        job_queue=job_queue,
    )
    app.state.voxbench = state

    @app.exception_handler(RunRepositoryError)
    async def handle_run_repository_error(
        _request: Request,
        _error: RunRepositoryError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "run repository is unavailable"},
            headers={"Retry-After": "1"},
        )

    app.include_router(create_runs_router())
    return app


def create_app_from_env(
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: MinioClientFactory | None = None,
    repository_engine_factory: EngineFactory | None = None,
    artifact_root: Path | None = None,
) -> FastAPI:
    repository_runtime = build_run_repository_from_env(
        environ,
        engine_factory=repository_engine_factory,
    )
    runtime = build_recording_storage_from_env(
        environ,
        client_factory=client_factory,
    )
    session_auth = build_remote_audio_session_from_env(
        environ,
        remote_audio_proxy_enabled=runtime.readiness.remote_audio_proxy_enabled,
    )
    return create_app(
        artifact_root=artifact_root,
        recording_sink=runtime.recording_sink,
        storage_readiness=runtime.readiness,
        remote_recording_reader=runtime.remote_recording_reader,
        remote_audio_access_token=runtime.remote_audio_access_token,
        remote_audio_session_auth=session_auth,
        repository=repository_runtime.repository,
        repository_readiness=repository_runtime.readiness,
        job_queue=repository_runtime.job_queue,
    )


app = create_app_from_env()
