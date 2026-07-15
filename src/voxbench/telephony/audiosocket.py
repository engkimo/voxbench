"""Asterisk AudioSocket protocol and an observed PCM loopback bridge."""

from __future__ import annotations

import asyncio
import struct
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from uuid import UUID

from voxbench.media import Pcm16MonoStreamResampler
from voxbench.observability import SipEvent, VoxBenchObserver
from voxbench.observability.observer import pcm_s16le_rms
from voxbench.realtime_providers import AudioChunk as ProviderAudioChunk
from voxbench.realtime_providers import (
    PlaybackPosition,
    ProviderEvent,
    RealtimeProviderSession,
)

TYPE_TERMINATE = 0x00
TYPE_UUID = 0x01
TYPE_DTMF = 0x03
TYPE_ERROR = 0xFF

AUDIO_TYPE_SAMPLE_RATES: dict[int, int] = {
    0x10: 8_000,
    0x11: 12_000,
    0x12: 16_000,
    0x13: 24_000,
    0x14: 32_000,
    0x15: 44_100,
    0x16: 48_000,
    0x17: 96_000,
    0x18: 192_000,
}


@dataclass(frozen=True)
class AudioSocketFrame:
    frame_type: int
    payload: bytes
    playback_position: PlaybackPosition | None = None


async def read_frame(reader: asyncio.StreamReader) -> AudioSocketFrame | None:
    """Read one AudioSocket frame, returning None on a clean socket close."""

    try:
        header = await reader.readexactly(3)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise ConnectionError("truncated AudioSocket header") from exc
    frame_type = header[0]
    payload_length = int.from_bytes(header[1:3], byteorder="big")
    try:
        payload = await reader.readexactly(payload_length)
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError("truncated AudioSocket payload") from exc
    return AudioSocketFrame(frame_type=frame_type, payload=payload)


async def write_frame(
    writer: asyncio.StreamWriter,
    frame_type: int,
    payload: bytes = b"",
) -> None:
    if not 0 <= frame_type <= 0xFF:
        raise ValueError("AudioSocket frame type must fit in one byte")
    if len(payload) > 0xFFFF:
        raise ValueError("AudioSocket payload exceeds 16-bit frame length")
    writer.write(bytes((frame_type,)) + len(payload).to_bytes(2, "big") + payload)
    await writer.drain()


def apply_agc(
    pcm_s16le: bytes,
    *,
    target_rms: float,
    max_gain: float,
    noise_floor: float,
) -> tuple[bytes, float]:
    """Apply a deterministic chunk-level RMS gain suitable for the demo bridge."""

    input_rms = pcm_s16le_rms(pcm_s16le)
    if input_rms <= noise_floor or input_rms <= 0.0:
        return pcm_s16le, 1.0
    gain = min(max_gain, target_rms / input_rms)
    output = bytearray(len(pcm_s16le))
    for offset, (sample,) in enumerate(struct.iter_unpack("<h", pcm_s16le)):
        value = round(sample * gain)
        struct.pack_into("<h", output, offset * 2, min(32767, max(-32768, value)))
    return bytes(output), gain


def apply_limiter(pcm_s16le: bytes, *, ceiling: float) -> bytes:
    """Clamp PCM16LE samples to a normalized peak ceiling."""

    if not 0.0 < ceiling <= 1.0:
        raise ValueError("limiter ceiling must be in (0, 1]")
    limit = round(32767 * ceiling)
    output = bytearray(len(pcm_s16le))
    for offset, (sample,) in enumerate(struct.iter_unpack("<h", pcm_s16le)):
        struct.pack_into("<h", output, offset * 2, min(limit, max(-limit, sample)))
    return bytes(output)


