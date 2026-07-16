from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATHS = (
    "examples/manifests/engine/asterisk.json",
    "examples/manifests/provider/gemini.json",
    "examples/manifests/processor/resampler.json",
    "examples/manifests/processor/agc.json",
    "examples/manifests/processor/limiter.json",
    "examples/manifests/processor/serializer.json",
)


def _json(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def _observed_payload(
    server_alias: str | None,
    integration_target_alias: str = "voice-target-a",
) -> dict:
    return {
        "config_name": "baseline",
        "configs": [_json("examples/configs/valid-baseline.json")],
        "manifests": [_json(path) for path in MANIFEST_PATHS],
        "environment": {
            "environment_profile": "integration",
            "server_alias": server_alias,
            "integration_target_alias": integration_target_alias,
        },
    }


def _record_completed_run(
    client: TestClient,
    *,
    server_alias: str | None,
    active_tasks: list[float],
    memory_rss_bytes: float | None = None,
    integration_target_alias: str = "voice-target-a",
) -> str:
    run = client.post(
        "/runs/observed",
        json=_observed_payload(server_alias, integration_target_alias),
    )
    assert run.status_code == 200
    run_id = run.json()["run_id"]
    metrics = [{"name": "active_tasks", "value": value} for value in active_tasks]
    if memory_rss_bytes is not None:
        metrics.append({"name": "memory_rss_bytes", "value": memory_rss_bytes})
    observed = client.post(
        "/v1/observations",
        json={"run_id": run_id, "metrics": metrics},
    )
    assert observed.status_code == 200
    completed = client.post(f"/runs/{run_id}/complete", json={})
    assert completed.status_code == 200
    return run_id


def test_cross_session_trends_detect_monotonic_growth_from_latest_run_values(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_ids = [
        _record_completed_run(
            client,
            server_alias="integration-host-a",
            active_tasks=[1, 2],
            memory_rss_bytes=100,
        ),
        _record_completed_run(
            client,
            server_alias="integration-host-a",
            active_tasks=[2],
            memory_rss_bytes=90,
            integration_target_alias="voice-target-b",
        ),
        _record_completed_run(
            client,
            server_alias="integration-host-a",
            active_tasks=[4],
            memory_rss_bytes=110,
        ),
    ]

    response = client.get("/runs/cross-session-trends")

    assert response.status_code == 200
    trends = {trend["metric"]: trend for trend in response.json()}
    active_tasks = trends["active_tasks"]
    assert active_tasks["state"] == "increasing"
    assert active_tasks["sample_count"] == 3
    assert active_tasks["first_value"] == 2
    assert active_tasks["latest_value"] == 4
    assert active_tasks["total_delta"] == 2
    assert [point["run_id"] for point in active_tasks["points"]] == run_ids

    memory = trends["memory_rss_bytes"]
    assert memory["state"] == "stable"
    assert memory["sample_count"] == 3
    assert [point["value"] for point in memory["points"]] == [100, 90, 110]


def test_cross_session_trends_require_three_ended_runs_on_the_same_server(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    _record_completed_run(
        client,
        server_alias="integration-host-a",
        active_tasks=[1],
    )
    _record_completed_run(
        client,
        server_alias="integration-host-a",
        active_tasks=[2],
    )
    _record_completed_run(
        client,
        server_alias="integration-host-b",
        active_tasks=[99],
    )
    _record_completed_run(client, server_alias=None, active_tasks=[100])

    running = client.post(
        "/runs/observed",
        json=_observed_payload("integration-host-a"),
    ).json()
    observed = client.post(
        "/v1/observations",
        json={
            "run_id": running["run_id"],
            "metrics": [{"name": "active_tasks", "value": 1000}],
        },
    )
    assert observed.status_code == 200

    response = client.get("/runs/cross-session-trends")

    assert response.status_code == 200
    trends = response.json()
    assert len(trends) == 2
    by_server = {trend["server_alias"]: trend for trend in trends}
    assert by_server["integration-host-a"]["state"] == "insufficient"
    assert by_server["integration-host-a"]["sample_count"] == 2
    assert by_server["integration-host-b"]["state"] == "insufficient"
    assert by_server["integration-host-b"]["sample_count"] == 1
