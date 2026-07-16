from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from voxbench.registry.service import RegistryService, load_json
from voxbench.synthetic_caller import SyntheticAudioSpec, generate_synthetic_artifacts
from voxbench.verification import select_full_reference_candidates

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (
    "examples/manifests/engine/asterisk.json",
    "examples/manifests/provider/gemini.json",
    "examples/manifests/processor/resampler.json",
    "examples/manifests/processor/agc.json",
    "examples/manifests/processor/limiter.json",
    "examples/manifests/processor/serializer.json",
)


def _resolved_baseline() -> dict:
    service = RegistryService()
    for manifest in MANIFESTS:
        service.register_manifest(load_json(ROOT / manifest))
    service.register_config(load_json(ROOT / "examples/configs/valid-baseline.json"))
    return deepcopy(service.resolve_config("baseline").resolved)


def _artifacts(tmp_path: Path, resolved: dict | None = None):
    return generate_synthetic_artifacts(
        resolved_config=resolved or _resolved_baseline(),
        output_root=tmp_path,
        audio_spec=SyntheticAudioSpec(
            sample_rate_hz=24_000,
            channels=1,
            duration_seconds=0.1,
            amplitude=10_000,
            frequency_hz=1_000,
        ),
    )


def test_full_reference_selection_pairs_ready_stage_references(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)

    selection = select_full_reference_candidates(
        stage_references=artifacts.stage_references,
        recordings=artifacts.recordings,
    )

    assert [candidate.stage for candidate in selection.candidates] == [
        "resampler",
        "agc",
        "limiter",
        "serializer",
    ]
    assert selection.blocked == ()
    serializer = selection.candidates[-1]
    assert serializer.transformations == (
        "resample:24000->8000",
        "codec-round-trip:mulaw",
    )
    assert serializer.comparison_format == {
        "encoding": "pcm16",
        "rate": 8000,
        "channels": 1,
    }


def test_full_reference_selection_blocks_unsupported_codec_and_missing_recording(
    tmp_path: Path,
) -> None:
    resolved = _resolved_baseline()
    stages = resolved["spec"]["media"]["pipeline"]
    serializer = next(stage for stage in stages if stage["type"] == "serializer")
    serializer["io"]["produces"]["encoding"] = "unsupported-codec"
    artifacts = _artifacts(tmp_path, resolved)
    recordings = [
        recording for recording in artifacts.recordings if recording.stage != "limiter"
    ]

    selection = select_full_reference_candidates(
        stage_references=artifacts.stage_references,
        recordings=recordings,
    )

    assert [candidate.stage for candidate in selection.candidates] == ["resampler", "agc"]
    assert [(block.stage, block.reason) for block in selection.blocked] == [
        ("limiter", "stage-recording-missing"),
        ("serializer", "codec-round-trip-required:unsupported-codec"),
    ]


def test_full_reference_selection_blocks_duplicate_reference(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    duplicate = [*artifacts.stage_references, artifacts.stage_references[0]]

    selection = select_full_reference_candidates(
        stage_references=duplicate,
        recordings=artifacts.recordings,
    )

    assert selection.blocked[-1].reason == "duplicate-stage-reference"


def test_full_reference_selection_blocks_comparison_format_mismatch(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    recordings = [
        replace(recording, format={**recording.format, "rate": 16_000})
        if recording.stage == "agc"
        else recording
        for recording in artifacts.recordings
    ]

    selection = select_full_reference_candidates(
        stage_references=artifacts.stage_references,
        recordings=recordings,
    )

    assert ("agc", "comparison-format-mismatch") in [
        (block.stage, block.reason) for block in selection.blocked
    ]