@dataclass
class LoopbackCallSession:
    call_id: str
    observer: VoxBenchObserver
    complete_run: Callable[[], None]
    target_rms: float = 3000.0
    max_gain: float = 8.0
    noise_floor: float = 200.0
    limiter_ceiling: float = 0.7
    flush_interval_seconds: float = 0.25
    _next_flush_at: float = field(default_factory=lambda: monotonic() + 0.25)
    _flush_task: asyncio.Task[int] | None = None

    async def process_audio(self, frame_type: int, pcm_s16le: bytes) -> bytes:
        sample_rate_hz = AUDIO_TYPE_SAMPLE_RATES[frame_type]
        self.observer.observe_stage_audio(
            stage="resampler",
            input_pcm_s16le=pcm_s16le,
            output_pcm_s16le=pcm_s16le,
            sample_rate_hz=sample_rate_hz,
        )
        agc_output, gain = apply_agc(
            pcm_s16le,
            target_rms=self.target_rms,
            max_gain=self.max_gain,
            noise_floor=self.noise_floor,
        )
        self.observer.observe_stage_audio(
            stage="agc",
            input_pcm_s16le=pcm_s16le,
            output_pcm_s16le=agc_output,
            sample_rate_hz=sample_rate_hz,
            gain_applied=gain,
        )
        limited = apply_limiter(agc_output, ceiling=self.limiter_ceiling)
        self.observer.observe_stage_audio(
            stage="limiter",
            input_pcm_s16le=agc_output,
            output_pcm_s16le=limited,
            sample_rate_hz=sample_rate_hz,
        )
        self.observer.observe_stage_audio(
            stage="serializer",
            input_pcm_s16le=limited,
            output_pcm_s16le=limited,
            sample_rate_hz=sample_rate_hz,
        )
        self._schedule_flush_if_due()
        return limited

    def observe_dtmf(self, digit: str) -> None:
        self.observer.observe_metric("dtmf_events", 1.0)

    async def close(self) -> None:
        self.observer.observe_sip_event(
            SipEvent(
                call_id=self.call_id,
                method="BYE",
                direction="in",
                summary_alias="audiosocket-call-ended",
            )
        )
        if self._flush_task is not None:
            await self._flush_task
        await asyncio.to_thread(self.observer.flush)
        await asyncio.to_thread(self.complete_run)

    def _schedule_flush_if_due(self) -> None:
        now = monotonic()
        if now < self._next_flush_at:
            return
        self._next_flush_at = now + self.flush_interval_seconds
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(asyncio.to_thread(self.observer.flush))


SessionFactory = Callable[[UUID], Awaitable[LoopbackCallSession]]


@dataclass
class AudioSocketLoopbackServer:
    session_factory: SessionFactory
    host: str = "127.0.0.1"
    port: int = 9019

    async def serve_forever(self) -> None:
        server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        async with server:
            await server.serve_forever()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        session: LoopbackCallSession | None = None
        try:
            identity = await read_frame(reader)
            if identity is None:
                return
            if identity.frame_type != TYPE_UUID or len(identity.payload) != 16:
                await write_frame(writer, TYPE_ERROR, b"invalid-uuid")
                return
            call_uuid = UUID(bytes=identity.payload)
            session = await self.session_factory(call_uuid)
            session.observer.observe_sip_event(
                SipEvent(
                    call_id=str(call_uuid),
                    method="INVITE",
                    direction="in",
                    summary_alias="asterisk-audiosocket-connected",
                )
            )

            while True:
                frame = await read_frame(reader)
                if frame is None or frame.frame_type == TYPE_TERMINATE:
                    break
                if frame.frame_type in AUDIO_TYPE_SAMPLE_RATES:
                    if len(frame.payload) % 2:
                        await write_frame(writer, TYPE_ERROR, b"invalid-pcm")
                        break
                    output = await session.process_audio(frame.frame_type, frame.payload)
                    await write_frame(writer, frame.frame_type, output)
                elif frame.frame_type == TYPE_DTMF and len(frame.payload) == 1:
                    session.observe_dtmf(frame.payload.decode("ascii", errors="replace"))
                elif frame.frame_type == TYPE_ERROR:
                    break
        finally:
            try:
                if session is not None:
                    await session.close()
            finally:
                writer.close()
                await writer.wait_closed()


@dataclass
class PlaybackBuffer:
    max_frames: int = 250
    _frames: deque[AudioSocketFrame] = field(default_factory=deque)
    _condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    _closed: bool = False

    async def put(self, frame: AudioSocketFrame) -> int:
        async with self._condition:
            dropped = 0
            if len(self._frames) >= self.max_frames:
                self._frames.popleft()
                dropped = 1
            self._frames.append(frame)
            self._condition.notify()
            return dropped

    async def get(self) -> AudioSocketFrame | None:
        async with self._condition:
            while not self._frames and not self._closed:
                await self._condition.wait()
            if self._frames:
                return self._frames.popleft()
            return None

    async def clear(self) -> int:
        async with self._condition:
            dropped = len(self._frames)
            self._frames.clear()
            return dropped

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()


