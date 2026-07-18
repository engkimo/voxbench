from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.engine_harness.storage import MinioRecordingSink
from voxbench.registry.service import load_json

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (
    "examples/manifests/engine/asterisk.json",
    "examples/manifests/provider/gemini.json",
    "examples/manifests/processor/resampler.json",
    "examples/manifests/processor/agc.json",
    "examples/manifests/processor/limiter.json",
    "examples/manifests/processor/serializer.json",
)


class FakeMinioClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payload = b""
        self.upload_path: Path | None = None
        self.sample_widths: list[int] = []

    def fput_object(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        self.upload_path = Path(kwargs["file_path"])
        assert self.upload_path.exists()
        self.payload = self.upload_path.read_bytes()
        with wave.open(str(self.upload_path), "rb") as wav:
            assert wav.getframerate() == 8_000
            assert wav.getnchannels() == 1
            self.sample_widths.append(wav.getsampwidth())
            assert wav.getsampwidth() in {1, 2}
            assert wav.getnframes() == 160
        return object()


def test_minio_recording_sink_uploads_wav_and_returns_credential_free_uri() -> None:
    client = FakeMinioClient()
    client.endpoint = "https://private.invalid"
    client.secret_key = "token=secret"
    sink = MinioRecordingSink(
        client=client,
        bucket="voxbench-recordings",
        prefix="stage-taps/v1",
    )

    artifact = sink.write_stage_wav(
        run_id="018f3f5e-1111-7777-8888-0123456789ab",
        stage="serializer",
        audio_format={"encoding": "pcm16", "rate": 8_000, "channels": 1},
        duration_ms=20.0,
    )

    assert client.calls == [
        {
            "bucket_name": "voxbench-recordings",
            "object_name": (
                "stage-taps/v1/018f3f5e-1111-7777-8888-0123456789ab/serializer.wav"
            ),
            "file_path": str(client.upload_path),
            "content_type": "audio/wav",
        }
    ]
    assert client.payload.startswith(b"RIFF")
    assert client.sample_widths == [2]
    assert client.upload_path is not None and not client.upload_path.exists()
    assert artifact.uri == (
        "s3://voxbench-recordings/stage-taps/v1/"
        "018f3f5e-1111-7777-8888-0123456789ab/serializer.wav"
    )
    assert "private.invalid" not in artifact.uri
    assert "secret" not in artifact.uri
    assert artifact.format == {"encoding": "pcm16", "rate": 8_000, "channels": 1}


@pytest.mark.parametrize(
    ("bucket", "prefix"),
    [
        ("UPPERCASE", "recordings"),
        ("127.0.0.1", "recordings"),
        ("voxbench..recordings", "recordings"),
        ("voxbench-recordings", "../private"),
        ("voxbench-recordings", "safe//private"),
    ],
)
def test_minio_recording_sink_rejects_unsafe_bucket_or_prefix(
    bucket: str,
    prefix: str,
) -> None:
    with pytest.raises(ValueError):
        MinioRecordingSink(client=FakeMinioClient(), bucket=bucket, prefix=prefix)


@pytest.mark.parametrize("value", ["../run", "run/private", "", "https://private.invalid"])
def test_minio_recording_sink_rejects_unsafe_run_and_stage_segments(value: str) -> None:
    sink = MinioRecordingSink(client=FakeMinioClient(), bucket="voxbench-recordings")

    with pytest.raises(ValueError):
        sink.write_stage_wav(
            run_id=value,
            stage="serializer",
            audio_format={"encoding": "pcm16", "rate": 8_000, "channels": 1},
            duration_ms=20.0,
        )
    with pytest.raises(ValueError):
        sink.write_stage_wav(
            run_id="safe-run",
            stage=value,
            audio_format={"encoding": "pcm16", "rate": 8_000, "channels": 1},
            duration_ms=20.0,
        )


def test_control_plane_uses_injected_minio_sink_without_payload_credentials() -> None:
    minio_client = FakeMinioClient()
    minio_client.endpoint = "https://private.invalid"
    minio_client.secret_key = "token=secret"
    app = create_app(
        recording_sink=MinioRecordingSink(
            client=minio_client,
            bucket="voxbench-recordings",
        )
    )
    client = TestClient(app)
    payload = {
        "config_name": "baseline",
        "configs": [load_json(ROOT / "examples/configs/valid-baseline.json")],
        "manifests": [load_json(ROOT / path) for path in MANIFESTS],
    }

    response = client.post("/runs", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert len(body["recordings"]) == 4
    assert all(
        recording["uri"].startswith("s3://voxbench-recordings/recordings/")
        for recording in body["recordings"]
    )
    serialized = response.text
    assert "private.invalid" not in serialized
    assert "token=secret" not in serialized
    assert len(minio_client.calls) == 4
    audio = client.get(f"/runs/{body['run_id']}/recordings/serializer/audio")
    assert audio.status_code == 404
    assert "not available locally" in audio.json()["detail"]
