"""Treatment-level aggregation for comparable full-reference scores."""

from __future__ import annotations

import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
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
_SAFE_TRANSFORMATION = re.compile(r"^[a-z0-9][a-z0-9_.:>-]{0,127}$")
_MAX_REPORT_BYTES = 1_000_000


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
            "score_range": {
                "maximum": self.contract.maximum_score,
                "minimum": self.contract.minimum_score,
            },
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


def write_full_reference_treatment_report(
    report: FullReferenceTreatmentReport,
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report.safe_payload(), handle, indent=2, sort_keys=True)
        handle.write("\n")


def load_full_reference_treatment_report(path: Path) -> FullReferenceTreatmentReport:
    """Load a bounded path-free treatment report with strict field validation."""

    if path.stat().st_size > _MAX_REPORT_BYTES:
        raise ValueError("treatment report exceeds size limit")
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("treatment report root must be an object")
    if "aggregate" in payload:
        payload = payload["aggregate"]
        if not isinstance(payload, dict):
            raise ValueError("treatment report aggregate must be an object")
    treatment = payload.get("treatment")
    if not isinstance(treatment, str) or not _SAFE_TREATMENT.fullmatch(treatment):
        raise ValueError("treatment report has an invalid treatment alias")
    minimum_samples = payload.get("minimum_samples")
    if not _non_negative_int(minimum_samples) or minimum_samples < 2:
        raise ValueError("treatment report has invalid minimum_samples")
    score_range = payload.get("score_range")
    if not isinstance(score_range, dict):
        raise ValueError("treatment report has invalid score_range")
    contract = FullReferenceScorerContract(
        scorer=payload.get("scorer"),
        metric_name=payload.get("metric_name"),
        minimum_score=_finite_number(score_range.get("minimum"), "score minimum"),
        maximum_score=_finite_number(score_range.get("maximum"), "score maximum"),
    )
    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, list):
        raise ValueError("treatment report stages must be an array")
    stages = tuple(
        _load_stage(treatment=treatment, contract=contract, payload=stage)
        for stage in raw_stages
    )
    if len({stage.stage for stage in stages}) != len(stages):
        raise ValueError("treatment report contains duplicate stages")
    return FullReferenceTreatmentReport(
        treatment=treatment,
        minimum_samples=minimum_samples,
        contract=contract,
        stages=stages,
    )


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


def _load_stage(
    *,
    treatment: str,
    contract: FullReferenceScorerContract,
    payload: Any,
) -> FullReferenceStageAggregate:
    if not isinstance(payload, dict):
        raise ValueError("treatment stage must be an object")
    stage = payload.get("stage")
    if not isinstance(stage, str) or not _SAFE_TREATMENT.fullmatch(stage):
        raise ValueError("treatment report has an invalid stage alias")
    state = payload.get("state")
    if state not in {"aggregated", "insufficient", "partial", "incomparable"}:
        raise ValueError("treatment report has an invalid aggregate state")
    count_names = (
        "samples_total",
        "scored_count",
        "unavailable_count",
        "blocked_count",
        "failed_count",
        "missing_count",
    )
    counts = {name: payload.get(name) for name in count_names}
    if not all(_non_negative_int(value) for value in counts.values()):
        raise ValueError("treatment report has an invalid sample count")
    if (
        counts["scored_count"]
        + counts["unavailable_count"]
        + counts["blocked_count"]
        + counts["failed_count"]
        + counts["missing_count"]
        != counts["samples_total"]
    ):
        raise ValueError("treatment report sample counts are inconsistent")
    transformations = payload.get("transformations")
    if not isinstance(transformations, list) or not all(
        isinstance(value, str) and _SAFE_TRANSFORMATION.fullmatch(value)
        for value in transformations
    ):
        raise ValueError("treatment report has invalid transformations")
    stats = {
        name: _optional_finite_number(payload.get(name), name)
        for name in ("mean", "median", "minimum", "maximum", "population_stddev")
    }
    if state == "aggregated" and any(value is None for value in stats.values()):
        raise ValueError("aggregated stage must contain complete statistics")
    if payload.get("scorer", contract.scorer) != contract.scorer or payload.get(
        "metric_name", contract.metric_name
    ) != contract.metric_name:
        raise ValueError("stage scorer metadata does not match report contract")
    return FullReferenceStageAggregate(
        treatment=treatment,
        stage=stage,
        scorer=contract.scorer,
        metric_name=contract.metric_name,
        state=state,
        samples_total=counts["samples_total"],
        scored_count=counts["scored_count"],
        unavailable_count=counts["unavailable_count"],
        blocked_count=counts["blocked_count"],
        failed_count=counts["failed_count"],
        missing_count=counts["missing_count"],
        mean=stats["mean"],
        median=stats["median"],
        minimum=stats["minimum"],
        maximum=stats["maximum"],
        population_stddev=stats["population_stddev"],
        transformations=tuple(transformations),
    )


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite_number(value: object, field: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"treatment report has invalid {field}")
    return float(value)


def _optional_finite_number(value: object, field: str) -> float | None:
    if value is None:
        return None
    return _finite_number(value, field)
