# Live Softphone Realtime Demo

This demo is staged as small vertical slices while real SIP/RTP media is wired:

```text
macOS softphone
  -> local Asterisk/PJSIP account
  -> VoxBench live demo bridge
  -> Gemini Live API or OpenAI Realtime API provider adapter
  -> VoxBench timeline, stage recordings, gain metrics, compare UI
```

The implemented paths are:

- simulated audio, SIP, and RTP points for UI development
- an observed Asterisk AudioSocket PCM loopback that accepts a real softphone
  call, applies AGC and limiting, records all stage taps, and returns audio
- provider-backed AudioSocket sessions for OpenAI Realtime and Gemini Live,
  including stateful PCM resampling and paced 20 ms playback
- a reusable observer API for applications that already own their provider or
  Pipecat pipeline

The AudioSocket bridge has both a local loopback mode and a provider-backed mode.
The latter uses the same observed stages while routing caller audio to OpenAI
Realtime or Gemini Live.

The same observation path is available to applications that already own their
provider or Pipecat pipeline. See [library-integration.md](library-integration.md).

## Current Provider Notes

- OpenAI Realtime: official docs describe stateful Realtime sessions on
  `/v1/realtime`, WebSocket client/server events, `session.update`, audio input
  buffers, and audio formats such as `audio/pcm` at 24000 Hz and `audio/pcmu`.
  See `https://developers.openai.com/api/docs/guides/realtime`,
  `https://developers.openai.com/api/docs/guides/realtime-websocket`, and
  `https://developers.openai.com/api/docs/guides/realtime-conversations`.
- Gemini Live: the Google Gen AI Python SDK exposes
  `client.aio.live.connect(...)`, `session.send_realtime_input(...)`, and
  `session.receive()`. The docs show realtime PCM input with
  `audio/pcm;rate=16000` and the `gemini-3.1-flash-live-preview` model for the
  Gemini Developer API.

## Prerequisites

- Python 3.12 and the VoxBench development install.
- Web UI dependencies under `web/`.
- For a real provider connection:
  - Install the optional dependencies with `pip install -e ".[live]"`.
  - OpenAI: `OPENAI_API_KEY`
  - Gemini: `GOOGLE_API_KEY` or `GEMINI_API_KEY`
- For real softphone media:
  - Asterisk 18 or newer with PJSIP, `app_audiosocket`, and `func_uuid`.
  - A macOS SIP softphone with a local account.
  - A local codec decision. Use 8 kHz PCM16 or G.711 mu-law at the telephony
    boundary, then resample to provider requirements: OpenAI Realtime commonly
    uses 24 kHz PCM input, while Gemini Live examples use 16 kHz PCM input.

Do not put API key values, raw SIP bodies, SDP, packet bodies, or external service
URLs into run payloads. Store aliases such as `env:OPENAI_API_KEY` or
`alias:local-asterisk-media-websocket`.

## Demo Configs

- `examples/configs/live-demo-openai-realtime.json`
- `examples/configs/live-demo-gemini-live.json`
- `examples/manifests/provider/openai-realtime.json`
- `examples/manifests/provider/gemini-live.json`

Both configs keep the same processor pipeline:

```text
resampler -> agc -> limiter -> serializer
```

The `agc` params are the demo knobs:

- `target_rms`
- `max_gain`
- `noise_floor`

## Run The Simulated Live Demo

Start the API:

```bash
uvicorn voxbench.control_plane.app:app --reload
```

For the complete Postgres/API/Web walkthrough, run `./scripts/dev-demo` from the
repository root instead. It creates an `rtp-gap` run and opens the selected run
automatically.

Create a Gemini Live-shaped simulated run:

```bash
curl -X POST http://127.0.0.1:8000/runs/live-demo/simulated \
  -H 'content-type: application/json' \
  -d '{
    "provider": "gemini-live",
    "scenario": "rtp-gap",
    "call_id": "local-softphone-simulated",
    "input_rms": 1000,
    "target_rms": 4000,
    "max_gain": 3.0,
    "noise_floor": 100
  }'
```

