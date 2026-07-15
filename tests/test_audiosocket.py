from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from voxbench.control_plane.app import create_app
from voxbench.live_demo.observed_run import build_audiosocket_observed_run_payload
from voxbench.media import resample_pcm16_mono
from voxbench.observability import ObservationBatch, ObservationTransport, VoxBenchObserver
from voxbench.realtime_providers import AudioChunk, PlaybackPosition, ProviderEvent
from voxbench.telephony import (
    AudioSocketLoopbackServer,
    AudioSocketRealtimeServer,
    LoopbackCallSession,
    RealtimeCallSession,
    apply_agc,
    apply_limiter,
    read_frame,
    write_frame,
)


class CapturingTransport(ObservationTransport):
    def __init__(self) -> None:
        self.batches: list[ObservationBatch] = []

    def send(self, batch: ObservationBatch) -> None:
        self.batches.append(batch)


class ApiTestTransport(ObservationTransport):
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def send(self, batch: ObservationBatch) -> None:
        response = self.client.post("/v1/observations", json=batch.to_payload())
        response.raise_for_status()


class FakeProviderSession:
    input_rate = 16_000
    output_rate = 24_000

    def __init__(self, messages=None) -> None:
        self.sent: list[AudioChunk] = []
        self.closed = False
        self.interrupts = 0
        self.messages = messages or [
            AudioChunk(pcm=_pcm(1000, frame_count=480), sample_rate=self.output_rate)
        ]

    async def send_pcm(self, audio: AudioChunk) -> None:
        self.sent.append(audio)

    async def receive_pcm(self):
        async for message in self.receive():
            if isinstance(message, AudioChunk):
                yield message

    async def receive(self):
        for message in self.messages:
            yield message

    async def interrupt(self) -> bool:
        self.interrupts += 1
        return True

    async def close(self) -> None:
        self.closed = True


def _pcm(value: int, frame_count: int = 160) -> bytes:
    return struct.pack("<h", value) * frame_count


def test_agc_and_limiter_process_pcm16le() -> None:
    amplified, gain = apply_agc(
        _pcm(1000),
        target_rms=3000.0,
        max_gain=2.0,
        noise_floor=100.0,
    )
    assert gain == pytest.approx(2.0)
    assert struct.unpack_from("<h", amplified)[0] == 2000

    limited = apply_limiter(_pcm(30_000), ceiling=0.5)
    assert struct.unpack_from("<h", limited)[0] == round(32767 * 0.5)

    unchanged, silent_gain = apply_agc(
        _pcm(20),
        target_rms=3000.0,
        max_gain=8.0,
        noise_floor=100.0,
    )
    assert unchanged == _pcm(20)
    assert silent_gain == 1.0


def test_pcm_resampler_changes_8khz_frame_to_provider_rate() -> None:
    source = _pcm(1200, frame_count=160)
    output = resample_pcm16_mono(source, input_rate=8_000, output_rate=24_000)
    assert len(output) == 960
    assert struct.unpack_from("<h", output)[0] == 1200


def test_streaming_resampler_preserves_chunk_phase() -> None:
    from voxbench.media import Pcm16MonoStreamResampler

    source = _pcm(1200, frame_count=320)
    stream = Pcm16MonoStreamResampler(input_rate=8_000, output_rate=24_000)
    chunked = stream.process(source[:320]) + stream.process(source[320:]) + stream.flush()
    one_shot = resample_pcm16_mono(source, input_rate=8_000, output_rate=24_000)
    assert chunked == one_shot


def test_audiosocket_server_echoes_observed_pcm() -> None:
    async def scenario() -> tuple[CapturingTransport, int]:
        transport = CapturingTransport()
        completed = 0

        async def session_factory(call_uuid) -> LoopbackCallSession:
            nonlocal completed

            def complete() -> None:
                nonlocal completed
                completed += 1

            return LoopbackCallSession(
                call_id=str(call_uuid),
                observer=VoxBenchObserver("run-1", transport),
                complete_run=complete,
                target_rms=2000.0,
                max_gain=4.0,
                noise_floor=100.0,
                limiter_ceiling=0.9,
            )

        bridge = AudioSocketLoopbackServer(session_factory=session_factory)
        server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        call_uuid = uuid4()
        await write_frame(writer, 0x01, call_uuid.bytes)
        await write_frame(writer, 0x10, _pcm(1000))

        echoed = await read_frame(reader)
        assert echoed is not None
        assert echoed.frame_type == 0x10
        assert struct.unpack_from("<h", echoed.payload)[0] == 2000

        await write_frame(writer, 0x00)
        assert await reader.read() == b""
        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
        return transport, completed

    transport, completed = asyncio.run(scenario())
    assert completed == 1
    assert len(transport.batches) == 1
    batch = transport.batches[0]
    assert {chunk.stage for chunk in batch.audio_chunks} == {
        "resampler",
        "agc",
        "limiter",
        "serializer",
    }
    assert {metric.name for metric in batch.metrics} >= {
        "input_rms",
        "output_rms",
        "delta_db",
        "gain_applied",
    }
    assert [event.method for event in batch.sip_events] == ["INVITE", "BYE"]


