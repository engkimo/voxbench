from __future__ import annotations

from dataclasses import replace

import pytest

from voxbench.verification import (
    FullReferenceScorerContract,
    FullReferenceStageAggregate,
    FullReferenceTreatmentReport,
    analyze_full_reference_repeatability,
)

CONTRACT = FullReferenceScorerContract("visqol", "visqol_moslqo", 1.0, 5.0)


def _stage(
    mean: float,
    *,
    stage: str = "serializer",
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


def _report(name: str, *stages: FullReferenceStageAggregate):
    return FullReferenceTreatmentReport(name, 3, CONTRACT, stages)


def test_repeatability_describes_three_complete_baseline_aggregates() -> None:
    report = analyze_full_reference_repeatability(
        reports=(
            _report("baseline-a", _stage(4.0)),
            _report("baseline-b", _stage(4.1)),
            _report("baseline-c", _stage(4.2)),
        )
    )

    stage = report.stages[0]
    assert stage.state == "calibrated"
    assert stage.repeat_count == 3
    assert stage.mean_of_means == pytest.approx(4.1)
    assert stage.minimum_mean == 4.0
    assert stage.maximum_mean == 4.2
    assert stage.observed_max_delta == pytest.approx(0.2)
    assert stage.population_stddev == pytest.approx(0.0816496581)
    assert report.safe_payload()["stages"][0]["reason_alias"] is None


def test_repeatability_suppresses_statistics_below_minimum_repeats() -> None:
    report = analyze_full_reference_repeatability(
        reports=(
            _report("baseline-a", _stage(4.0)),
            _report("baseline-b", _stage(4.1)),
        )
    )

    stage = report.stages[0]
    assert stage.state == "indeterminate"
    assert stage.reason_alias == "insufficient-repeats"
    assert stage.observed_max_delta is None


@pytest.mark.parametrize(
    ("reports", "reason"),
    [
        (
            (
                _report("a", _stage(4.0)),
                _report("b"),
                _report("c", _stage(4.1)),
            ),
            "stage-missing",
        ),
        (
            (
                _report("a", _stage(4.0)),
                _report("b", _stage(4.1, state="partial")),
                _report("c", _stage(4.2)),
            ),
            "stage-not-aggregated",
        ),
        (
            (
                _report("a", _stage(4.0)),
                _report("b", _stage(4.1, transformations=("visqol-mode:audio",))),
                _report("c", _stage(4.2)),
            ),
            "transformation-mismatch",
        ),
    ],
)
def test_repeatability_keeps_incomplete_inputs_indeterminate(
    reports: tuple[FullReferenceTreatmentReport, ...],
    reason: str,
) -> None:
    report = analyze_full_reference_repeatability(reports=reports)

    assert report.stages[0].state == "indeterminate"
    assert report.stages[0].reason_alias == reason


def test_repeatability_keeps_contract_mismatch_indeterminate() -> None:
    mismatched = replace(
        _report("c", _stage(4.2)),
        contract=FullReferenceScorerContract("other", "other_score", 0.0, 1.0),
    )

    report = analyze_full_reference_repeatability(
        reports=(
            _report("a", _stage(4.0)),
            _report("b", _stage(4.1)),
            mismatched,
        )
    )

    assert report.stages[0].reason_alias == "scorer-contract-mismatch"


def test_repeatability_requires_three_minimum_repeats() -> None:
    with pytest.raises(ValueError, match="at least 3"):
        analyze_full_reference_repeatability(
            reports=(_report("a", _stage(4.0)),),
            minimum_repeats=2,
        )
