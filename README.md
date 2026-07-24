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
- A read-only Asterisk AMI RTCP collector that normalizes aggregate jitter,
  packet loss, RTT, and media direction without storing channel/address/SSRC data
- A provider-agnostic `voxbench.observability` library boundary for existing
  direct-provider, Pipecat, and custom telephony applications
- An opt-in SQLAlchemy/Postgres run repository that restores completed and
  observed runs, normalized telemetry, verification results, and recording metadata
- A Postgres-backed persistent async queue with leases, heartbeat, fenced result
  commits, supervised polling, restart recovery, and process-local safe telemetry

This implementation intentionally does not include SIP packet capture, production
live-host hardening, production-validated multi-process worker deployment, or the
scale profile.

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

The run repository defaults to process-local memory. To persist run state in
Postgres, install the optional psycopg driver, apply migrations, and select the
repository before starting the API:

```bash
python -m pip install -e ".[postgres]"
export VOXBENCH_RUN_REPOSITORY=postgres
export VOXBENCH_DATABASE_URL='postgresql+psycopg://voxbench:<password>@db.internal/voxbench'
export VOXBENCH_POSTGRES_PROBE=true                 # optional; default: false
export VOXBENCH_POSTGRES_PROBE_TIMEOUT_MS=2000      # optional; 10..10000
export VOXBENCH_POSTGRES_STATEMENT_TIMEOUT_MS=5000  # optional; 100..30000
alembic upgrade head
uvicorn voxbench.control_plane.app:app
```

`VOXBENCH_DATABASE_URL` is read only from the process environment and must use
the explicit `postgresql+psycopg` dialect. By default, API startup constructs the
engine but does not perform an implicit connection or migration.
`GET /repository/readiness` therefore reports Postgres as `configured` with
`connectivity-and-migrations-not-checked`; it does not claim `ready`. The URL,
host, username, and password are excluded from readiness and runtime
representations. Alembic reads the same environment variable, so credentials do
not need to be written to `alembic.ini`.

The Postgres readiness probe is disabled by default. When explicitly enabled, it
runs once during startup in a daemon worker, waits at most the configured 10–10,000
ms, executes fixed `SELECT 1` and Alembic-version queries, and reports `ready`
only when the database contains exactly migration head `0009_timeline_events`.
A connection/query failure, migration mismatch, or timeout reports `unavailable`
with a fixed safe reason alias; startup does not echo or persist the underlying
driver error. A timed-out driver call may continue in its daemon worker until the
driver or operating system returns, so deployments should also set a bounded
psycopg `connect_timeout` in the database URL.

Production-created psycopg connections also receive a per-session PostgreSQL
`statement_timeout` through libpq connection options. The default is 5,000 ms and
the accepted range is 100–30,000 ms, preventing a stuck query or row lock from
holding a worker indefinitely. An injected test/custom engine factory is
responsible for applying an equivalent timeout itself. `connect_timeout` remains
a separate libpq connection parameter and should also be bounded in the URL.

If a repository operation fails after startup, the API returns a fixed
`503 {"detail":"run repository is unavailable"}` with `Retry-After: 1`.
SQL statements, driver messages, connection details, and query parameters are
not returned to the caller. This mapping does not retry mutations; callers must
decide whether an operation is safe to retry.

Each repository save is one SQLAlchemy transaction. The current MVP replaces a
run's normalized child rows atomically and records their ordinal positions so a
process restart reconstructs recording, span, metric, verification, SIP, RTP, and
typed event ordering deterministically. Apply migrations through `0009_timeline_events` before
enabling Postgres. The default `memory` mode remains compatible with existing
local development and tests.

Postgres mode now also provisions a persistent `run_jobs` lease queue capability.
Its state machine provides idempotent enqueue by run, `FOR UPDATE SKIP LOCKED`
claiming, bounded 5–300 second leases, opaque lease tokens, heartbeat extension,
delayed retry, and an attempt limit. Expired leases can be reclaimed with a new
token, so stale workers cannot heartbeat or finalize a job through the queue.
`GET /repository/readiness` exposes the configured statement timeout and only
process-local safe worker telemetry: enabled/running booleans plus processed,
error, and lease-loss counters. It does not expose worker aliases, job IDs, lease
tokens, database identities, or exception messages; counters reset on restart.

