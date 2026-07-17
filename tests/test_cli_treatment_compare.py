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


def _report(name: str, mean: float, *, state: str = "aggregated"):
    stage = FullReferenceStageAggregate(
        treatment=name,
        stage="serializer",
        scorer="visqol",
        metric_name="visqol_moslqo",
        state=state,
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
    return FullReferenceTreatmentReport(name, 3, CONTRACT, (stage,))


def _write(path: Path, report: FullReferenceTreatmentReport) -> None:
    write_full_reference_treatment_report(report, path)


def test_compare_treatments_cli_returns_regression_exit_and_path_free_json(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    _write(baseline, _report("baseline", 4.2))
    _write(current, _report("candidate", 3.9))

    result = CliRunner().invoke(
        app,
        [
            "visqol-compare-treatments",
            "--baseline",
            str(baseline),
            "--current",
            str(current),
            "--stable-tolerance",
            "0.1",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["stages"][0]["state"] == "regressed"
    assert payload["stages"][0]["delta"] == pytest.approx(-0.3)
    assert str(tmp_path) not in result.stdout


def test_compare_treatments_cli_returns_indeterminate_exit_for_partial_input(
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    _write(baseline, _report("baseline", 4.2))
    _write(current, _report("candidate", 4.2, state="partial"))

    result = CliRunner().invoke(
        app,
        [
            "visqol-compare-treatments",
            "--baseline",
            str(baseline),
            "--current",
            str(current),
            "--stable-tolerance",
            "0.1",
        ],
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["stages"][0]["state"] == "indeterminate"
    assert payload["stages"][0]["reason_alias"] == "current-not-aggregated"
