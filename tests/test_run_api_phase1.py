from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.engine_harness.models import HarnessResult
from voxbench.registry.service import load_json

ROOT = Path(__file__).resolve().parents[1]

MANIFESTS = [
    ROOT / "examples/manifests/engine/asterisk.json",
    ROOT / "examples/manifests/provider/gemini.json",
    ROOT / "examples/manifests/processor/resampler.json",
    ROOT / "examples/manifests/processor/agc.json",
    ROOT / "examples/manifests/processor/limiter.json",
    ROOT / "examples/manifests/processor/serializer.json",
]


def test_post_runs_creates_run_recordings_and_spans(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
        "call_id": "sip-call-id-example",
    }

    response = client.post("/runs", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["status"] == "completed"
    assert body["call_id"] == "sip-call-id-example"
    assert len(body["recordings"]) == 4
    assert len(body["spans"]) == 5
    assert {metric["name"] for metric in body["metrics"]} >= {
        "cpu",
        "active_tasks",
        "loop_lag",
    }
    assert all(metric["stage"] is None for metric in body["metrics"] if metric["name"] in {
        "cpu",
        "active_tasks",
        "loop_lag",
    })
    assert {recording["stage"] for recording in body["recordings"]} == {
        "resampler",
        "agc",
        "limiter",
        "serializer",
    }
    for recording in body["recordings"]:
        path = Path(recording["uri"].removeprefix("file://"))
        assert path.exists()
        assert path.read_bytes().startswith(b"RIFF")
        assert path.stat().st_size > 44
        assert recording["duration_ms"] > 0

    run_spans = [span for span in body["spans"] if span["name"] == "voxbench.run"]
    assert len(run_spans) == 1
    assert run_spans[0]["attrs"]["voxbench.run_id"] == body["run_id"]
    assert run_spans[0]["attrs"]["voxbench.config_hash"] == body["config_hash"]
    assert run_spans[0]["attrs"]["conversation_id"] == body["conversation_id"]

    get_response = client.get(f"/runs/{body['run_id']}")
    assert get_response.status_code == 200
    assert get_response.json()["run_id"] == body["run_id"]


def test_post_runs_with_default_relative_artifact_root() -> None:
    app = create_app()
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
    }

    response = client.post("/runs", json=payload)

    assert response.status_code == 200
    assert response.json()["recordings"][0]["uri"].startswith("file://")


def test_get_runs_lists_recent_run_summaries(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
    }

    first = client.post("/runs", json=payload).json()
    second = client.post("/runs", json=payload).json()

    response = client.get("/runs")

    assert response.status_code == 200
    summaries = response.json()
    assert [summary["run_id"] for summary in summaries] == [second["run_id"], first["run_id"]]
    assert summaries[0]["config_hash"] == second["config_hash"]
    assert summaries[0]["recording_count"] == 4
    assert summaries[0]["violation_count"] == 0
    assert summaries[0]["provider"] == "gemini"
    assert summaries[0]["engine"] == "asterisk"


