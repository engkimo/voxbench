"""OpenTelemetry helpers for Phase 1 harness spans."""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from voxbench.engine_harness.models import SpanArtifact


class HarnessTracer:
    """Owns an isolated tracer provider and in-memory exporter per harness."""

    def __init__(self) -> None:
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("voxbench.engine_harness")

    def finished_spans(self) -> list[SpanArtifact]:
        spans = self.exporter.get_finished_spans()
        artifacts: list[SpanArtifact] = []
        for span in spans:
            context = span.get_span_context()
            parent = span.parent
            artifacts.append(
                SpanArtifact(
                    trace_id=f"{context.trace_id:032x}",
                    span_id=f"{context.span_id:016x}",
                    parent_id=f"{parent.span_id:016x}" if parent is not None else None,
                    name=span.name,
                    start_ns=span.start_time,
                    end_ns=span.end_time,
                    attrs=dict(span.attributes or {}),
                )
            )
        return artifacts


def span_attrs(
    *,
    run_id: str,
    config_hash: str,
    conversation_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "voxbench.run_id": run_id,
        "voxbench.config_hash": config_hash,
        "conversation_id": conversation_id,
    }
    if extra:
        attrs.update(extra)
    return attrs

