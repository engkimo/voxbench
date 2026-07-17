from __future__ import annotations

import struct
import subprocess
import wave
from pathlib import Path
from typing import Any

from voxbench.verification import (
    FullReferenceCandidate,
    FullReferenceSelection,
    VisqolCliScorer,
    full_reference_scores_to_metrics,
    score_full_reference_selection,
)


def _write_wav(
    path: Path,
    *,
    sample_rate: int = 8_000,
    channels: int = 1,
    sample_width: int = 2,
) -> None:
    samples = [((index % 20) - 10) * 1_000 for index in range(80)]
    interleaved = [sample for sample in samples for _ in range(channels)]
    pcm = struct.pack(f"<{len(interleaved)}h", *interleaved)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def _candidate(reference: Path, degraded: Path, *, rate: int = 8_000):
    comparison_format = {"encoding": "pcm16", "rate": rate, "channels": 1}
    return FullReferenceCandidate(
        stage="serializer",
        reference_uri=reference.as_uri(),
        degraded_uri=degraded.as_uri(),
        comparison_format=comparison_format,
        degraded_format=dict(comparison_format),
        transformations=("codec-round-trip:mulaw",),
    )


def _executable(tmp_path: Path) -> Path:
    binary = tmp_path / "visqol"
    binary.write_bytes(b"fake executable")
    binary.chmod(0o700)
    return binary


def test_visqol_speech_adapter_resamples_both_inputs_and_cleans_temporary_wavs(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference-source.wav"
    degraded = tmp_path / "degraded-source.wav"
    _write_wav(reference)
    _write_wav(degraded)
    observed: dict[str, Any] = {}

    def fake_run(
        command: list[str],
        *,
        check: bool,
        stderr: int,
        stdout: int,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        observed.update(
            command=command,
            check=check,
            stderr=stderr,
            stdout=stdout,
            timeout=timeout,
        )
        reference_path = Path(command[command.index("--reference_file") + 1])
        degraded_path = Path(command[command.index("--degraded_file") + 1])
        results_path = Path(command[command.index("--results_csv") + 1])
        observed["temporary_paths"] = (reference_path, degraded_path, results_path)
        for path in (reference_path, degraded_path):
            with wave.open(str(path), "rb") as wav:
                assert wav.getframerate() == 16_000
                assert wav.getnchannels() == 1
                assert wav.getsampwidth() == 2
                assert wav.getnframes() == 160
        results_path.write_text(
            "reference,degraded,moslqo\nreference.wav,degraded.wav,4.25\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    scorer = VisqolCliScorer(
        binary=_executable(tmp_path),
        mode="speech",
        process_runner=fake_run,
    )
    report = score_full_reference_selection(
        FullReferenceSelection(
            candidates=(_candidate(reference, degraded),),
            blocked=(),
        ),
        scorer,
    )

    result = report.results[0]
    assert result.state == "scored"
    assert result.score == 4.25
    assert result.transformations == (
        "visqol-mode:speech",
        "resample:8000->16000",
    )
    assert "--use_speech_mode" in observed["command"]
    assert observed["check"] is False
    assert observed["stderr"] == subprocess.DEVNULL
    assert observed["stdout"] == subprocess.DEVNULL
    assert observed["timeout"] == 120.0
    assert all(not path.exists() for path in observed["temporary_paths"])
    assert full_reference_scores_to_metrics(report)[0].value == 4.25


def test_visqol_audio_adapter_uses_48khz_without_speech_flag(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    degraded = tmp_path / "degraded.wav"
    _write_wav(reference, sample_rate=48_000)
    _write_wav(degraded, sample_rate=48_000)
    observed_command: list[str] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        observed_command.extend(command)
        results_path = Path(command[command.index("--results_csv") + 1])
        results_path.write_text("reference,degraded,moslqo\na,b,4.7\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    report = score_full_reference_selection(
        FullReferenceSelection(
            candidates=(_candidate(reference, degraded, rate=48_000),),
            blocked=(),
        ),
        VisqolCliScorer(
            binary=_executable(tmp_path),
            mode="audio",
            process_runner=fake_run,
        ),
    )

    assert report.results[0].transformations == ("visqol-mode:audio",)
    assert "--use_speech_mode" not in observed_command


def test_missing_visqol_binary_is_unavailable_without_reading_inputs(tmp_path: Path) -> None:
    missing_input = tmp_path / "also-missing.wav"
    scorer = VisqolCliScorer(binary=tmp_path / "missing-visqol")

    report = score_full_reference_selection(
        FullReferenceSelection(
            candidates=(_candidate(missing_input, missing_input),),
            blocked=(),
        ),
        scorer,
    )

    assert report.results[0].state == "unavailable"
    assert report.results[0].reason_alias == "visqol-binary-unavailable"
    assert full_reference_scores_to_metrics(report) == []


def test_invalid_wav_is_a_safe_stage_failure_without_running_visqol(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    degraded = tmp_path / "degraded.wav"
    _write_wav(reference, sample_rate=16_000)
    _write_wav(degraded, sample_rate=16_000, channels=2)
    process_called = False

    def fake_run(*_: Any, **__: Any) -> subprocess.CompletedProcess[bytes]:
        nonlocal process_called
        process_called = True
        raise AssertionError("process should not run")

    report = score_full_reference_selection(
        FullReferenceSelection(
            candidates=(_candidate(reference, degraded, rate=16_000),),
            blocked=(),
        ),
        VisqolCliScorer(binary=_executable(tmp_path), process_runner=fake_run),
    )

    assert process_called is False
    assert report.results[0].state == "failed"
    assert report.results[0].reason_alias == "scorer-error"


def test_visqol_process_output_is_not_retained_in_failure_report(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    degraded = tmp_path / "degraded.wav"
    _write_wav(reference, sample_rate=16_000)
    _write_wav(degraded, sample_rate=16_000)

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=b"token=secret",
            stderr=b"failed at https://private.invalid/model",
        )

    report = score_full_reference_selection(
        FullReferenceSelection(
            candidates=(_candidate(reference, degraded, rate=16_000),),
            blocked=(),
        ),
        VisqolCliScorer(binary=_executable(tmp_path), process_runner=fake_run),
    )

    assert report.results[0].state == "failed"
    assert report.results[0].reason_alias == "scorer-error"
    assert "secret" not in repr(report)
    assert "private.invalid" not in repr(report)