def test_post_runs_stores_environment_metadata_and_readiness(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
        "environment": {
            "environment_profile": "demo",
            "server_alias": "demo-host-a",
            "integration_target_alias": "crm-sandbox",
            "environment_snapshot_hash": "envsha-20260630-a",
            "started_from": "phase4-dry-run",
            "operator_note": "codec path ready, waiting on route confirmation",
            "manual_blockers": ["route-confirmation"],
            "tags": ["phase4", "demo-prep"],
            "related_internal_ref": "handoff-20260630",
            "secret_ref_names": ["gemini-api-key-ref"],
        },
        "readiness_checklist": [
            {
                "item_id": "ai_phone_setup_complete",
                "label": "AI phone setup complete",
                "status": "pass",
                "note": "phone alias registered",
            },
            {
                "item_id": "connection_route_verified",
                "label": "Connection route verified",
                "status": "fail",
                "note": "route owner confirmation pending",
            },
            {
                "item_id": "host_metrics_enabled",
                "label": "Host metrics enabled",
                "status": "unknown",
            },
        ],
    }

    response = client.post("/runs", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["environment"]["environment_profile"] == "demo"
    assert body["environment"]["server_alias"] == "demo-host-a"
    assert body["environment"]["secret_ref_names"] == ["gemini-api-key-ref"]
    assert body["readiness_summary"] == {
        "passed_count": 1,
        "failed_count": 1,
        "unknown_count": 1,
        "manual_blocker_count": 1,
        "incomplete_count": 3,
    }

    timeline_response = client.get(f"/runs/{body['run_id']}/timeline")
    assert timeline_response.status_code == 200
    timeline = timeline_response.json()
    assert timeline["environment"]["integration_target_alias"] == "crm-sandbox"
    assert timeline["readiness_summary"]["incomplete_count"] == 3
    assert [item["status"] for item in timeline["readiness_checklist"]] == [
        "pass",
        "fail",
        "unknown",
    ]

    summaries = client.get("/runs").json()
    assert summaries[0]["environment_profile"] == "demo"
    assert summaries[0]["server_alias"] == "demo-host-a"
    assert summaries[0]["integration_target_alias"] == "crm-sandbox"
    assert summaries[0]["readiness_failed_count"] == 1
    assert summaries[0]["readiness_unknown_count"] == 1
    assert summaries[0]["manual_blocker_count"] == 1
    assert summaries[0]["tags"] == ["phase4", "demo-prep"]


def test_get_runs_live_preview_lists_readiness_blockers_and_host_metrics(
    tmp_path: Path,
) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
        "environment": {
            "environment_profile": "integration",
            "server_alias": "int-host-a",
            "integration_target_alias": "crm-sandbox",
            "manual_blockers": ["db-registration"],
            "tags": ["phase4"],
        },
        "readiness_checklist": [
            {
                "item_id": "ai_phone_setup_complete",
                "label": "AI phone setup complete",
                "status": "pass",
            },
            {
                "item_id": "intermediate_db_environment_registration_complete",
                "label": "Intermediate DB/environment registration complete",
                "status": "fail",
            },
        ],
    }

    created = client.post("/runs", json=payload).json()

    response = client.get("/runs/live-preview")

    assert response.status_code == 200
    preview = response.json()
    assert len(preview) == 1
    assert preview[0]["run_id"] == created["run_id"]
    assert preview[0]["environment_profile"] == "integration"
    assert preview[0]["server_alias"] == "int-host-a"
    assert preview[0]["integration_target_alias"] == "crm-sandbox"
    assert preview[0]["readiness_summary"]["failed_count"] == 1
    assert preview[0]["readiness_summary"]["manual_blocker_count"] == 1
    assert preview[0]["manual_blockers"] == ["db-registration"]
    assert {metric["name"] for metric in preview[0]["latest_host_metrics"]} == {
        "cpu",
        "active_tasks",
        "loop_lag",
    }
    assert preview[0]["tags"] == ["phase4"]


def test_live_websocket_streams_live_preview_snapshots(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
        "environment": {
            "environment_profile": "demo",
            "server_alias": "demo-host-a",
            "manual_blockers": ["route-confirmation"],
        },
        "readiness_checklist": [
            {
                "item_id": "connection_route_verified",
                "label": "Connection route verified",
                "status": "fail",
            },
        ],
    }
    created = client.post("/runs", json=payload).json()

    with client.websocket_connect("/live?interval_ms=100") as websocket:
        snapshot = websocket.receive_json()

    assert len(snapshot) == 1
    assert snapshot[0]["run_id"] == created["run_id"]
    assert snapshot[0]["status"] == "completed"
    assert snapshot[0]["environment_profile"] == "demo"
    assert snapshot[0]["manual_blockers"] == ["route-confirmation"]
    assert snapshot[0]["readiness_summary"]["failed_count"] == 1
    assert {metric["name"] for metric in snapshot[0]["latest_host_metrics"]} == {
        "cpu",
        "active_tasks",
        "loop_lag",
    }


