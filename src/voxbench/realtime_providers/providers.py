"""Provider adapters for Realtime-style voice sessions.

The concrete network bridges are intentionally thin in this slice. They expose
readiness and session-shape details without requiring optional SDK packages or
API keys for local simulated runs.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class AudioChunk:
    pcm: bytes
    sample_rate: int
    channels: int = 1
    encoding: str = "pcm16"
    item_id: str | None = None
    content_index: int | None = None


@dataclass(frozen=True)
class PlaybackPosition:
    """Provider conversation position represented by audio heard by the caller."""

    item_id: str
    content_index: int
    audio_end_ms: int


ProviderEventType = Literal[
    "input_speech_started",
    "input_speech_stopped",
    "response_started",
    "response_done",
    "interrupted",
]


@dataclass(frozen=True)
class ProviderEvent:
    event_type: ProviderEventType


@dataclass(frozen=True)
class ProviderReadiness:
    provider: str
    model: str
    env_var: str
    has_api_key: bool
    missing_optional_dependency: str | None
    dry_run: bool
    alternate_env_vars: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.dry_run or (self.has_api_key and self.missing_optional_dependency is None)


class RealtimeProviderSession(Protocol):
    input_rate: int
    output_rate: int
    auto_interrupts_on_speech: bool

    async def send_pcm(self, audio: AudioChunk) -> None:
        """Send one mono PCM audio chunk to the provider session."""

    def receive(self) -> AsyncIterator[AudioChunk | ProviderEvent]:
        """Yield provider audio and normalized lifecycle events."""

    def receive_pcm(self) -> AsyncIterator[AudioChunk]:
        """Yield provider response audio chunks."""

    async def interrupt(self) -> bool:
        """Cancel active output when supported, returning whether a request was sent."""

    async def truncate_audio(self, position: PlaybackPosition) -> bool:
        """Remove provider audio after the last position heard by the caller."""

    async def close(self) -> None:
        """Close provider resources."""


@dataclass
class DryRunRealtimeProviderSession:
    """In-memory session used by tests and local demo scaffolding."""

    provider: str
    input_rate: int
    output_rate: int
    auto_interrupts_on_speech: bool = False
    _sent: list[AudioChunk] = field(default_factory=list)

    async def send_pcm(self, audio: AudioChunk) -> None:
        self._sent.append(audio)

    async def receive_pcm(self) -> AsyncIterator[AudioChunk]:
        async for message in self.receive():
            if isinstance(message, AudioChunk):
                yield message

    async def receive(self) -> AsyncIterator[AudioChunk | ProviderEvent]:
        if not self._sent:
            return
        yield AudioChunk(
            pcm=self._sent[-1].pcm,
            sample_rate=self.output_rate,
            channels=self._sent[-1].channels,
            encoding=self._sent[-1].encoding,
        )

    async def interrupt(self) -> bool:
        return False

    async def truncate_audio(self, position: PlaybackPosition) -> bool:
        return False

    async def close(self) -> None:
        return None


@dataclass
class OpenAIRealtimeWebSocketSession:
    """OpenAI Realtime WebSocket session using PCM input and output events."""

    websocket: Any
    input_rate: int = 24000
    output_rate: int = 24000
    auto_interrupts_on_speech: bool = True
    _response_active: bool = False

    async def send_pcm(self, audio: AudioChunk) -> None:
        _validate_pcm_chunk(audio, expected_rate=self.input_rate)
        await self.websocket.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio.pcm).decode("ascii"),
                }
            )
        )

    async def receive_pcm(self) -> AsyncIterator[AudioChunk]:
        async for message in self.receive():
            if isinstance(message, AudioChunk):
                yield message

    async def receive(self) -> AsyncIterator[AudioChunk | ProviderEvent]:
        async for raw_message in self.websocket:
            event = json.loads(raw_message)
            event_type = event.get("type")
            if event_type in {"response.output_audio.delta", "response.audio.delta"}:
                delta = event.get("delta")
                if isinstance(delta, str):
                    item_id = event.get("item_id")
                    content_index = event.get("content_index")
                    yield AudioChunk(
                        pcm=base64.b64decode(delta, validate=True),
                        sample_rate=self.output_rate,
                        item_id=item_id if isinstance(item_id, str) else None,
                        content_index=(
                            content_index if isinstance(content_index, int) else None
                        ),
                    )
            elif event_type == "input_audio_buffer.speech_started":
                yield ProviderEvent("input_speech_started")
            elif event_type == "input_audio_buffer.speech_stopped":
                yield ProviderEvent("input_speech_stopped")
            elif event_type == "response.created":
                self._response_active = True
                yield ProviderEvent("response_started")
            elif event_type == "response.done":
                self._response_active = False
                yield ProviderEvent("response_done")
            elif event_type == "error":
                error = event.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                raise RuntimeError(f"OpenAI Realtime error: {message or 'unknown error'}")

    async def interrupt(self) -> bool:
        if not self._response_active:
            return False
        await self.websocket.send(json.dumps({"type": "response.cancel"}))
        self._response_active = False
        return True

    async def truncate_audio(self, position: PlaybackPosition) -> bool:
        await self.websocket.send(
            json.dumps(
                {
                    "type": "conversation.item.truncate",
                    "item_id": position.item_id,
                    "content_index": position.content_index,
                    "audio_end_ms": position.audio_end_ms,
                }
            )
        )
        return True

    async def close(self) -> None:
        await self.websocket.close()


@dataclass
class GeminiLiveSdkSession:
    """Google Gen AI SDK Live session wrapper."""

    session: Any
    context_manager: Any
    blob_type: Any
    input_rate: int = 16000
    output_rate: int = 24000
    auto_interrupts_on_speech: bool = True

    async def send_pcm(self, audio: AudioChunk) -> None:
        _validate_pcm_chunk(audio, expected_rate=self.input_rate)
        await self.session.send_realtime_input(
            media=self.blob_type(
                data=audio.pcm,
                mime_type=f"audio/pcm;rate={self.input_rate}",
            )
        )

    async def receive_pcm(self) -> AsyncIterator[AudioChunk]:
        async for message in self.receive():
            if isinstance(message, AudioChunk):
                yield message

    async def receive(self) -> AsyncIterator[AudioChunk | ProviderEvent]:
        while True:
            received = False
            async for response in self.session.receive():
                received = True
                data = getattr(response, "data", None)
                if isinstance(data, bytes) and data:
                    yield AudioChunk(pcm=data, sample_rate=self.output_rate)
                server_content = getattr(response, "server_content", None)
                if server_content is not None:
                    if getattr(server_content, "interrupted", False):
                        yield ProviderEvent("interrupted")
                    if getattr(server_content, "turn_complete", False):
                        yield ProviderEvent("response_done")
            if not received:
                return

    async def interrupt(self) -> bool:
        # Gemini Live VAD owns response interruption and reports it in server_content.
        return False

    async def truncate_audio(self, position: PlaybackPosition) -> bool:
        # Gemini owns its playback conversation state and exposes no matching cursor API.
        return False

    async def close(self) -> None:
        await self.context_manager.__aexit__(None, None, None)


@dataclass(frozen=True)
class OpenAIRealtimeProvider:
    model: str = "gpt-realtime-2.1"
    api_key_env_var: str = "OPENAI_API_KEY"
    voice: str = "marin"
    input_rate: int = 24000
    output_rate: int = 24000
    output_encoding: str = "audio/pcm"
    instructions: str = (
        "You are a concise voice assistant. Reply in the language used by the caller."
    )

    provider_name: str = "openai-realtime"

    def readiness(self, *, dry_run: bool = True) -> ProviderReadiness:
        return ProviderReadiness(
            provider=self.provider_name,
            model=self.model,
            env_var=self.api_key_env_var,
            has_api_key=bool(os.environ.get(self.api_key_env_var)),
            missing_optional_dependency=_missing_dependency("websockets"),
            dry_run=dry_run,
        )

    def session_update_event(self) -> dict[str, object]:
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": self.model,
                "instructions": self.instructions,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": self.input_rate},
                        "turn_detection": {"type": "semantic_vad"},
                    },
                    "output": {
                        "format": {"type": self.output_encoding},
                        "voice": self.voice,
                    },
                },
            },
        }

    async def connect(self, *, dry_run: bool = True) -> RealtimeProviderSession:
        readiness = self.readiness(dry_run=dry_run)
        if dry_run:
            return DryRunRealtimeProviderSession(
                self.provider_name,
                self.input_rate,
                self.output_rate,
            )
        if not readiness.ready:
            raise RuntimeError(_readiness_error(readiness))
        from websockets.asyncio.client import connect

        websocket = await connect(
            f"wss://api.openai.com/v1/realtime?model={self.model}",
            additional_headers={
                "Authorization": f"Bearer {os.environ[self.api_key_env_var]}"
            },
            max_size=16 * 1024 * 1024,
        )
        await websocket.send(json.dumps(self.session_update_event()))
        return OpenAIRealtimeWebSocketSession(
            websocket=websocket,
            input_rate=self.input_rate,
            output_rate=self.output_rate,
        )


@dataclass(frozen=True)
class GeminiLiveProvider:
    model: str = "gemini-live-2.5-flash-preview"
    api_key_env_var: str = "GOOGLE_API_KEY"
    alternate_api_key_env_vars: tuple[str, ...] = ("GEMINI_API_KEY",)
    input_rate: int = 16000
    output_rate: int = 24000
    instructions: str = (
        "You are a concise voice assistant. Reply in the language used by the caller."
    )

    provider_name: str = "gemini-live"

    def readiness(self, *, dry_run: bool = True) -> ProviderReadiness:
        return ProviderReadiness(
            provider=self.provider_name,
            model=self.model,
            env_var=self.api_key_env_var,
            alternate_env_vars=self.alternate_api_key_env_vars,
            has_api_key=self._has_api_key(),
            missing_optional_dependency=_missing_dependency("google.genai"),
            dry_run=dry_run,
        )

    def live_connect_config(self) -> dict[str, object]:
        return {
            "model": self.model,
            "config": {
                "response_modalities": ["AUDIO"],
                "system_instruction": self.instructions,
            },
            "input_mime_type": f"audio/pcm;rate={self.input_rate}",
        }

    async def connect(self, *, dry_run: bool = True) -> RealtimeProviderSession:
        readiness = self.readiness(dry_run=dry_run)
        if dry_run:
            return DryRunRealtimeProviderSession(
                self.provider_name,
                self.input_rate,
                self.output_rate,
            )
        if not readiness.ready:
            raise RuntimeError(_readiness_error(readiness))
        from google import genai
        from google.genai import types

        api_key = next(
            os.environ[env_var]
            for env_var in (self.api_key_env_var, *self.alternate_api_key_env_vars)
            if os.environ.get(env_var)
        )
        client = genai.Client(api_key=api_key)
        context_manager = client.aio.live.connect(
            model=self.model,
            config={
                "response_modalities": ["AUDIO"],
                "system_instruction": self.instructions,
            },
        )
        session = await context_manager.__aenter__()
        return GeminiLiveSdkSession(
            session=session,
            context_manager=context_manager,
            blob_type=types.Blob,
            input_rate=self.input_rate,
            output_rate=self.output_rate,
        )

    def _has_api_key(self) -> bool:
        return any(
            bool(os.environ.get(env_var))
            for env_var in (self.api_key_env_var, *self.alternate_api_key_env_vars)
        )


def _missing_dependency(module_name: str) -> str | None:
    try:
        spec = importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        spec = None
    if spec is None:
        return module_name
    return None


def _readiness_error(readiness: ProviderReadiness) -> str:
    missing = []
    if not readiness.has_api_key:
        env_vars = ", ".join((readiness.env_var, *readiness.alternate_env_vars))
        missing.append(f"missing API key env var: {env_vars}")
    if readiness.missing_optional_dependency:
        missing.append(f"missing optional dependency: {readiness.missing_optional_dependency}")
    return f"{readiness.provider} is not ready ({'; '.join(missing)})"


def _validate_pcm_chunk(audio: AudioChunk, *, expected_rate: int) -> None:
    if audio.encoding != "pcm16":
        raise ValueError("realtime providers require pcm16 audio")
    if audio.channels != 1:
        raise ValueError("realtime providers require mono audio")
    if audio.sample_rate != expected_rate:
        raise ValueError(
            f"provider input rate must be {expected_rate} Hz, got {audio.sample_rate} Hz"
        )
    if len(audio.pcm) % 2:
        raise ValueError("pcm16 audio length must be divisible by two")
