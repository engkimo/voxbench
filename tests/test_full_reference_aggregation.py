from __future__ import annotations

import pytest

from voxbench.verification import (
    FullReferenceScorerContract,
    FullReferenceScoreResult,
    FullReferenceScoringReport,
    aggregate_full_reference_reports,
)

CONTRACT = FullReferenceScorerContract(
    scorer="visqol",
    metric_name="visqol_moslqo",
    minimum_score=1.0,
    maximum_score=5.0,
)


def _result(
    *,
    stage: str = "serializer",
    state: str = "scored",
    score: float | None = 4.0,
    transformations: tuple[str, ...] = ("visqol-mode:speech",),
) -> FullReferenceScoreResult:
    return FullReferenceScoreResult(
        stage=stage,
        scorer=CONTRACT.scorer,
        metric_name=CONTRACT.metric_name,
        state=state,
        score=score,
        reason_alias=None if state == "scored" else f"sample-{state}",
        transformations=transformations,
    )


def _report(*results: FullReferenceScoreResult) -> FullReferenceScoringReport:
    return FullReferenceScoringReport(contract=CONTRACT, results=results)


def test_treatment_aggregates_three_comparable_samples() -> None:
    treatment = aggregate_full_reference_reports(
        treatment="baseline-speech",
        reports=(
            _report(_result(score=3.0)),
            _report(_result(score=4.0)),
            _report(_result(score=5.0)),
        ),
    )

    stage = treatment.stages[0]
    assert stage.state == "aggregated"
    assert stage.samples_total == 3
    assert stage.scored_count == 3
    assert stage.mean == 4.0
    assert stage.median == 4.0
    assert stage.minimum == 3.0
    assert stage.maximum == 5.0
    assert stage.population_stddev == pytest.approx(0.8164965809)
    assert stage.transformations == ("visqol-mode:speech",)
    assert treatment.safe_payload()["stages"][0]["mean"] == 4.0


def test_treatment_does_not_publish_statistics_below_minimum_samples() -> None:
    treatment = aggregate_full_reference_reports(
        treatment="baseline-speech",
        reports=(_report(_result(score=4.0)), _report(_result(score=4.5))),
    )

    stage = treatment.stages[0]
    assert stage.state == "insufficient"
    assert stage.scored_count == 2
    assert stage.mean is None
    assert stage.population_stddev is None


def test_treatment_marks_mixed_score_states_as_partial() -> None:
    treatment = aggregate_full_reference_reports(
        treatment="baseline-speech",
        reports=(
            _report(_result(score=4.0)),
            _report(_result(score=4.2)),
            _report(_result(score=4.4)),
            _report(_result(state="unavailable", score=None)),
        ),
    )

    stage = treatment.stages[0]
    assert stage.state == "partial"
    assert stage.scored_count == 3
    assert stage.unavailable_count == 1
    assert stage.mean == pytest.approx(4.2)


def test_treatment_marks_missing_stage_as_partial() -> None:
    treatment = aggregate_full_reference_reports(
        treatment="baseline-speech",
        reports=(
            _report(_result()),
            _report(),
            _report(_result(score=4.2)),
        ),
        minimum_samples=2,
    )

    stage = treatment.stages[0]
    assert stage.state == "partial"
    assert stage.missing_count == 1
    assert stage.scored_count == 2


def test_treatment_rejects_different_transformation_chains_as_incomparable() -> None:
    treatment = aggregate_full_reference_reports(
        treatment="baseline-speech",
        reports=(
            _report(_result(transformations=("visqol-mode:speech",))),
            _report(_result(transformations=("visqol-mode:audio",))),
            _report(_result(transformations=("visqol-mode:speech",))),
        ),
    )

    stage = treatment.stages[0]
    assert stage.state == "incomparable"
    assert stage.transformations == ()
    assert stage.mean == 4.0


def test_treatment_rejects_contract_mismatch_and_duplicate_stage() -> None:
    other_contract = FullReferenceScorerContract(
        scorer="other-scorer",
        metric_name="other_score",
        minimum_score=0.0,
        maximum_score=1.0,
    )
    with pytest.raises(ValueError, match="same scorer contract"):
        aggregate_full_reference_reports(
            treatment="baseline-speech",
            reports=(
                _report(_result()),
                FullReferenceScoringReport(contract=other_contract, results=()),
            ),
        )

    with pytest.raises(ValueError, match="one result per stage"):
        aggregate_full_reference_reports(
            treatment="baseline-speech",
            reports=(_report(_result(), _result(score=4.2)),),
        )


@pytest.mark.parametrize("treatment", ["Baseline", "secret/value", ""])
def test_treatment_requires_a_safe_alias(treatment: str) -> None:
    with pytest.raises(ValueError, match="safe lowercase alias"):
        aggregate_full_reference_reports(
            treatment=treatment,
            reports=(_report(_result()),),
        )
