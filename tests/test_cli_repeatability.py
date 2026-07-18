from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from voxbench.cli.main import app
from voxbench.verification import (
    FullReferenceScorerContract,
    FullReferenceStageAggregate,
    FullReferenceTreatmentReport,
    write_full_reference_treatment_report,
)

CONTRACT = FullReferenceScorerContract("visqol", "visqol_moslqo", 1.0, 5.0)


def _write_report(path: Path, treatment: str, mean: float) -> None:
    stage = FullReferenceStageAggregate(
        treatment=treatment,
        stage="serializer",
        scorer="visqol",
        metric_name="visqol_moslqo",
        state="aggregated",
        samples_total=3,
        scored_count=3,
        unavailable_count=0,
        blocked_count=0,
        failed_count=0,
        missing_count=0,
        mean=mean,
        median=mean,
        minimum=mean,
        maximum=mean,
        population_stddev=0.0,
        transformations=("visqol-mode:speech",),
    )
    write_full_reference_treatment_report(
        FullReferenceTreatmentReport(treatment, 3, CONTRACT, (stage,)),
        path,
    )


def test_repeatability_cli_reports_observed_variation_without_tolerance(tmp_path: Path) -> None:
    paths = [tmp_path / f"baseline-{index}.json" for index in range(3)]
    for path, mean in zip(paths, (4.0, 4.1, 4.2), strict=True):
        _write_report(path, path.stem, mean)
    arguments = ["visqol-calibrate-repeatability"]
    for path in paths:
        arguments.extend(("--report", str(path)))

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    stage = payload["stages"][0]
    assert stage["state"] == "calibrated"
    assert stage["observed_max_delta"] == pytest.approx(0.2)
    assert "tolerance" not in stage
    assert str(tmp_path) not in result.stdout


def test_repeatability_cli_is_indeterminate_with_too_few_reports(tmp_path: Path) -> None:
    paths = [tmp_path / f"baseline-{index}.json" for index in range(2)]
    for path, mean in zip(paths, (4.0, 4.1), strict=True):
        _write_report(path, path.stem, mean)
    arguments = ["visqol-calibrate-repeatability"]
    for path in paths:
        arguments.extend(("--report", str(path)))

    result = CliRunner().invoke(app, arguments)

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["stages"][0]["reason_alias"] == "insufficient-repeats"
    assert payload["stages"][0]["observed_max_delta"] is None
