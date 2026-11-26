from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app


def _create_diagnostic_run(client: TestClient) -> tuple[str, dict]:
    response = client.post(
        "/runs/live-demo/simulated",
        json={
            "provider": "gemini-live",
            "scenario": "rtp-gap",
            "duration_ms": 3000,
        },
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    return run_id, client.get(f"/runs/{run_id}/timeline").json()


def _command(run_id: str, command_type: str, **values: object) -> dict:
    return {
        "command_id": str(uuid4()),
        "type": command_type,
        "run_id": run_id,
        **values,
    }


def test_ui_command_resolves_incident_and_evidence_scope(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id, timeline = _create_diagnostic_run(client)
    incident = next(
        item for item in timeline["lanes"]["incidents"] if item["rule_id"] == "rtp_sequence_gap_v1"
    )
    evidence_ref = incident["evidence_refs"][0]

    incident_response = client.post(
        f"/runs/{run_id}/ui-commands/resolve",
        json=_command(
            run_id,
            "select_incident",
            incident_id=incident["incident_id"],
        ),
    )
    evidence_response = client.post(
        f"/runs/{run_id}/ui-commands/resolve",
        json=_command(
            run_id,
            "focus_evidence",
            evidence_ref=evidence_ref,
        ),
    )

    assert incident_response.status_code == 200
    assert incident_response.json()["target"] == {
        "run_id": run_id,
        "incident_id": incident["incident_id"],
        "evidence_ref": incident["incident_id"],
        "category": "transport",
        "panel": None,
        "stage": None,
        "direction": "received",
        "artifact_id": None,
        "focus_ms": incident["start_ms"],
        "start_ms": incident["start_ms"],
        "end_ms": incident["end_ms"],
        "highlight_ms": 3000,
    }
    assert evidence_response.status_code == 200
    assert evidence_response.json()["target"]["evidence_ref"] == evidence_ref
    assert evidence_response.json()["target"]["category"] == "transport"


def test_ui_command_keeps_verified_focus_inside_padded_window(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id, timeline = _create_diagnostic_run(client)
    incident = next(
        item for item in timeline["lanes"]["incidents"] if item["rule_id"] == "rtp_arrival_stall_v1"
    )

    window = client.post(
        f"/runs/{run_id}/ui-commands/resolve",
        json=_command(
            run_id,
            "set_time_window",
            evidence_ref=incident["incident_id"],
            padding_ms=250,
        ),
    )
    assert window.status_code == 200
    assert window.json()["target"]["focus_ms"] == incident["start_ms"]
    assert window.json()["target"]["start_ms"] == max(0, incident["start_ms"] - 250)
    assert window.json()["target"]["end_ms"] == min(3000, incident["end_ms"] + 250)


def test_ui_command_rejects_unknown_or_cross_run_targets(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id, _ = _create_diagnostic_run(client)
    other_run_id, _ = _create_diagnostic_run(client)

    unknown_evidence = client.post(
        f"/runs/{run_id}/ui-commands/resolve",
        json=_command(run_id, "focus_evidence", evidence_ref="event:not-present"),
    )
    route_mismatch = client.post(
        f"/runs/{run_id}/ui-commands/resolve",
        json=_command(other_run_id, "open_panel", panel="transport"),
    )
    unsafe_reference = client.post(
        f"/runs/{run_id}/ui-commands/resolve",
        json=_command(
            run_id,
            "focus_evidence",
            evidence_ref="javascript:alert(1)",
        ),
    )

    arbitrary_cursor = client.post(
        f"/runs/{run_id}/ui-commands/resolve",
        json=_command(run_id, "set_time_window", evidence_ref="incident", focus_ms=9),
    )

    assert arbitrary_cursor.status_code == 422
    assert unknown_evidence.status_code == 422
    assert unknown_evidence.json()["detail"] == ("evidence reference is outside the command run")
    assert route_mismatch.status_code == 422
    assert unsafe_reference.status_code == 422
