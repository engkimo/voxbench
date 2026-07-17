from __future__ import annotations

import json
import struct
import sys
import wave
from pathlib import Path

from typer.testing import CliRunner

from voxbench.cli.main import app

runner = CliRunner()


def _write_wav(path: Path, *, sample_rate: int = 8_000) -> None:
    samples = [((index % 20) - 10) * 1_000 for index in range(80)]
    pcm = struct.pack(f"<{len(samples)}h", *samples)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)


def _fake_visqol_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "visqol-fake"
    binary.write_text(
        f"""#!{sys.executable}
import pathlib
import sys

arguments = sys.argv[1:]
results = pathlib.Path(arguments[arguments.index("--results_csv") + 1])
results.write_text("reference,degraded,moslqo\\na,b,4.4\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary


def test_visqol_score_cli_outputs_safe_structured_result(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    degraded = tmp_path / "degraded.wav"
    _write_wav(reference)
    _write_wav(degraded)

    result = runner.invoke(
        app,
        [
            "visqol-score",
            "--reference",
            str(reference),
            "--degraded",
            str(degraded),
            "--binary",
            str(_fake_visqol_binary(tmp_path)),
            "--stage",
            "serializer",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "metric_name": "visqol_moslqo",
        "reason_alias": None,
        "score": 4.4,
        "scorer": "visqol",
        "stage": "serializer",
        "state": "scored",
        "transformations": ["visqol-mode:speech", "resample:8000->16000"],
    }
    assert str(reference) not in result.stdout
    assert str(degraded) not in result.stdout


def test_visqol_score_cli_reports_missing_binary_as_unavailable(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    degraded = tmp_path / "degraded.wav"
    _write_wav(reference, sample_rate=16_000)
    _write_wav(degraded, sample_rate=16_000)

    result = runner.invoke(
        app,
        [
            "visqol-score",
            "--reference",
            str(reference),
            "--degraded",
            str(degraded),
            "--binary",
            str(tmp_path / "missing-visqol"),
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["state"] == "unavailable"
    assert payload["reason_alias"] == "visqol-binary-unavailable"
    assert payload["score"] is None


def test_visqol_score_cli_rejects_mismatched_wav_formats(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    degraded = tmp_path / "degraded.wav"
    _write_wav(reference, sample_rate=8_000)
    _write_wav(degraded, sample_rate=16_000)

    result = runner.invoke(
        app,
        [
            "visqol-score",
            "--reference",
            str(reference),
            "--degraded",
            str(degraded),
            "--binary",
            str(tmp_path / "missing-visqol"),
        ],
    )

    assert result.exit_code == 2
    assert "matching uncompressed mono" in result.output
    assert "PCM16 WAV files" in result.output
