"""Phase 1 engine harness."""

from __future__ import annotations

from uuid import uuid4

from voxbench.engine_harness.models import HarnessResult
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

        return HarnessResult(
            run_id=run_id,
            conversation_id=conversation_id,
            recordings=recordings,
            spans=self.tracer.finished_spans(),
        )

