"""Treatment-level aggregation for comparable full-reference scores."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any, Literal

from voxbench.verification.scoring import (
    FullReferenceScorerContract,
    FullReferenceScoringReport,
)

FullReferenceAggregateState = Literal[
    "aggregated",
    "insufficient",
    "partial",
    "incomparable",
]

_SAFE_TREATMENT = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


@dataclass(frozen=True)
class FullReferenceStageAggregate:
    treatment: str
    stage: str
    scorer: str
    metric_name: str
    state: FullReferenceAggregateState
    samples_total: int
    scored_count: int
    unavailable_count: int
    blocked_count: int
    failed_count: int
    missing_count: int
    mean: float | None
    median: float | None
    minimum: float | None
    maximum: float | None
    population_stddev: float | None
    transformations: tuple[str, ...]


@dataclass(frozen=True)
class FullReferenceTreatmentReport:
    treatment: str
    minimum_samples: int
    contract: FullReferenceScorerContract
    stages: tuple[FullReferenceStageAggregate, ...]

    def safe_payload(self) -> dict[str, Any]:
        return {
            "metric_name": self.contract.metric_name,
            "minimum_samples": self.minimum_samples,
            "scorer": self.contract.scorer,
            "stages": [
                {
                    "blocked_count": stage.blocked_count,
                    "failed_count": stage.failed_count,
                    "maximum": stage.maximum,
                    "mean": stage.mean,
                    "median": stage.median,
                    "minimum": stage.minimum,
                    "missing_count": stage.missing_count,
                    "population_stddev": stage.population_stddev,
                    "samples_total": stage.samples_total,
                    "scored_count": stage.scored_count,
                    "stage": stage.stage,
                    "state": stage.state,
                    "transformations": list(stage.transformations),
                    "unavailable_count": stage.unavailable_count,
                }
                for stage in self.stages
            ],
            "treatment": self.treatment,
        }


def aggregate_full_reference_reports(
    *,
    treatment: str,
    reports: tuple[FullReferenceScoringReport, ...],
    minimum_samples: int = 3,
) -> FullReferenceTreatmentReport:
    """Aggregate only comparable stage scores across one declared treatment."""

    if not _SAFE_TREATMENT.fullmatch(treatment):
        raise ValueError("treatment must be a safe lowercase alias")
    if minimum_samples < 2:
        raise ValueError("minimum_samples must be at least 2")
    if not reports:
        raise ValueError("at least one scoring report is required")

    contract = reports[0].contract
    stage_order: list[str] = []
    indexed_reports = []
    for report in reports:
        if report.contract != contract:
            raise ValueError("all reports must use the same scorer contract")
        indexed = {}
        for result in report.results:
            if result.stage in indexed:
                raise ValueError("a report must contain at most one result per stage")
            indexed[result.stage] = result
            if result.stage not in stage_order:
                stage_order.append(result.stage)
        indexed_reports.append(indexed)

    stages = tuple(
        _aggregate_stage(
            treatment=treatment,
            stage=stage,
            contract=contract,
            indexed_reports=indexed_reports,
            minimum_samples=minimum_samples,
        )
        for stage in stage_order
    )
    return FullReferenceTreatmentReport(
        treatment=treatment,
        minimum_samples=minimum_samples,
        contract=contract,
        stages=stages,
    )


def _aggregate_stage(
    *,
    treatment: str,
    stage: str,
    contract: FullReferenceScorerContract,
    indexed_reports: list[dict[str, Any]],
    minimum_samples: int,
) -> FullReferenceStageAggregate:
    results = [indexed[stage] for indexed in indexed_reports if stage in indexed]
    scores = [result.score for result in results if result.state == "scored"]
    if any(score is None for score in scores):
        raise ValueError("scored results must contain a numeric score")
    numeric_scores = [float(score) for score in scores]
    transformation_sets = {
        result.transformations for result in results if result.state == "scored"
    }
    missing_count = len(indexed_reports) - len(results)
    unavailable_count = sum(result.state == "unavailable" for result in results)
    blocked_count = sum(result.state == "blocked" for result in results)
    failed_count = sum(result.state == "failed" for result in results)
    has_partial_samples = any(
        (unavailable_count, blocked_count, failed_count, missing_count)
    )

    if len(transformation_sets) > 1:
        state: FullReferenceAggregateState = "incomparable"
    elif has_partial_samples:
        state = "partial"
    elif len(numeric_scores) < minimum_samples:
        state = "insufficient"
    else:
        state = "aggregated"

    enough_scores = len(numeric_scores) >= minimum_samples
    return FullReferenceStageAggregate(
        treatment=treatment,
        stage=stage,
        scorer=contract.scorer,
        metric_name=contract.metric_name,
        state=state,
        samples_total=len(indexed_reports),
        scored_count=len(numeric_scores),
        unavailable_count=unavailable_count,
        blocked_count=blocked_count,
        failed_count=failed_count,
        missing_count=missing_count,
        mean=statistics.fmean(numeric_scores) if enough_scores else None,
        median=statistics.median(numeric_scores) if enough_scores else None,
        minimum=min(numeric_scores) if enough_scores else None,
        maximum=max(numeric_scores) if enough_scores else None,
        population_stddev=statistics.pstdev(numeric_scores) if enough_scores else None,
        transformations=(next(iter(transformation_sets)) if len(transformation_sets) == 1 else ()),
    )
