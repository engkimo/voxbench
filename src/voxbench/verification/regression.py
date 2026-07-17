"""Explicit regression policy for aggregated full-reference treatments."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from voxbench.verification.aggregation import (
    FullReferenceStageAggregate,
    FullReferenceTreatmentReport,
)

FullReferenceRegressionState = Literal[
    "improved",
    "stable",
    "regressed",
    "indeterminate",
]


@dataclass(frozen=True)
class FullReferenceRegressionPolicy:
    stable_tolerance: float
    higher_is_better: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.stable_tolerance) or self.stable_tolerance < 0:
            raise ValueError("stable_tolerance must be finite and non-negative")


@dataclass(frozen=True)
class FullReferenceStageRegression:
    stage: str
    state: FullReferenceRegressionState
    baseline_mean: float | None
    current_mean: float | None
    delta: float | None
    tolerance: float
    reason_alias: str | None


@dataclass(frozen=True)
class FullReferenceRegressionReport:
    baseline_treatment: str
    current_treatment: str
    scorer: str
    metric_name: str
    policy: FullReferenceRegressionPolicy
    stages: tuple[FullReferenceStageRegression, ...]

    def safe_payload(self) -> dict[str, Any]:
        return {
            "baseline_treatment": self.baseline_treatment,
            "current_treatment": self.current_treatment,
            "higher_is_better": self.policy.higher_is_better,
            "metric_name": self.metric_name,
            "scorer": self.scorer,
            "stable_tolerance": self.policy.stable_tolerance,
            "stages": [
                {
                    "baseline_mean": stage.baseline_mean,
                    "current_mean": stage.current_mean,
                    "delta": stage.delta,
                    "reason_alias": stage.reason_alias,
                    "stage": stage.stage,
                    "state": stage.state,
                    "tolerance": stage.tolerance,
                }
                for stage in self.stages
            ],
            "summary": {
                state: sum(stage.state == state for stage in self.stages)
                for state in ("improved", "stable", "regressed", "indeterminate")
            },
        }


def compare_full_reference_treatments(
    *,
    baseline: FullReferenceTreatmentReport,
    current: FullReferenceTreatmentReport,
    policy: FullReferenceRegressionPolicy,
) -> FullReferenceRegressionReport:
    """Compare aggregate means only when both stage treatments are complete/comparable."""

    baseline_by_stage = {stage.stage: stage for stage in baseline.stages}
    current_by_stage = {stage.stage: stage for stage in current.stages}
    stage_order = [*baseline_by_stage]
    stage_order.extend(stage for stage in current_by_stage if stage not in baseline_by_stage)
    contracts_match = baseline.contract == current.contract
    results = tuple(
        _compare_stage(
            stage=stage,
            baseline=baseline_by_stage.get(stage),
            current=current_by_stage.get(stage),
            policy=policy,
            contracts_match=contracts_match,
        )
        for stage in stage_order
    )
    return FullReferenceRegressionReport(
        baseline_treatment=baseline.treatment,
        current_treatment=current.treatment,
        scorer=current.contract.scorer,
        metric_name=current.contract.metric_name,
        policy=policy,
        stages=results,
    )


def _compare_stage(
    *,
    stage: str,
    baseline: FullReferenceStageAggregate | None,
    current: FullReferenceStageAggregate | None,
    policy: FullReferenceRegressionPolicy,
    contracts_match: bool,
) -> FullReferenceStageRegression:
    reason_alias = _indeterminate_reason(
        baseline=baseline,
        current=current,
        contracts_match=contracts_match,
    )
    if reason_alias is not None:
        return FullReferenceStageRegression(
            stage=stage,
            state="indeterminate",
            baseline_mean=baseline.mean if baseline else None,
            current_mean=current.mean if current else None,
            delta=None,
            tolerance=policy.stable_tolerance,
            reason_alias=reason_alias,
        )

    assert baseline is not None and baseline.mean is not None
    assert current is not None and current.mean is not None
    delta = current.mean - baseline.mean
    directed_delta = delta if policy.higher_is_better else -delta
    if directed_delta > policy.stable_tolerance:
        state: FullReferenceRegressionState = "improved"
    elif directed_delta < -policy.stable_tolerance:
        state = "regressed"
    else:
        state = "stable"
    return FullReferenceStageRegression(
        stage=stage,
        state=state,
        baseline_mean=baseline.mean,
        current_mean=current.mean,
        delta=delta,
        tolerance=policy.stable_tolerance,
        reason_alias=None,
    )


def _indeterminate_reason(
    *,
    baseline: FullReferenceStageAggregate | None,
    current: FullReferenceStageAggregate | None,
    contracts_match: bool,
) -> str | None:
    if not contracts_match:
        return "scorer-contract-mismatch"
    if baseline is None:
        return "baseline-stage-missing"
    if current is None:
        return "current-stage-missing"
    if baseline.state != "aggregated":
        return "baseline-not-aggregated"
    if current.state != "aggregated":
        return "current-not-aggregated"
    if baseline.transformations != current.transformations:
        return "transformation-mismatch"
    if baseline.mean is None or current.mean is None:
        return "aggregate-mean-missing"
    return None
