"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from voxbench.control_plane.audio_session import (
    RemoteAudioSessionAuth,
    build_remote_audio_session_from_env,
)
from voxbench.control_plane.job_queue import RunJobQueue, RunJobQueueError
from voxbench.control_plane.repository_config import (
    EngineFactory,
    RepositoryReadiness,
    build_run_repository_from_env,
)
from voxbench.control_plane.run_api import (
    RunApiState,
    RunRepository,
    RunRepositoryError,
    StoredRun,
    _populate_stored_run_result,
    create_runs_router,
)
from voxbench.control_plane.run_worker import (
    FencedRunRepository,
    RunJobWorker,
    RunWorkerSupervisor,
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
    persistent_run_submitter: Callable[[StoredRun], str] | None = None,
    run_worker_supervisor: RunWorkerSupervisor | None = None,
) -> FastAPI:
    supervisor = run_worker_supervisor

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if supervisor is not None:
            supervisor.start()
        try:
            yield
        finally:
            if supervisor is not None:
                supervisor.stop()

    app = FastAPI(title="VoxBench Control Plane", lifespan=lifespan)
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
        persistent_run_submitter=persistent_run_submitter,
    )
    if supervisor is None and job_queue is not None and persistent_run_submitter is not None:
        supervisor = RunWorkerSupervisor(
            RunJobWorker(
                queue=job_queue,
                repository=cast(FencedRunRepository, state.repository),
                execute=lambda run: _populate_stored_run_result(run, state),
                worker_id=f"worker-{uuid4().hex}",
            )
        )
    app.state.voxbench = state
    app.state.run_worker_supervisor = supervisor

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

    @app.exception_handler(RunJobQueueError)
    async def handle_run_job_queue_error(
        _request: Request,
        _error: RunJobQueueError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "run job queue is unavailable"},
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
        persistent_run_submitter=(
            repository_runtime.repository.save_queued_run
            if repository_runtime.job_queue is not None
            else None
        ),
    )


app = create_app_from_env()
