"""Phase 1 engine harness."""

from __future__ import annotations

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
        with self.tracer.tracer.start_as_current_span(
            "voxbench.run",
            attributes=span_attrs(
                run_id=run_id,
                config_hash=config_hash,
                conversation_id=conversation_id,
            ),
        ):
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
                        )
                    )
                    metrics.extend(_nominal_cadence_metrics(resolved_config, stage.stage))

        return HarnessResult(
            run_id=run_id,
            conversation_id=conversation_id,
            recordings=recordings,
            spans=self.tracer.finished_spans(),
            metrics=metrics,
        )


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
