from __future__ import annotations

import time
import wave
from io import BytesIO
from threading import Event, Thread
from typing import Any

import pytest

from voxbench.engine_harness.storage import (
    MinioRecordingReader,
    RemoteRecordingBusyError,
    RemoteRecordingIdentityError,
    RemoteRecordingInvalidContentError,
    RemoteRecordingTimeoutError,
    RemoteRecordingTooLargeError,
    RemoteRecordingUnavailableError,
)

RUN_ID = "018f3f5e-1111-7777-8888-0123456789ab"
STAGE = "serializer"
URI = f"s3://voxbench-recordings/stage-taps/v1/{RUN_ID}/{STAGE}.wav"


def _wav_payload(frame_count: int = 160) -> bytes:
    stream = BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(b"\x00\x00" * frame_count)
    return stream.getvalue()


class FakeObjectResponse:
    def __init__(self, payload: bytes, *, failure: Exception | None = None) -> None:
        self.payload = payload
        self.failure = failure
        self.offset = 0
        self.closed = False

    def read(self, amount: int) -> bytes:
        if self.failure is not None:
            raise self.failure
        chunk = self.payload[self.offset : self.offset + amount]
        self.offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeReadClient:
    def __init__(self, response: FakeObjectResponse) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> FakeObjectResponse:
        self.calls.append(kwargs)
        return self.response


def _reader(
    client: FakeReadClient,
    *,
    max_bytes: int = 1024,
    timeout_seconds: float = 1.0,
    max_concurrent_reads: int = 2,
) -> MinioRecordingReader:
    return MinioRecordingReader(
        client=client,
        bucket="voxbench-recordings",
        prefix="stage-taps/v1",
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        max_concurrent_reads=max_concurrent_reads,
    )


def test_minio_recording_reader_reads_exact_bounded_wav_and_closes_response() -> None:
    response = FakeObjectResponse(_wav_payload())
    client = FakeReadClient(response)

    payload = _reader(client).read_wav(uri=URI, run_id=RUN_ID, stage=STAGE)

    assert payload == _wav_payload()
    assert client.calls == [
        {
            "bucket_name": "voxbench-recordings",
            "object_name": f"stage-taps/v1/{RUN_ID}/{STAGE}.wav",
            "length": 1025,
        }
    ]
    assert response.closed is True


@pytest.mark.parametrize(
    "uri",
    [
        f"s3://other-bucket/stage-taps/v1/{RUN_ID}/{STAGE}.wav",
        f"s3://voxbench-recordings/other-prefix/{RUN_ID}/{STAGE}.wav",
        f"s3://voxbench-recordings/stage-taps/v1/other-run/{STAGE}.wav",
        f"s3://voxbench-recordings/stage-taps/v1/{RUN_ID}/agc.wav",
        f"s3://voxbench-recordings/stage-taps/v1/{RUN_ID}/{STAGE}.wav?version=secret",
        f"https://private.invalid/stage-taps/v1/{RUN_ID}/{STAGE}.wav",
    ],
)
def test_minio_recording_reader_rejects_identity_mismatch_before_network(uri: str) -> None:
    client = FakeReadClient(FakeObjectResponse(_wav_payload()))

    with pytest.raises(RemoteRecordingIdentityError):
        _reader(client).read_wav(uri=uri, run_id=RUN_ID, stage=STAGE)

    assert client.calls == []


def test_minio_recording_reader_rejects_oversize_and_closes_response() -> None:
    response = FakeObjectResponse(_wav_payload())
    client = FakeReadClient(response)

    with pytest.raises(RemoteRecordingTooLargeError):
        _reader(client, max_bytes=44).read_wav(uri=URI, run_id=RUN_ID, stage=STAGE)

    assert response.closed is True


def test_minio_recording_reader_rejects_non_wav_content() -> None:
    response = FakeObjectResponse(b"not-a-wave-object")

    with pytest.raises(RemoteRecordingInvalidContentError):
        _reader(FakeReadClient(response)).read_wav(uri=URI, run_id=RUN_ID, stage=STAGE)

    assert response.closed is True


def test_minio_recording_reader_discards_raw_read_error() -> None:
    response = FakeObjectResponse(
        b"",
        failure=RuntimeError("token=private-secret endpoint=https://private.invalid"),
    )

    with pytest.raises(RemoteRecordingUnavailableError) as caught:
        _reader(FakeReadClient(response)).read_wav(uri=URI, run_id=RUN_ID, stage=STAGE)

    assert str(caught.value) == "remote-recording-read-failed"
    assert caught.value.__suppress_context__ is True
    assert response.closed is True


def test_minio_recording_reader_enforces_total_read_deadline() -> None:
    class SlowResponse(FakeObjectResponse):
        def read(self, amount: int) -> bytes:
            time.sleep(0.02)
            return super().read(amount)

    response = SlowResponse(_wav_payload())

    with pytest.raises(RemoteRecordingTimeoutError):
        _reader(
            FakeReadClient(response),
            timeout_seconds=0.001,
        ).read_wav(uri=URI, run_id=RUN_ID, stage=STAGE)

    assert response.closed is True


def test_minio_recording_reader_rejects_concurrent_read_above_limit() -> None:
    entered = Event()
    release = Event()

    class BlockingResponse(FakeObjectResponse):
        def read(self, amount: int) -> bytes:
            entered.set()
            release.wait()
            return super().read(amount)

    client = FakeReadClient(BlockingResponse(_wav_payload()))
    reader = _reader(client, max_concurrent_reads=1)
    result: list[bytes] = []
    worker = Thread(
        target=lambda: result.append(reader.read_wav(uri=URI, run_id=RUN_ID, stage=STAGE))
    )
    worker.start()
    assert entered.wait(timeout=1)

    try:
        with pytest.raises(RemoteRecordingBusyError):
            reader.read_wav(uri=URI, run_id=RUN_ID, stage=STAGE)
    finally:
        release.set()
        worker.join(timeout=1)

    assert result == [_wav_payload()]


def test_minio_recording_reader_rejects_excessive_inflight_capacity() -> None:
    with pytest.raises(ValueError, match="in-flight capacity"):
        _reader(
            FakeReadClient(FakeObjectResponse(_wav_payload())),
            max_bytes=64 * 1024 * 1024,
            max_concurrent_reads=3,
        )
