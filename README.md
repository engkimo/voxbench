# VoxBench

VoxBench is an early OSS implementation of the schema and registry foundation
described in `DESIGN.md`.

Implemented so far:

- config and capability manifest JSON Schemas
- SQLAlchemy models and an Alembic initial migration for `plugins` and `configs`
- overlay resolution, deterministic resolved-config hashing, and static manifest validation
- example manifests/configs and acceptance tests
- a Phase 1 `POST /runs` vertical slice that issues a `run_id`, passes a resolved
  config to the engine harness boundary, writes per-stage WAV tap artifacts, and
  stores OpenTelemetry spans with `voxbench.run_id`
- Phase 2 verification results, synthetic caller artifacts, cadence metrics, and
  lossy-expected handling
- Phase 3 Web timeline with recent runs, two-run compare, stage detail,
  recording playback, waveform display, A/B playback coordination, and metric deltas
- Phase 4 pre-live run environment metadata, readiness checklist, host metrics,
  live preview, WebSocket `/live`, background async runs, and an async run UI
- Live softphone demo scaffolding with OpenAI Realtime/Gemini Live provider
  boundaries, demo configs, and a simulated audio bridge that emits stage gain
  metrics plus structured SIP/RTP timeline points
- An Asterisk AudioSocket PCM loopback CLI for placing a real local softphone
  call through observed AGC/limiter stages
- A provider-agnostic `voxbench.observability` library boundary for existing
  direct-provider, Pipecat, and custom telephony applications

This implementation intentionally does not include real SIP/RTP/live-host integration,
cross-session leak trend analysis, persistent production job leasing, or the scale profile.

## Install for development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Resolve and validate a config

The CLI resolves optional parent overlays, validates referenced plugin manifests,
prints the resolved config, and prints the deterministic SHA-256 hash.

```bash
voxbench resolve-config \
  --config examples/configs/valid-baseline.json \
  --manifest examples/manifests/engine/asterisk.json \
  --manifest examples/manifests/provider/gemini.json \
  --manifest examples/manifests/processor/resampler.json \
  --manifest examples/manifests/processor/agc.json \
  --manifest examples/manifests/processor/limiter.json \
  --manifest examples/manifests/processor/serializer.json
```

The same behavior is available as a Python API:

```python
from voxbench.registry.service import RegistryService

service = RegistryService.from_files(
    config_paths=["examples/configs/valid-baseline.json"],
    manifest_paths=[
        "examples/manifests/engine/asterisk.json",
        "examples/manifests/provider/gemini.json",
        "examples/manifests/processor/resampler.json",
        "examples/manifests/processor/agc.json",
        "examples/manifests/processor/limiter.json",
        "examples/manifests/processor/serializer.json",
    ],
)
resolved = service.resolve_config("baseline")
print(resolved.hash)
```

## Run the control-plane API

Start the control-plane API:

```bash
uvicorn voxbench.control_plane.app:app --reload
```

Post a single run with the example config and manifests:

```bash
python - <<'PY'
import json
from pathlib import Path

import httpx

root = Path(".")
manifest_paths = [
    "examples/manifests/engine/asterisk.json",
    "examples/manifests/provider/gemini.json",
    "examples/manifests/processor/resampler.json",
    "examples/manifests/processor/agc.json",
    "examples/manifests/processor/limiter.json",
    "examples/manifests/processor/serializer.json",
]
payload = {
    "config_name": "baseline",
    "configs": [json.loads((root / "examples/configs/valid-baseline.json").read_text())],
    "manifests": [json.loads((root / path).read_text()) for path in manifest_paths],
    "call_id": "sip-call-id-example",
}
response = httpx.post("http://127.0.0.1:8000/runs", json=payload, timeout=10)
response.raise_for_status()
print(json.dumps(response.json(), indent=2))
PY
```

The response includes `run_id`, `conversation_id`, recording artifact URIs, and spans.
Local development stores WAV tap artifacts under `artifacts/recordings/`.

## Run the Web UI

```bash
cd web
npm install
npm run dev -- --port 5173
```

Open `http://127.0.0.1:5173/`. The Web UI can inspect a run timeline, compare two
runs, play stage recordings, watch live run status, and start an async run from an
example payload.

## Phase 4 pre-live workflow

Phase 4 pre-live work is focused on demo/integration readiness before real
live-host/SIP/RTP wiring. The run request accepts an `environment` object and a
`readiness_checklist`. Store only aliases and references in these fields; do not
store personal names, Slack IDs, external URLs, or secret values.

Generate a safe example payload:

