"""Send a small external-pipeline observation run to a local VoxBench server."""

from __future__ import annotations

import argparse
import json
import math
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from voxbench.observability import (
    HttpObservationTransport,
    RtpPacketTapAdapter,
    RtpStats,
    SipEvent,
    VoxBenchObserver,
)

ROOT = Path(__file__).resolve().parents[2]


def load_json(relative_path: str) -> dict[str, Any]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def run_payload(provider: str) -> dict[str, Any]:
    config_path = f"examples/configs/live-demo-{provider}.json"
    config = load_json(config_path)
    return {
        "config_name": config["meta"]["name"],
        "configs": [config],
        "manifests": [
            load_json("examples/manifests/engine/asterisk.json"),
            load_json(f"examples/manifests/provider/{provider}.json"),
            load_json("examples/manifests/processor/resampler.json"),
            load_json("examples/manifests/processor/agc.json"),
            load_json("examples/manifests/processor/limiter.json"),
            load_json("examples/manifests/processor/serializer.json"),
        ],
        "call_id": "direct-pipeline-example",
    }


def tone(*, rms: float, sample_rate_hz: int, duration_ms: int) -> bytes:
    frame_count = round(sample_rate_hz * duration_ms / 1000)
    amplitude = min(32767.0, rms * math.sqrt(2.0))
    return b"".join(
        struct.pack(
            "<h",
            round(amplitude * math.sin(2.0 * math.pi * 440.0 * index / sample_rate_hz)),
        )
        for index in range(frame_count)
    )


def rtp_datagram(sequence_number: int, rtp_timestamp: int) -> bytes:
    return (
        bytes((0x80, 0x00))
        + sequence_number.to_bytes(2, byteorder="big")
        + rtp_timestamp.to_bytes(4, byteorder="big")
        + b"\x00\x00\x00\x01"
        + b"transient-example-media"
    )


def observe_rtp_scenario(
    observer: VoxBenchObserver,
    scenario: str,
) -> None:
    started_at = datetime.now(UTC)
    tap = RtpPacketTapAdapter(
        observer,
        stream_alias="caller-audio",
        direction="received",
        clock_rate_hz=8_000,
        alignment_uncertainty_ms=2.0,
        capture_drop_counter_supported=True,
        started_at=started_at,
    )
    tap.observe_datagram(
        rtp_datagram(100, 1_600),
        ts=started_at + timedelta(milliseconds=20),
    )
    tap.observe_datagram(
        rtp_datagram(101 if scenario == "clean" else 102, 1_920),
        ts=started_at + timedelta(milliseconds=60),
    )
    if scenario == "capture-drop":
        tap.record_capture_drop()
    tap.report_health(ts=started_at + timedelta(milliseconds=80))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--provider",
        choices=("openai-realtime", "gemini-live"),
        default="openai-realtime",
    )
    parser.add_argument(
        "--rtp-scenario",
        choices=("clean", "verified-gap", "capture-drop"),
        default="verified-gap",
    )
    args = parser.parse_args()

    transport = HttpObservationTransport(args.base_url)
    run = transport.start_run(run_payload(args.provider))
    observer = VoxBenchObserver(run["run_id"], transport)
    observer.observe_sip_event(
        SipEvent(
            call_id="direct-pipeline-example",
            method="INVITE",
            direction="in",
            summary_alias="local-softphone-invite",
        )
    )

    stage_input = tone(rms=900.0, sample_rate_hz=16_000, duration_ms=500)
    stage_output = tone(rms=1800.0, sample_rate_hz=16_000, duration_ms=500)
    observer.observe_stage_audio(
        stage="agc",
        input_pcm_s16le=stage_input,
        output_pcm_s16le=stage_output,
        sample_rate_hz=16_000,
        gain_applied=2.0,
    )
    observer.observe_rtp_stats(RtpStats(jitter_ms=1.1, loss_pct=0.0, mos=4.4))
    observe_rtp_scenario(observer, args.rtp_scenario)
    observer.flush()
    completed = transport.complete_run(run["run_id"])
    print(
        f"completed VoxBench run {completed['run_id']} "
        f"with RTP scenario {args.rtp_scenario}"
    )


if __name__ == "__main__":
    main()
