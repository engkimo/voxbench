# VoxBench Roadmap

This roadmap describes direction, not delivery dates. Priorities may change as
real call evidence and contributor feedback reveal better abstractions.

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

See [PROGRESS.md](PROGRESS.md) for completed implementation slices.

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

- Add pcap and live RTP packet-tap adapters with explicit capture-health
  accounting.
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
