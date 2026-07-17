from __future__ import annotations

from dataclasses import replace

import pytest

from voxbench.verification import (
    FullReferenceRegressionPolicy,
    FullReferenceScorerContract,
    FullReferenceStageAggregate,
    FullReferenceTreatmentReport,
    compare_full_reference_treatments,
)

CONTRACT = FullReferenceScorerContract("visqol", "visqol_moslqo", 1.0, 5.0)


def _stage(
    stage: str,
    mean: float,
    *,
    state: str = "aggregated",
    transformations: tuple[str, ...] = ("visqol-mode:speech",),
) -> FullReferenceStageAggregate:
    return FullReferenceStageAggregate(
        treatment="unused",
        stage=stage,
        scorer="visqol",
        metric_name="visqol_moslqo",
        state=state,
        samples_total=3,
        scored_count=3,
        unavailable_count=0,
        blocked_count=0,
        failed_count=0,
        missing_count=0,
        mean=mean,
        median=mean,
        minimum=mean,
        maximum=mean,
        population_stddev=0.0,
        transformations=transformations,
    )


def _treatment(name: str, *stages: FullReferenceStageAggregate):
    return FullReferenceTreatmentReport(
        treatment=name,
        minimum_samples=3,
        contract=CONTRACT,
        stages=stages,
    )


def test_regression_policy_classifies_improved_stable_and_regressed_stages() -> None:
    report = compare_full_reference_treatments(
        baseline=_treatment(
            "baseline",
            _stage("resampler", 4.0),
            _stage("agc", 4.0),
            _stage("serializer", 4.0),
        ),
        current=_treatment(
            "candidate",
            _stage("resampler", 4.2),
            _stage("agc", 4.1),
            _stage("serializer", 3.8),
        ),
        policy=FullReferenceRegressionPolicy(stable_tolerance=0.1),
    )

    assert [(stage.stage, stage.state) for stage in report.stages] == [
        ("resampler", "improved"),
        ("agc", "stable"),
        ("serializer", "regressed"),
    ]
    assert report.stages[0].delta == pytest.approx(0.2)
    assert report.safe_payload()["summary"] == {
        "improved": 1,
        "stable": 1,
        "regressed": 1,
        "indeterminate": 0,
    }


def test_regression_policy_supports_lower_is_better_metrics() -> None:
    report = compare_full_reference_treatments(
        baseline=_treatment("baseline", _stage("latency", 4.0)),
        current=_treatment("candidate", _stage("latency", 3.7)),
        policy=FullReferenceRegressionPolicy(
            stable_tolerance=0.1,
            higher_is_better=False,
        ),
    )

    assert report.stages[0].state == "improved"
    assert report.stages[0].delta == pytest.approx(-0.3)


@pytest.mark.parametrize(
    ("baseline_stage", "current_stage", "reason"),
    [
        (None, _stage("agc", 4.0), "baseline-stage-missing"),
        (_stage("agc", 4.0), None, "current-stage-missing"),
        (
            _stage("agc", 4.0, state="partial"),
            _stage("agc", 4.0),
            "baseline-not-aggregated",
        ),
        (
            _stage("agc", 4.0),
            _stage("agc", 4.0, state="insufficient"),
            "current-not-aggregated",
        ),
        (
            _stage("agc", 4.0),
            _stage("agc", 4.0, transformations=("visqol-mode:audio",)),
            "transformation-mismatch",
        ),
    ],
)
def test_regression_policy_keeps_incomplete_or_incomparable_stages_indeterminate(
    baseline_stage: FullReferenceStageAggregate | None,
    current_stage: FullReferenceStageAggregate | None,
    reason: str,
) -> None:
    baseline = _treatment("baseline", *(stage for stage in (baseline_stage,) if stage))
    current = _treatment("candidate", *(stage for stage in (current_stage,) if stage))

    report = compare_full_reference_treatments(
        baseline=baseline,
        current=current,
        policy=FullReferenceRegressionPolicy(stable_tolerance=0.1),
    )

    assert report.stages[0].state == "indeterminate"
    assert report.stages[0].reason_alias == reason
    assert report.stages[0].delta is None


def test_regression_policy_marks_contract_mismatch_indeterminate() -> None:
    current = _treatment("candidate", _stage("agc", 4.2))
    current = replace(
        current,
        contract=FullReferenceScorerContract("other", "other_score", 0.0, 1.0),
    )

    report = compare_full_reference_treatments(
        baseline=_treatment("baseline", _stage("agc", 4.0)),
        current=current,
        policy=FullReferenceRegressionPolicy(stable_tolerance=0.1),
    )

    assert report.stages[0].state == "indeterminate"
    assert report.stages[0].reason_alias == "scorer-contract-mismatch"


@pytest.mark.parametrize("tolerance", [-0.1, float("nan"), float("inf")])
def test_regression_policy_rejects_invalid_tolerance(tolerance: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        FullReferenceRegressionPolicy(stable_tolerance=tolerance)