Create an OpenAI Realtime-shaped simulated run:

```bash
curl -X POST http://127.0.0.1:8000/runs/live-demo/simulated \
  -H 'content-type: application/json' \
  -d '{
    "provider": "openai-realtime",
    "call_id": "local-softphone-simulated",
    "input_rms": 2600,
    "target_rms": 3000,
    "max_gain": 8.0,
    "noise_floor": 200
  }'
```

The response contains a `run_id`. Open the Web UI, select the run, and inspect:

- SIP ladder lane
- RTP quality lane
- the high-confidence observation-point sequence gap Incident
- the medium-confidence arrival-stall Incident
- stage recordings and waveform
- `input_rms`, `output_rms`, `delta_db`, and `gain_applied`
- AGC params in the payload/config
- run compare by creating a second run with different AGC values

## Run The Web UI

```bash
cd web
npm install
npm run dev -- --port 5173
```

Open `http://127.0.0.1:5173/`.

The existing Async run panel can also run the demo configs manually. Load or paste
one of the live-demo configs and the matching provider manifest, then adjust the
AGC controls. The controls update the payload stage params before the next run.

## Provider-Connected Path

The AudioSocket loopback and realtime bridge share the local PJSIP endpoint,
extension routing, run lifecycle, and PCM observation path. Provider mode:

1. Convert telephony audio at the provider boundary:
   - 8 kHz G.711 mu-law or 8 kHz PCM16 from softphone/Asterisk.
   - PCM16 resampled to the provider input rate.
   - Provider response audio converted back to the telephony codec.
2. Opens the selected provider session and streams audio bidirectionally.
3. Emits SIP aliases, provider lifecycle events, stage metrics, and stage PCM
   through batched `POST /v1/observations` requests. Aggregate RTP statistics can
   also be supplied through that batch contract or `POST /v1/rtp-stats`.
4. Keep raw SIP bodies, SDP, packet payloads, and secret values out of stored run
   data.

Raw pcap/Wireshark import remains outside this slice.

## Run A Real Softphone Loopback

Asterisk's `AudioSocket()` application sends and receives signed 16-bit PCM over
a small TCP framing protocol. Asterisk 18+ supports 8 kHz mono PCM; current
versions also define AudioSocket frame types for higher PCM rates. The official
references are:

- `https://docs.asterisk.org/Configuration/Channel-Drivers/AudioSocket/`
- `https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Applications/AudioSocket/`

The fastest supported local setup is the repository's Asterisk 20 container:

```bash
./scripts/asterisk-local up
```

It waits until the PJSIP endpoint and AudioSocket module are ready, then prints
all Telephone and optional AMI values. SIP, RTP, and AMI are published on macOS
loopback only. Configure the macOS Telephone app as:

- account/description: `VoxBench`
- full name/display: `VoxBench 6001`
- domain/SIP server: `127.0.0.1`
- transport: UDP
- port: `5060`
- username and authorization user: `6001`
- password: `voxbench-6001-local-only`
- outbound proxy: blank
- STUN: off
- preferred codec: PCMU / G.711 mu-law

The password is a development default protected only by the loopback port
binding. Override it before startup with `VOXBENCH_SIP_PASSWORD` if required.
The original native-install snippets remain available under
`examples/asterisk/`; do not copy the Docker dialplan unchanged because its
AudioSocket target is `host.docker.internal`.

Start the Control Plane:

```bash
uvicorn voxbench.control_plane.app:app --reload
```

Start the observed AudioSocket bridge in another terminal:

```bash
voxbench audiosocket-loopback \
  --control-plane-url http://127.0.0.1:8001 \
  --provider openai-realtime \
  --target-rms 3000 \
  --max-gain 8 \
  --noise-floor 200
```

This is deliberately an echo/processing loopback. The provider name is stored
for comparison metadata, but `audiosocket-loopback` never opens a provider
network session. For a real Gemini conversation:

```fish
set -gx GOOGLE_API_KEY 'your-key'
./scripts/asterisk-local gemini
```

