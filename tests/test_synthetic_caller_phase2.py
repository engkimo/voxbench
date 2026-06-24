from __future__ import annotations

from pathlib import Path

from voxbench.registry.service import RegistryService, load_json
from voxbench.synthetic_caller import (
    SyntheticAudioSpec,
    SyntheticStageDegradation,
    generate_synthetic_artifacts,
)
from voxbench.verification import verify_recordings

ROOT = Path(__file__).resolve().parents[1]

MANIFESTS = [
    ROOT / "examples/manifests/engine/asterisk.json",
    ROOT / "examples/manifests/provider/gemini.json",
    ROOT / "examples/manifests/processor/resampler.json",
    ROOT / "examples/manifests/processor/agc.json",
    ROOT / "examples/manifests/processor/limiter.json",
    ROOT / "examples/manifests/processor/serializer.json",
]


def test_synthetic_artifacts_verify_clean_path(tmp_path: Path) -> None:
    resolved = _resolved_baseline()

    artifacts = generate_synthetic_artifacts(
        resolved_config=resolved,
        output_root=tmp_path,
        audio_spec=_audio_spec(),
    )

    assert Path(artifacts.reference_uri.removeprefix("file://")).exists()
    assert {recording.stage for recording in artifacts.recordings} == {
        "resampler",
        "agc",
        "limiter",
        "serializer",
    }
    results = verify_recordings(
        resolved_config=resolved,
        recordings=artifacts.recordings,
        metrics=artifacts.metrics,
    )

    assert results
    assert all(result.passed for result in results)
    assert {(result.stage, result.invariant) for result in results} == {
        ("resampler", "isochronous"),
        ("agc", "level_preserving"),
        ("limiter", "level_preserving"),
        ("serializer", "duration_preserving"),
        ("serializer", "isochronous"),
    }


def test_synthetic_artifacts_trigger_duration_failure(tmp_path: Path) -> None:
    resolved = _resolved_baseline()

    artifacts = generate_synthetic_artifacts(
        resolved_config=resolved,
        output_root=tmp_path,
        audio_spec=_audio_spec(),
        degradations={"serializer": SyntheticStageDegradation(duration_scale=0.75)},
    )
    results = verify_recordings(
        resolved_config=resolved,
        recordings=artifacts.recordings,
        metrics=artifacts.metrics,
    )

    result = _only(results, stage="serializer", invariant="duration_preserving")
    assert result.passed is False
    assert result.observed["duration_ratio"] == 0.75


def test_synthetic_artifacts_trigger_level_failure(tmp_path: Path) -> None:
    resolved = _resolved_baseline()

    artifacts = generate_synthetic_artifacts(
        resolved_config=resolved,
        output_root=tmp_path,
        audio_spec=_audio_spec(),
        degradations={"agc": SyntheticStageDegradation(level_scale=0.2)},
    )
    results = verify_recordings(
        resolved_config=resolved,
        recordings=artifacts.recordings,
        metrics=artifacts.metrics,
    )

    result = _only(results, stage="agc", invariant="level_preserving")
    assert result.passed is False
    assert result.observed["delta_db"] < -3.0


def test_synthetic_artifacts_trigger_isochronous_failure(tmp_path: Path) -> None:
    resolved = _resolved_baseline()

    artifacts = generate_synthetic_artifacts(
        resolved_config=resolved,
        output_root=tmp_path,
        audio_spec=_audio_spec(),
        degradations={
            "serializer": SyntheticStageDegradation(
                frames_in=50.0,
                frames_out=7.0,
                frame_cadence_jitter_ms=8.0,
            )
        },
    )
    results = verify_recordings(
        resolved_config=resolved,
        recordings=artifacts.recordings,
        metrics=artifacts.metrics,
    )

    result = _only(results, stage="serializer", invariant="isochronous")
    assert result.passed is False
    assert result.observed["frames_out_in_ratio"] == 0.14


def _resolved_baseline() -> dict[str, object]:
    service = RegistryService()
    for manifest in MANIFESTS:
        service.register_manifest(load_json(manifest))
    service.register_config(load_json(ROOT / "examples/configs/valid-baseline.json"))
    return service.resolve_config("baseline").resolved


def _audio_spec() -> SyntheticAudioSpec:
    return SyntheticAudioSpec(
        sample_rate_hz=1000,
        channels=1,
        duration_seconds=1.0,
        amplitude=10_000,
        frequency_hz=20.0,
    )


def _only(results, *, stage: str, invariant: str):
    matches = [
        result
        for result in results
        if result.stage == stage and result.invariant == invariant
    ]
    assert len(matches) == 1
    return matches[0]
