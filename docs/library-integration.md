# Library Integration

VoxBench can observe an existing voice application without owning its provider,
telephony, or Pipecat pipeline. The application keeps processing audio and sends
only structured observations and selected PCM stage taps to the VoxBench Control
Plane.

```text
existing voice application
  |-- direct OpenAI Realtime / Gemini Live connection
  |-- Pipecat pipeline
  `-- custom SIP or WebRTC bridge
          |
          `-- VoxBenchObserver -> batch ingest -> timeline / WAV / compare
```

The public boundary is `voxbench.observability`. It has no OpenAI, Gemini, Pipecat,
or HTTP client package dependency. `HttpObservationTransport` uses the Python
standard library.

## Lifecycle

1. Start an observed run with the same resolved-config inputs used by normal
   VoxBench runs.
2. Attach a `VoxBenchObserver` to stage boundaries in the application.
3. Flush observations every 250-1000 ms, at an utterance boundary, or from a
   non-audio worker. Do not block a realtime audio callback on HTTP.
4. Complete the run to execute recording verification and make the run immutable,
   or fail it with a safe diagnostic alias if the owning pipeline aborts.

```python
import asyncio

from voxbench.observability import HttpObservationTransport, VoxBenchObserver

transport = HttpObservationTransport("http://127.0.0.1:8000")
run = transport.start_run(run_payload)
observer = VoxBenchObserver(run["run_id"], transport)

# Call at the boundary around an AGC, resampler, limiter, or provider adapter.
observer.observe_stage_audio(
    stage="agc",
    input_pcm_s16le=input_pcm,
    output_pcm_s16le=output_pcm,
    sample_rate_hz=16000,
    gain_applied=current_gain,
)

# Flush outside the audio callback in an async application.
await asyncio.to_thread(observer.flush)
await asyncio.to_thread(transport.complete_run, run["run_id"])
```

On failure, flush any final observations and store only a diagnostic alias:

```python
await asyncio.to_thread(observer.flush)
await asyncio.to_thread(
    transport.fail_run,
    run["run_id"],
    "provider-session-error",
)
```

`observe_stage_audio` emits `input_rms`, `output_rms`, and `delta_db` on every
call. It emits `gain_applied` when supplied and records the output PCM as that
stage's WAV tap. Use `record_output=False` for high-frequency measurements where
only metrics are needed.

The batch endpoint also accepts aliased SIP events and aggregate RTP statistics.
Its schema does not accept raw SIP bodies, SDP, packet payloads, external URLs,
or secret values.

## RTP Packet Tap Adapter

`RtpPacketTapAdapter` is the reusable boundary for an application that already
receives RTP datagrams. It synchronously decodes only the RTP fixed header and
queues safe observations in memory; it does not perform HTTP or retain the media
payload or SSRC.

```python
from voxbench.observability import RtpPacketTapAdapter

tap = RtpPacketTapAdapter(
    observer,
    stream_alias="caller-audio-a",
    direction="received",
    clock_rate_hz=8000,
    clock_domain="control_plane_wall",
    alignment_uncertainty_ms=2.0,
    capture_drop_counter_supported=True,
)

# Call from the application's RTP receive boundary. The datagram payload is
# transient and is not present in the returned packet observation.
tap.observe_datagram(datagram, ts=received_at)

# Increment this when the owning bounded queue, socket telemetry, or packet
# capture library reports a drop. The adapter cannot infer kernel drops itself.
if media_queue_overflowed:
    tap.record_capture_drop()

# Emit one health window before the observer batch is flushed.
health = tap.report_health(ts=health_reported_at)
```

Flush from a non-media worker. Keep each batch below 128 timeline events; at
normal 20 ms RTP cadence, a 250-1000 ms interval is safely bounded. A decode
failure raises `ValueError`, increments the health window's decode-error counter,
and never queues malformed packet data.

Set `capture_drop_counter_supported=True` only when the integration can account
for its receive/capture path. Sources may include an application queue overflow
counter, libpcap capture statistics, or platform socket overflow telemetry. Feed
the reported delta to `record_capture_drop(...)`. Zero reported drops then marks
that window `verified`; any drop or decode error marks it `compromised`. When no
counter exists, leave the default `False`, which remains
`not independently verified`.

