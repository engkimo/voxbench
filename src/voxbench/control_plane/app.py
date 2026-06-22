"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from voxbench.control_plane.run_api import RunApiState, create_runs_router


def create_app(*, artifact_root: Path | None = None) -> FastAPI:
    app = FastAPI(title="VoxBench Control Plane")
    state = RunApiState(artifact_root=artifact_root or Path("artifacts/recordings"))
    app.state.voxbench = state
    app.include_router(create_runs_router())
    return app


app = create_app()