`PostgresRunRepository.commit_leased_result(...)` locks the matching job, verifies
the run/job/worker/opaque-token/unexpired-lease tuple, and commits the normalized
run result plus terminal job state in one transaction. A stale or expired worker
therefore cannot overwrite the stored result, and a database failure cannot
commit only one side of that transition.

In Postgres mode, `/runs/async` now stores the initial run and its queued job in
one transaction, then returns 202. FastAPI lifespan starts one supervised polling
thread per application process and signals/stops it during shutdown. The worker
claims one job, loads its run, maintains a periodic lease heartbeat during harness
execution, schedules a bounded retry, and uses the fenced repository commit for
success or final failure. Heartbeat rejection or a heartbeat database error marks
the lease lost and discards the local result. The worker result projection never
includes the opaque lease token.

Worker lease duration is bounded to 5–300 seconds, heartbeat cadence to at least
1 second and at most half the lease, and retry delay to 0–3,600 seconds. A missing
run terminally fails its orphan job with the fixed `run-not-found` alias. The
supervisor uses bounded idle/error waits and a bounded shutdown join; queued jobs
and expired leases are naturally recovered by the next process through claim.
Memory mode keeps the existing process-local daemon behavior for local/test
compatibility.

The queue and fencing contracts are designed for multiple Postgres application
processes, but production rollout should still validate real-Postgres concurrent
claim behavior, shutdown telemetry, and deployment migrations before increasing
worker count. The opt-in integration tests create and drop a unique schema in a
disposable test database, apply the complete Alembic history through the expected
head, and directly verify epoch-nanosecond span persistence, locked-row skipping,
and stale-lease fencing:

```bash
export VOXBENCH_TEST_POSTGRES_URL='postgresql+psycopg://user:<password>@localhost/testdb'
pytest -q -m postgres_integration tests/test_postgres_integration.py
```

The configured test role must be allowed to create and drop schemas. Never point
this variable at a production database. Without the variable, these tests are
collected and explicitly skipped; the normal SQLite and SQL compilation coverage
continues to run.

The engine harness also exposes `MinioRecordingSink` for the official MinIO
Python client. Install `.[storage]` and provision the bucket separately. Stage WAVs are uploaded with
`content_type="audio/wav"`; returned artifacts use only
`s3://<bucket>/<prefix>/<run>/<stage>.wav`. Endpoint and credentials never enter
the artifact URI. Bucket, prefix, run, and stage values are validated as safe
object-key components.

The default API stays local. To select MinIO at process startup, set these
deployment environment variables before starting Uvicorn:

```bash
export VOXBENCH_RECORDING_SINK=minio
export VOXBENCH_MINIO_ENDPOINT=minio.internal:9000
export VOXBENCH_MINIO_ACCESS_KEY='<access-key>'
export VOXBENCH_MINIO_SECRET_KEY='<secret-key>'
export VOXBENCH_MINIO_BUCKET=voxbench-recordings
export VOXBENCH_MINIO_PREFIX=recordings       # optional; default: recordings
export VOXBENCH_MINIO_SECURE=true             # optional; true or false
export VOXBENCH_MINIO_PROBE_BUCKET=false      # optional; default: false
export VOXBENCH_MINIO_PROBE_TIMEOUT_MS=2000   # optional; 10..10000
export VOXBENCH_MINIO_IO_TIMEOUT_MS=5000      # optional; 100..30000
export VOXBENCH_REMOTE_AUDIO_PROXY=false      # optional; default: false
uvicorn voxbench.control_plane.app:app --reload
```

These values are read only from the process environment; run request models
forbid unknown fields, so storage credentials cannot be supplied in a run
payload. `GET /storage/readiness` returns only the mode, safe bucket/prefix
aliases, TLS choice, and a fixed reason alias. MinIO state is `configured`, not
`ready` by default, because the default startup path intentionally performs no
network or bucket probe. Invalid configuration fails startup with a fixed safe
error alias rather than echoing a value. `create_app(recording_sink=...)` remains
available for deployment/test injection and reports only an opaque `injected`
mode.

