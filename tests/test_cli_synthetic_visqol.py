from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from voxbench.cli.main import app

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (
    "examples/manifests/engine/asterisk.json",
    "examples/manifests/provider/gemini.json",
    "examples/manifests/processor/resampler.json",
    "examples/manifests/processor/agc.json",
    "examples/manifests/processor/limiter.json",
    "examples/manifests/processor/serializer.json",
)


def _fake_visqol_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "visqol-fake"
    binary.write_text(
        f"""#!{sys.executable}
import pathlib
import sys

arguments = sys.argv[1:]
results = pathlib.Path(arguments[arguments.index("--results_csv") + 1])
results.write_text("reference,degraded,moslqo\\na,b,4.35\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary


def _base_arguments(tmp_path: Path) -> list[str]:
    arguments = [
        "synthetic-visqol",
        "--config",
        str(ROOT / "examples/configs/valid-baseline.json"),
        "--output-root",
        str(tmp_path / "artifacts"),
        "--binary",
        str(_fake_visqol_binary(tmp_path)),
        "--duration-seconds",
        "0.05",
    ]
    for manifest in MANIFESTS:
        arguments.extend(("--manifest", str(ROOT / manifest)))
    return arguments


def test_synthetic_visqol_cli_generates_scores_and_a_safe_report(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, _base_arguments(tmp_path))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state"] == "complete"
    assert payload["summary"] == {
        "invariants_failed": 0,
        "invariants_passed": 5,
        "scores_blocked": 0,
        "scores_failed": 0,
        "scores_scored": 4,
        "scores_unavailable": 0,
    }
    assert [score["stage"] for score in payload["full_reference"]] == [
        "resampler",
        "agc",
        "limiter",
        "serializer",
    ]
    assert all(score["score"] == 4.35 for score in payload["full_reference"])
    assert payload["full_reference"][-1]["transformations"] == [
        "resample:24000->8000",
        "codec-round-trip:mulaw",
        "visqol-mode:speech",
        "resample:8000->16000",
    ]

    report_path = tmp_path / "artifacts/verification-report.json"
    assert json.loads(report_path.read_text(encoding="utf-8")) == payload
    assert (tmp_path / "artifacts/references/serializer.wav").exists()
    serialized = json.dumps(payload)
    assert "file://" not in serialized
    assert str(tmp_path) not in serialized
    assert "secret://" not in serialized


def test_synthetic_visqol_cli_persists_partial_report_when_binary_is_missing(
    tmp_path: Path,
) -> None:
    arguments = _base_arguments(tmp_path)
    binary_index = arguments.index("--binary") + 1
    arguments[binary_index] = str(tmp_path / "missing-visqol")

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["state"] == "partial"
    assert payload["summary"]["scores_unavailable"] == 4
    assert payload["summary"]["scores_scored"] == 0
    assert (tmp_path / "artifacts/verification-report.json").exists()