VoxBench changes the diagnostic claim accordingly:

| Capture health | Sequence-gap result |
|---|---|
| Verified, zero drop/decode errors | High-confidence absence at the observation point |
| Compromised | Low-confidence `may be capture loss`; do not attribute it to the network |
| Counter unavailable | High-confidence observed absence, but capture continuity remains unverified |

Even a verified capture window does not identify which network segment lost a
packet. Tap placement must be known before path attribution. Rotate
`stream_alias` when the owning integration detects an SSRC/source lifetime
change; use a safe alias and do not send the SSRC itself.

Use `clock_domain` to name the timestamp source and
`alignment_uncertainty_ms` to report the measured or conservative alignment
bound. Packet pairs are compared only inside the same stream, direction, and
clock domain. The uncertainty is preserved on derived evidence and Incidents.

## Direct Provider Applications

For an application that talks directly to OpenAI Realtime or Gemini Live, place
observer calls at these boundaries:

- telephony input after codec decode
- after resampling to the provider input rate
- before and after AGC/limiter
- provider output before telephony encoding

The provider session stays owned by the application. VoxBench receives PCM and
metrics only, so provider API keys never enter an observation payload.

## Pipecat Applications

For Pipecat, keep the existing pipeline and add a thin FrameProcessor or callback
beside the processor being tuned. Read the PCM bytes from the incoming and outgoing
audio frames, call `observe_stage_audio`, and push the original frame onward.
Run `observer.flush()` from a periodic task or executor rather than inside
`process_frame`.

This boundary is intentionally independent of Pipecat frame class versions. A
project can adapt its installed Pipecat version without changing VoxBench's core
package. VoxBench's existing `engine_harness.pipecat_adapter` remains the optional
boundary for projects that want VoxBench to construct the Pipecat pipeline.

## Local Example

Start the complete local stack first:

```bash
./scripts/dev-demo
```

The launcher prints the actual API URL. In another terminal with `.venv` active,
use that URL below. For example, if it prints `http://127.0.0.1:8002`:

```bash
python examples/integrations/observe_direct_pipeline.py \
  --base-url http://127.0.0.1:8002 \
  --provider openai-realtime \
  --rtp-scenario verified-gap
```

Switch to `--provider gemini-live` to create the matching Gemini-shaped run. The
example does not open a provider connection or require an API key; it demonstrates
the exact observation path an existing provider loop uses.

Run all three RTP conditions and open each printed `run_id` from **Recent runs**:

```bash
python examples/integrations/observe_direct_pipeline.py \
  --base-url http://127.0.0.1:8002 --rtp-scenario clean
python examples/integrations/observe_direct_pipeline.py \
  --base-url http://127.0.0.1:8002 --rtp-scenario verified-gap
python examples/integrations/observe_direct_pipeline.py \
  --base-url http://127.0.0.1:8002 --rtp-scenario capture-drop
```

Expected results:

- `clean`: no sequence-gap or arrival-stall Incident.
- `verified-gap`: `RTP sequence gap observed`, confidence `high`, capture
  continuity `verified`.
- `capture-drop`: `RTP sequence gap may be capture loss`, confidence `low`,
  capture continuity `compromised`, and the capture-health event in the evidence
  chain.

For a real integration, repeat this check with one actual call and verify:

1. Received and sent media use separate safe stream aliases.
2. The alias rotates when the owning RTP source/SSRC lifetime changes.
3. Queue, socket, or capture-library drop deltas reach `record_capture_drop`.
4. Health reports appear every 250-1000 ms and use the same clock domain as
   their packets.
5. Invalid/non-RTP datagrams increase decode errors without exposing their bytes.
6. Selecting an Incident moves the common cursor to both the derived gap and its
   capture-health evidence.
7. Audio playback and packet evidence refer to the expected direction and call
   phase.

The Control Plane endpoints used by the library are:

- `POST /runs/observed`
- `POST /v1/observations`
- `POST /runs/{run_id}/complete`
- `POST /runs/{run_id}/fail`

During the run, the Web UI can fetch the normal run timeline and stage recording
audio endpoints. Completing the run computes the existing VoxBench verifications.
Failure aliases must not contain URLs, raw provider responses, SIP/SDP, or secrets.