Set `VOXBENCH_MINIO_PROBE_BUCKET=true` to perform one bounded bucket-existence
probe during startup. A successful probe reports `ready`; a missing bucket, SDK
failure, or timeout reports `unavailable` with a fixed reason alias. The timeout
limits startup waiting to 10–10,000 ms. The probe never creates a bucket, retries,
or returns a raw SDK error. Keep the default `false` when startup must not make a
network request.

Remote audio retrieval is disabled by default and remote recordings continue to
return 404. To opt in, provide a high-entropy process secret of 32–256 ASCII
characters through the deployment secret manager and set:

```bash
export VOXBENCH_REMOTE_AUDIO_PROXY=true
export VOXBENCH_REMOTE_AUDIO_BEARER_TOKEN='<high-entropy-bearer-token>'
export VOXBENCH_REMOTE_AUDIO_MAX_BYTES=10485760    # optional; 44..67108864
export VOXBENCH_REMOTE_AUDIO_MAX_CONCURRENT=2      # optional; 1..8
```

Then request the existing audio endpoint with the token:

```bash
curl \
  -H "Authorization: Bearer ${VOXBENCH_REMOTE_AUDIO_BEARER_TOKEN}" \
  'http://127.0.0.1:8000/runs/<run-id>/recordings/<stage>/audio' \
  --output recording.wav
```

The proxy never returns a presigned URL. It accepts only the exact configured
bucket/prefix/run/stage object identity, requests at most the configured byte
limit plus one byte, rejects oversized or non-WAV content, bounds concurrent
reads and total in-flight payload capacity to 128 MiB, and uses a TLS-verifying
HTTP client with connect/read timeouts and no automatic retry. SDK/configuration
errors are mapped to fixed HTTP details.
`GET /storage/readiness` exposes only whether the proxy capability is enabled;
the Bearer token is excluded from runtime representations and responses.

Local filesystem recording playback remains backward-compatible and does not
require this remote-object Bearer token.

For Web playback, keep the remote-object Bearer token server-to-server and enable
the optional browser session exchange with two separate high-entropy secrets:

```bash
export VOXBENCH_WEB_AUDIO_SESSION=true
export VOXBENCH_WEB_AUDIO_LOGIN_TOKEN='<operator-login-token>'
export VOXBENCH_WEB_AUDIO_SESSION_SECRET='<distinct-cookie-signing-secret>'
export VOXBENCH_WEB_AUDIO_SESSION_TTL_SECONDS=900  # optional; 60..3600
export VOXBENCH_WEB_AUDIO_COOKIE_SECURE=true       # optional; default: true
```

The login token and signing secret must each contain 32–256 ASCII characters,
must not contain whitespace, and must differ from each other. Web session mode
also requires the remote audio proxy. The UI submits the operator login token
once to `POST /auth/remote-audio/session`, immediately clears the input on
success, and never writes it to browser storage. The API returns a signed,
short-lived `HttpOnly`, `SameSite=Strict` cookie; the audio endpoint accepts that
cookie or the original server-to-server Bearer credential. Status and logout are
available at `GET` and `DELETE /auth/remote-audio/session`. Login payloads are
bounded, authentication failures are fixed aliases, and neither secret appears
in readiness or runtime representations.

Serve the Web UI and `/api` through the same origin. `Secure=true` is the
production default and requires HTTPS. For loopback HTTP development only, set
`VOXBENCH_WEB_AUDIO_COOKIE_SECURE=false`; readiness exposes this non-secret flag
so the UI can warn about the development configuration. A custom cross-origin
deployment requires an explicit CORS and credential-policy review. Never place
the process Bearer, operator login token, or signing secret in frontend source or
persistent browser storage.

## Run the Web UI

```bash
cd web
npm install
npm run dev -- --port 5173
```