```bash
curl 'http://127.0.0.1:8000/runs/example-payload?environment_profile=demo'
```

Start it as a background run:

```bash
curl -X POST http://127.0.0.1:8000/runs/async \
  -H 'content-type: application/json' \
  --data @payload.json
```

Inspect current run status:

```bash
curl http://127.0.0.1:8000/runs/live-preview
```

The live preview includes run status, environment profile, server/target aliases,
readiness summary, manual blockers, tags, and latest host metrics. `WS /live`
streams the same projection as repeated snapshots. Host metrics currently include
`cpu`, `active_tasks`, and `loop_lag` sampled by the harness during the synthetic run.

SIP/RTP integration can start with structured ingest endpoints before wiring a real
collector. These endpoints attach data to an existing run and intentionally avoid raw
packet bodies, SDP, external URLs, and secret values:

```bash
curl -X POST http://127.0.0.1:8000/v1/sip-events \
  -H 'content-type: application/json' \
  -d '{"run_id":"<run_id>","method":"INVITE","direction":"in","summary_alias":"invite-received"}'

curl -X POST http://127.0.0.1:8000/v1/rtp-stats \
  -H 'content-type: application/json' \
  -d '{"run_id":"<run_id>","jitter_ms":3.5,"loss_pct":0.2,"mos":4.1}'
```

`GET /runs/{run_id}/timeline` includes these points in `lanes.sip_ladder` and
`lanes.rtp_quality`.

The Web UI provides an `Async run` panel:

- `Load example` fetches the server-side example payload.
- Environment controls update profile, server alias, target alias, tags, manual
  blockers, and secret reference names.
- AGC controls update the `agc` stage `target_rms`, `max_gain`, and `noise_floor`
  params in the payload.
- Readiness controls update standard checklist statuses.
- The JSON textarea remains available for configs/manifests and advanced edits.

## Live softphone realtime demo scaffold

See [docs/demo-live-softphone.md](docs/demo-live-softphone.md) for the demo
architecture, prerequisites, provider env vars, softphone/Asterisk target shape,
and current limitations.

The simulated path creates a normal VoxBench run, writes non-silent stage WAV
taps, and emits SIP/RTP/gain timeline data without Asterisk or an API key:

```bash
curl -X POST http://127.0.0.1:8000/runs/live-demo/simulated \
  -H 'content-type: application/json' \
  -d '{"provider":"gemini-live","input_rms":1000,"target_rms":4000,"max_gain":3.0,"noise_floor":100}'
```

Switch `"provider"` to `"openai-realtime"` to use the OpenAI Realtime demo
config. API key values are never stored; provider configs use env var aliases
such as `env:OPENAI_API_KEY` and `env:GOOGLE_API_KEY`.

Existing applications can start an observed run and batch PCM stage taps,
gain metrics, SIP events, and RTP statistics through the public Python API. See
[docs/library-integration.md](docs/library-integration.md) for direct-provider and
Pipecat integration patterns, or run the local example:

```bash
python examples/integrations/observe_direct_pipeline.py --provider openai-realtime
```

For a real macOS softphone audio loopback through Asterisk, install the local-only
config snippets under `examples/asterisk/`, then run:

```bash
voxbench audiosocket-loopback --provider openai-realtime
```

Calling extension `7000` from the configured `6001` account sends PCM through
the VoxBench observer, AGC, limiter, stage WAV taps, and back to the softphone.
To replace loopback with a real provider session, install `.[live]`, set
`OPENAI_API_KEY` or `GOOGLE_API_KEY`, and run:

```bash
voxbench audiosocket-realtime --provider openai-realtime
```

Switch the provider argument to `gemini-live` for Gemini. API key values remain
in environment variables and are not stored in run payloads or artifacts. The
realtime bridge uses stateful streaming resampling, paced 20 ms AudioSocket
output, local playback clearing on barge-in, and failed-run aliases visible in
Live preview. OpenAI server VAD cancels an interrupted response and the bridge
truncates the unplayed assistant audio at the caller's playback position. Initial
provider connection is retried three times by default; use `--connect-attempts`
and `--connect-backoff-seconds` to tune it. Mid-call disconnects fail the run
as `provider-stream-ended` or `provider-session-error` instead of silently
resetting conversation state or completing the run. Live preview projects the
connection as `pending`, `connected`, `exhausted`, or `unobserved` and shows its
attempt/retry/failure counts. A real Asterisk/provider call is environment
validation and is not performed by the automated suite.

## Verification

```bash
ruff check .
pytest
```
