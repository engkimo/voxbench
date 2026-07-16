from __future__ import annotations

import asyncio

import pytest

from voxbench.observability import ObservationBatch, ObservationTransport, VoxBenchObserver
from voxbench.telephony import (
    AmiProtocolError,
    AmiRtcpCollector,
    read_ami_message,
    rtcp_event_to_stats,
)


class CapturingTransport(ObservationTransport):
    def __init__(self) -> None:
        self.batches: list[ObservationBatch] = []

    def send(self, batch: ObservationBatch) -> None:
        self.batches.append(batch)


def test_received_rtcp_event_converts_fixed_fraction_and_clock_units() -> None:
    stats = rtcp_event_to_stats(
        {
            "Event": "RTCPReceived",
            "Channel": "PJSIP/private-channel",
            "From": "203.0.113.5:12345",
            "SSRC": "123456",
            "RTT": "0.0125",
            "ReportCount": "2",
            "Report0FractionLost": "13",
            "Report0IAJitter": "80",
            "Report1FractionLost": "26",
            "Report1IAJitter": "160",
        },
        clock_rate_hz=8_000,
    )

    assert stats is not None
    assert stats.direction == "received"
    assert stats.loss_pct == pytest.approx(26 / 256 * 100)
    assert stats.jitter_ms == pytest.approx(20.0)
    assert stats.rtt_ms == pytest.approx(12.5)
    assert stats.mos is None
    assert set(stats.to_payload()) == {
        "ts",
        "jitter_ms",
        "loss_pct",
        "mos",
        "direction",
        "rtt_ms",
    }


def test_sent_rtcp_event_has_no_rtt_and_unrelated_events_are_ignored() -> None:
    stats = rtcp_event_to_stats(
        {
            "Event": "RTCPSent",
            "RTT": "3.5",
            "ReportCount": "1",
            "Report0FractionLost": "0",
            "Report0IAJitter": "48",
        },
        clock_rate_hz=48_000,
    )

    assert stats is not None
    assert stats.direction == "sent"
    assert stats.jitter_ms == pytest.approx(1.0)
    assert stats.loss_pct == 0.0
    assert stats.rtt_ms is None
    assert rtcp_event_to_stats({"Event": "Hangup"}, clock_rate_hz=8_000) is None


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (
            {"Event": "RTCPReceived", "ReportCount": "1", "Report0FractionLost": "256"},
            "loss value",
        ),
        (
            {"Event": "RTCPReceived", "ReportCount": "1", "Report0IAJitter": "nan"},
            "not finite",
        ),
        ({"Event": "RTCPReceived", "ReportCount": "33"}, "report count"),
        ({"Event": "RTCPReceived", "ReportCount": "0", "RTT": "-1"}, "RTT value"),
    ],
)
def test_invalid_rtcp_values_are_rejected(event: dict[str, str], message: str) -> None:
    with pytest.raises(AmiProtocolError, match=message):
        rtcp_event_to_stats(event, clock_rate_hz=8_000)


def test_rtcp_conversion_rejects_nonpositive_clock_rate() -> None:
    with pytest.raises(ValueError, match="clock_rate_hz"):
        rtcp_event_to_stats({"Event": "RTCPReceived"}, clock_rate_hz=0)


def test_read_ami_message_parses_one_bounded_header_block() -> None:
    async def exercise() -> dict[str, str] | None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"Event: RTCPReceived\r\nReportCount: 0\r\nRTT: 0.01\r\n\r\n")
        reader.feed_eof()
        return await read_ami_message(reader)

    assert asyncio.run(exercise()) == {
        "Event": "RTCPReceived",
        "ReportCount": "0",
        "RTT": "0.01",
    }


def test_collector_authenticates_and_forwards_only_safe_aggregate_fields() -> None:
    async def exercise() -> tuple[int, bytes, CapturingTransport]:
        login_action: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        async def handle_client(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter,
        ) -> None:
            writer.write(b"Asterisk Call Manager/10.0\r\n")
            await writer.drain()
            action = await reader.readuntil(b"\r\n\r\n")
            login_action.set_result(action)
            writer.write(b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n")
            writer.write(
                b"Event: RTCPReceived\r\n"
                b"Channel: PJSIP/private-channel\r\n"
                b"From: 203.0.113.5:12345\r\n"
                b"SSRC: 123456\r\n"
                b"RTT: 0.02\r\n"
                b"ReportCount: 1\r\n"
                b"Report0FractionLost: 13\r\n"
                b"Report0IAJitter: 80\r\n\r\n"
            )
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = CapturingTransport()
        observer = VoxBenchObserver("run-safe", transport)
        collector = AmiRtcpCollector(
            host="127.0.0.1",
            port=port,
            username="collector",
            secret="fake-test-secret",
            clock_rate_hz=8_000,
        )
        try:
            count = await collector.collect(observer, max_events=1)
            action = await login_action
        finally:
            server.close()
            await server.wait_closed()
        return count, action, transport

    count, action, transport = asyncio.run(exercise())

    assert count == 1
    assert b"Username: collector" in action
    assert b"Secret: fake-test-secret" in action
    assert len(transport.batches) == 1
    payload = transport.batches[0].to_payload()
    assert payload["rtp_stats"][0]["direction"] == "received"
    assert payload["rtp_stats"][0]["rtt_ms"] == pytest.approx(20.0)
    serialized = repr(payload)
    assert "private-channel" not in serialized
    assert "203.0.113.5" not in serialized
    assert "123456" not in serialized
    assert "fake-test-secret" not in serialized
