"""Safe Asterisk AMI RTCP event collection for aggregate RTP telemetry."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from typing import Final

from voxbench.observability import RtpStats, VoxBenchObserver

AMI_DEFAULT_PORT: Final = 5038
AMI_MAX_LINE_BYTES: Final = 8_192
AMI_MAX_HEADERS: Final = 128
AMI_MAX_MESSAGE_BYTES: Final = 65_536
AMI_MAX_REPORTS: Final = 32


class AmiError(RuntimeError):
    """Base error whose message is safe to show to an operator."""


class AmiConnectionError(AmiError):
    """AMI could not be reached or the connection ended unexpectedly."""


class AmiAuthenticationError(AmiError):
    """AMI rejected the configured credentials."""


class AmiProtocolError(AmiError):
    """AMI returned data outside the collector's bounded contract."""


async def read_ami_message(reader: asyncio.StreamReader) -> dict[str, str] | None:
    """Read one CRLF-delimited AMI header block with bounded memory use."""

    headers: dict[str, str] = {}
    total_bytes = 0
    while True:
        try:
            line = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError) as exc:
            raise AmiProtocolError("AMI sent an overlong line") from exc
        if not line:
            if headers:
                raise AmiConnectionError("AMI connection ended during a message")
            return None
        total_bytes += len(line)
        if len(line) > AMI_MAX_LINE_BYTES or total_bytes > AMI_MAX_MESSAGE_BYTES:
            raise AmiProtocolError("AMI message exceeded the size limit")
        if line in {b"\r\n", b"\n"}:
            return headers
        if len(headers) >= AMI_MAX_HEADERS:
            raise AmiProtocolError("AMI message contained too many headers")
        try:
            decoded = line.rstrip(b"\r\n").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AmiProtocolError("AMI message was not valid UTF-8") from exc
        name, separator, value = decoded.partition(":")
        if not separator or not name.strip():
            raise AmiProtocolError("AMI message contained a malformed header")
        headers[name.strip()] = value.strip()


def rtcp_event_to_stats(
    event: Mapping[str, str],
    *,
    clock_rate_hz: int,
) -> RtpStats | None:
    """Convert an RTCP AMI event to safe, aggregate, unit-normalized values."""

    if clock_rate_hz <= 0:
        raise ValueError("clock_rate_hz must be positive")
    values = {key.lower(): value for key, value in event.items()}
    event_name = values.get("event")
    if event_name not in {"RTCPReceived", "RTCPSent"}:
        return None

    direction = "received" if event_name == "RTCPReceived" else "sent"
    report_count = _parse_int(values.get("reportcount", "0"), "ReportCount")
    if not 0 <= report_count <= AMI_MAX_REPORTS:
        raise AmiProtocolError("AMI RTCP report count was outside the supported range")

    loss_values: list[float] = []
    jitter_values: list[float] = []
    for index in range(report_count):
        fraction_value = values.get(f"report{index}fractionlost")
        if fraction_value is not None:
            fraction = _parse_int(fraction_value, "FractionLost")
            if not 0 <= fraction <= 255:
                raise AmiProtocolError("AMI RTCP loss value was outside the supported range")
            loss_values.append(fraction / 256.0 * 100.0)

        jitter_value = values.get(f"report{index}iajitter")
        if jitter_value is not None:
            jitter = _parse_float(jitter_value, "IAJitter")
            if jitter < 0:
                raise AmiProtocolError("AMI RTCP jitter value was negative")
            jitter_values.append(jitter / clock_rate_hz * 1_000.0)

    rtt_ms: float | None = None
    if direction == "received" and (rtt_value := values.get("rtt")) is not None:
        rtt_seconds = _parse_float(rtt_value, "RTT")
        if rtt_seconds < 0:
            raise AmiProtocolError("AMI RTCP RTT value was negative")
        rtt_ms = rtt_seconds * 1_000.0

    jitter_ms = max(jitter_values, default=None)
    loss_pct = max(loss_values, default=None)
    if jitter_ms is None and loss_pct is None and rtt_ms is None:
        return None
    return RtpStats(
        jitter_ms=jitter_ms,
        loss_pct=loss_pct,
        direction=direction,
        rtt_ms=rtt_ms,
    )


