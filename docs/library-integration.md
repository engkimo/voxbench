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

With the Control Plane and Web UI running, execute:

```bash
python examples/integrations/observe_direct_pipeline.py \
  --provider openai-realtime
```

Switch to `--provider gemini-live` to create the matching Gemini-shaped run. The
example does not open a provider connection or require an API key; it demonstrates
the exact observation path an existing provider loop uses.

The Control Plane endpoints used by the library are:

- `POST /runs/observed`
- `POST /v1/observations`
- `POST /runs/{run_id}/complete`
- `POST /runs/{run_id}/fail`

During the run, the Web UI can fetch the normal run timeline and stage recording
audio endpoints. Completing the run computes the existing VoxBench verifications.
Failure aliases must not contain URLs, raw provider responses, SIP/SDP, or secrets.
