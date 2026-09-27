# VoxBench Roadmap

This roadmap describes direction, not delivery dates. Priorities may change as
real call evidence and contributor feedback reveal better abstractions.

Current status is maintained in [the implementation inventory](docs/implementation-status.md).
Dated progress/memory entries are historical. Design proposals and local
candidates are not automatically available in the committed baseline.

## Product north star

Align every signal in an AI voice call on one timeline, then let an operator move
from an audible symptom to the first observed cause without overstating what the
system knows.

## Available today

- Schema and manifest validation with deterministic resolved-config hashes.
- Stage-native `resampler`, `agc`, `limiter`, and `serializer` recordings.
- Duration, level, cadence, clipping-suspicion, and silence evidence.
- A common-time-axis inspector for signaling, provider, pipeline, buffer,
  transport, host, and recording evidence.
- Two-run comparison, stage playback, waveform display, and metric deltas.
- OpenAI Realtime and Gemini Live provider boundaries.
- Local Asterisk AudioSocket and aggregate AMI RTCP collection.
- Barge-in evidence that correlates provider chunks with locally discarded
  playback frames.
- Postgres run persistence and a leased, fenced async job queue.
- A three-second synthetic demo requiring no provider account or Asterisk.

See [PROGRESS.md](PROGRESS.md) for historical implementation slices and the
inventory for current availability and verification scope.

## Now: stabilize the diagnostic UI candidate

- Review and package typed UI-command resolution and local diagnostic SSE as a
  complete, independent change, including currently untracked modules/tests.
- Verify an isolated intended checkout and the browser evidence/acknowledgement path.
- Keep the deterministic local adapter distinct from production model diagnosis.
- Define session limits, durable events/jobs, cancellation/reconnection,
  guided investigation and OIDC authorization as separate acceptance work.

## Next: separate STT, LLM and TTS models

C1 configuration/capability contracts, C2 metadata-only service observation and
C3A deterministic causal latency analysis
are implemented as local candidates: [current v2 contract](docs/cascade-config-v2.md)
and [service observation API](docs/cascade-observation-api.md), plus the
[causal analysis contract](docs/cascade-analysis.md). Recording maps, inspector
support, adapters and execution remain planned.

The next goal is a matched direct/Pipecat pair of deterministic fake Cascade
projects. Both will emit the common contract so VoxBench can validate, tune and
compare runtime behavior before real providers are selected.

- Add versioned realtime/cascade configuration and role/modality capabilities
  while preserving legacy config hashes.
- Observe existing cascades with metadata-only service events and explicit
  turn/request/response/TTS-segment correlation.
- Separate STT finalization, turn coordination, LLM answer generation, text
  aggregation, TTS first audio and local playback waits.
- Preserve clock uncertainty, cancellation epochs and segment recording maps.
- Add component model badges, service sublanes and comparable run evidence.
- Follow observation support with optional framework-independent test-call execution.
- Keep realtime/cascade topology independent of runtime choice. Support Pipecat,
  other frameworks, and direct SDK/HTTP/WebSocket applications through the same
  observation contract; add a shared conformance suite and framework-free example.
- Use optional framework adapters and a runtime/application-launcher contract
  for test calls, with no mandatory Pipecat package.

Observation-first and framework-independent support are product decisions;
contract details and the first provider/additional-framework combination remain
proposed or unselected. See [the cascade investigation and design](docs/cascade-design.md).
Published work items and dependencies are tracked in [the issue plan](docs/cascade-issue-plan.md)
and [parent issue #15](https://github.com/engkimo/voxbench/issues/15).

## Now: make real-call diagnosis obvious

- Distinguish synthetic demo runs from real calls at every selection point.
- Keep run provenance, duration, provider, and evidence coverage visible near
  the Call inspector.
- Improve cursor-linked listening and stage-to-stage comparison.
- Explain empty or unobserved lanes in the UI instead of showing ambiguous
  absence.
- Add exportable, privacy-safe diagnostic summaries for bug reports.
- Complete the contributor workflow, starter issues, and adapter documentation.

## Next: audible quality evidence

- Detect click/pop discontinuities and correlate them with queue clearing,
  provider chunk boundaries, and serializer frame boundaries.
- Add integrated loudness, loudness range, true peak, crest factor, and gain
  envelope observations with explicit window and channel contracts.
- Separate acoustic echo, caller speech, and false VAD/barge-in hypotheses.
- Add caller-side and remote-playout adapters without enabling sensitive-media
  retention by default.
- Expand deterministic synthetic fixtures for clipping, gaps, stalls, gain
  pumping, and truncation.

## Next: transport and packet proof

- Add raw pcap import and wire the existing RTP packet-tap/capture-health adapter
  to real deployment-specific receive paths and drop counters.
- Preserve direction, clock-rate, extended sequence, arrival cadence, and
  capture-drop evidence without persisting packet payloads.
- Add redacted SIP transaction and SDP-derived format metadata behind an
  explicit privacy boundary.
- Correlate verified packet gaps with AudioSocket media time and audible stage
  artifacts.
- Keep aggregate RTCP degradation separate from packet-level proof.

## Next: adapter ecosystem

- Provide a small conformance suite for `RealtimeProviderSession`.
- Add adapter examples for direct providers, Pipecat, and common telephony media
  streams.
- Normalize lifecycle differences without hiding provider-specific limitations.
- Keep provider credentials, raw errors, item IDs, and external URLs out of run
  records.
- Demonstrate that a new provider can be added without changing the core
  observation and timeline contracts.

See [docs/provider-adapter-guide.md](docs/provider-adapter-guide.md).

## Later: production hardening

- Validate multi-process Postgres workers under deployment failure and restart.
- Validate remote object storage, retention, and authenticated playback at
  deployment scale.
- Add operator authorization and audit boundaries for sensitive recordings.
- Define bounded retention and deletion workflows for runs and artifacts.
- Establish scale profiles and load tests for high-cardinality observations.
- Package deployment examples only after their security boundaries are explicit.

## Non-goals

VoxBench is not intended to become:

- a SIP server, PBX, or replacement for Asterisk;
- a Voice AI orchestration framework;
- a packet payload archive;
- a default recorder of personal conversations;
- a provider benchmark that ignores nondeterminism and sample size; or
- a system that turns missing telemetry into a passing health signal.

## Good contribution entry points

| Experience | Suggested work |
| --- | --- |
| First open-source contribution | Run provenance badge, copyable deep link, empty-lane guidance, docs |
| React/TypeScript | Timeline interaction, evidence coverage, stage listening, accessible visualization |
| Python | Observation adapters, safe failure aliases, CLI ergonomics, deterministic fixtures |
| Audio/DSP | Click/pop, loudness, true peak, gain envelope, echo and VAD evidence |
| VoIP/RTP | Packet tap, capture health, SIP metadata, RTCP interpretation |
| Voice AI providers | Provider lifecycle normalization and conformance tests |
| Operations | Postgres workers, storage, retention, authentication, deployment validation |

Before starting a substantial item, open or comment on an issue so the evidence
contract and privacy boundary can be agreed first.
