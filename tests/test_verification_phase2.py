from __future__ import annotations

import math
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.engine_harness.models import MetricArtifact, RecordingArtifact
from voxbench.registry.service import RegistryService, load_json
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


def test_verifier_passes_duration_and_level_invariants(tmp_path: Path) -> None:
    resolved = _resolved_baseline()
    recordings = _recordings_for(
        tmp_path,
        [
            ("resampler", 1.0, 10_000),
            ("agc", 1.0, 9_500),
            ("limiter", 1.0, 9_000),
            ("serializer", 1.0, 9_000),
        ],
    )

    results = verify_recordings(resolved_config=resolved, recordings=recordings)

    assert results
    assert all(result.passed for result in results)
    assert {(result.stage, result.invariant) for result in results} == {
        ("agc", "level_preserving"),
        ("limiter", "level_preserving"),
        ("serializer", "duration_preserving"),
    }


def test_verifier_fails_shortened_duration(tmp_path: Path) -> None:
    resolved = _resolved_baseline()
    recordings = _recordings_for(
        tmp_path,
        [
            ("resampler", 1.0, 10_000),
            ("agc", 1.0, 10_000),
            ("limiter", 1.0, 10_000),
            ("serializer", 0.75, 10_000),
        ],
    )

    results = verify_recordings(resolved_config=resolved, recordings=recordings)

    duration_result = _only(results, stage="serializer", invariant="duration_preserving")
    assert duration_result.passed is False
    assert duration_result.observed["duration_ratio"] == 0.75


def test_verifier_fails_level_drop(tmp_path: Path) -> None:
    resolved = _resolved_baseline()
    recordings = _recordings_for(
        tmp_path,
        [
            ("resampler", 1.0, 10_000),
            ("agc", 1.0, 2_000),
            ("limiter", 1.0, 2_000),
            ("serializer", 1.0, 2_000),
        ],
    )

    results = verify_recordings(resolved_config=resolved, recordings=recordings)

    level_result = _only(results, stage="agc", invariant="level_preserving")
    assert level_result.passed is False
    assert level_result.observed["delta_db"] < -3.0


def test_verifier_passes_isochronous_cadence_metrics() -> None:
    resolved = _resolved_baseline()
    metrics = _cadence_metrics(stage="serializer", frames_in=50, frames_out=50, jitter_ms=1.0)

    results = verify_recordings(resolved_config=resolved, recordings=[], metrics=metrics)

    cadence_result = _only(results, stage="serializer", invariant="isochronous")
    assert cadence_result.passed is True
    assert cadence_result.observed["frames_out_in_ratio"] == 1.0


def test_verifier_fails_isochronous_cadence_metrics() -> None:
    resolved = _resolved_baseline()
    metrics = _cadence_metrics(stage="serializer", frames_in=50, frames_out=7, jitter_ms=8.0)

    results = verify_recordings(resolved_config=resolved, recordings=[], metrics=metrics)

    cadence_result = _only(results, stage="serializer", invariant="isochronous")
    assert cadence_result.passed is False
    assert cadence_result.observed["frames_out_in_ratio"] == 0.14


def test_run_metrics_and_verifications_endpoints_include_stage_tap_recording_checks(
    tmp_path: Path,
) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
    }

    response = client.post("/runs", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert {metric["name"] for metric in body["metrics"]} >= {
        "frames_in",
        "frames_out",
        "frame_cadence_jitter_ms",
        "expected_frame_interval_ms",
    }
    assert {
        (verification["stage"], verification["invariant"], verification["passed"])
        for verification in body["verifications"]
    } == {
        ("resampler", "isochronous", True),
        ("serializer", "duration_preserving", True),
        ("serializer", "isochronous", True),
    }

    metrics_response = client.get(f"/runs/{body['run_id']}/metrics")
    assert metrics_response.status_code == 200
    assert len(metrics_response.json()) == len(body["metrics"])

    verifications_response = client.get(f"/runs/{body['run_id']}/verifications")
    assert verifications_response.status_code == 200
    assert verifications_response.json() == body["verifications"]


def _resolved_baseline() -> dict[str, object]:
    service = RegistryService()
    for manifest in MANIFESTS:
        service.register_manifest(load_json(manifest))
    service.register_config(load_json(ROOT / "examples/configs/valid-baseline.json"))
    return service.resolve_config("baseline").resolved


def _recordings_for(
    root: Path,
    specs: list[tuple[str, float, int]],
) -> list[RecordingArtifact]:
    recordings = []
    for stage, duration_seconds, amplitude in specs:
        path = root / f"{stage}.wav"
        _write_sine_wav(path, duration_seconds=duration_seconds, amplitude=amplitude)
        recordings.append(
            RecordingArtifact(
                stage=stage,
                uri=path.as_uri(),
                format={"encoding": "pcm16", "rate": 1000, "channels": 1},
                duration_ms=duration_seconds * 1000.0,
            )
        )
    return recordings


def _write_sine_wav(path: Path, *, duration_seconds: float, amplitude: int) -> None:
    rate = 1000
    frame_count = int(rate * duration_seconds)
    frames = bytearray()
    for index in range(frame_count):
        sample = int(amplitude * math.sin(2.0 * math.pi * index / 50.0))
        frames.extend(struct.pack("<h", sample))

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))


def _cadence_metrics(
    *,
    stage: str,
    frames_in: float,
    frames_out: float,
    jitter_ms: float,
) -> list[MetricArtifact]:
    ts = datetime.now(UTC)
    return [
        MetricArtifact(stage=stage, name="frames_in", value=frames_in, ts=ts),
        MetricArtifact(stage=stage, name="frames_out", value=frames_out, ts=ts),
        MetricArtifact(stage=stage, name="frame_cadence_jitter_ms", value=jitter_ms, ts=ts),
        MetricArtifact(stage=stage, name="expected_frame_interval_ms", value=20.0, ts=ts),
    ]


def _only(results, *, stage: str, invariant: str):
    matches = [
        result
        for result in results
        if result.stage == stage and result.invariant == invariant
    ]
    assert len(matches) == 1
    return matches[0]