The helper runs `audiosocket-realtime --provider gemini-live`, verifies the
local prerequisites, performs a no-audio credential/model preflight, and keeps
the API key in the process environment only. The default model is
`gemini-3.1-flash-live-preview`; override it with `--model` only when validating
a deliberately pinned alternative.

Use the actual Control Plane URL printed by `./scripts/dev-demo` if it is not
port `8001`. The bridge listens on `127.0.0.1:9019`; the supplied container
already targets it as `host.docker.internal:9019`. Keep SIP and AudioSocket
listeners local during the demo; neither listener includes production TLS
hardening.

Call `7000`. The caller should hear the processed version of their own voice.
While the call is active, the run remains visible in the Web UI with these stage
taps:

```text
resampler -> agc -> limiter -> serializer
```

The bridge updates `input_rms`, `output_rms`, `delta_db`, and `gain_applied` and
flushes PCM observations away from the audio write path. The AudioSocket UUID is
used as the aliased call ID. Raw SIP messages and SDP are not received or stored
by the bridge.

To compare AGC settings, end the call, restart the bridge with different gain
arguments, place another call, and select both runs in the Web UI compare view.

Use these diagnostics if Telephone does not show the account as available:

```bash
./scripts/asterisk-local status
./scripts/asterisk-local logs
```

`status` must show container health `healthy`, endpoint `6001`, the AudioSocket
modules as `Running`, and extension `7000`. In Telephone, disable and re-enable
the account after an Asterisk rebuild to force immediate registration.

## Collect Asterisk RTCP Quality

Asterisk emits aggregate `RTCPReceived` and `RTCPSent` events through AMI. The
collector reads only those events from a reporting-only account and sends the
following normalized values to the active run:

- `direction`: `received` or `sent`
- `loss_pct`: the largest report-block fixed-fraction loss converted to percent
- `jitter_ms`: the largest report-block interarrival jitter converted from RTP
  timestamp units with the configured codec clock rate
- `rtt_ms`: received-event RTT converted from seconds to milliseconds

It does not map Asterisk MES to MOS. It also does not forward Channel, caller ID,
source/destination address, SSRC, raw SIP, SDP, or packet bodies.

Install `examples/asterisk/manager.conf.example` as part of the local Asterisk
configuration and replace its secret only in the installed copy. The example
binds AMI to loopback, grants only the `reporting` read class, grants no write
permissions, and filters the account to the two RTCP event names. Production use
requires its own network isolation, authentication, and TLS review.

Set the account values in environment variables. Do not put them on the command
line or in a run payload:

```bash
export VOXBENCH_AMI_USERNAME=voxbench-rtcp
export VOXBENCH_AMI_SECRET='REPLACE_WITH_LOCAL_SECRET'
```

The recommended local workflow attaches the collector automatically when each
AudioSocket run is created:

```fish
./scripts/asterisk-local gemini \
  --collect-rtcp \
  --experiment-condition no-interruption
```

The helper supplies the development AMI credentials through environment
variables. The bridge starts one collector after provider connection succeeds,
binds it to the new run ID, and cancels it before that run is completed. Collector
failure is reported as operational evidence but does not fail an otherwise valid
voice call.

To invoke the bridge directly, set `VOXBENCH_AMI_USERNAME` and
`VOXBENCH_AMI_SECRET`, then pass `--collect-rtcp`, `--ami-host`, `--ami-port`, and
`--ami-clock-rate-hz`. The clock rate is the RTP codec clock, not necessarily an
audio output sample rate. Use 8000 for PCMU/PCMA and configure the actual clock
for other codecs.

The standalone `voxbench asterisk-ami-rtcp --run-id ...` command remains
available for integrations that manage run and collector lifecycles separately.

The Web RTP quality panel shows direction, jitter, loss, RTT, MOS when separately
supplied, and each point's relative time. Live preview also shows an RTP collector
block: `connected` after AMI login, `collecting` after the first normalized RTCP
event, and `failed` after a safe collector failure observation. Its event count
is additive across collector restarts for the same run.

Keep only one call active while automatic collection is enabled. The privacy-safe
collector intentionally drops Asterisk Channel, address, and SSRC fields, so AMI
events from concurrent calls cannot be attributed safely.

