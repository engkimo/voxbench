from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.control_plane.real_call_diagnostics import analyze_real_call_experiment


class FakeRun:
    def __init__(
        self,
        *,
        run_id: str,
        condition: str,
        barge_in_count: int,
        rtp_report_count: int = 2,
        duration_seconds: int = 25,
        config_hash: str = "same-config",
    ) -> None:
        self.run_id = run_id
        self.provider = "gemini-live"
        self.config_hash = config_hash
        self.started_at = datetime.now(UTC)
        self.ended_at = self.started_at + timedelta(seconds=duration_seconds)
        self.environment = SimpleNamespace(
            tags=["live-demo", "audiosocket", f"experiment-{condition}"],
            started_from="voxbench-audiosocket-realtime",
        )
        self._timeline = SimpleNamespace(
            lanes=SimpleNamespace(
                recordings=[
                    SimpleNamespace(stage="serializer", duration_ms=duration_seconds * 1000)
                ],
                rtp_quality=[SimpleNamespace() for _ in range(rtp_report_count)],
                incidents=[
                    SimpleNamespace(rule_id="barge_in_sequence") for _ in range(barge_in_count)
                ],
            )
        )

    def to_timeline(self):
        return self._timeline


def test_controlled_real_call_experiment_is_ready() -> None:
    baseline = FakeRun(run_id="baseline", condition="no-interruption", barge_in_count=0)
    interrupted = FakeRun(
        run_id="interrupted",
        condition="intentional-barge-in",
        barge_in_count=1,
    )

    result = analyze_real_call_experiment(baseline, interrupted)

    assert result.status == "ready"
    assert all(
        criterion.status == "pass" for criterion in result.criteria if criterion.key != "config"
    )
    assert any(finding.title == "Controlled interruption reproduced" for finding in result.findings)


def test_missing_compare_returns_one_next_step() -> None:
    baseline = FakeRun(run_id="baseline", condition="no-interruption", barge_in_count=0)

    result = analyze_real_call_experiment(baseline, None)

    assert result.status == "needs-compare"
    assert result.compare is None
    assert result.next_actions == [
        "Complete one intentional-barge-in call, then select its run as Compare."
    ]


def test_missing_rtcp_and_baseline_barge_in_are_inconclusive() -> None:
    baseline = FakeRun(
        run_id="baseline",
        condition="no-interruption",
        barge_in_count=1,
        rtp_report_count=0,
    )
    interrupted = FakeRun(
        run_id="interrupted",
        condition="intentional-barge-in",
        barge_in_count=0,
        rtp_report_count=0,
    )

    result = analyze_real_call_experiment(baseline, interrupted)

    assert result.status == "inconclusive"
    assert any(item.key == "rtcp" and item.status == "fail" for item in result.criteria)
    assert any(item.key == "compare-barge-in" and item.status == "fail" for item in result.criteria)
    assert any("headset" in action.lower() for action in result.next_actions)
    assert any("--collect-rtcp" in action for action in result.next_actions)


def test_real_call_diagnostic_endpoint_validates_run_scope(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    created = client.post(
        "/runs/live-demo/simulated",
        json={"provider": "gemini-live", "scenario": "clean", "duration_ms": 3000},
    )
    run_id = created.json()["run_id"]

    baseline_only = client.post(
        "/diagnostics/real-call-experiment",
        json={"primary_run_id": run_id},
    )
    duplicate = client.post(
        "/diagnostics/real-call-experiment",
        json={"primary_run_id": run_id, "compare_run_id": run_id},
    )

    assert baseline_only.status_code == 200
    assert baseline_only.json()["status"] == "needs-compare"
    assert duplicate.status_code == 409
