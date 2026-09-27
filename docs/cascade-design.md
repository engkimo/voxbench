# STT → LLM → TTS cascade support: investigation and design

Status: design with C1, C2 and C3A implemented on
[Draft PR #25](https://github.com/engkimo/voxbench/pull/25), pending merge,
2026-09-27.
The current C1 shape and examples are in [cascade-config-v2.md](cascade-config-v2.md).
The C2 service-event contract is in
[cascade-observation-api.md](cascade-observation-api.md). The
[causal analysis contract](cascade-analysis.md) is implemented on the review branch. Recording
maps, adapters and runtime remain planned. Current status is in
[implementation-status.md](implementation-status.md).

## 1. Intended behavior and initial scope

VoxBench should observe and diagnose both integrated speech-to-speech realtime
services and a cascade whose STT, text LLM and TTS use independent models/providers.
Operators should see the exact model combination, where response time was spent,
where audio first degraded, and what happened to in-flight work on interruption.

The user agreed to observation-first planning, followed by optional test-call
execution, and requested issue-sized implementation slices. The explicit scope
includes Pipecat, other frameworks/middleware, and applications using provider
SDKs/HTTP/WebSockets directly without middleware. No particular provider or
additional framework is selected yet. Concrete contract shapes below remain
design proposals until implemented and verified.

```text
caller audio -> input PCM processors -> STT -> turn/context coordinator
                                              |
                                              v
                                            text LLM
                                              |
                                              v
                                        text aggregation
                                              |
                                              v
                                             TTS
                                              |
                                              v
                            output PCM processors -> local playback -> caller
```

Conversation orchestration remains owned by the existing application/runtime.
VoxBench supplies observation contracts, deterministic analysis, evidence
projections and thin optional runtime integrations. The diagnostic agent's
`ModelAdapter` remains separate from the conversation's LLM service.

For the first release, the semantic graph has one STT, one LLM, one aggregator
and one TTS. It supports multiple turns, multiple TTS segments per response and
overlapping generation/playback. Arbitrary branching, multiple voices, provider
fallback, speculative execution by VoxBench and a workflow editor are later work.
An external application's speculative requests may be marked as such, but are
excluded from the committed-turn latency path.

### 1.1 Independent compatibility dimensions

| AI topology | Middleware/runtime | Required observation path |
| --- | --- | --- |
| Realtime | Pipecat or another framework | Optional framework hook/adapter → common observation contract |
| Realtime | Direct SDK/HTTP/WebSocket application | Application-owned hook → common observation contract |
| Cascade | Pipecat or another framework | Per-service/turn/playback hooks → common service observation contract |
| Cascade | Direct STT/LLM/TTS application | Per-service/turn/playback hooks → the same service observation contract |

Telephony/WebRTC transport and implementation language are additional independent
choices. A Python SDK is a convenience; the versioned HTTP contract must also
support non-Python applications. This matrix is a target, not a claim that every
framework/provider already has a tested native adapter.

Common observer, ingest, analyzers and UI must not require Pipecat classes or any
other framework's frame/context/queue types. A dedicated adapter maps available
boundaries; an application without one can use direct instrumentation. Unsupported
observations remain unobserved, and a framework's bot-start event is not promoted
to verified remote playout. Core must import successfully with no framework SDK.

## 2. Findings from the current implementation

| Existing boundary | Finding | Required change |
| --- | --- | --- |
| `schemas.py:AiConfig` / `VoiceSpec` | Strict v1 config requires one `provider`, `model`, `params` | Versioned realtime/cascade union |
| `schemas.py:ProviderCaps` | Audio-oriented rates, codec and turn-owner capabilities | Role and modality contracts per service |
| `registry/service.py:_validate_cross_fields` | Resolves one provider manifest and treats its caps as the call's AI contract | Resolve three services and validate each boundary |
| `registry/merge.py:deep_merge` | Recursively merges object fields, including incompatible shapes | Reject cross-mode/version inheritance unless explicitly replaced |
| `engine_harness/plan.py:build_stage_plan` | One ordered PCM pipeline, keyed by stage `type` | Independent input/output chains with stable stage IDs |
| `engine_harness/harness.py:run_once` | Produces nominal taps; does not run a real cascade | Keep execution readiness distinct from artifact generation |
| `realtime_providers/providers.py:RealtimeProviderSession` | PCM in/out and five lifecycle events; no STT/text/TTS handoff | Keep compatible; introduce separate runtime service adapters |
| `engine_harness/pipecat_adapter.py` | Only a thin `Pipeline([...])` construction boundary | Add a versioned optional observer/integration adapter |
| `observability/observer.py` | Generic scalar events, audio taps, bounded batch splitting | Typed service observations and service-aware recording mapping |
| `run_api.py:/v1/observations` | All referenced `stage` values must be in `media.pipeline` | Distinguish service components and PCM stages |
| `run_api.py:_create_running_run`, `StoredRun`, `models.py:Run` | One provider string is required throughout run projections/storage | Add AI mode/component projection; retain legacy provider field |
| `run_api.py` / `verification/core.py` | Audio adjacency and final-stage rules assume a single PCM chain | Validate each PCM chain separately; never compare STT audio to TTS audio for duration/level preservation |
| `web/src/App.tsx` / `types.ts` | One Provider lane and stage identity based on PCM stage names | STT/LLM/aggregation/TTS sublanes and component-aware filters |
| `control_plane/ui_commands.py` local candidate | Filter stages come from PCM lanes; evidence targets come from existing primitives | Include service component IDs after candidate stabilization |

The current generic attribute sanitizer rejects selected sensitive string markers
and limits scalar sizes. It is not a field allowlist for transcript/prompt/text
content. Cascade privacy must therefore be enforced by a new closed typed
contract, not by assuming arbitrary strings are harmless aliases.

## 3. Research and implications

Pipecat documents separate STT, context, LLM, TTS and transport processors, and
text aggregation between streamed LLM output and TTS. This supports using a thin
adapter while the application/framework keeps orchestration ownership.
[Official pipeline documentation](https://docs.pipecat.ai/pipecat/learn/pipeline).

Its metrics distinguish service TTFB, TTS first audio, LLM answer output, text
aggregation and user-to-bot latency. We should preserve a metric's measurement
boundary and translate seconds to milliseconds explicitly. A framework metric
without turn/request correlation stays service-level evidence; it cannot be
assigned to a particular turn by proximity alone.
[Official metrics documentation](https://docs.pipecat.ai/pipecat/fundamentals/metrics).

The STT guide distinguishes speech-end-to-final-segment timing from turn-based
STT, where the server itself defines the turn boundary. We cannot assume all STT
providers expose an independent speech end or treat a configured P99 latency as
a measurement of the current call.
[Official STT latency documentation](https://docs.pipecat.ai/pipecat/fundamentals/stt-latency-tuning).

These are design implications drawn from the documentation. The repository does
not pin or install Pipecat today, and the researched latest API is not a verified
local dependency. Choose and pin an adapter compatibility range during its
implementation, recording framework version and metric definitions. Do not copy
current API names into core assumptions.

## 4. Versioned configuration proposal

Keep `voxbench/v1` parsing, validation, resolution and serialized hashes intact.
Introduce `voxbench/v2` for the new shape. Dispatch by `apiVersion`, then
discriminate `spec.ai` by `mode`. Do not add defaults to a v1 resolved object or
rewrite previously stored runs. Generate version-specific JSON schemas alongside
the existing schema; add an explicit v1→v2 conversion command later.

### 4.1 Semantic services and audio processors

The v2 AI object has exactly one of these shapes:

```yaml
# Semantic fragment of a proposed voxbench/v2 cascade config.
# Provider/model names are placeholders. This illustrative fragment omits
# required manifest_version and audio selections; use examples/v2 for full configs.
ai:
  mode: cascade
  stt:
    id: stt-main
    plugin: example-stt
    model: pinned-stt-model
    params: {}
  llm:
    id: llm-main
    plugin: example-text-llm
    model: pinned-llm-model
    params: {}
    system_prompt_ref: prompt-version-ref
    tools: []
  text_aggregation:
    id: text-aggregation-main
    plugin: example-sentence-aggregator
    params: {}
  tts:
    id: tts-main
    plugin: example-tts
    model: pinned-tts-model
    params: {}
```

```yaml
# Alternate v2 AI object: integrated speech-to-speech.
ai:
  mode: realtime
  realtime:
    id: realtime-main
    plugin: example-realtime
    model: pinned-realtime-model
    params: {}
    system_prompt_ref: prompt-version-ref
    tools: []
```

`spec.media` has `input_pipeline` and `output_pipeline`. Each PCM stage has a
run-unique `id`, plus its existing `type`, `plugin`, `params` and effective
manifest contract. IDs are globally unique across services, aggregation and PCM
processors. Using `type` as identity would collide when both directions contain
a resampler or AGC.

Input processors connect decoded telephony PCM to STT/realtime input. Output
processors connect TTS/realtime output to decoded telephony PCM/framing. Empty
chains are allowed only when the boundary formats already match. STT, text LLM
and TTS are not placed in the PCM stage list. Aggregation is an explicit text
processor whose policy/version affects config hashes and comparison.

Credential references are declared by each plugin's params schema; actual
credentials remain at deployment/runtime boundaries. Safe run projections use
declared aliases and allowlisted params. Prompt contents, tool inputs, voice
samples and private/custom voice identifiers do not become ordinary metadata.

### 4.2 Capability manifests and validation

Keep plugin kinds `engine`, `provider`, `processor`. STT/LLM/TTS remain provider
plugins with role-specific capabilities. Text aggregation is a processor with a
text contract, without audio invariants. Introduce a separately versioned v2
manifest schema; existing unversioned manifests stay v1.

Proposed provider `service_caps`:

- `roles`: subset of `stt`, `llm`, `tts`, `realtime`.
- `input_modality` / `output_modality`: audio→text, text→text, text→audio, audio→audio.
- audio input/output formats: supported encodings, rates, channels per boundary.
- observed lifecycle support: partial/final output, response boundaries, usage.
- endpointing/turn-boundary behavior and supported ownership arrangements.
- cancellation behavior: request sent, terminal outcome acknowledgement,
  transport abort support; conversation truncation separately if supported.
- declaration of model selection/readiness limitations and safe failure aliases.

Registry hard failures cover missing/wrong-role manifests, invalid model params,
duplicate IDs, modality mismatch, adjacent PCM IO mismatch, missing resampler,
unsupported turn ownership, forbidden overrides and ambiguous authorities.

Do not compare the telephony wire codec directly with a text LLM or PCM-only STT
provider's supported codecs. Validate the engine's codec/decode contract, PCM
processor formats, and service formats at their actual connection boundaries.
An application-owned integration may leave a capability unobserved, but that
produces readiness `unknown`, not a fabricated pass.

Published resolution should pin manifest versions/digests as well as selected
models and aggregation policy. V2 overlays cannot silently switch AI mode or
inherit v1 shapes through `deep_merge`; require a new base or an explicit
conversion/replacement operation. STT-only or TTS-only changes within the same
mode remain ordinary overlays.

### 4.3 Turn ownership

Extend the v2 turn contract with separate `speech_detector`, `end_of_turn` and
`interruption` authority references. Each decision has one designated authority;
multiple observations may exist. A client VAD, STT endpointing notification and
semantic turn detector may cooperate, but STT `final` is not automatically a
committed conversational turn. Reject two independent coordinators that both
submit the same turn to the LLM.

End-of-turn and barge-in need distinct settings/capabilities. TTS does not own
caller VAD. The initial optional runtime submits committed turns only; STT
revision and speculative LLM output stay outside that execution scope.

## 5. Observation, correlation and privacy contract

### 5.1 Identity

Use `run_id` as the call anchor and safe aliases for the following relationships:

| Identity | Scope and purpose |
| --- | --- |
| `component_id` | Stable configured STT/LLM/TTS/aggregation/PCM node |
| `turn_alias` | Committed conversational turn; may contain multiple STT segments |
| `request_alias` | One component operation/attempt; session alias for continuous STT |
| `parent_request_alias` | Explicit causal parent; absent when not known |
| `response_alias` | Assistant response, including separate LLM operations around tool execution |
| `segment_alias` | Text unit/TTS request and its audio, preserving order |
| `generation_epoch` | Local cancellation generation; separates old and new output |
| `collector_alias` | Distinguishes concurrent producers and their event ID namespaces |

Do not persist provider item IDs, transcript-derived identifiers/hashes or raw
frame content. Streaming STT has a long-lived session and bounded segment
operations; do not pretend it has one request per audio chunk. A request retry
gets a new alias linked to the same operation/turn. Missing causal links remain
unknown rather than being inferred from nearest timestamps.

### 5.2 Closed service events

Add an optional `service_events` collection to the existing observation batch.
Use a discriminated, versioned `ServiceObservation` contract with closed fields
and enum-specific scalar payloads. Preserve existing generic batches unchanged.
Initial event families:

- `stt.segment_started`, `stt.partial_observed`, `stt.final_observed`:
  safe segment/revision aliases, final flag, counts if allowed; no text.
- `turn.speech_started`, `turn.speech_ended`, `turn.committed`:
  authority, measured boundary, coverage; no invented waveform segmentation.
- `llm.request_started`, `llm.first_output`, `llm.first_answer_text`,
  `llm.response_completed`: output kind, safe finish reason, usage if observed.
- `text.segment_ready`: parent LLM operation, segment ordinal and aggregation
  contract. No text content or hash.
- `tts.request_started`, `tts.first_audio`, `tts.audio_progress`,
  `tts.request_completed`: segment, actual format, generated media duration.
- `service.failed`, `service.cancel_requested`, `service.cancel_outcome`:
  safe outcome alias; requesting cancellation is not observing its success.
- `playback.segment_enqueued`, `playback.segment_write_started`,
  `playback.segment_write_stopped`, `playback.queue_cleared`,
  `playback.stale_output_dropped`: response/segment/epoch and local media duration.

The integration must distinguish service response headers, first answer text,
first PCM output and first local write. A TTS start frame is not sufficient proof
of first audio. Tool calls/reasoning output are not spoken answer text. Completion
observations do not imply all generated audio was written or heard.

Typed events translate to existing `timeline_events` with reserved scalar keys,
`category=provider/conversation/buffer`, `stage=component_id` where appropriate,
and `correlation_alias=request_alias`. Parent/turn/segment aliases remain
allowlisted scalar fields. Validate component existence, role/event compatibility
and same-run relationships server-side. An absent parent may be reported as
missing while waiting for out-of-order batches; never attach a cross-run parent.

The typed path enforces privacy regardless of producer behavior. Reserved service
event names through the generic path must pass the same validator, so generic
attrs cannot bypass it. Reject text/prompt/transcript/tool arguments, raw errors,
URLs, provider identifiers and secrets. User audio retention stays a separate
explicit permission. Cascade application's necessary external STT/LLM/TTS data
flow is separate from retention in VoxBench and from diagnostic-model egress.

### 5.3 Storage and bounds

Reuse the existing event table for the initial metadata contract; no unbounded
raw response table. Keep at most 16 scalar attrs in a normalized event and 128
total timeline/service events per batch. Helpers split batches outside audio
callbacks. Emit lifecycle boundaries and bounded progress windows rather than
one event per token/sample. Define per-run event/queue budgets and visible
observation-drop counters; batch limits alone do not bound memory usage.

Event IDs include a collector namespace and stable local ordinal. Retries reuse
the same ID and payload. Same-ID/same-payload is idempotent; a different payload
is a conflict. Lifecycle grouping must tolerate reordered batches, repeated
events and missing terminals, retaining `completion_observed=false`.

Initially derive a safe `ai_mode` and `ai_components` projection from the pinned
resolved config. Keep existing run `provider` for compatibility: actual provider
for v1/realtime, reserved `cascade` for cascade. New consumers use components;
never concatenate credential/endpoint data into this label. Mode/components must
appear consistently in run detail, recent, timeline, live preview and compare.
Additional DB indexes/normalized component rows can follow measured needs.

### 5.4 Framework-independent instrumentation and conformance

Define service observation DTOs independently of all framework/provider SDKs.
Python helpers accept typed metadata and enqueue it without blocking media paths;
document the equivalent HTTP payloads for non-Python callers. Do not require a
Pipecat pipeline to start/register an observed run, correlate a turn or complete it.

Provide one deterministic trace suite used by both direct integrations and optional
framework adapters. The adapter reports its version, runtime alias and actually
observed boundary coverage. Test the same multi-turn, segmented output, interruption,
missing-boundary, clock, retry and privacy scenarios through common projections.
Compatibility means the adapter passes these cases for its documented boundaries,
not that it synthesizes missing framework events.

The no-middleware reference integration is a small application-owned STT/LLM/TTS
loop with fake service SDKs first. It owns coordination and emits only common
metadata; it can demonstrate real SDK calls after providers are selected. Optional
Pipecat and another framework adapter implement the same mapping. The second
framework is selected for validation later; no LiveKit or other adapter is claimed
to exist today. Optional dependency absence must never break the direct example.

## 6. Timing and deterministic analysis

Every derived latency includes start/end evidence refs, scope aliases, measurement
definition/version, clock domain and alignment uncertainty. Use local monotonic
timestamps plus a bounded run-clock mapping whenever available. Cross-domain
subtraction requires calibrated mapping; otherwise return `indeterminate`.

| Measurement | Definition and limitation |
| --- | --- |
| STT finalization wait | Observed speech end → final segment used by the committed turn; undefined if independent speech end is unavailable |
| Turn coordination wait | Final required segment → committed turn; not a second STT inference measurement |
| LLM first-output wait | Actual operation start → first observed output, with output kind |
| LLM first-answer-text wait | Operation start → first answer text eligible for speech; not reasoning/tool-call latency |
| Text aggregation wait | First answer text in the source unit → segment ready for TTS; per-segment scope |
| TTS first-audio wait | Segment's actual TTS operation start → first PCM output; excludes pre-request queueing |
| TTS dispatch queue wait | Segment ready → TTS request actually started |
| Output/playback start wait | First corresponding TTS PCM output → first corresponding local frame write |
| End-to-end local response wait | Speech end → first local frame write for the response causally tied to that committed turn |
| Generated/written/discarded duration | Separate media-time counters by response/segment/epoch; none means remote heard duration |

Do not add full STT, LLM and TTS operation durations as end-to-end response time.
Generation and playback overlap, and a response may contain several requests.
Compute actual causal intervals/critical path, preserving tool execution,
dispatch/aggregation waits and non-overlapping unexplained spans. If endpoints
cannot be linked, display available intervals without a complete decomposition.
For barge-in during existing playback, match the next committed response rather
than reusing an old response's playback timestamp.

A framework-reported metric without endpoints stays an observed metric with its
native definition. Do not manufacture two timestamps from one duration. Never
label an observed text chunk as a token unless the adapter guarantees token
granularity. No observed request or audio boundary means `unobserved`, not zero.

Initial diagnostics are evidence-based:

- Service failure and safe outcome, with component scope.
- Policy/SLO overrun only with an explicit applicable latency contract.
- Interrupted generation and observed queue-discard/stale-output events.
- Local playback gap suspected only with the relevant continuity contract and
  generation/playback evidence; active LLM text generation alone is insufficient.
- Existing audio duration/level/PCM-quality rules within each audio chain.

STT recognition accuracy, semantic answer quality and pronunciation accuracy are
`not measured` without an opted-in reference/content evaluation. No WER, TTS
quality MOS or semantic score is inferred from ordinary metadata. Full-reference
audio scoring requires a valid same-content reference; caller PCM is not the
reference for a newly generated TTS reply.

## 7. Audio taps and recording time mapping

Useful tap boundaries include decoded caller input, STT input after input
processors, original TTS PCM output, each output processor and final local write.
Record only declared/allowed taps; ordinary service metadata does not opt in
conversation recording. Do not create WAVs for STT text or LLM output.

Input and output chains have independent formats, directions and media origins.
Audio invariants compare adjacent PCM boundaries in the same chain/segment;
they do not assert duration or level preservation across speech recognition and
speech synthesis.

Concatenated stage WAV time is not run wall time: there can be long pauses and
generated audio can precede paced writes. Introduce segment-level mapping records
with artifact/stage ID, response/segment aliases, recording sample start/count,
format, source clock/run-relative boundary and uncertainty. Source-generation
mapping and paced-write mapping are distinct. A source receive timestamp alone
does not prove when each generated sample was played.

Timeline cursor → recording seek uses a known piecewise map. In gaps or when the
map is missing, show that boundary and offer media-time playback instead of
seeking by `run_ms` into a concatenated WAV. V1 playback remains compatible, with
its existing alignment limitations exposed. Verified UI commands must resolve
to the mapped recording position for v2.

Recording storage currently has one artifact/buffer per run/stage and rewrites
the growing observed WAV. Start with compatible artifacts plus persisted mapping
metadata; high-throughput segmented streaming storage is separate hardening.

## 8. Interruptions, cancellation and optional execution

On an observed interruption, the application/runtime coordinator advances the
generation epoch, prevents old output from entering the playback queue, clears
queued frames, cancels unfinished LLM/TTS requests when supported, and continues
caller/STT input. Late callbacks/audio from old epochs are dropped with bounded
evidence. Provider cancellation acknowledgements and transport aborts are recorded
separately; unsupported cancellation does not prevent local stale-output suppression.

The STT session is not canceled merely because assistant playback is interrupted.
TTS request completion is not response playback completion. Conversation-history
commit/truncation belongs to the application's policy; remote heard words cannot
be reconstructed from local writes alone. Realtime `truncate_audio` remains a
capability of the existing integrated provider, not a mandatory method on STT/TTS.

For execution support, define separate `SttServiceAdapter`, `TextLlmServiceAdapter`
and `TtsServiceAdapter` runtime boundaries with readiness, typed outputs, safe
errors, cancellation semantics and close. Text necessarily exists inside the
application runtime but is not serialized into observation events. Optional
Pipecat factories construct framework processors; core does not recreate its
queues/context/turn machinery. Existing `RealtimeProviderSession` stays intact.

Separate test orchestration from the chosen conversation implementation with a
framework-independent `RuntimeAdapter`/application-launcher boundary: readiness,
start a test call with pinned config/run ID/observation transport, cancel/stop,
and close. An application-owned direct runtime, a Pipecat runtime and another
framework runtime can implement it without changing core analysis. Capabilities
must state whether the runtime supports execution or observation only; inability
to launch a call does not prevent observing one. Do not wrap another framework
inside Pipecat or require its installation to run direct-provider tests.

Execution requires exact model/format preflight, synthetic/fake adapters for
default tests, bounded queues/timeouts and resource shutdown. A nominal harness
must reject cascade execution as unsupported until a real execution adapter is
available, rather than reporting a successful conversation from generated taps.

## 9. UI and diagnostic-agent integration

- Show `Realtime` or `Cascade` and all configured model components near the run
  selector, recent list and comparison.
- Keep the five timeline primitives and existing categories. Add STT, turn,
  LLM, aggregation and TTS sublanes within provider/conversation/buffer views.
- Select a turn/response to view its explicit request/segment chain and latency
  endpoints. Uncorrelated service metrics remain separate.
- Show generated, queued, written and discarded audio quantities separately.
- Display missing lifecycle/correlation/clock/playout coverage by component.
- Compare exact STT/LLM/TTS models, aggregation/turn policy, route, formats and
  measurement definitions. Label multiple simultaneous changes explicitly.
  Matched configuration still does not imply deterministic provider output.
- Extend component filters/evidence resolution in the typed UI-command candidate
  after stabilization. Reuse validated primitives instead of arbitrary selectors.
- Deterministic diagnostic bundles include component coverage and causal metrics.
  A future diagnostic model receives safe metadata; cascade support does not
  automatically permit transcript/audio egress.

## 10. Implementation sequence and acceptance gates

Each slice must leave v1 realtime/observer workflows working. These cascade slices
are distinct from the diagnostic-agent Phase 0–5 roadmap.

Published tasks: [issue plan and dependencies](cascade-issue-plan.md), under
[parent issue #15](https://github.com/engkimo/voxbench/issues/15). Issue publication
tracks planned work; code status is recorded separately in the implementation
inventory and Draft PR #25.

| Slice | Work | Acceptance gate |
| --- | --- | --- |
| S0: baseline stabilization | Review/package the existing UI/SSE candidate independently; current inventory and document policy | Isolated intended file set passes checks; UI verified; feature commit/status explicit |
| C1: versioned contracts | v2 config/manifest unions, stable IDs, input/output PCM chains, central normalized read helpers | Legacy resolved bytes/hashes unchanged; invalid roles, formats, overlays and ownership fail |
| C2: service observation | Typed helper/batch path, event normalization, aliases, limits, metadata-only fixtures | Fake multi-turn/segmented run ingests without content; retries/conflicts/reorder and missing boundaries handled |
| C3a: causal analysis | Component projection, causal grouping, deterministic latency | Overlap/clock/cancel fixtures yield correct values or indeterminate; Postgres reconstruction matches |
| C3b: audio chains and recording maps | Per-chain verification and persisted segment tap mappings | Input/output identities and formats remain independent; cursor seek preserves gaps and uncertainty |
| C4: cascade inspector | Component model badges, sublanes, latency/evidence view, comparison and mapped listening | Operator identifies STT wait vs aggregation vs TTS/playback; unknowns visible; v1 playback preserved |
| C5a: direct integration and conformance | No-middleware example, common trace suite and language-independent HTTP guide | Framework SDKs absent; same semantic fixtures/coverage rules pass; no callback blocking/content retention |
| C5b: optional framework integrations | Versioned Pipecat adapter and one other framework adapter | Same conformance suite; only real available boundaries mapped; framework-specific packages optional |
| C6: optional test-call execution | Framework-independent runtime/launcher contract and service factories, fake first, then selected combination | Direct runtime works without Pipecat; readiness, overlap, interruption stale-drop and complete cleanup verified |

C1–C3 can target the committed observation contracts without depending on the
local Ask VoxBench implementation. S0 is required before making that candidate
a shipped dependency. Record each implemented slice/commit in the status inventory.

The implementation review branch combines C1–C3A in Draft PR #25: closed
schemas and registry validation, metadata-only observation, and deterministic
causal analysis. It retains the slice boundaries in tests and documentation and
adds no live provider, framework runtime, or Web feature.

After C3A, the user-designated next goal is a matched pair of deterministic fake
Cascade test applications: one direct application-owned STT/LLM/TTS loop and one
Pipecat-backed loop. Both use the same events, scenarios and analysis outputs so
VoxBench can compare runtime behavior without making Pipecat a core dependency.
This combines the proof work planned in C5A/C5B; real providers remain a later,
explicitly selected validation step.

### Required fixtures

1. One committed turn and two TTS segments; LLM completes after playback starts.
2. Multiple STT partial/final revisions; one final is selected by the coordinator.
3. Two overlapping turns and an interruption: late old-epoch TTS output is dropped.
4. Missing STT speech-end or request linkage: no invented finalization/e2e latency.
5. Separate uncalibrated clocks: cross-boundary timing is indeterminate.
6. Failed/unsupported cancellation: local stale-output suppression still holds.
7. Tool-call-only first LLM output, then an answer from another operation.
8. Generic reserved-name bypass, text/secret/URL fields and cross-run refs rejected.
9. Duplicate event delivery and changed-payload collision; collector namespaces.
10. Duplicate processor types in input/output chains and differing rates.
11. Same v1 config before/after schema changes: exact resolved object/hash stable.
12. Gapped concatenated recording and early TTS generation: cursor seeks using the
    correct segment/write map, or explicitly cannot align.
13. Postgres restart reconstructs components/events/maps/derived evidence.
14. Metadata-only run with no WAV/transcript: accuracy/quality remains not measured.
15. No Pipecat/other framework SDK installed: core and direct cascade example work.
16. Direct, Pipecat and second-framework traces normalize to the same evidence
    semantics for equivalent observed boundaries; missing boundaries stay unknown.
17. Observation-only application cannot be launched: readiness says unsupported
    execution while observation remains available.

## 11. Decisions still needed for environment integration

- Existing cascade observation precedes optional execution; identify the first
  actual application whose observed boundaries will validate the design.
- First STT/LLM/TTS combination, exact model versions, language and deployment
  regions; credentials are not needed for C1–C4.
- Existing runtime (direct/Pipecat/other), its version and actual available hooks;
  choose a second framework to demonstrate core-independent integration.
- Turn detection, aggregation, interruption policy and applicable latency SLOs.
- Which audio taps/content evaluation are explicitly permitted, with retention
  and recording playback authorization.

The versioned config, causal observation and compatibility design can proceed
without selecting vendors. These environment choices become prerequisites for
an actual integration/execution validation, not permission to fake its evidence.
