"""Thin Pipecat integration boundary.

Phase 1 keeps concrete engine/provider processors behind plugin adapters. The only
Pipecat API used here is the documented Pipeline([...]) constructor.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from voxbench.engine_harness.errors import HarnessDependencyError


class PipecatProcessorAdapter(Protocol):
    """Adapter implemented by engine/provider/processor plugins."""

    def build_processor(self) -> object:
        """Return a Pipecat FrameProcessor-compatible object."""


def build_pipeline(processors: Sequence[object]) -> object:
    """Build a Pipecat Pipeline from plugin-provided processors."""

    try:
        from pipecat.pipeline.pipeline import Pipeline
    except ImportError as exc:
        raise HarnessDependencyError("pipecat is required to build a runtime pipeline") from exc
    return Pipeline(list(processors))