@dataclass
class RealtimeCallSession:
    call_id: str
    observer: VoxBenchObserver
    provider_session: RealtimeProviderSession
    complete_run: Callable[[], None]
    fail_run: Callable[[str], None] | None = None
    target_rms: float = 3000.0
    max_gain: float = 8.0
    noise_floor: float = 200.0
    limiter_ceiling: float = 0.7
    telephony_rate: int = 8_000
    _closed: bool = False
    _input_resampler: Pcm16MonoStreamResampler | None = None
    _output_resampler: Pcm16MonoStreamResampler | None = None
    _last_playback_position: PlaybackPosition | None = None
    _last_enqueued_playback_position: PlaybackPosition | None = None
    _inflight_playback_position: PlaybackPosition | None = None
    _inflight_started_at: float | None = None
    _current_provider_item: tuple[str, int] | None = None

    async def send_audio(self, frame_type: int, pcm_s16le: bytes) -> None:
        input_rate = AUDIO_TYPE_SAMPLE_RATES[frame_type]
        if self._input_resampler is None or self._input_resampler.input_rate != input_rate:
            self._input_resampler = Pcm16MonoStreamResampler(
                input_rate=input_rate,
                output_rate=self.provider_session.input_rate,
            )
        provider_pcm = self._input_resampler.process(pcm_s16le)
        if not provider_pcm:
            return
        self.observer.observe_metric(
            "provider_input_rms",
            pcm_s16le_rms(provider_pcm),
        )
        await self.provider_session.send_pcm(
            ProviderAudioChunk(
                pcm=provider_pcm,
                sample_rate=self.provider_session.input_rate,
            )
        )

    async def receive_audio(self) -> AsyncIterator[AudioSocketFrame]:
        playback = PlaybackBuffer()
        producer = asyncio.create_task(self._produce_provider_audio(playback))
        try:
            while True:
                frame = await playback.get()
                if frame is None:
                    break
                yield frame
            await producer
        finally:
            if not producer.done():
                producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer

    async def _produce_provider_audio(self, playback: PlaybackBuffer) -> None:
        packet_buffer = bytearray()
        packet_bytes = round(self.telephony_rate * 0.02) * 2
        try:
            async for message in self.provider_session.receive():
                if isinstance(message, ProviderEvent):
                    self.observer.observe_metric(f"provider_{message.event_type}", 1.0)
                    if message.event_type in {"input_speech_started", "interrupted"}:
                        if message.event_type == "input_speech_started":
                            auto_interrupts = getattr(
                                self.provider_session,
                                "auto_interrupts_on_speech",
                                False,
                            )
                            interrupt = getattr(self.provider_session, "interrupt", None)
                            if auto_interrupts:
                                self.observer.observe_metric(
                                    "provider_auto_interrupts",
                                    1.0,
                                )
                            elif interrupt is not None and await interrupt():
                                self.observer.observe_metric(
                                    "provider_interrupt_requests",
                                    1.0,
                                )
                        truncate = getattr(
                            self.provider_session,
                            "truncate_audio",
                            None,
                        )
                        position = self._effective_playback_position()
                        if self._current_provider_item is not None and (
                            position is None
                            or (
                                position.item_id,
                                position.content_index,
                            )
                            != self._current_provider_item
                        ):
                            position = PlaybackPosition(
                                item_id=self._current_provider_item[0],
                                content_index=self._current_provider_item[1],
                                audio_end_ms=0,
                            )
                        if (
                            position is not None
                            and truncate is not None
                            and await truncate(position)
                        ):
                            self.observer.observe_metric(
                                "provider_truncate_requests",
                                1.0,
                            )
                        self._last_playback_position = None
                        self._last_enqueued_playback_position = None
                        self._inflight_playback_position = None
                        self._inflight_started_at = None
                        self._current_provider_item = None
                        self._output_resampler = None
                        packet_buffer.clear()
                        dropped = await playback.clear()
                        self.observer.observe_metric("barge_in_events", 1.0)
                        if dropped:
                            self.observer.observe_metric(
                                "output_frames_dropped",
                                float(dropped),
                            )
                    continue

                provider_audio = message
                if provider_audio.encoding != "pcm16" or provider_audio.channels != 1:
                    raise ValueError("provider output must be mono pcm16")
                provider_item = (
                    (provider_audio.item_id, provider_audio.content_index)
                    if provider_audio.item_id is not None
                    and provider_audio.content_index is not None
                    else None
                )
                if provider_item != self._current_provider_item:
                    packet_buffer.clear()
                    self._last_enqueued_playback_position = None
                    self._current_provider_item = provider_item
                    self._output_resampler = None
                if (
                    self._output_resampler is None
                    or self._output_resampler.input_rate != provider_audio.sample_rate
                ):
                    self._output_resampler = Pcm16MonoStreamResampler(
                        input_rate=provider_audio.sample_rate,
                        output_rate=self.telephony_rate,
                    )
                resampled = self._output_resampler.process(provider_audio.pcm)
                if not resampled:
                    continue
                self.observer.observe_stage_audio(
                    stage="resampler",
                    input_pcm_s16le=provider_audio.pcm,
                    output_pcm_s16le=resampled,
                    sample_rate_hz=self.telephony_rate,
                )
                agc_output, gain = apply_agc(
                    resampled,
                    target_rms=self.target_rms,
                    max_gain=self.max_gain,
                    noise_floor=self.noise_floor,
                )
                self.observer.observe_stage_audio(
                    stage="agc",
                    input_pcm_s16le=resampled,
                    output_pcm_s16le=agc_output,
                    sample_rate_hz=self.telephony_rate,
                    gain_applied=gain,
                )
                limited = apply_limiter(agc_output, ceiling=self.limiter_ceiling)
                self.observer.observe_stage_audio(
                    stage="limiter",
                    input_pcm_s16le=agc_output,
                    output_pcm_s16le=limited,
                    sample_rate_hz=self.telephony_rate,
                )
                self.observer.observe_stage_audio(
                    stage="serializer",
                    input_pcm_s16le=limited,
                    output_pcm_s16le=limited,
                    sample_rate_hz=self.telephony_rate,
                )
                packet_buffer.extend(limited)
                while len(packet_buffer) >= packet_bytes:
                    payload = bytes(packet_buffer[:packet_bytes])
                    del packet_buffer[:packet_bytes]
                    playback_position = None
                    if self._current_provider_item is not None:
                        playback_position = self._next_playback_position()
                    dropped = await playback.put(
                        AudioSocketFrame(
                            frame_type=0x10,
                            payload=payload,
                            playback_position=playback_position,
                        )
                    )
                    if dropped:
                        self.observer.observe_metric("output_frames_dropped", 1.0)
        finally:
            await playback.close()

    def _next_playback_position(self) -> PlaybackPosition:
        if self._current_provider_item is None:
            raise RuntimeError("provider playback item is not active")
        position = self._last_enqueued_playback_position
        if (
            position is None
            or self._current_provider_item is None
            or (position.item_id, position.content_index)
            != self._current_provider_item
        ):
            audio_end_ms = 0
        else:
            audio_end_ms = position.audio_end_ms
        next_position = PlaybackPosition(
            item_id=self._current_provider_item[0],
            content_index=self._current_provider_item[1],
            audio_end_ms=audio_end_ms + 20,
        )
        self._last_enqueued_playback_position = next_position
        return next_position

    def mark_output_played(self, frame: AudioSocketFrame) -> None:
        position = frame.playback_position
        if position is None or self._current_provider_item is None:
            return
        if (position.item_id, position.content_index) != self._current_provider_item:
            return
        self._last_playback_position = position
        if self._inflight_playback_position == position:
            self._inflight_playback_position = None
            self._inflight_started_at = None

    def mark_output_started(self, frame: AudioSocketFrame) -> None:
        if frame.playback_position is None:
            return
        self._inflight_playback_position = frame.playback_position
        self._inflight_started_at = monotonic()

    def _effective_playback_position(self) -> PlaybackPosition | None:
        inflight = self._inflight_playback_position
        started_at = self._inflight_started_at
        if inflight is None or started_at is None:
            return self._last_playback_position
        frame_start_ms = max(0, inflight.audio_end_ms - 20)
        if (
            self._last_playback_position is not None
            and (
                self._last_playback_position.item_id,
                self._last_playback_position.content_index,
            )
            == (inflight.item_id, inflight.content_index)
        ):
            frame_start_ms = self._last_playback_position.audio_end_ms
        elapsed_ms = max(0, int((monotonic() - started_at) * 1000))
        return PlaybackPosition(
            item_id=inflight.item_id,
            content_index=inflight.content_index,
            audio_end_ms=min(inflight.audio_end_ms, frame_start_ms + elapsed_ms),
        )

    def observe_dtmf(self, digit: str) -> None:
        self.observer.observe_metric("dtmf_events", 1.0)

    async def close(self, failure_alias: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self.observer.observe_sip_event(
            SipEvent(
                call_id=self.call_id,
                method="BYE",
                direction="in",
                summary_alias="provider-call-ended",
            )
        )
        try:
            await self.provider_session.close()
        except Exception:
            failure_alias = failure_alias or "provider-close-error"
        await asyncio.to_thread(self.observer.flush)
        if failure_alias is not None and self.fail_run is not None:
            await asyncio.to_thread(self.fail_run, failure_alias)
        else:
            await asyncio.to_thread(self.complete_run)


RealtimeSessionFactory = Callable[[UUID], Awaitable[RealtimeCallSession]]


@dataclass
class AudioSocketRealtimeServer:
    session_factory: RealtimeSessionFactory
    host: str = "127.0.0.1"
    port: int = 9019

    async def serve_forever(self) -> None:
        server = await asyncio.start_server(self._handle_connection, self.host, self.port)
        async with server:
            await server.serve_forever()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        session: RealtimeCallSession | None = None
        output_task: asyncio.Task[None] | None = None
        failure_alias: str | None = None
        try:
            identity = await read_frame(reader)
            if identity is None:
                return
            if identity.frame_type != TYPE_UUID or len(identity.payload) != 16:
                await write_frame(writer, TYPE_ERROR, b"invalid-uuid")
                return
            call_uuid = UUID(bytes=identity.payload)
            session = await self.session_factory(call_uuid)
            session.observer.observe_sip_event(
                SipEvent(
                    call_id=str(call_uuid),
                    method="INVITE",
                    direction="in",
                    summary_alias="asterisk-provider-connected",
                )
            )
            output_task = asyncio.create_task(self._pump_output(session, writer))

            while True:
                if output_task.done():
                    await output_task
                    break
                frame = await read_frame(reader)
                if frame is None or frame.frame_type == TYPE_TERMINATE:
                    break
                if frame.frame_type in AUDIO_TYPE_SAMPLE_RATES:
                    if len(frame.payload) % 2:
                        break
                    await session.send_audio(frame.frame_type, frame.payload)
                elif frame.frame_type == TYPE_DTMF and len(frame.payload) == 1:
                    session.observe_dtmf(frame.payload.decode("ascii", errors="replace"))
                elif frame.frame_type == TYPE_ERROR:
                    break
        except Exception:
            failure_alias = "realtime-bridge-error"
        finally:
            if output_task is not None and not output_task.done():
                output_task.cancel()
                with suppress(asyncio.CancelledError):
                    await output_task
            try:
                if session is not None:
                    await session.close(failure_alias)
            finally:
                writer.close()
                await writer.wait_closed()

    async def _pump_output(
        self,
        session: RealtimeCallSession,
        writer: asyncio.StreamWriter,
    ) -> None:
        next_send_at = monotonic()
        previous_frame: AudioSocketFrame | None = None
        stream = session.receive_audio()
        while True:
            delay = next_send_at - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if previous_frame is not None:
                session.mark_output_played(previous_frame)
                previous_frame = None
            try:
                frame = await anext(stream)
            except StopAsyncIteration:
                break
            await write_frame(writer, frame.frame_type, frame.payload)
            session.mark_output_started(frame)
            duration = len(frame.payload) / (2 * session.telephony_rate)
            next_send_at = max(next_send_at + duration, monotonic())
            previous_frame = frame
        delay = next_send_at - monotonic()
        if previous_frame is not None and delay > 0:
            await asyncio.sleep(delay)
        if previous_frame is not None:
            session.mark_output_played(previous_frame)