Open `http://127.0.0.1:5173/`. The Web UI can inspect a run timeline, compare two
runs, play stage recordings, watch live run status, and start an async run from an
example payload.

For the shortest product walkthrough, use **Hear the pipeline in 3 seconds** at
the top of the page:

1. Choose the target loudness. **Balanced +2.5 dB** is the safe default.
2. Select **Run audible demo**. No provider API key or Asterisk setup is needed.
3. The completed run is selected automatically. Use **Listen to every stage** to
   play the three-second recording before and after AGC/limiting.
4. Run another target loudness, then select a previous result under **Recent
   runs** or enter its ID in **Compare** to inspect the two results.

The quick demo uses a local synthetic tone so gain changes are immediately
audible and deterministic; it is not a speech-quality benchmark. Real speech,
provider, SIP, and RTP validation still require the corresponding integration
inputs. Custom payload controls and operational diagnostics are collapsed by
default so they do not interrupt the first-use path.

After a run is selected, **Call inspector** projects existing observations onto
one shared ruler. It distinguishes typed point events, span intervals, numeric
series, audio artifacts, and derived incidents. Selecting an incident moves the
shared cursor and opens its observed/expected evidence; selecting a stage audio
artifact lets playback and the ruler follow the same cursor. Missing conversation
telemetry is shown as `not observed`, never as healthy.

Run selections are deep-linkable without browser storage:

```text
http://127.0.0.1:5173/?run_id=<run_id>&compare_run_id=<optional_run_id>
```

The typed timeline remains backward compatible with persisted metrics, spans,
SIP/RTP observations, recordings, and verification failures. AudioSocket barge-in
steps are additionally persisted as safe correlated events: provider speech or
interrupt notification, interrupt path, truncation position, playback queue clear,
and completion. Selecting the derived incident shows that evidence chain on the
shared cursor. Discarded queue duration is not presented as audible tail because
the remote listener's actual playout is not yet observed. Packet capture,
sample-accurate cross-clock alignment, and typed caller/assistant speech intervals
remain future work.

Directional RTP observations also produce transport evidence when loss reaches
1%, jitter reaches 30 ms, or MOS falls to 3.5 or below. Consecutive degraded
observations within five seconds are grouped into one interval and incident.
Two consecutive elevated-loss observations are labeled `RTP loss burst suspected`,
not as a confirmed media gap: aggregate statistics do not contain the RTP sequence
or packet-arrival evidence required to prove a missing-packet interval. The active
thresholds are always included in the incident's Expected contract.

Failed Stage duration checks are localized on recording media time. The inspector
marks the output duration where contraction begins, shades the missing interval up
to the previous Stage duration, and links both adjacent recordings as evidence.
Stage signal metrics also emit one peak-change event per Stage when absolute RMS
delta reaches 1 dB, carrying input/output RMS, delta dB, and applied gain. These are
signal-level measurements, not LUFS loudness, true peak, or clipping measurements;
those require dedicated collectors before VoxBench can make those claims.

Observed PCM Stage output now includes sample peak dBFS, PCM16 full-scale sample
percentage, digital-silence sample percentage, and chunk duration. The first Stage
where full-scale endpoint samples appear produces a medium-confidence
`Clipping suspected` incident; downstream propagation does not create duplicate
incidents. This is not an intersample true-peak or definitive clipping measurement.
Digital silence below -60 dBFS for at least 98% of samples is joined into evidence
windows of 200 ms or longer. Silence remains evidence-only until caller/assistant
speech context can establish that the interval was unexpected.

If `web/vite.config.ts` changes while the development server is already running,
restart `npm run dev`. The `/api` proxy carries both REST and WebSocket traffic;
the UI falls back to REST polling if the socket is unavailable.

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

Cross-session resource trends are derived from the latest metric of each ended
run on the same `server_alias`:

```bash
curl http://127.0.0.1:8000/runs/cross-session-trends
```

The detector requires at least three ended runs. It marks `active_tasks` or
externally observed `memory_rss_bytes` as `increasing` only when every successive
value is non-decreasing and the total delta is positive. Running runs, missing
server aliases, and metrics from different servers are not combined. Live preview
polls this projection and highlights increasing trends separately from per-run
host metrics.

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

