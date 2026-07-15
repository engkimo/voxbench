from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass

import pytest

from voxbench.realtime_providers import (
    AudioChunk,
    GeminiLiveSdkSession,
    OpenAIRealtimeProvider,
    OpenAIRealtimeWebSocketSession,
    PlaybackPosition,
    ProviderEvent,
)


class FakeWebSocket:
    def __init__(self, incoming: list[dict]) -> None:
        self.incoming = [json.dumps(event) for event in incoming]
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.pop(0)


@dataclass
class FakeBlob:
    data: bytes
    mime_type: str


@dataclass
class FakeGeminiResponse:
    data: bytes | None = None
    server_content: object | None = None


@dataclass
class FakeServerContent:
    interrupted: bool = False
    turn_complete: bool = False


class FakeGeminiSession:
    def __init__(self) -> None:
        self.sent: list[FakeBlob] = []
        self.receive_calls = 0

    async def send_realtime_input(self, *, media: FakeBlob) -> None:
        self.sent.append(media)

    def receive(self):
        self.receive_calls += 1
        responses = [FakeGeminiResponse(data=b"gemini-audio")] if self.receive_calls == 1 else []

        async def iterator():
            for response in responses:
                yield response

        return iterator()


class FakeContextManager:
    def __init__(self) -> None:
        self.exited = False

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exited = True


def test_openai_realtime_session_sends_and_receives_pcm() -> None:
    async def scenario() -> tuple[FakeWebSocket, list[AudioChunk]]:
        encoded = base64.b64encode(b"provider-audio").decode("ascii")
        websocket = FakeWebSocket(
            [
                {"type": "session.updated"},
                {
                    "type": "response.output_audio.delta",
                    "item_id": "msg_assistant_1",
                    "content_index": 0,
                    "delta": encoded,
                },
                {"type": "response.output_audio.done"},
            ]
        )
        session = OpenAIRealtimeWebSocketSession(websocket)
        await session.send_pcm(AudioChunk(pcm=b"\x01\x00", sample_rate=24_000))
        received = [chunk async for chunk in session.receive_pcm()]
        await session.close()
        return websocket, received

    websocket, received = asyncio.run(scenario())
    sent = json.loads(websocket.sent[0])
    assert sent["type"] == "input_audio_buffer.append"
    assert base64.b64decode(sent["audio"]) == b"\x01\x00"
    assert received == [
        AudioChunk(
            pcm=b"provider-audio",
            sample_rate=24_000,
            item_id="msg_assistant_1",
            content_index=0,
        )
    ]
    assert websocket.closed
    assert OpenAIRealtimeWebSocketSession(websocket).auto_interrupts_on_speech


def test_gemini_live_session_sends_and_receives_pcm() -> None:
    async def scenario() -> tuple[FakeGeminiSession, FakeContextManager, list[AudioChunk]]:
        sdk_session = FakeGeminiSession()
        context_manager = FakeContextManager()
        session = GeminiLiveSdkSession(
            session=sdk_session,
            context_manager=context_manager,
            blob_type=FakeBlob,
        )
        await session.send_pcm(AudioChunk(pcm=b"\x02\x00", sample_rate=16_000))
        received = [chunk async for chunk in session.receive_pcm()]
        await session.close()
        return sdk_session, context_manager, received

    sdk_session, context_manager, received = asyncio.run(scenario())
    assert sdk_session.sent == [
        FakeBlob(data=b"\x02\x00", mime_type="audio/pcm;rate=16000")
    ]
    assert received == [AudioChunk(pcm=b"gemini-audio", sample_rate=24_000)]
    assert context_manager.exited


def test_provider_session_rejects_wrong_pcm_format() -> None:
    session = OpenAIRealtimeWebSocketSession(FakeWebSocket([]))

    with pytest.raises(ValueError, match="24000 Hz"):
        asyncio.run(session.send_pcm(AudioChunk(pcm=b"\x00\x00", sample_rate=8_000)))


def test_openai_session_update_uses_pcm_and_semantic_vad() -> None:
    event = OpenAIRealtimeProvider().session_update_event()
    audio = event["session"]["audio"]
    assert audio["input"]["format"] == {"type": "audio/pcm", "rate": 24_000}
    assert audio["input"]["turn_detection"] == {"type": "semantic_vad"}
    assert audio["output"]["format"] == {"type": "audio/pcm"}


def test_openai_realtime_session_normalizes_lifecycle_events() -> None:
    async def scenario():
        session = OpenAIRealtimeWebSocketSession(
            FakeWebSocket(
                [
                    {"type": "input_audio_buffer.speech_started"},
                    {"type": "input_audio_buffer.speech_stopped"},
                    {"type": "response.created"},
                    {"type": "response.done"},
                ]
            )
        )
        return [message async for message in session.receive()]

    assert asyncio.run(scenario()) == [
        ProviderEvent("input_speech_started"),
        ProviderEvent("input_speech_stopped"),
        ProviderEvent("response_started"),
        ProviderEvent("response_done"),
    ]


def test_openai_realtime_session_cancels_only_an_active_response() -> None:
    async def scenario():
        websocket = FakeWebSocket([{"type": "response.created"}])
        session = OpenAIRealtimeWebSocketSession(websocket)
        assert await session.interrupt() is False
        stream = session.receive()
        assert await anext(stream) == ProviderEvent("response_started")
        assert await session.interrupt() is True
        assert await session.interrupt() is False
        return [json.loads(message) for message in websocket.sent]

    assert asyncio.run(scenario()) == [{"type": "response.cancel"}]


def test_openai_realtime_session_truncates_at_played_audio_position() -> None:
    async def scenario():
        websocket = FakeWebSocket([])
        session = OpenAIRealtimeWebSocketSession(websocket)
        sent = await session.truncate_audio(
            PlaybackPosition(
                item_id="msg_assistant_1",
                content_index=0,
                audio_end_ms=340,
            )
        )
        return sent, [json.loads(message) for message in websocket.sent]

    sent, events = asyncio.run(scenario())
    assert sent is True
    assert events == [
        {
            "type": "conversation.item.truncate",
            "item_id": "msg_assistant_1",
            "content_index": 0,
            "audio_end_ms": 340,
        }
    ]


def test_gemini_live_session_normalizes_interruption_events() -> None:
    class InterruptingGeminiSession(FakeGeminiSession):
        def receive(self):
            self.receive_calls += 1
            responses = (
                [
                    FakeGeminiResponse(
                        server_content=FakeServerContent(interrupted=True)
                    ),
                    FakeGeminiResponse(
                        server_content=FakeServerContent(turn_complete=True)
                    ),
                ]
                if self.receive_calls == 1
                else []
            )

            async def iterator():
                for response in responses:
                    yield response

            return iterator()

    async def scenario():
        session = GeminiLiveSdkSession(
            session=InterruptingGeminiSession(),
            context_manager=FakeContextManager(),
            blob_type=FakeBlob,
        )
        return [message async for message in session.receive()]

    assert asyncio.run(scenario()) == [
        ProviderEvent("interrupted"),
        ProviderEvent("response_done"),
    ]
