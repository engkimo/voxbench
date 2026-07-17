from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from voxbench.registry.service import RegistryService, load_json
from voxbench.synthetic_caller import (
    SyntheticAudioSpec,
    SyntheticStageDegradation,
    run_synthetic_verification,
    write_synthetic_verification_report,
)
from voxbench.verification import (
    FullReferenceCandidate,
    FullReferenceScorerContract,
    FullReferenceScorerReadiness,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (
    "examples/manifests/engine/asterisk.json",
    "examples/manifests/provider/gemini.json",
    "examples/manifests/processor/resampler.json",
    "examples/manifests/processor/agc.json",
    "examples/manifests/processor/limiter.json",
    "examples/manifests/processor/serializer.json",
)
FAKE_CONTRACT = FullReferenceScorerContract(
    scorer="synthetic-fake",
    metric_name="synthetic_moslqo",
    minimum_score=1.0,
    maximum_score=5.0,
)


@dataclass
class SyntheticFakeScorer:
    available: bool = True
    contract: FullReferenceScorerContract = FAKE_CONTRACT

    def readiness(self) -> FullReferenceScorerReadiness:
        return FullReferenceScorerReadiness(
            available=self.available,
            reason_alias=None if self.available else "fake-scorer-unavailable",
        )

    def score(self, candidate: FullReferenceCandidate) -> float:
        return {
            "resampler": 4.8,
            "agc": 4.6,
            "limiter": 4.4,
            "serializer": 4.2,
        }[candidate.stage]


def _resolved_baseline() -> dict:
    service = RegistryService()
    for manifest in MANIFESTS:
        service.register_manifest(load_json(ROOT / manifest))
    service.register_config(load_json(ROOT / "examples/configs/valid-baseline.json"))
    return deepcopy(service.resolve_config("baseline").resolved)


def _audio_spec() -> SyntheticAudioSpec:
    return SyntheticAudioSpec(
        sample_rate_hz=24_000,
        channels=1,
        duration_seconds=0.1,
        amplitude=10_000,
        frequency_hz=1_000,
    )


def test_synthetic_verification_combines_invariants_scores_metrics_and_safe_report(
    tmp_path: Path,
) -> None:
    run = run_synthetic_verification(
        resolved_config=_resolved_baseline(),
        output_root=tmp_path / "artifacts",
        audio_spec=_audio_spec(),
        scorer=SyntheticFakeScorer(),
    )

    assert run.state == "complete"
    assert all(result.passed for result in run.invariant_results)
    assert [result.state for result in run.full_reference.results] == ["scored"] * 4
    score_metrics = [metric for metric in run.metrics if metric.name == "synthetic_moslqo"]
    assert [(metric.stage, metric.value) for metric in score_metrics] == [
        ("resampler", 4.8),
        ("agc", 4.6),
        ("limiter", 4.4),
        ("serializer", 4.2),
    ]
    serializer = run.full_reference.results[-1]
    assert serializer.transformations == (
        "resample:24000->8000",
        "codec-round-trip:mulaw",
    )

    report_path = tmp_path / "report.json"
    write_synthetic_verification_report(run, report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["state"] == "complete"
    assert payload["summary"] == {
        "invariants_failed": 0,
        "invariants_passed": 5,
        "scores_blocked": 0,
        "scores_failed": 0,
        "scores_scored": 4,
        "scores_unavailable": 0,
    }
    serialized = json.dumps(payload)
    assert "file://" not in serialized
    assert "asterisk.example.invalid" not in serialized
    assert "secret://" not in serialized


def test_synthetic_verification_is_partial_when_scorer_is_unavailable(tmp_path: Path) -> None:
    run = run_synthetic_verification(
        resolved_config=_resolved_baseline(),
        output_root=tmp_path,
        audio_spec=_audio_spec(),
        scorer=SyntheticFakeScorer(available=False),
    )

    assert run.state == "partial"
    assert all(result.state == "unavailable" for result in run.full_reference.results)
    assert not any(metric.name == "synthetic_moslqo" for metric in run.metrics)
    assert run.safe_payload()["summary"]["scores_unavailable"] == 4


def test_synthetic_verification_fails_on_invariant_failure(tmp_path: Path) -> None:
    run = run_synthetic_verification(
        resolved_config=_resolved_baseline(),
        output_root=tmp_path,
        audio_spec=_audio_spec(),
        scorer=SyntheticFakeScorer(),
        degradations={"serializer": SyntheticStageDegradation(duration_scale=0.75)},
    )

    assert run.state == "failed"
    assert any(not result.passed for result in run.invariant_results)
    assert run.safe_payload()["summary"]["invariants_failed"] == 1
