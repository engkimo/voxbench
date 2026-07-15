"""Phase 1 engine harness."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from voxbench.engine_harness.models import HarnessResult, MetricArtifact
from voxbench.engine_harness.plan import build_stage_plan
from voxbench.engine_harness.storage import RecordingSink
from voxbench.engine_harness.telemetry import HarnessTracer, span_attrs


class EngineHarness:
    """Thin wrapper that wires run_id, StageTap artifacts, and OTel spans."""

    def __init__(
        self,
        *,
        recording_sink: RecordingSink,
        tracer: HarnessTracer | None = None,
    ) -> None:
        self.recording_sink = recording_sink
        self.tracer = tracer or HarnessTracer()

    def run_once(
        self,
        *,
        run_id: str,
        resolved_config: dict[str, object],
        config_hash: str,
    ) -> HarnessResult:
        conversation_id = str(uuid4())
        recordings = []
        metrics = []
        host_sample_start = _host_sample_start()
        metrics.extend(_host_metrics(resolved_config, host_sample_start))
        with self.tracer.tracer.start_as_current_span(
            "voxbench.run",
            attributes=span_attrs(
                run_id=run_id,
                config_hash=config_hash,
                conversation_id=conversation_id,
            ),
        ):
            recording_duration_ms = _nominal_recording_duration_ms(resolved_config)
            for stage in build_stage_plan(resolved_config):
                with self.tracer.tracer.start_as_current_span(
                    f"stage.tap.{stage.stage}",
                    attributes=span_attrs(
                        run_id=run_id,
                        config_hash=config_hash,
                        conversation_id=conversation_id,
                        extra={
                            "voxbench.stage": stage.stage,
                            "voxbench.plugin": stage.plugin,
                        },
                    ),
                ):
                    recordings.append(
                        self.recording_sink.write_stage_wav(
                            run_id=run_id,
                            stage=stage.stage,
                            audio_format=stage.format,
                            duration_ms=recording_duration_ms,
                        )
                    )
                    metrics.extend(_nominal_cadence_metrics(resolved_config, stage.stage))
                    metrics.extend(_host_metrics(resolved_config, host_sample_start))

        metrics.extend(_host_metrics(resolved_config, host_sample_start))
        return HarnessResult(
            run_id=run_id,
            conversation_id=conversation_id,
            recordings=recordings,
            spans=self.tracer.finished_spans(),
            metrics=metrics,
        )


def _nominal_recording_duration_ms(resolved_config: dict[str, Any]) -> float:
    transport = resolved_config.get("spec", {}).get("transport", {})
    if not isinstance(transport, dict):
        return 0.0

    ptime_ms = transport.get("ptime_ms")
    if isinstance(ptime_ms, int | float) and ptime_ms > 0:
        return float(ptime_ms)
    return 0.0


def _nominal_cadence_metrics(
    resolved_config: dict[str, Any],
    stage_name: str,
) -> list[MetricArtifact]:
    stage_config = _stage_config(resolved_config, stage_name)
    if stage_config is None or "isochronous" not in stage_config.get("invariants_applicable", []):
        return []

    ptime_ms = resolved_config.get("spec", {}).get("transport", {}).get("ptime_ms")
    if not isinstance(ptime_ms, int | float) or ptime_ms <= 0:
        return []

    ts = datetime.now(UTC)
    return [
        MetricArtifact(stage=stage_name, name="frames_in", value=1.0, ts=ts),
        MetricArtifact(stage=stage_name, name="frames_out", value=1.0, ts=ts),
        MetricArtifact(stage=stage_name, name="frame_cadence_jitter_ms", value=0.0, ts=ts),
        MetricArtifact(
            stage=stage_name,
            name="expected_frame_interval_ms",
            value=float(ptime_ms),
            ts=ts,
        ),
    ]


def _stage_config(resolved_config: dict[str, Any], stage_name: str) -> dict[str, Any] | None:
    stages = resolved_config.get("spec", {}).get("media", {}).get("pipeline", [])
    if not isinstance(stages, list):
        return None
    for stage in stages:
        if isinstance(stage, dict) and stage.get("type") == stage_name:
            return stage
    return None


def _host_sample_start() -> tuple[float, float]:
    return (time.perf_counter(), time.process_time())


def _host_metrics(
    resolved_config: dict[str, Any],
    sample_start: tuple[float, float],
) -> list[MetricArtifact]:
    requested = resolved_config.get("spec", {}).get("observability", {}).get("host_metrics", [])
    if not isinstance(requested, list):
        return []

    requested_names = {name for name in requested if isinstance(name, str)}
    if not requested_names:
        return []

    wall_start, process_start = sample_start
    ts = datetime.now(UTC)
    metrics: list[MetricArtifact] = []
    if "cpu" in requested_names:
        wall_elapsed = max(time.perf_counter() - wall_start, 1e-9)
        process_elapsed = max(time.process_time() - process_start, 0.0)
        metrics.append(
            MetricArtifact(
                stage=None,
                name="cpu",
                value=(process_elapsed / wall_elapsed) * 100.0,
                ts=ts,
            )
        )
    if "active_tasks" in requested_names:
        metrics.append(
            MetricArtifact(
                stage=None,
                name="active_tasks",
                value=float(_active_task_count()),
                ts=ts,
            )
        )
    if "loop_lag" in requested_names:
        metrics.append(
            MetricArtifact(
                stage=None,
                name="loop_lag",
                value=_scheduler_lag_ms(),
                ts=ts,
            )
        )
    return metrics


def _active_task_count() -> int:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    return len(asyncio.all_tasks(loop))


def _scheduler_lag_ms() -> float:
    start = time.perf_counter()
    time.sleep(0)
    return max(0.0, (time.perf_counter() - start) * 1000.0)