class AmiRtcpCollector:
    """Collect RTCP events from a read-only AMI account into one VoxBench run."""

    def __init__(
        self,
        *,
        host: str,
        port: int = AMI_DEFAULT_PORT,
        username: str,
        secret: str,
        clock_rate_hz: int = 8_000,
        connect_timeout_seconds: float = 5.0,
    ) -> None:
        if not 1 <= port <= 65_535:
            raise ValueError("port must be between 1 and 65535")
        if clock_rate_hz <= 0:
            raise ValueError("clock_rate_hz must be positive")
        if connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        _validate_action_value(username, "username")
        _validate_action_value(secret, "secret")
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.clock_rate_hz = clock_rate_hz
        self.connect_timeout_seconds = connect_timeout_seconds

    async def collect(
        self,
        observer: VoxBenchObserver,
        *,
        max_events: int | None = None,
    ) -> int:
        """Connect, authenticate, and flush each safe RTCP point as it arrives."""

        if max_events is not None and max_events < 1:
            raise ValueError("max_events must be positive when provided")
        reader: asyncio.StreamReader
        writer: asyncio.StreamWriter
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host,
                    self.port,
                    limit=AMI_MAX_LINE_BYTES,
                ),
                timeout=self.connect_timeout_seconds,
            )
        except (TimeoutError, OSError) as exc:
            raise AmiConnectionError("AMI connection could not be established") from exc

        try:
            await self._authenticate(reader, writer)
            collected = 0
            async for event in self._events(reader):
                stats = rtcp_event_to_stats(event, clock_rate_hz=self.clock_rate_hz)
                if stats is None:
                    continue
                observer.observe_rtp_stats(stats)
                try:
                    await asyncio.to_thread(observer.flush)
                except Exception as exc:
                    raise AmiConnectionError("VoxBench observation delivery failed") from exc
                collected += 1
                if max_events is not None and collected >= max_events:
                    return collected
            raise AmiConnectionError("AMI connection ended while collecting events")
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            greeting = await asyncio.wait_for(
                reader.readline(),
                timeout=self.connect_timeout_seconds,
            )
        except TimeoutError as exc:
            raise AmiConnectionError("AMI did not send a greeting") from exc
        except ValueError as exc:
            raise AmiProtocolError("AMI sent an overlong greeting") from exc
        if len(greeting) > AMI_MAX_LINE_BYTES or not greeting.startswith(b"Asterisk Call Manager/"):
            raise AmiProtocolError("AMI greeting was not recognized")
        writer.write(
            (
                "Action: Login\r\n"
                f"Username: {self.username}\r\n"
                f"Secret: {self.secret}\r\n"
                "Events: on\r\n\r\n"
            ).encode()
        )
        await writer.drain()

        while True:
            message = await read_ami_message(reader)
            if message is None:
                raise AmiConnectionError("AMI connection ended during authentication")
            response = _header(message, "Response")
            if response is None:
                continue
            if response.lower() != "success":
                raise AmiAuthenticationError("AMI authentication was rejected")
            return

    async def _events(
        self,
        reader: asyncio.StreamReader,
    ) -> AsyncIterator[dict[str, str]]:
        while (message := await read_ami_message(reader)) is not None:
            if _header(message, "Event") in {"RTCPReceived", "RTCPSent"}:
                yield message


def _header(message: Mapping[str, str], name: str) -> str | None:
    target = name.lower()
    return next((value for key, value in message.items() if key.lower() == target), None)


def _parse_int(value: str, field: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise AmiProtocolError(f"AMI RTCP {field} was not an integer") from exc


def _parse_float(value: str, field: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise AmiProtocolError(f"AMI RTCP {field} was not numeric") from exc
    if not math.isfinite(parsed):
        raise AmiProtocolError(f"AMI RTCP {field} was not finite")
    return parsed


def _validate_action_value(value: str, field: str) -> None:
    if not value or "\r" in value or "\n" in value:
        raise ValueError(f"{field} must be non-empty and contain no line breaks")
