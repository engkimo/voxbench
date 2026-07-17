"""End-to-end orchestration for deterministic synthetic verification."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from voxbench.engine_harness.models import MetricArtifact
from voxbench.synthetic_caller.offline import (
    SyntheticArtifacts,
    SyntheticAudioSpec,
    SyntheticStageDegradation,
    generate_synthetic_artifacts,
)
from voxbench.verification import (
    FullReferenceScorer,
    FullReferenceScoringReport,
    FullReferenceTreatmentReport,
    VerificationResult,
    aggregate_full_reference_reports,
    full_reference_scores_to_metrics,
    score_full_reference_selection,
    select_full_reference_candidates,
    verify_recordings,
)

SyntheticVerificationState = Literal["complete", "partial", "failed"]
SyntheticTreatmentState = Literal["complete", "partial", "failed"]


@dataclass(frozen=True)
class SyntheticVerificationRun:
    state: SyntheticVerificationState
    artifacts: SyntheticArtifacts
    invariant_results: tuple[VerificationResult, ...]
    full_reference: FullReferenceScoringReport
    metrics: tuple[MetricArtifact, ...]

    def safe_payload(self) -> dict[str, Any]:
        """Return an auditable report without artifact URIs or scorer process output."""

        score_results = [
            {
                "metric_name": result.metric_name,
                "reason_alias": result.reason_alias,
                "score": result.score,
                "scorer": result.scorer,
                "stage": result.stage,
                "state": result.state,
                "transformations": list(result.transformations),
            }
            for result in self.full_reference.results
        ]
        invariant_results = [
            {
                "detail": result.detail,
                "expected": result.expected,
                "invariant": result.invariant,
                "observed": result.observed,
                "passed": result.passed,
                "stage": result.stage,
            }
            for result in self.invariant_results
        ]
        return {
            "artifacts": {
                "recording_stages": [recording.stage for recording in self.artifacts.recordings],
                "reference_stages": [
                    reference.stage for reference in self.artifacts.stage_references
                ],
            },
            "full_reference": score_results,
            "invariants": invariant_results,
            "state": self.state,
            "summary": {
                "invariants_failed": sum(not result.passed for result in self.invariant_results),
                "invariants_passed": sum(result.passed for result in self.invariant_results),
                "scores_blocked": sum(
                    result.state == "blocked" for result in self.full_reference.results
                ),
                "scores_failed": sum(
                    result.state == "failed" for result in self.full_reference.results
                ),
                "scores_scored": sum(
                    result.state == "scored" for result in self.full_reference.results
                ),
                "scores_unavailable": sum(
                    result.state == "unavailable" for result in self.full_reference.results
                ),
            },
        }


@dataclass(frozen=True)
class SyntheticTreatmentRun:
    treatment: str
    state: SyntheticTreatmentState
    samples: tuple[SyntheticVerificationRun, ...]
    aggregate: FullReferenceTreatmentReport

    def safe_payload(self) -> dict[str, Any]:
        return {
            "aggregate": self.aggregate.safe_payload(),
            "sample_count": len(self.samples),
            "sample_states": [sample.state for sample in self.samples],
            "state": self.state,
            "treatment": self.treatment,
        }


def run_synthetic_verification(
    *,
    resolved_config: dict[str, Any],
    output_root: Path,
    audio_spec: SyntheticAudioSpec,
    scorer: FullReferenceScorer,
    degradations: dict[str, SyntheticStageDegradation] | None = None,
) -> SyntheticVerificationRun:
    """Generate, verify, select, score, and combine one synthetic run."""

    artifacts = generate_synthetic_artifacts(
        resolved_config=resolved_config,
        output_root=output_root,
        audio_spec=audio_spec,
        degradations=degradations,
    )
    invariant_results = tuple(
        verify_recordings(
            resolved_config=resolved_config,
            recordings=artifacts.recordings,
            metrics=artifacts.metrics,
        )
    )
    selection = select_full_reference_candidates(
        stage_references=artifacts.stage_references,
        recordings=artifacts.recordings,
    )
    full_reference = score_full_reference_selection(selection, scorer)
    score_metrics = full_reference_scores_to_metrics(full_reference)
    state = _verification_state(invariant_results, full_reference)
    return SyntheticVerificationRun(
        state=state,
        artifacts=artifacts,
        invariant_results=invariant_results,
        full_reference=full_reference,
        metrics=(*artifacts.metrics, *score_metrics),
    )


def write_synthetic_verification_report(
    run: SyntheticVerificationRun,
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(run.safe_payload(), handle, indent=2, sort_keys=True)
        handle.write("\n")


def run_synthetic_treatment(
    *,
    treatment: str,
    sample_count: int,
    minimum_samples: int,
    resolved_config: dict[str, Any],
    output_root: Path,
    audio_spec: SyntheticAudioSpec,
    scorer: FullReferenceScorer,
    frequency_step_hz: float = 100.0,
) -> SyntheticTreatmentRun:
    """Run comparable content samples and aggregate their stage scores."""

    if sample_count < 2:
        raise ValueError("sample_count must be at least 2")
    if frequency_step_hz < 0:
        raise ValueError("frequency_step_hz must be non-negative")
    samples = []
    for index in range(sample_count):
        sample_root = output_root / f"sample-{index + 1:03d}"
        sample = run_synthetic_verification(
            resolved_config=resolved_config,
            output_root=sample_root,
            audio_spec=replace(
                audio_spec,
                frequency_hz=audio_spec.frequency_hz + index * frequency_step_hz,
            ),
            scorer=scorer,
        )
        write_synthetic_verification_report(
            sample,
            sample_root / "verification-report.json",
        )
        samples.append(sample)
    aggregate = aggregate_full_reference_reports(
        treatment=treatment,
        reports=tuple(sample.full_reference for sample in samples),
        minimum_samples=minimum_samples,
    )
    if any(sample.state == "failed" for sample in samples):
        state: SyntheticTreatmentState = "failed"
    elif any(sample.state == "partial" for sample in samples) or any(
        stage.state != "aggregated" for stage in aggregate.stages
    ):
        state = "partial"
    else:
        state = "complete"
    return SyntheticTreatmentRun(
        treatment=treatment,
        state=state,
        samples=tuple(samples),
        aggregate=aggregate,
    )


def write_synthetic_treatment_report(run: SyntheticTreatmentRun, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(run.safe_payload(), handle, indent=2, sort_keys=True)
        handle.write("\n")


def _verification_state(
    invariant_results: tuple[VerificationResult, ...],
    full_reference: FullReferenceScoringReport,
) -> SyntheticVerificationState:
    if any(not result.passed for result in invariant_results) or any(
        result.state == "failed" for result in full_reference.results
    ):
        return "failed"
    if not full_reference.results or any(
        result.state in {"unavailable", "blocked"} for result in full_reference.results
    ):
        return "partial"
    return "complete"