For a local Asterisk call, install `examples/asterisk/manager.conf.example`, set
the AMI credentials only in environment variables, and collect RTCP quality into
an active run:

```bash
export VOXBENCH_AMI_USERNAME=voxbench-rtcp
export VOXBENCH_AMI_SECRET='REPLACE_WITH_LOCAL_SECRET'
voxbench asterisk-ami-rtcp --run-id '<run_id>' --clock-rate-hz 8000
```

The codec RTP clock rate must match the active call. The collector converts AMI
fixed-fraction loss, timestamp-unit jitter, and seconds RTT to loss percent and
milliseconds. It never forwards AMI Channel, caller identity, address, or SSRC
fields. See `docs/demo-live-softphone.md` for the complete local setup.
Live preview separates these operational metrics into an `RTP collector` block
with `connected`, `collecting`, or `failed` state and collected-event/failure
counts; they are not mixed into the generic host metric tiles.

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
barge-in handling is recorded as one correlated causal sequence, including the
interrupt path, truncation position, and queued frames discarded from local playback.
These are bridge observations, not a claim about remote audible-tail duration. Initial
provider connection is retried three times by default; use `--connect-attempts`
and `--connect-backoff-seconds` to tune it. Mid-call disconnects fail the run
as `provider-stream-ended` or `provider-session-error` instead of silently
resetting conversation state or completing the run. Live preview projects the
connection as `pending`, `connected`, `exhausted`, or `unobserved` and shows its
attempt/retry/failure counts. A real Asterisk/provider call is environment
validation and is not performed by the automated suite.

## Verification

Offline synthetic artifacts include both the original clean reference and one
reference per resolved pipeline stage. Each stage reference records the stage
format, decoded PCM comparison format, transformations, and whether a future
full-reference scorer may safely use it. PCM stages are generated at their
expected rate/channels. G.711 mu-law stages at 8 kHz use a deterministic
PCM16-to-mu-law-to-PCM16 round-trip reference. Unsupported codecs remain
explicitly blocked, preventing ViSQOL/PESQ from using an invalid clean PCM
reference. See ITU-T G.711 and RFC 3551 section 4.5.14 for the codec contract.
`select_full_reference_candidates(...)` then pairs only ready references with a
stage recording whose decoded `encoding`, `rate`, and `channels` match. Missing
recordings, unsupported codecs, duplicate references, and format mismatches are
returned as safe block reasons before any external scorer is invoked.
`score_full_reference_selection(...)` provides the next optional-dependency
boundary. A scorer declares its safe name, metric name, and numeric range, then
reports readiness before any candidate is read. Each result is explicitly
`scored`, `unavailable`, `blocked`, or `failed`; raw dependency errors and unsafe
paths/URLs are discarded. Only successful finite in-range scores become numeric
metrics, so a missing or failed scorer is never represented as a misleading zero.