def test_audiosocket_observed_payload_resolves(tmp_path: Path) -> None:
    payload = build_audiosocket_observed_run_payload(
        provider="openai-realtime",
        call_id=str(uuid4()),
        target_rms=2500.0,
        max_gain=3.0,
        noise_floor=150.0,
    )
    config = payload["configs"][0]
    agc = next(stage for stage in config["spec"]["media"]["pipeline"] if stage["type"] == "agc")
    assert agc["params"] == {
        "target_rms": 2500.0,
        "max_gain": 3.0,
        "noise_floor": 150.0,
    }
    assert config["spec"]["engine"]["params"]["websocket_url"] == (
        "alias:local-asterisk-audiosocket"
    )

    response = TestClient(create_app(artifact_root=tmp_path / "recordings")).post(
        "/runs/observed",
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "running"


def test_realtime_call_session_resamples_provider_audio_and_observes_stages() -> None:
    async def scenario():
        transport = CapturingTransport()
        provider = FakeProviderSession()
        completed = False

        def complete() -> None:
            nonlocal completed
            completed = True

        session = RealtimeCallSession(
            call_id="call-1",
            observer=VoxBenchObserver("run-1", transport),
            provider_session=provider,
            complete_run=complete,
            target_rms=2000.0,
            noise_floor=100.0,
        )
        await session.send_audio(0x10, _pcm(800, frame_count=160))
        output = [frame async for frame in session.receive_audio()]
        await session.close()
        return transport, provider, completed, output

    transport, provider, completed, output = asyncio.run(scenario())
    assert provider.sent[0].sample_rate == 16_000
    assert len(provider.sent[0].pcm) == 636
    assert len(output) == 1
    assert output[0].frame_type == 0x10
    assert len(output[0].payload) == 320
    assert struct.unpack_from("<h", output[0].payload)[0] == 2000
    assert provider.closed
    assert completed
    batch = transport.batches[0]
    assert {chunk.stage for chunk in batch.audio_chunks} == {
        "resampler",
        "agc",
        "limiter",
        "serializer",
    }
    assert any(metric.name == "provider_input_rms" for metric in batch.metrics)


def test_realtime_call_session_drops_buffered_audio_on_barge_in() -> None:
    async def scenario():
        transport = CapturingTransport()
        provider = FakeProviderSession(
            messages=[
                AudioChunk(pcm=_pcm(500, frame_count=960), sample_rate=24_000),
                ProviderEvent("input_speech_started"),
                AudioChunk(pcm=_pcm(1000, frame_count=480), sample_rate=24_000),
            ]
        )
        session = RealtimeCallSession(
            call_id="call-1",
            observer=VoxBenchObserver("run-1", transport),
            provider_session=provider,
            complete_run=lambda: None,
            target_rms=1000.0,
            max_gain=1.0,
            noise_floor=100.0,
        )
        output = [frame async for frame in session.receive_audio()]
        await session.close()
        return output, transport, provider

    output, transport, provider = asyncio.run(scenario())
    assert len(output) == 1
    assert struct.unpack_from("<h", output[0].payload)[0] == 1000
    assert provider.interrupts == 1
    metrics = [metric for batch in transport.batches for metric in batch.metrics]
    assert any(metric.name == "barge_in_events" for metric in metrics)
    assert any(metric.name == "output_frames_dropped" for metric in metrics)
    assert any(metric.name == "provider_interrupt_requests" for metric in metrics)


def test_realtime_call_session_truncates_openai_item_at_played_position() -> None:
    class TruncatingProviderSession(FakeProviderSession):
        auto_interrupts_on_speech = True

        def __init__(self) -> None:
            super().__init__(messages=[])
            self.release_barge_in = asyncio.Event()
            self.barge_in_processed = asyncio.Event()
            self.truncations: list[PlaybackPosition] = []

        async def receive(self):
            yield AudioChunk(
                pcm=_pcm(500, frame_count=960),
                sample_rate=24_000,
                item_id="msg_assistant_1",
                content_index=0,
            )
            await self.release_barge_in.wait()
            yield ProviderEvent("input_speech_started")

        async def truncate_audio(self, position: PlaybackPosition) -> bool:
            self.truncations.append(position)
            self.barge_in_processed.set()
            return True

    async def scenario():
        transport = CapturingTransport()
        provider = TruncatingProviderSession()
        session = RealtimeCallSession(
            call_id="call-1",
            observer=VoxBenchObserver("run-1", transport),
            provider_session=provider,
            complete_run=lambda: None,
            target_rms=500.0,
            max_gain=1.0,
            noise_floor=100.0,
        )
        stream = session.receive_audio()
        first_frame = await anext(stream)
        session.mark_output_played(first_frame)
        provider.release_barge_in.set()
        await provider.barge_in_processed.wait()
        remaining = [frame async for frame in stream]
        await session.close()
        return first_frame, remaining, provider, transport

    first_frame, remaining, provider, transport = asyncio.run(scenario())
    assert first_frame.playback_position == PlaybackPosition(
        item_id="msg_assistant_1",
        content_index=0,
        audio_end_ms=20,
    )
    assert remaining == []
    assert provider.truncations == [first_frame.playback_position]
    assert provider.interrupts == 0
    metrics = [metric for batch in transport.batches for metric in batch.metrics]
    assert any(metric.name == "provider_truncate_requests" for metric in metrics)
    assert any(metric.name == "provider_auto_interrupts" for metric in metrics)


def test_realtime_audiosocket_path_reaches_control_plane(tmp_path: Path) -> None:
    client = TestClient(create_app(artifact_root=tmp_path / "recordings"))
    call_uuid = uuid4()
    payload = build_audiosocket_observed_run_payload(
        provider="openai-realtime",
        call_id=str(call_uuid),
        target_rms=2000.0,
        max_gain=4.0,
        noise_floor=100.0,
        mode="provider",
    )
    started = client.post("/runs/observed", json=payload)
    assert started.status_code == 200
    run_id = started.json()["run_id"]

    async def scenario() -> FakeProviderSession:
        provider = FakeProviderSession(
            messages=[
                ProviderEvent("response_started"),
                AudioChunk(pcm=_pcm(1000, frame_count=480), sample_rate=24_000),
                ProviderEvent("response_done"),
            ]
        )

        async def session_factory(received_uuid) -> RealtimeCallSession:
            assert received_uuid == call_uuid

            def complete() -> None:
                response = client.post(f"/runs/{run_id}/complete", json={})
                response.raise_for_status()

            def fail(failure_alias: str) -> None:
                response = client.post(
                    f"/runs/{run_id}/fail",
                    json={"failure_alias": failure_alias},
                )
                response.raise_for_status()

            return RealtimeCallSession(
                call_id=str(call_uuid),
                observer=VoxBenchObserver(run_id, ApiTestTransport(client)),
                provider_session=provider,
                complete_run=complete,
                fail_run=fail,
                target_rms=2000.0,
                max_gain=4.0,
                noise_floor=100.0,
            )

        bridge = AudioSocketRealtimeServer(session_factory=session_factory)
        server = await asyncio.start_server(bridge._handle_connection, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await write_frame(writer, 0x01, call_uuid.bytes)
        await write_frame(writer, 0x10, _pcm(800, frame_count=160))

        response_audio = await read_frame(reader)
        assert response_audio is not None
        assert response_audio.frame_type == 0x10
        assert len(response_audio.payload) == 320
        assert struct.unpack_from("<h", response_audio.payload)[0] == 2000
        await write_frame(writer, 0x00)
        assert await reader.read() == b""

        writer.close()
        await writer.wait_closed()
        server.close()
        await server.wait_closed()
        return provider

    provider = asyncio.run(scenario())
    assert provider.closed
    assert provider.sent
    assert client.get(f"/runs/{run_id}").json()["status"] == "completed"

    timeline = client.get(f"/runs/{run_id}/timeline").json()["lanes"]
    assert [event["method"] for event in timeline["sip_ladder"]] == ["INVITE", "BYE"]
    stage_names = {lane["stage"] for lane in timeline["stages"]}
    assert {"resampler", "agc", "limiter", "serializer"} <= stage_names
    metric_names = {
        metric["name"] for lane in timeline["stages"] for metric in lane["metrics"]
    }
    assert {"input_rms", "output_rms", "delta_db", "gain_applied"} <= metric_names
    assert {"provider_input_rms", "provider_response_started", "provider_response_done"} <= {
        metric["name"] for metric in timeline["host"]
    }
    recording_audio = client.get(f"/runs/{run_id}/recordings/agc/audio")
    assert recording_audio.status_code == 200
    assert recording_audio.content.startswith(b"RIFF")
