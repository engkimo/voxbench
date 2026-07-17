from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from voxbench.verification import (
    FullReferenceBlock,
    FullReferenceCandidate,
    FullReferenceScorerContract,
    FullReferenceScorerReadiness,
    FullReferenceSelection,
    full_reference_scores_to_metrics,
    score_full_reference_selection,
)


def _candidate(stage: str) -> FullReferenceCandidate:
    comparison_format = {"encoding": "pcm16", "rate": 16_000, "channels": 1}
    return FullReferenceCandidate(
        stage=stage,
        reference_uri=f"file:///artifacts/{stage}-reference.wav",
        degraded_uri=f"file:///artifacts/{stage}-recording.wav",
        comparison_format=comparison_format,
        degraded_format=dict(comparison_format),
        transformations=(),
    )


VISQOL_CONTRACT = FullReferenceScorerContract(
    scorer="visqol",
    metric_name="visqol_moslqo",
    minimum_score=1.0,
    maximum_score=5.0,
)


@dataclass
class FakeScorer:
    values: dict[str, float | Exception]
    ready: FullReferenceScorerReadiness = field(
        default_factory=lambda: FullReferenceScorerReadiness(available=True)
    )
    contract: FullReferenceScorerContract = VISQOL_CONTRACT
    calls: list[str] = field(default_factory=list)

    def readiness(self) -> FullReferenceScorerReadiness:
        return self.ready

    def score(self, candidate: FullReferenceCandidate) -> float:
        self.calls.append(candidate.stage)
        value = self.values[candidate.stage]
        if isinstance(value, Exception):
            raise value
        return value


def test_scoring_isolates_failures_and_emits_metrics_for_successes_only() -> None:
    selection = FullReferenceSelection(
        candidates=(_candidate("resampler"), _candidate("agc"), _candidate("limiter")),
        blocked=(FullReferenceBlock(stage="serializer", reason="codec-not-supported"),),
    )
    scorer = FakeScorer(
        values={
            "resampler": 4.7,
            "agc": RuntimeError("token=secret at https://private.invalid/model"),
            "limiter": 3.9,
        }
    )

    report = score_full_reference_selection(selection, scorer)

    assert scorer.calls == ["resampler", "agc", "limiter"]
    assert [
        (result.stage, result.state, result.score, result.reason_alias)
        for result in report.results
    ] == [
        ("resampler", "scored", 4.7, None),
        ("agc", "failed", None, "scorer-error"),
        ("limiter", "scored", 3.9, None),
        ("serializer", "blocked", None, "codec-not-supported"),
    ]
    assert "secret" not in repr(report)
    assert "private.invalid" not in repr(report)

    observed_at = datetime(2026, 7, 17, 1, 2, 3, tzinfo=UTC)
    metrics = full_reference_scores_to_metrics(report, ts=observed_at)
    assert [
        (metric.stage, metric.name, metric.value, metric.ts) for metric in metrics
    ] == [
        ("resampler", "visqol_moslqo", 4.7, observed_at),
        ("limiter", "visqol_moslqo", 3.9, observed_at),
    ]


def test_unavailable_scorer_does_not_read_candidate_files_or_emit_zeroes() -> None:
    selection = FullReferenceSelection(
        candidates=(_candidate("resampler"),),
        blocked=(FullReferenceBlock(stage="serializer", reason="input-blocked"),),
    )
    scorer = FakeScorer(
        values={},
        ready=FullReferenceScorerReadiness(
            available=False,
            reason_alias="optional-dependency-missing",
        ),
    )

    report = score_full_reference_selection(selection, scorer)

    assert scorer.calls == []
    assert [(result.state, result.score, result.reason_alias) for result in report.results] == [
        ("unavailable", None, "optional-dependency-missing"),
        ("blocked", None, "input-blocked"),
    ]
    assert full_reference_scores_to_metrics(report) == []


def test_unsafe_readiness_and_block_reasons_are_replaced() -> None:
    selection = FullReferenceSelection(
        candidates=(_candidate("resampler"),),
        blocked=(
            FullReferenceBlock(
                stage="serializer",
                reason="dependency at https://private.invalid?token=secret",
            ),
        ),
    )
    scorer = FakeScorer(
        values={},
        ready=FullReferenceScorerReadiness(
            available=False,
            reason_alias="missing /Users/private/model.pb",
        ),
    )

    report = score_full_reference_selection(selection, scorer)

    assert [result.reason_alias for result in report.results] == [
        "scorer-unavailable",
        "input-blocked",
    ]
    assert "private" not in repr(report)
    assert "secret" not in repr(report)


class BrokenReadinessScorer(FakeScorer):
    def readiness(self) -> FullReferenceScorerReadiness:
        raise RuntimeError("load failed for /Users/private/model.pb?token=secret")


def test_readiness_exception_becomes_safe_unavailable_state() -> None:
    scorer = BrokenReadinessScorer(values={})

    report = score_full_reference_selection(
        FullReferenceSelection(candidates=(_candidate("agc"),), blocked=()),
        scorer,
    )

    assert scorer.calls == []
    assert report.results[0].state == "unavailable"
    assert report.results[0].reason_alias == "scorer-readiness-error"
    assert "secret" not in repr(report)


@pytest.mark.parametrize(
    ("value", "reason_alias"),
    [
        (float("nan"), "score-not-finite"),
        (float("inf"), "score-not-finite"),
        (0.99, "score-out-of-range"),
        (5.01, "score-out-of-range"),
    ],
)
def test_invalid_score_never_becomes_a_metric(value: float, reason_alias: str) -> None:
    scorer = FakeScorer(values={"agc": value})

    report = score_full_reference_selection(
        FullReferenceSelection(candidates=(_candidate("agc"),), blocked=()),
        scorer,
    )

    assert report.results[0].state == "failed"
    assert report.results[0].score is None
    assert report.results[0].reason_alias == reason_alias
    assert full_reference_scores_to_metrics(report) == []


@pytest.mark.parametrize(
    "contract",
    [
        ("ViSQOL", "visqol_moslqo", 1.0, 5.0),
        ("visqol", "mos lqo", 1.0, 5.0),
        ("visqol", "visqol_moslqo", float("nan"), 5.0),
        ("visqol", "visqol_moslqo", 5.0, 5.0),
    ],
)
def test_scorer_contract_rejects_unsafe_or_invalid_metadata(
    contract: tuple[str, str, float, float],
) -> None:
    with pytest.raises(ValueError):
        FullReferenceScorerContract(*contract)
