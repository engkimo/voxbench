"""Safe execution boundary for optional full-reference audio scorers."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from voxbench.engine_harness.models import MetricArtifact
from voxbench.verification.full_reference import (
    FullReferenceCandidate,
    FullReferenceSelection,
)

FullReferenceScoreState = Literal["scored", "unavailable", "blocked", "failed"]

_SAFE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_REASON = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_SAFE_TRANSFORMATION = re.compile(r"^[a-z0-9][a-z0-9_.:>-]{0,127}$")


@dataclass(frozen=True)
class FullReferenceScorerContract:
    """Stable metadata needed to normalize one scorer's output."""

    scorer: str
    metric_name: str
    minimum_score: float
    maximum_score: float

    def __post_init__(self) -> None:
        if not _SAFE_NAME.fullmatch(self.scorer):
            raise ValueError("scorer must be a safe lowercase alias")
        if not _SAFE_NAME.fullmatch(self.metric_name):
            raise ValueError("metric_name must be a safe lowercase alias")
        if not math.isfinite(self.minimum_score) or not math.isfinite(self.maximum_score):
            raise ValueError("score bounds must be finite")
        if self.minimum_score >= self.maximum_score:
            raise ValueError("minimum_score must be less than maximum_score")


@dataclass(frozen=True)
class FullReferenceScorerReadiness:
    available: bool
    reason_alias: str | None = None


@dataclass(frozen=True)
class FullReferenceMeasurement:
    score: float
    transformations: tuple[str, ...] = ()


class FullReferenceScorer(Protocol):
    """Optional scorer adapter implemented by a dependency-specific module."""

    contract: FullReferenceScorerContract

    def readiness(self) -> FullReferenceScorerReadiness: ...

    def score(
        self,
        candidate: FullReferenceCandidate,
    ) -> float | FullReferenceMeasurement: ...


@dataclass(frozen=True)
class FullReferenceScoreResult:
    stage: str
    scorer: str
    metric_name: str
    state: FullReferenceScoreState
    score: float | None
    reason_alias: str | None
    transformations: tuple[str, ...] = ()


@dataclass(frozen=True)
class FullReferenceScoringReport:
    contract: FullReferenceScorerContract
    results: tuple[FullReferenceScoreResult, ...]


def score_full_reference_selection(
    selection: FullReferenceSelection,
    scorer: FullReferenceScorer,
) -> FullReferenceScoringReport:
    """Score eligible pairs while converting all failures to safe explicit states."""

    contract = scorer.contract
    results: list[FullReferenceScoreResult] = []

    try:
        readiness = scorer.readiness()
    except Exception:  # A scorer dependency must not leak its raw error into artifacts.
        readiness = FullReferenceScorerReadiness(
            available=False,
            reason_alias="scorer-readiness-error",
        )

    if not readiness.available:
        reason_alias = _safe_reason(readiness.reason_alias, fallback="scorer-unavailable")
        results.extend(
            _result(
                candidate.stage,
                contract,
                state="unavailable",
                reason_alias=reason_alias,
                transformations=_safe_transformations(candidate.transformations),
            )
            for candidate in selection.candidates
        )
    else:
        results.extend(_score_candidate(candidate, scorer) for candidate in selection.candidates)

    results.extend(
        _result(
            block.stage,
            contract,
            state="blocked",
            reason_alias=_safe_reason(block.reason, fallback="input-blocked"),
        )
        for block in selection.blocked
    )
    return FullReferenceScoringReport(contract=contract, results=tuple(results))


def full_reference_scores_to_metrics(
    report: FullReferenceScoringReport,
    *,
    ts: datetime | None = None,
) -> list[MetricArtifact]:
    """Convert successful scores only; status states never become synthetic zeroes."""

    observed_at = ts or datetime.now(UTC)
    return [
        MetricArtifact(
            stage=result.stage,
            name=result.metric_name,
            value=result.score,
            ts=observed_at,
        )
        for result in report.results
        if result.state == "scored" and result.score is not None
    ]


def _score_candidate(
    candidate: FullReferenceCandidate,
    scorer: FullReferenceScorer,
) -> FullReferenceScoreResult:
    contract = scorer.contract
    transformations = _safe_transformations(candidate.transformations)
    try:
        raw_measurement = scorer.score(candidate)
        if isinstance(raw_measurement, FullReferenceMeasurement):
            score = float(raw_measurement.score)
            transformations += _safe_transformations(
                raw_measurement.transformations,
                fallback="scorer-input-transform",
            )
        else:
            score = float(raw_measurement)
    except Exception:  # Raw binary/library/path errors are intentionally discarded.
        return _result(
            candidate.stage,
            contract,
            state="failed",
            reason_alias="scorer-error",
            transformations=transformations,
        )
    if not math.isfinite(score):
        return _result(
            candidate.stage,
            contract,
            state="failed",
            reason_alias="score-not-finite",
            transformations=transformations,
        )
    if not contract.minimum_score <= score <= contract.maximum_score:
        return _result(
            candidate.stage,
            contract,
            state="failed",
            reason_alias="score-out-of-range",
            transformations=transformations,
        )
    return _result(
        candidate.stage,
        contract,
        state="scored",
        score=score,
        transformations=transformations,
    )


def _result(
    stage: str,
    contract: FullReferenceScorerContract,
    *,
    state: FullReferenceScoreState,
    score: float | None = None,
    reason_alias: str | None = None,
    transformations: tuple[str, ...] = (),
) -> FullReferenceScoreResult:
    return FullReferenceScoreResult(
        stage=stage,
        scorer=contract.scorer,
        metric_name=contract.metric_name,
        state=state,
        score=score,
        reason_alias=reason_alias,
        transformations=transformations,
    )


def _safe_reason(reason: str | None, *, fallback: str) -> str:
    if reason is not None and _SAFE_REASON.fullmatch(reason):
        return reason
    return fallback


def _safe_transformations(
    transformations: tuple[str, ...],
    *,
    fallback: str = "reference-transform",
) -> tuple[str, ...]:
    return tuple(
        transformation
        if isinstance(transformation, str) and _SAFE_TRANSFORMATION.fullmatch(transformation)
        else fallback
        for transformation in transformations
    )