def test_live_preview_exposes_running_run_during_create(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    original_create_harness = app.state.voxbench.create_harness
    harness_started = threading.Event()
    release_harness = threading.Event()

    class BlockingHarness:
        def __init__(self) -> None:
            self.inner = original_create_harness()

        def run_once(
            self,
            *,
            run_id: str,
            resolved_config: dict[str, object],
            config_hash: str,
        ) -> HarnessResult:
            harness_started.set()
            assert release_harness.wait(timeout=5)
            return self.inner.run_once(
                run_id=run_id,
                resolved_config=resolved_config,
                config_hash=config_hash,
            )

    app.state.voxbench.create_harness = lambda: BlockingHarness()
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
        "environment": {
            "environment_profile": "demo",
            "server_alias": "demo-host-a",
        },
    }
    responses = []

    thread = threading.Thread(
        target=lambda: responses.append(client.post("/runs", json=payload)),
        daemon=True,
    )
    thread.start()
    assert harness_started.wait(timeout=5)

    running_response = client.get("/runs/live-preview")

    assert running_response.status_code == 200
    running_preview = running_response.json()
    assert len(running_preview) == 1
    assert running_preview[0]["status"] == "running"
    assert running_preview[0]["ended_at"] is None
    assert running_preview[0]["latest_host_metrics"] == []

    release_harness.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert responses[0].status_code == 200

    completed_preview = client.get("/runs/live-preview").json()
    assert completed_preview[0]["status"] == "completed"
    assert completed_preview[0]["ended_at"] is not None
    assert {metric["name"] for metric in completed_preview[0]["latest_host_metrics"]} == {
        "cpu",
        "active_tasks",
        "loop_lag",
    }


def test_post_runs_async_returns_running_and_finishes_in_background(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    original_create_harness = app.state.voxbench.create_harness
    harness_started = threading.Event()
    release_harness = threading.Event()

    class BlockingHarness:
        def __init__(self) -> None:
            self.inner = original_create_harness()

        def run_once(
            self,
            *,
            run_id: str,
            resolved_config: dict[str, object],
            config_hash: str,
        ) -> HarnessResult:
            harness_started.set()
            assert release_harness.wait(timeout=5)
            return self.inner.run_once(
                run_id=run_id,
                resolved_config=resolved_config,
                config_hash=config_hash,
            )

    app.state.voxbench.create_harness = lambda: BlockingHarness()
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
        "environment": {
            "environment_profile": "integration",
            "server_alias": "int-host-a",
        },
    }

    response = client.post("/runs/async", json=payload)

    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "running"
    assert accepted["ended_at"] is None
    assert harness_started.wait(timeout=5)

    running_preview = client.get("/runs/live-preview").json()
    assert running_preview[0]["run_id"] == accepted["run_id"]
    assert running_preview[0]["status"] == "running"

    release_harness.set()
    completed = _wait_for_status(client, accepted["run_id"], "completed")
    assert completed["ended_at"] is not None
    assert {metric["name"] for metric in completed["latest_host_metrics"]} == {
        "cpu",
        "active_tasks",
        "loop_lag",
    }


def test_get_runs_example_payload_can_start_async_run(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)

    payload_response = client.get("/runs/example-payload?environment_profile=integration")

    assert payload_response.status_code == 200
    payload = payload_response.json()
    assert payload["config_name"] == "baseline"
    assert payload["configs"]
    assert payload["manifests"]
    assert payload["environment"]["environment_profile"] == "integration"
    assert payload["environment"]["server_alias"] == "integration-host-a"
    assert payload["environment"]["secret_ref_names"] == ["example-provider-api-key-ref"]
    assert all("http" not in ref for ref in payload["environment"]["secret_ref_names"])

    accepted_response = client.post("/runs/async", json=payload)

    assert accepted_response.status_code == 202
    accepted = accepted_response.json()
    assert accepted["status"] in {"running", "completed"}
    completed = _wait_for_status(client, accepted["run_id"], "completed")
    assert completed["environment_profile"] == "integration"
    assert {metric["name"] for metric in completed["latest_host_metrics"]} == {
        "cpu",
        "active_tasks",
        "loop_lag",
    }