`VisqolCliScorer` is an optional adapter for an explicitly installed official
ViSQOL binary. `speech` mode prepares both reference and degraded inputs at
16 kHz; `audio` mode prepares both at 48 kHz. The stage-native WAVs are not
overwritten, scorer inputs live only in a temporary directory, and the selected
mode plus any resampling are retained in the score result transformations. The
binary's stdout/stderr and raw process errors are discarded. If the binary is
absent, the candidate is reported as `unavailable`. VoxBench does not install or
redistribute ViSQOL; build/install it separately according to the
[official Google ViSQOL documentation](https://github.com/google/visqol).

```python
from pathlib import Path

from voxbench.verification import VisqolCliScorer, score_full_reference_selection

scorer = VisqolCliScorer(binary=Path("/path/to/visqol"), mode="speech")
report = score_full_reference_selection(selection, scorer)
```

To score an existing matching mono PCM16 WAV pair and receive a path-free JSON
result, use the CLI. It exits `0` when scored, `2` when the optional binary is
unavailable or CLI input is invalid, and `1` for a scorer execution failure.

```bash
voxbench visqol-score \
  --reference artifacts/reference.wav \
  --degraded artifacts/recording.wav \
  --binary /path/to/visqol \
  --mode speech \
  --stage serializer
```

For an end-to-end deterministic run, `synthetic-visqol` resolves a config,
generates stage-native references and recordings, evaluates signal invariants,
scores every eligible stage, and writes `verification-report.json` under the
output root. The persisted report contains stage names, observations, scores,
safe reasons, and the complete reference/scorer transformation chain, but no
artifact URI, config secret reference, binary output, or absolute path. The
default duration is five seconds, within ViSQOL's documented practical guidance.

```bash
voxbench synthetic-visqol \
  --config examples/configs/valid-baseline.json \
  --manifest examples/manifests/engine/asterisk.json \
  --manifest examples/manifests/provider/gemini.json \
  --manifest examples/manifests/processor/resampler.json \
  --manifest examples/manifests/processor/agc.json \
  --manifest examples/manifests/processor/limiter.json \
  --manifest examples/manifests/processor/serializer.json \
  --output-root artifacts/synthetic-visqol \
  --binary /path/to/visqol \
  --mode speech
```

Because one MOS-LQO value is not a regression conclusion,
`aggregate_full_reference_reports(...)` combines multiple reports only within a
declared safe treatment alias and identical scorer contract. The default minimum
is three scored samples. It reports mean, median, min/max, and population standard
deviation only after that minimum is met. Missing or non-scored samples make the
stage `partial`; differing transformation chains make it `incomparable`; too few
otherwise-valid samples remain `insufficient`.

Treatment aggregates can be compared with an explicit
`FullReferenceRegressionPolicy`. The caller supplies a finite non-negative stable
tolerance and metric direction. A stage becomes `improved`, `stable`, or
`regressed` only when both sides are fully aggregated under the same scorer
contract and transformation chain. Missing, partial, insufficient, or
incomparable data remains `indeterminate` with a safe reason alias.

Persisted reports can be compared without regenerating audio. The loader accepts
both a standalone aggregate payload and `synthetic-visqol-treatment`'s wrapper,
with a 1 MB bound and strict validation of aliases, score range, finite
statistics, counts, and transformations. The CLI exits `1` if any stage
regressed, `2` if any stage is indeterminate, and `0` otherwise.

```bash
voxbench visqol-compare-treatments \
  --baseline artifacts/baseline/treatment-report.json \
  --current artifacts/candidate/treatment-report.json \
  --stable-tolerance 0.1
```

Before choosing that tolerance, repeated baseline reports can be summarized with
`visqol-calibrate-repeatability`. At least three reports are required. The output
contains mean-of-means, minimum/maximum treatment mean, observed maximum pairwise
delta, and population standard deviation. It deliberately does not emit a
recommended tolerance or statistical-significance claim.

```bash
voxbench visqol-calibrate-repeatability \
  --report artifacts/baseline-1/treatment-report.json \
  --report artifacts/baseline-2/treatment-report.json \
  --report artifacts/baseline-3/treatment-report.json
```

`synthetic-visqol-treatment` runs that policy end to end. It creates
`sample-001`, `sample-002`, and so on, varies source frequency while retaining
the same config and scorer treatment, persists each verification report, and
writes a path-free `treatment-report.json`. The command exits `0` only when all
stages are aggregated, `2` for an incomplete/insufficient treatment, and `1`
for a failed sample.

```bash
voxbench synthetic-visqol-treatment \
  --config examples/configs/valid-baseline.json \
  --manifest examples/manifests/engine/asterisk.json \
  --manifest examples/manifests/provider/gemini.json \
  --manifest examples/manifests/processor/resampler.json \
  --manifest examples/manifests/processor/agc.json \
  --manifest examples/manifests/processor/limiter.json \
  --manifest examples/manifests/processor/serializer.json \
  --output-root artifacts/baseline-treatment \
  --binary /path/to/visqol \
  --treatment baseline-speech \
  --sample-count 3
```

```bash
ruff check .
pytest
```
