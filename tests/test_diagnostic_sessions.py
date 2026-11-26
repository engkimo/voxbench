from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app


def _create_run(client: TestClient) -> str:
    response = client.post(
        "/runs/live-demo/simulated",
        json={
            "provider": "gemini-live",
            "scenario": "rtp-gap",
            "duration_ms": 3000,
        },
    )
    assert response.status_code == 200
    return response.json()["run_id"]


def _sse_payloads(response_text: str) -> list[dict]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def test_deterministic_diagnostic_session_streams_resolved_ui_command(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_run(client)

    created = client.post(
        f"/runs/{run_id}/diagnostic-sessions",
        json={"message": "Guide me to the strongest evidence."},
    )

    assert created.status_code == 201
    session = created.json()
    assert session["run_id"] == run_id
    assert session["adapter"] == "deterministic-local"
    assert session["command_count"] == 1
    assert session["acknowledged_command_count"] == 0

    events_response = client.get(f"/diagnostic-sessions/{session['session_id']}/events")
    assert events_response.status_code == 200
    assert events_response.headers["content-type"].startswith("text/event-stream")
    payloads = _sse_payloads(events_response.text)
    assert [payload["type"] for payload in payloads] == [
        "status",
        "evidence_found",
        "answer_delta",
        "ui_command_proposed",
        "ui_command",
        "completed",
    ]
    command = next(
        payload["payload"]["command"] for payload in payloads if payload["type"] == "ui_command"
    )
    assert command["status"] == "resolved"
    assert command["target"]["run_id"] == run_id
    assert command["type"] == "select_incident"


def test_diagnostic_command_acknowledgement_is_idempotent_and_replayable(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_run(client)
    session = client.post(
        f"/runs/{run_id}/diagnostic-sessions",
        json={},
    ).json()
    initial_events = _sse_payloads(
        client.get(f"/diagnostic-sessions/{session['session_id']}/events").text
    )
    command = next(
        payload["payload"]["command"]
        for payload in initial_events
        if payload["type"] == "ui_command"
    )
    result = {
        "command_id": command["command_id"],
        "outcome": "applied",
        "detail": "Selected the incident.",
    }

    acknowledged = client.post(
        f"/diagnostic-sessions/{session['session_id']}/ui-command-results",
        json=result,
    )
    duplicate = client.post(
        f"/diagnostic-sessions/{session['session_id']}/ui-command-results",
        json=result,
    )
    conflict = client.post(
        f"/diagnostic-sessions/{session['session_id']}/ui-command-results",
        json={**result, "outcome": "blocked"},
    )

    assert acknowledged.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json() == acknowledged.json()
    assert conflict.status_code == 409
    snapshot = client.get(f"/diagnostic-sessions/{session['session_id']}").json()
    assert snapshot["acknowledged_command_count"] == 1
    replay = _sse_payloads(
        client.get(f"/diagnostic-sessions/{session['session_id']}/events?after=6").text
    )
    assert [payload["type"] for payload in replay] == ["ui_command_result"]
    assert replay[0]["payload"]["command_id"] == command["command_id"]


def test_diagnostic_session_rejects_unknown_scope_and_bad_resume(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    run_id = _create_run(client)
    session = client.post(
        f"/runs/{run_id}/diagnostic-sessions",
        json={},
    ).json()

    unknown_run = client.post(
        f"/runs/{uuid4()}/diagnostic-sessions",
        json={},
    )
    unknown_command = client.post(
        f"/diagnostic-sessions/{session['session_id']}/ui-command-results",
        json={
            "command_id": str(uuid4()),
            "outcome": "applied",
            "detail": "Not part of this session.",
        },
    )
    bad_resume = client.get(
        f"/diagnostic-sessions/{session['session_id']}/events",
        headers={"Last-Event-ID": "not-an-ordinal"},
    )

    assert unknown_run.status_code == 404
    assert unknown_command.status_code == 422
    assert bad_resume.status_code == 400
    negative_resume = client.get(
        f"/diagnostic-sessions/{session['session_id']}/events",
        headers={"Last-Event-ID": "-1"},
    )
    assert negative_resume.status_code == 400