## Matched Choppy-Audio Experiment

Use identical provider model, codec, route, gain, noise floor, caller phrase, and
call duration for both conditions.

1. Use a headset to prevent speaker audio from re-entering the microphone.
2. Start `no-interruption` with automatic RTCP collection, call `7000`, speak one
   fixed prompt, and remain silent until Gemini finishes.
3. Keep the call active for 20–30 seconds, hang up, and retain the printed run ID.
4. Restart the bridge with `--experiment-condition intentional-barge-in`.
5. Repeat the same prompt, but interrupt Gemini once at the same relative phrase.
6. Retain an independent caller-side recording for each call and name it with the
   corresponding run ID.
7. Select the no-interruption run as **Primary** and the interruption run as
   **Compare**.

Confirm all of the following before attributing choppy audio:

- **RTP quality** contains received or sent points rather than remaining empty.
- The experiment condition is present in the run environment tags/operator note.
- The same provider model appears on both runs.
- Stage WAVs identify the first local stage containing the audible defect.
- Barge-in evidence explains any queued audio disposal.
- The caller-side recording confirms that the candidate instant was actually
  audible after Asterisk RTP and softphone playout.

Relevant Asterisk references:

- `https://docs.asterisk.org/Asterisk_22_Documentation/API_Documentation/AMI_Events/RTCPReceived/`
- `https://docs.asterisk.org/Asterisk_21_Documentation/API_Documentation/AMI_Events/RTCPSent/`
- `https://docs.asterisk.org/Configuration/Interfaces/Asterisk-Manager-Interface-AMI/AMI-Event-Filtering/`
- `https://docs.asterisk.org/Configuration/Interfaces/Asterisk-Manager-Interface-AMI/The-Asterisk-Manager-TCP-IP-API/`

## Run A Provider-Backed Call

Install the optional live dependencies and set exactly the provider key needed by
the selected run:

```bash
pip install -e ".[live]"
export OPENAI_API_KEY="..."
# or: export GOOGLE_API_KEY="..."
```

The values remain process environment variables. VoxBench stores only the env var
name in run metadata.

Start the realtime bridge instead of the loopback bridge:

```bash
voxbench audiosocket-realtime \
  --provider openai-realtime \
  --target-rms 3000 \
  --max-gain 8 \
  --noise-floor 200 \
  --connect-attempts 3 \
  --connect-backoff-seconds 0.5
```

For Gemini Live, use:

```bash
voxbench audiosocket-realtime --provider gemini-live
```

Pin the exact provider model when comparing model generations. The selected model
is stored in the resolved run config and included in the environment target alias,
so the Primary and Compare runs remain identifiable:

```bash
voxbench audiosocket-realtime \
  --provider gemini-live \
  --model gemini-3.1-flash-live-preview

# Run this separately after stopping the first bridge.
# Replace the value with the exact model ID used by the target AI phone.
voxbench audiosocket-realtime \
  --provider gemini-live \
  --model '<exact-3.1-model-id>'
```

The bridge performs these format transitions:

```text
Asterisk AudioSocket PCM16LE 8 kHz
  -> OpenAI Realtime PCM16LE 24 kHz input
  -> OpenAI Realtime PCM16LE 24 kHz output
  -> VoxBench resampler / AGC / limiter / serializer
  -> AudioSocket PCM16LE 8 kHz, paced as 20 ms frames
```

Gemini uses 16 kHz PCM input and 24 kHz PCM output. Both adapters rely on
provider-side VAD to create responses. OpenAI uses semantic VAD; Gemini Live's
realtime input path responds automatically based on its VAD.

The bridge normalizes provider speech/response lifecycle events. On caller speech
or a Gemini interruption event, it clears queued local playback and emits
`barge_in_events` and `output_frames_dropped`. While an OpenAI response is active,
OpenAI's server VAD cancels the response; the bridge avoids a duplicate cancel,
tracks the assistant item and paced AudioSocket playback position, and sends
`conversation.item.truncate` with the audio duration actually delivered to the
caller. It records `provider_auto_interrupts` and `provider_truncate_requests`.
Provider and bridge failures end the observed run with a non-secret
`failure_alias`, which appears in Live preview.

