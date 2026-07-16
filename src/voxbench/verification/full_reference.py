"""Selection boundary for future full-reference audio quality scorers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from voxbench.engine_harness.models import RecordingArtifact


class StageReferenceLike(Protocol):
    stage: str
    uri: str
    comparison_format: dict[str, Any]
    transformations: tuple[str, ...]
    comparison_ready: bool
    blocked_reason: str | None


@dataclass(frozen=True)
class FullReferenceCandidate:
    stage: str
    reference_uri: str
    degraded_uri: str
    comparison_format: dict[str, Any]
    degraded_format: dict[str, Any]
    transformations: tuple[str, ...]


@dataclass(frozen=True)
class FullReferenceBlock:
    stage: str
    reason: str


@dataclass(frozen=True)
class FullReferenceSelection:
    candidates: tuple[FullReferenceCandidate, ...]
    blocked: tuple[FullReferenceBlock, ...]


def select_full_reference_candidates(
    *,
    stage_references: Iterable[StageReferenceLike],
    recordings: Iterable[RecordingArtifact],
) -> FullReferenceSelection:
    """Pair ready stage references with recordings without invoking a scorer."""

    recordings_by_stage = {recording.stage: recording for recording in recordings}
    candidates = []
    blocked = []
    seen_stages: set[str] = set()
    for reference in stage_references:
        if reference.stage in seen_stages:
            blocked.append(
                FullReferenceBlock(
                    stage=reference.stage,
                    reason="duplicate-stage-reference",
                )
            )
            continue
        seen_stages.add(reference.stage)
        if not reference.comparison_ready:
            blocked.append(
                FullReferenceBlock(
                    stage=reference.stage,
                    reason=reference.blocked_reason or "reference-not-ready",
                )
            )
            continue
        recording = recordings_by_stage.get(reference.stage)
        if recording is None:
            blocked.append(
                FullReferenceBlock(
                    stage=reference.stage,
                    reason="stage-recording-missing",
                )
            )
            continue
        if not _comparison_formats_match(reference.comparison_format, recording.format):
            blocked.append(
                FullReferenceBlock(
                    stage=reference.stage,
                    reason="comparison-format-mismatch",
                )
            )
            continue
        candidates.append(
            FullReferenceCandidate(
                stage=reference.stage,
                reference_uri=reference.uri,
                degraded_uri=recording.uri,
                comparison_format=dict(reference.comparison_format),
                degraded_format=dict(recording.format),
                transformations=tuple(reference.transformations),
            )
        )
    return FullReferenceSelection(
        candidates=tuple(candidates),
        blocked=tuple(blocked),
    )


def _comparison_formats_match(
    reference_format: dict[str, Any],
    recording_format: dict[str, Any],
) -> bool:
    return all(
        reference_format.get(key) == recording_format.get(key)
        for key in ("encoding", "rate", "channels")
    )
