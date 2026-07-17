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


def _fake_binary(tmp_path: Path) -> Path:
    binary = tmp_path / "visqol-fake"
    binary.write_text(
        f"""#!{sys.executable}
import pathlib
import sys
arguments = sys.argv[1:]
results = pathlib.Path(arguments[arguments.index("--results_csv") + 1])
results.write_text("reference,degraded,moslqo\\na,b,4.2\\n", encoding="utf-8")
""",
        encoding="utf-8",
    )
    binary.chmod(0o700)
    return binary


def _arguments(tmp_path: Path) -> list[str]:
    arguments = [
        "synthetic-visqol-treatment",
        "--config",
        str(ROOT / "examples/configs/valid-baseline.json"),
        "--output-root",
        str(tmp_path / "treatment"),
        "--binary",
        str(_fake_binary(tmp_path)),
        "--treatment",
        "baseline-speech",
        "--sample-count",
        "3",
        "--minimum-samples",
        "3",
        "--duration-seconds",
        "0.05",
    ]
    for manifest in MANIFESTS:
        arguments.extend(("--manifest", str(ROOT / manifest)))
    return arguments


def test_synthetic_treatment_cli_persists_samples_and_aggregates(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, _arguments(tmp_path))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state"] == "complete"
    assert payload["sample_count"] == 3
    assert payload["sample_states"] == ["complete"] * 3
    assert payload["aggregate"]["treatment"] == "baseline-speech"
    assert all(stage["state"] == "aggregated" for stage in payload["aggregate"]["stages"])
    assert all(stage["mean"] == 4.2 for stage in payload["aggregate"]["stages"])
    root = tmp_path / "treatment"
    assert json.loads((root / "treatment-report.json").read_text()) == payload
    for index in range(1, 4):
        assert (root / f"sample-{index:03d}/verification-report.json").exists()
    assert str(tmp_path) not in result.stdout


def test_synthetic_treatment_cli_is_partial_below_minimum(tmp_path: Path) -> None:
    arguments = _arguments(tmp_path)
    arguments[arguments.index("--minimum-samples") + 1] = "4"

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["state"] == "partial"
    assert all(stage["state"] == "insufficient" for stage in payload["aggregate"]["stages"])
    assert all(stage["mean"] is None for stage in payload["aggregate"]["stages"])