def test_post_runs_rejects_raw_external_references_in_environment(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
        "environment": {
            "environment_profile": "demo",
            "server_alias": "https://demo.example.invalid",
        },
    }

    response = client.post("/runs", json=payload)

    assert response.status_code == 422


def test_get_run_timeline_groups_stage_metrics_and_recordings(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
    }

    response = client.post("/runs", json=payload)
    assert response.status_code == 200
    run = response.json()

    timeline_response = client.get(f"/runs/{run['run_id']}/timeline")

    assert timeline_response.status_code == 200
    timeline = timeline_response.json()
    assert timeline["run_id"] == run["run_id"]
    assert timeline["config_hash"] == run["config_hash"]
    assert timeline["t0"]
    assert timeline["lanes"]["sip_ladder"] == []
    assert timeline["lanes"]["rtp_quality"] == []
    assert timeline["lanes"]["turns"] == []
    assert {metric["name"] for metric in timeline["lanes"]["host"]} == {
        "cpu",
        "active_tasks",
        "loop_lag",
    }
    assert len(timeline["lanes"]["host"]) >= 6
    assert all(metric["ts"] >= 0 for metric in timeline["lanes"]["host"])
    assert {stage["stage"] for stage in timeline["lanes"]["stages"]} == {
        "resampler",
        "agc",
        "limiter",
        "serializer",
    }
    assert len(timeline["lanes"]["recordings"]) == len(run["recordings"])
    assert timeline["lanes"]["events"] == []
    assert timeline["lanes"]["incidents"] == []
    assert {artifact["stage"] for artifact in timeline["lanes"]["artifacts"]} == {
        "resampler",
        "agc",
        "limiter",
        "serializer",
    }
    assert all(
        artifact["artifact_ref"].startswith("recording:")
        for artifact in timeline["lanes"]["artifacts"]
    )
    assert {series["category"] for series in timeline["lanes"]["series"]} >= {
        "pipeline",
        "runtime",
    }
    assert all(
        point["t_rel_ms"] >= 0
        for series in timeline["lanes"]["series"]
        for point in series["points"]
    )
    assert {interval["category"] for interval in timeline["lanes"]["intervals"]} >= {
        "pipeline",
        "session",
    }

    serializer_stage = _stage_lane(timeline, "serializer")
    assert serializer_stage["violations"] == []
    assert {metric["name"] for metric in serializer_stage["metrics"]} >= {
        "frames_in",
        "frames_out",
        "frame_cadence_jitter_ms",
        "expected_frame_interval_ms",
    }
    assert all(metric["ts"] >= 0 for metric in serializer_stage["metrics"])


