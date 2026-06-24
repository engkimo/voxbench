"""Run API models and router."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from voxbench.engine_harness.harness import EngineHarness
from voxbench.engine_harness.models import MetricArtifact, RecordingArtifact, SpanArtifact
from voxbench.engine_harness.storage import LocalRecordingSink
from voxbench.registry.errors import RegistryError
from voxbench.registry.service import RegistryService
from voxbench.verification import VerificationResult, verify_recordings


class RunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_name: str
    configs: list[dict[str, Any]]
    manifests: list[dict[str, Any]]
    call_id: str | None = None


class RecordingResponse(BaseModel):
    stage: str
    uri: str
    format: dict[str, Any]
    duration_ms: float


class SpanResponse(BaseModel):
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    start_ns: int
    end_ns: int
    attrs: dict[str, Any]


class VerificationResponse(BaseModel):
    stage: str
    invariant: str
    passed: bool
    observed: dict[str, Any]
    expected: dict[str, Any]
    detail: str


class MetricResponse(BaseModel):
    stage: str | None
    name: str
    value: float
    ts: datetime


class RunResponse(BaseModel):
    run_id: str
    config_hash: str
    call_id: str | None
    conversation_id: str
    provider: str
    engine: str
    status: str
    recordings: list[RecordingResponse]
    spans: list[SpanResponse]
    metrics: list[MetricResponse]
    verifications: list[VerificationResponse]


@dataclass
class StoredRun:
    run_id: str
    config_hash: str
    call_id: str | None
    conversation_id: str
    provider: str
    engine: str
    status: str
    started_at: datetime
    ended_at: datetime
    resolved_config: dict[str, Any]
    recordings: list[RecordingArtifact]
    spans: list[SpanArtifact]
    metrics: list[MetricArtifact]
    verifications: list[VerificationResult] = field(default_factory=list)

    def to_response(self) -> RunResponse:
        return RunResponse(
            run_id=self.run_id,
            config_hash=self.config_hash,
            call_id=self.call_id,
            conversation_id=self.conversation_id,
            provider=self.provider,
            engine=self.engine,
            status=self.status,
            recordings=[RecordingResponse(**recording.__dict__) for recording in self.recordings],
            spans=[SpanResponse(**span.__dict__) for span in self.spans],
            metrics=[MetricResponse(**metric.__dict__) for metric in self.metrics],
            verifications=[
                VerificationResponse(**verification.__dict__)
                for verification in self.verifications
            ],
        )


@dataclass
class InMemoryRunRepository:
    runs: dict[str, StoredRun] = field(default_factory=dict)

    def save(self, run: StoredRun) -> None:
        self.runs[run.run_id] = run

    def get(self, run_id: str) -> StoredRun | None:
        return self.runs.get(run_id)


@dataclass
class RunApiState:
    artifact_root: Path = Path("artifacts/recordings")
    repository: InMemoryRunRepository = field(default_factory=InMemoryRunRepository)

    def create_harness(self) -> EngineHarness:
        return EngineHarness(recording_sink=LocalRecordingSink(self.artifact_root))


def get_run_api_state(request: Request) -> RunApiState:
    return request.app.state.voxbench


RunApiStateDependency = Annotated[RunApiState, Depends(get_run_api_state)]


def create_runs_router() -> APIRouter:
    router = APIRouter()

    @router.post("/runs", response_model=RunResponse)
    async def create_run(
        request: RunCreateRequest,
        api_state: RunApiStateDependency,
    ) -> RunResponse:
        registry = RegistryService()
        try:
            for manifest in request.manifests:
                registry.register_manifest(manifest)
            for config in request.configs:
                registry.register_config(config)
            resolved = registry.resolve_config(request.config_name)
        except RegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        run_id = str(uuid4())
        started_at = datetime.now(UTC)
        harness_result = api_state.create_harness().run_once(
            run_id=run_id,
            resolved_config=resolved.resolved,
            config_hash=resolved.hash,
        )
        ended_at = datetime.now(UTC)

        stored = StoredRun(
            run_id=run_id,
            config_hash=resolved.hash,
            call_id=request.call_id,
            conversation_id=harness_result.conversation_id,
            provider=resolved.resolved["spec"]["ai"]["provider"],
            engine=resolved.resolved["spec"]["engine"]["kind"],
            status="completed",
            started_at=started_at,
            ended_at=ended_at,
            resolved_config=resolved.resolved,
            recordings=harness_result.recordings,
            spans=harness_result.spans,
            metrics=harness_result.metrics,
            verifications=verify_recordings(
                resolved_config=resolved.resolved,
                recordings=harness_result.recordings,
                metrics=harness_result.metrics,
            ),
        )
        api_state.repository.save(stored)
        return stored.to_response()

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(
        run_id: str,
        api_state: RunApiStateDependency,
    ) -> RunResponse:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        return stored.to_response()

    @router.get("/runs/{run_id}/verifications", response_model=list[VerificationResponse])
    async def get_run_verifications(
        run_id: str,
        api_state: RunApiStateDependency,
    ) -> list[VerificationResponse]:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        return [
            VerificationResponse(**verification.__dict__)
            for verification in stored.verifications
        ]

    @router.get("/runs/{run_id}/metrics", response_model=list[MetricResponse])
    async def get_run_metrics(
        run_id: str,
        api_state: RunApiStateDependency,
    ) -> list[MetricResponse]:
        stored = api_state.repository.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"unknown run '{run_id}'")
        return [MetricResponse(**metric.__dict__) for metric in stored.metrics]

    return router
