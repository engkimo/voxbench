"""Descriptive repeatability analysis for full-reference treatment aggregates."""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Literal

from voxbench.verification.aggregation import (
    FullReferenceStageAggregate,
    FullReferenceTreatmentReport,
)
from voxbench.verification.scoring import FullReferenceScorerContract

FullReferenceRepeatabilityState = Literal["calibrated", "indeterminate"]


@dataclass(frozen=True)
class FullReferenceStageRepeatability:
    stage: str
    state: FullReferenceRepeatabilityState
    repeat_count: int
    mean_of_means: float | None
    minimum_mean: float | None
    maximum_mean: float | None
    observed_max_delta: float | None
    population_stddev: float | None
    transformations: tuple[str, ...]
    reason_alias: str | None = None


@dataclass(frozen=True)
class FullReferenceRepeatabilityReport:
    contract: FullReferenceScorerContract
    minimum_repeats: int
    repeat_count: int
    stages: tuple[FullReferenceStageRepeatability, ...]

    def safe_payload(self) -> dict[str, Any]:
        return {
            "metric_name": self.contract.metric_name,
            "minimum_repeats": self.minimum_repeats,
            "repeat_count": self.repeat_count,
            "scorer": self.contract.scorer,
            "stages": [
                {
                    "maximum_mean": stage.maximum_mean,
                    "mean_of_means": stage.mean_of_means,
                    "minimum_mean": stage.minimum_mean,
                    "observed_max_delta": stage.observed_max_delta,
                    "population_stddev": stage.population_stddev,
                    "reason_alias": stage.reason_alias,
                    "repeat_count": stage.repeat_count,
                    "stage": stage.stage,
                    "state": stage.state,
                    "transformations": list(stage.transformations),
                }
                for stage in self.stages
            ],
        }


def analyze_full_reference_repeatability(
    *,
    reports: tuple[FullReferenceTreatmentReport, ...],
    minimum_repeats: int = 3,
) -> FullReferenceRepeatabilityReport:
    """Describe repeated baseline variation without choosing a regression threshold."""

    if minimum_repeats < 3:
        raise ValueError("minimum_repeats must be at least 3")
    if not reports:
        raise ValueError("at least one treatment report is required")
    contract = reports[0].contract
    contracts_match = all(report.contract == contract for report in reports)
    stage_order: list[str] = []
    indexed_reports = []
    for report in reports:
        indexed = {stage.stage: stage for stage in report.stages}
        if len(indexed) != len(report.stages):
            raise ValueError("treatment report contains duplicate stages")
        indexed_reports.append(indexed)
        stage_order.extend(stage for stage in indexed if stage not in stage_order)
    stages = tuple(
        _analyze_stage(
            stage=stage,
            indexed_reports=indexed_reports,
            minimum_repeats=minimum_repeats,
            contracts_match=contracts_match,
        )
        for stage in stage_order
    )
    return FullReferenceRepeatabilityReport(
        contract=contract,
        minimum_repeats=minimum_repeats,
        repeat_count=len(reports),
        stages=stages,
    )


def _analyze_stage(
    *,
    stage: str,
    indexed_reports: list[dict[str, FullReferenceStageAggregate]],
    minimum_repeats: int,
    contracts_match: bool,
) -> FullReferenceStageRepeatability:
    values = [indexed.get(stage) for indexed in indexed_reports]
    reason_alias = _indeterminate_reason(
        values=values,
        minimum_repeats=minimum_repeats,
        contracts_match=contracts_match,
    )
    if reason_alias is not None:
        return FullReferenceStageRepeatability(
            stage=stage,
            state="indeterminate",
            repeat_count=sum(value is not None for value in values),
            mean_of_means=None,
            minimum_mean=None,
            maximum_mean=None,
            observed_max_delta=None,
            population_stddev=None,
            transformations=(),
            reason_alias=reason_alias,
        )
    aggregates = [value for value in values if value is not None]
    means = [value.mean for value in aggregates]
    assert all(mean is not None for mean in means)
    numeric_means = [float(mean) for mean in means]
    minimum = min(numeric_means)
    maximum = max(numeric_means)
    return FullReferenceStageRepeatability(
        stage=stage,
        state="calibrated",
        repeat_count=len(aggregates),
        mean_of_means=statistics.fmean(numeric_means),
        minimum_mean=minimum,
        maximum_mean=maximum,
        observed_max_delta=maximum - minimum,
        population_stddev=statistics.pstdev(numeric_means),
        transformations=aggregates[0].transformations,
    )


def _indeterminate_reason(
    *,
    values: list[FullReferenceStageAggregate | None],
    minimum_repeats: int,
    contracts_match: bool,
) -> str | None:
    if not contracts_match:
        return "scorer-contract-mismatch"
    if len(values) < minimum_repeats:
        return "insufficient-repeats"
    if any(value is None for value in values):
        return "stage-missing"
    aggregates = [value for value in values if value is not None]
    if any(value.state != "aggregated" for value in aggregates):
        return "stage-not-aggregated"
    if len({value.transformations for value in aggregates}) != 1:
        return "transformation-mismatch"
    if any(value.mean is None for value in aggregates):
        return "aggregate-mean-missing"
    return None
