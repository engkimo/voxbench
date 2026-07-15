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
  `audio/pcm;rate=16000` and the `gemini-live-2.5-flash-preview` model for the
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

Create a Gemini Live-shaped simulated run:

```bash
curl -X POST http://127.0.0.1:8000/runs/live-demo/simulated \
  -H 'content-type: application/json' \
  -d '{
    "provider": "gemini-live",
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

Install the example configuration snippets into the local Asterisk configuration,
then replace `REPLACE_WITH_LOCAL_SECRET` only in the local copy:

- `examples/asterisk/pjsip.conf.example`
- `examples/asterisk/extensions.conf.example`

The example binds SIP to loopback only. Configure the macOS softphone as:

- SIP server: `127.0.0.1`
- transport: UDP
- port: `5060`
- username/auth ID: `6001`
- password: the local value used in `pjsip.conf`
- dial: `7000`

Start the Control Plane:

```bash
uvicorn voxbench.control_plane.app:app --reload
```

Start the observed AudioSocket bridge in another terminal:

```bash
voxbench audiosocket-loopback \
  --provider openai-realtime \
  --target-rms 3000 \
  --max-gain 8 \
  --noise-floor 200
```

This command listens on `127.0.0.1:9019`. For Asterisk in a container, expose
the bridge appropriately and set `VOXBENCH_AUDIOSOCKET` in the dialplan to
`host.docker.internal:9019`. Keep SIP and AudioSocket listeners local during the
demo; neither listener includes production authentication or TLS hardening.

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
  --noise-floor 200
```

For Gemini Live, use:

```bash
voxbench audiosocket-realtime --provider gemini-live
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

This remains a demo-grade bridge. The dependency-free linear resampler preserves
phase across streaming chunks, but production integrations should inject their
existing high-quality resampler. Reconnection, validation of the truncation timing
against a real provider call, real RTP quality collection, and production auth/TLS
are still hardening work. The automated suite exercises an actual localhost
AudioSocket TCP path with a fake provider; a real provider call still requires
local Asterisk, a softphone, network access, and the selected API key.