def test_ingest_sip_events_and_rtp_stats_into_timeline(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
        "call_id": "sip-call-id-example",
    }
    run = client.post("/runs", json=payload).json()

    sip_response = client.post(
        "/v1/sip-events",
        json={
            "run_id": run["run_id"],
            "call_id": "sip-call-id-example",
            "method": "INVITE",
            "direction": "in",
            "status_code": 100,
            "summary_alias": "invite-received",
        },
    )
    rtp_response = client.post(
        "/v1/rtp-stats",
        json={
            "run_id": run["run_id"],
            "jitter_ms": 3.5,
            "loss_pct": 0.2,
            "mos": 4.1,
            "direction": "received",
            "rtt_ms": 12.5,
        },
    )

    assert sip_response.status_code == 200
    assert sip_response.json()["method"] == "INVITE"
    assert rtp_response.status_code == 200
    assert rtp_response.json()["mos"] == 4.1

    timeline_response = client.get(f"/runs/{run['run_id']}/timeline")

    assert timeline_response.status_code == 200
    timeline = timeline_response.json()
    assert timeline["lanes"]["sip_ladder"] == [
        {
            "ts": timeline["lanes"]["sip_ladder"][0]["ts"],
            "call_id": "sip-call-id-example",
            "method": "INVITE",
            "direction": "in",
            "status_code": 100,
            "summary_alias": "invite-received",
        }
    ]
    assert timeline["lanes"]["sip_ladder"][0]["ts"] >= 0
    assert timeline["lanes"]["rtp_quality"] == [
        {
            "ts": timeline["lanes"]["rtp_quality"][0]["ts"],
            "jitter_ms": 3.5,
            "loss_pct": 0.2,
            "mos": 4.1,
            "direction": "received",
            "rtt_ms": 12.5,
        }
    ]
    assert timeline["lanes"]["rtp_quality"][0]["ts"] >= 0
    assert timeline["lanes"]["events"] == [
        {
            "event_id": "sip:0",
            "category": "signaling",
            "name": "sip.invite",
            "t_rel_ms": timeline["lanes"]["events"][0]["t_rel_ms"],
            "clock_domain": "control_plane_wall",
            "alignment_uncertainty_ms": None,
            "direction": "in",
            "stage": None,
            "stream_alias": None,
            "source": "sip_event",
            "correlation_alias": None,
            "attributes": {
                "method": "INVITE",
                "status_code": 100,
                "summary_alias": "invite-received",
            },
        }
    ]
    assert timeline["lanes"]["events"][0]["t_rel_ms"] >= 0
    transport_series = [
        series
        for series in timeline["lanes"]["series"]
        if series["category"] == "transport"
    ]
    assert {series["name"] for series in transport_series} == {
        "jitter_ms",
        "loss_pct",
        "mos",
        "rtt_ms",
    }
    assert all(series["direction"] == "received" for series in transport_series)

    invalid_rtp_response = client.post(
        "/v1/rtp-stats",
        json={
            "run_id": run["run_id"],
            "jitter_ms": -1,
            "loss_pct": 101,
            "direction": "sideways",
        },
    )
    assert invalid_rtp_response.status_code == 422


def test_sip_rtp_ingest_rejects_unknown_run_and_raw_external_reference(
    tmp_path: Path,
) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)

    missing_response = client.post(
        "/v1/rtp-stats",
        json={"run_id": "missing-run", "jitter_ms": 1.0},
    )
    raw_reference_response = client.post(
        "/v1/sip-events",
        json={
            "run_id": "missing-run",
            "method": "INVITE",
            "direction": "in",
            "summary_alias": "https://pbx.example.invalid/call",
        },
    )

    assert missing_response.status_code == 404
    assert raw_reference_response.status_code == 422


def test_get_run_recording_audio_streams_stage_wav(tmp_path: Path) -> None:
    app = create_app(artifact_root=tmp_path / "recordings")
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(path) for path in MANIFESTS],
    }

    response = client.post("/runs", json=payload)
    assert response.status_code == 200
    run = response.json()

    audio_response = client.get(f"/runs/{run['run_id']}/recordings/resampler/audio")

    assert audio_response.status_code == 200
    assert audio_response.headers["content-type"].startswith("audio/wav")
    assert audio_response.content.startswith(b"RIFF")

    missing_response = client.get(f"/runs/{run['run_id']}/recordings/not-a-stage/audio")
    assert missing_response.status_code == 404


def _stage_lane(timeline: dict[str, object], stage_name: str) -> dict[str, object]:
    stages = timeline["lanes"]["stages"]
    for stage in stages:
        if stage["stage"] == stage_name:
            return stage
    raise AssertionError(f"unknown stage lane: {stage_name}")


def _wait_for_status(
    client: TestClient,
    run_id: str,
    expected_status: str,
) -> dict[str, object]:
    for _ in range(50):
        preview = client.get("/runs/live-preview").json()
        match = next((run for run in preview if run["run_id"] == run_id), None)
        if match is not None and match["status"] == expected_status:
            return match
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach status {expected_status}")