### Validate Provider Audio Before Barge-in

The realtime bridge keeps a bounded, metadata-only flight recorder for provider
audio near each normalized `input_speech_started` or `interrupted` event. It does
not persist provider packet bodies or raw provider item IDs. When queued audio is
cleared, the run records:

- provider audio chunk count during the preceding 30 ms and 100 ms;
- provider chunk ordinal, duration, RMS, silence percentage, and arrival lead;
- each discarded 20 ms AudioSocket frame, its source chunk range, queue depth,
  RMS, silence percentage, and whether it exceeded the configured noise floor;
- complete and partial queued audio duration;
- signal-bearing discarded duration;
- audio already written to AudioSocket before the control event; and
- an explicit `remote_playout_observed: false` boundary.

In the Web UI, select the **Barge-in packet evidence** incident in **Call
inspector**. The **Local packet proof** card summarizes the provider burst,
discarded signal, first-arrival lead, and audio written before the control event.
Put a second run ID in **Compare** to show the same measurements side-by-side. The
environment target row includes the exact provider model.

For an initial live check, make at least five matched calls per model. For a claim
intended to distinguish provider behavior, use at least 30 calls per model because
the meeting hypothesis concerns nondeterministic differences of only tens of
milliseconds. Keep the prompt, route, codec, ptime, bridge settings, caller
utterance, and interruption timing fixed. In each call:

1. let the assistant begin a scripted reply;
2. interrupt it at the same word or elapsed time;
3. end the call and copy the run ID printed by the bridge;
4. inspect the Barge-in incident; and
5. retain a caller-side recording if the conclusion must say what the caller
   actually heard.

The same evidence can be inspected without the Web UI:

```bash
RUN_ID='<run-id-printed-by-the-bridge>'
curl -s "http://127.0.0.1:8000/runs/$RUN_ID/timeline" |
  jq '.lanes.incidents[]
      | select(.rule_id == "barge_in_sequence")
      | {title, severity, summary, observed, expected, evidence_refs}'
```

Here “provider chunk” means an application-level audio chunk yielded by the
provider adapter. It is not an RTP datagram. RTP sequence, arrival cadence, and
capture-health evidence use the separate RTP observation path.

Before a provider session is established, the bridge retries initial connection
failures with bounded exponential backoff. The observed run is created first, so
`provider_connect_attempts`, `provider_connect_retries`,
`provider_connect_failures`, and `provider_connect_exhausted` remain visible. If
all attempts fail, the run ends with `provider-connect-error`; raw provider errors
are not stored. This policy does not reconnect an established mid-call session,
because doing so would reset provider conversation state and should not be hidden
from the operator.

Once connected, OpenAI and Gemini sessions are treated as persistent receive
streams. A clean EOF while the AudioSocket call is still active fails the run as
`provider-stream-ended`; a receive exception is reduced to the safe alias
`provider-session-error`. VoxBench records `provider_stream_ended` or
`provider_stream_errors` without persisting the raw provider exception, URL,
response body, or credential material. Finite dry-run/fake sessions remain valid
for local tests and embedding examples.

Live preview renders these metrics as a dedicated Provider connection block. Its
state is `pending` before a provider outcome is observed, `connected` after a
successful initial session, `exhausted` after all attempts fail, and `unobserved`
when a provider-mode run ends without connection telemetry. Connection metrics are
removed from the generic host metric tiles to keep the operational signal clear.

This remains a demo-grade bridge. The dependency-free linear resampler preserves
phase across streaming chunks, but production integrations should inject their
existing high-quality resampler. Stateful mid-call recovery, validation of the
truncation timing and AMI RTCP values against a real provider/Asterisk call, and
production auth/TLS are still hardening work. The automated suite exercises an
actual localhost AudioSocket TCP path with a fake provider; a real provider call
still requires local Asterisk, a softphone, network access, and the selected API
key.
