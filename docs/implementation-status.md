# VoxBench implementation status

Last audited: 2026-09-27. This inventory replaces dated pending lists as the entry
point for current implementation status. It does not turn a local candidate into
a published feature.

## Baseline and status vocabulary

The publication worktree baseline is `ae575b6` (`Detect click/pop discontinuities
with bounded PCM evidence`).
Use `git rev-parse HEAD` and `git status --short` to check whether that baseline
has changed; this document is an audit record, not a live Git query.

| Status | Meaning |
| --- | --- |
| Committed | Implementation exists in the audited Git baseline |
| Local candidate | Implementation exists locally, with verification recorded below, but is absent from the baseline |
| Planned | Design exists; runnable implementation is absent |
| Deployment validation pending | A code boundary exists, but the named deployment behavior has not been established by this audit |

Product decisions belong in `MEMORY.md`; architecture belongs in `DESIGN.md` and
feature designs. Dated progress entries and Serena implementation memories retain
their historical meaning. Never use an old percentage or pending list as a
current readiness assessment.

The framework-independent realtime/cascade plan is tracked by
[parent issue #15](https://github.com/engkimo/voxbench/issues/15) and the
[issue dependency plan](cascade-issue-plan.md). C1, C2 and C3A are local candidates;
other tasks remain planned. The
UI/SSE stabilization issue #16 is separate from cascade contract issue #17.

## Current inventory

| Area | Status | Evidence and remaining boundary |
| --- | --- | --- |
| Config/manifest schemas, overlays, resolved hashes, static validation | Committed | `schemas.py`, `registry/`; currently one `ai.provider/model`, v1 only |
| V2 realtime/cascade config and versioned capabilities | Local candidate | `schemas_v2.py`, `registry/v2.py`, `registry/config_views.py`, `test_registry_v2.py`; role/format/ID/authority checks, manifest pins, same-mode overlays; [contract guide](cascade-config-v2.md) |
| Framework-independent service event observation | Local candidate | `observability/service_events.py`, observer/HTTP batch extension and `test_service_observations.py`; v1/v2, persistence, causal aliases, privacy, idempotency/conflicts, bounded queues/run events; [HTTP contract](cascade-observation-api.md) |
| Cascade causal grouping and latency analysis | Local candidate | `observability/cascade_analysis.py`, safe run component projections and `test_cascade_analysis.py`; explicit parent chains, endpoint evidence, critical path, clock/missing states and opt-in SLOs; [analysis contract](cascade-analysis.md) |
| Harness WAV taps and OTel spans | Committed | `engine_harness/`; the default `run_once` generates nominal artifacts, not a real provider conversation |
| Signal invariants and synthetic full-reference scoring | Committed | `verification/`, `synthetic_caller/`; optional ViSQOL CLI, explicit blocked/unavailable states, aggregation/regression/calibration |
| Common timeline and deterministic incidents | Committed | `run_api.py`, `tests/test_timeline_stage_diagnostics.py`; five primitives, clock uncertainty, explicit observation boundaries |
| Stage playback, waveform, two-run comparison, deep links | Committed | `web/src/App.tsx`, `web/src/types.ts` |
| Observed-run library integration | Committed | `observability/observer.py`; framework-independent Python/HTTP path, existing applications keep pipeline ownership |
| Pipecat helper and other-framework adapters | Helper committed; dedicated adapters planned | `engine_harness/pipecat_adapter.py` only wraps pipeline construction; Pipecat is not a required package, other middleware uses application instrumentation today |
| RTP fixed-header tap and capture-health accounting | Committed | `RtpPacketTapAdapter`, observer and timeline tests; owner must supply actual drop counters |
| Caller VAD, local playback, barge-in and queue-discard evidence | Committed | `telephony/audiosocket.py`; local write/disposal is not remote audible playout |
| Asterisk AudioSocket and Gemini/OpenAI realtime adapters | Committed | `telephony/`, `realtime_providers/`; production timing/auth/TLS remain deployment work |
| Automatic per-call AMI RTCP collection and matched-condition tags | Committed | `ff65ada` / PR #8; aggregate evidence, one active local test call for safe attribution |
| Cross-session resource trend detector | Committed | `/runs/cross-session-trends`, `tests/test_cross_session_trends.py`; three or more ended runs on the same environment/server |
| Postgres run persistence and leased/fenced async worker | Committed | repository, job queue, worker, migrations; multi-process deployment soak remains pending |
| MinIO sink and bounded authenticated remote playback | Committed | storage/audio-session boundaries; remote audio cookie is not product-wide operator authentication |
| Typed UI-command resolver and Web dispatcher | Local candidate | Exact files below; nine command types, run/evidence target resolution and client idempotence |
| Deterministic diagnostic sessions, finite SSE replay and result acknowledgement | Local candidate | Exact files below; process-local store, one selected incident/event, no external model |
| Session/event/result persistence, model orchestration, diagnostic jobs/cancel, guided sequence | Planned | `diagnostic-agent-design.md`; not completed by the local SSE slice |
| Product-wide OIDC authentication, authorization, audit/retention | Planned | `diagnostic-agent-design.md`; required before production diagnostic integration |
| Cascade recording maps, inspector, test projects, adapters and runtime | Planned | `cascade-design.md`; causal analysis is local, while v2 audio chunks and execution remain explicitly unsupported. Next goal: matched direct/Pipecat fake Cascade projects for tuning and evaluation |
| Raw PCAP import, log/source snapshot connectors, second engine, scale profile | Planned | Designs/roadmap; not established by the RTP tap or second realtime provider |

## Local UI/SSE candidate audit

The local candidate is self-contained only when all these implementation files
are included. Four existing implementation files are modified:

- `src/voxbench/control_plane/run_api.py`
- `web/src/App.tsx`
- `web/src/styles.css`
- `web/src/types.ts`

Eight required implementation/test files are currently untracked:

- `src/voxbench/control_plane/ui_commands.py`
- `src/voxbench/control_plane/diagnostic_sessions.py`
- `tests/test_ui_commands.py`
- `tests/test_diagnostic_sessions.py`
- `web/src/AgentUiCommandPanel.tsx`
- `web/src/AgentDiagnosticPanel.tsx`
- `web/src/uiCommands.ts`
- `web/src/diagnosticSessions.ts`

Related documentation changes already exist in `MEMORY.md`, `README.md`, and
`docs/diagnostic-agent-design.md`. They must accompany stabilization. Current
tracked code imports untracked modules; copying only tracked changes would
produce an incomplete application.

Verified local endpoints:

```text
POST /runs/{run_id}/ui-commands/resolve
POST /runs/{run_id}/diagnostic-sessions
GET  /diagnostic-sessions/{session_id}
GET  /diagnostic-sessions/{session_id}/events
POST /diagnostic-sessions/{session_id}/ui-command-results
```

The deterministic planner selects the highest-severity incident, then the
earliest typed event, then an available panel. The submitted question is stored,
but does not change that selection. Session creation finishes the investigation
synchronously. SSE returns a finite snapshot of already-created events and
supports ordinal replay via `Last-Event-ID`/`after`; it is not a long-running
stream of newly arriving investigation events. The Web panel closes on
`completed` or stream error. Server replay support does not establish automatic
client reconnection. Acknowledgements are process-local and conflicting duplicate
results return `409`.

The local store has no session TTL/capacity eviction. Session references and
typed targets are validated, but product-wide operator authorization is absent.
Comparison currently checks run existence, not a production authorizer's grant.
This candidate cannot be described as a production diagnostic agent.

## Verification recorded on 2026-09-18

| Check | Result | Scope |
| --- | --- | --- |
| `.venv/bin/ruff check .` | Passed | Current local Python source |
| `.venv/bin/pytest -q` | 330 passed, 4 skipped | Python 3.14.3; four opt-in Postgres tests skipped; one Starlette/httpx deprecation warning |
| `npm --prefix web run build` | Passed | TypeScript and Vite production build |

The same checks also passed in a temporary checkout assembled from
`git archive ef7deec` plus exactly the twelve implementation/test files listed
above: 330 passed / 4 skipped, Ruff passed, Web build passed. Python packages and
Web dependencies were reused from the installed development environment; the
application source came from that isolated checkout. Personal files and other
untracked implementation files were not included. This establishes the candidate's
source-file completeness, not a clean dependency install or published feature.

This audit did not validate a browser interaction, actual STT/TTS/provider call,
new Postgres connectivity, remote MinIO deployment, OIDC login, or published CI.
Historical real-call evidence is not a substitute for those checks.

## Separate UI/SSE stabilization plan

1. Review the twelve implementation/test files as a separate existing change.
   Keep the documentation-consistency/cascade design change independently
   reviewable. Include every imported new module; exclude personal local files.
2. Isolated source-file verification is complete for the audited candidate:
   the exact twelve-file set passed Python checks and Web build on top of the
   committed baseline. Before publication, repeat with the final reviewed file
   set and freshly installed dependencies in the supported CI environment.
3. Check the UI path in a browser: incident/event fallback, shared cursor,
   scope rejection, duplicate command, playback/autoplay result, SSE replay and
   acknowledgement. Use synthetic evidence.
4. Record remaining runtime limits explicitly. Session capacity/TTL, Web races
   and reconnection, authorization, and durable diagnostic work each need their
   own acceptance criteria before production use.
5. Commit the reviewed change as its own unit when implementation stabilization
   is requested, then update this inventory with its commit and verification.
   C1 implementation has not committed or published the UI/SSE candidate.

Cascade contracts are implemented against the committed v1 contracts independently
of that step. The local UI dispatcher is an optional integration candidate with a
separate stabilization dependency, rather than an implicit released dependency.

## C1 config/capability local candidate (2026-09-18)

Scope: [#17](https://github.com/engkimo/voxbench/issues/17), implemented locally,
not committed or published. The issue remains open for review/publication.
[cascade-config-v2.md](cascade-config-v2.md) records the implemented contract.

Required new files:

- `src/voxbench/schemas_v2.py`
- `src/voxbench/registry/v2.py`
- `src/voxbench/registry/config_views.py`
- `tests/test_registry_v2.py`
- `tests/fixtures/configs/v1-baseline.resolved.json`
- `tests/fixtures/configs/v1-overlay.resolved.json`
- `schemas/config.v2.schema.json`, `schemas/manifest.v2.schema.json`
- The two config and eight manifest JSON documents under `examples/v2/`
- `docs/cascade-config-v2.md` and the current status/decision references

Required existing-source changes: `registry/service.py`, `schemas_export.py`,
`engine_harness/plan.py`, `engine_harness/harness.py`,
`synthetic_caller/offline.py`, `verification/core.py`, and only the v2 rejection
guard/import in `control_plane/run_api.py`. That API file also contains the
independent pre-existing UI/SSE candidate; publishing its entire working-tree
diff would combine scopes.

| Check | Final result | Scope |
| --- | --- | --- |
| Current-tree Ruff and full Python suite | Passed; 408 passed / 4 skipped | Includes the independent UI/SSE candidate and 78 C1 acceptance cases |
| Isolated `git archive ef7deec` plus C1 only | Ruff passed; 401 passed / 4 skipped | Excludes untracked UI/SSE modules/tests and all personal files; only C1 API guard was applied to the committed API source |
| V1 contract files | Byte-identical to HEAD | `schemas.py`, `config.schema.json`, `manifest.schema.json`; frozen baseline/overlay JSON and hashes pass |
| CLI examples | Realtime and cascade passed | Static resolution without middleware or provider calls; four/seven manifest pins respectively |
| Documentation links and diff whitespace | Passed | Current design/status/contract documents |

Both checkouts use Python 3.14.3 and existing installed dependencies; this is
source-completeness verification, not a fresh dependency install or published
CI. Four opt-in Postgres tests are skipped and one Starlette/httpx deprecation
warning remains. Stored-v1 roundtrip uses the SQLAlchemy repository with SQLite.
No browser or real provider call is needed for C1 and none was run. Web sources
are unchanged by this slice; their earlier build belongs to the UI/SSE audit.

At C1 completion, v2 supported static config resolution only. C2 now permits
observation-only run creation and safe typed service-event ingest. Legacy harness,
synthetic generation, audio verification and execution APIs still fail before
output. C3A now derives causal timing; recording mapping, UI, framework/provider
test projects/adapters and execution remain separate future slices. Validation does not establish live
provider/model readiness or complete observation coverage.

## C2 service-observation local candidate (2026-09-27)

Scope: [#18](https://github.com/engkimo/voxbench/issues/18), implemented locally,
not committed or published. The issue remains open for review/publication.
[cascade-observation-api.md](cascade-observation-api.md) is the normative HTTP,
privacy, identity and retry guide.

Required new files:

- `src/voxbench/observability/service_events.py`
- `tests/test_service_observations.py` (25 focused acceptance cases)
- `schemas/service-event.v1.schema.json`
- `docs/cascade-observation-api.md`

Required existing-source changes: `observability/observer.py` and `__init__.py`,
`schemas_export.py`, C2 endpoint/normalization changes in
`control_plane/run_api.py`, and the changed observation expectation in
`test_registry_v2.py`. The API file also contains the independent UI/SSE
candidate; the isolated verification applies only C1/C2 diff hunks to HEAD.

| Check | Final result | Scope |
| --- | --- | --- |
| Current-tree Ruff and full Python suite | Passed; 433 passed / 4 skipped | Includes UI/SSE, C1 and 25 C2 focused tests |
| Isolated `git archive ef7deec` plus C1+C2 | Ruff passed; 426 passed / 4 skipped | UI/SSE modules/tests and personal files excluded; imports resolve from isolated source |
| Plain HTTP and Python observer | Passed | Multi-turn/multi-segment cascade, v1/v2 realtime, no framework SDK |
| Persistence/reconstruction | Passed with SQLAlchemy SQLite | Safe event fields and derived missing-parent coverage survive repository reload |
| Privacy/identity/retry limits | Passed | Extra content/raw/cross-run fields fail; exact retry succeeds; conflict 409; collectors namespaced; combined batch/queue/run bounds |
| Service-event JSON Schema | Generated and validated | Standalone draft 2020-12 artifact matches exported model |

Both runs use Python 3.14.3 and existing installed dependencies; this remains
source-completeness verification rather than a fresh dependency install or
published CI. Four opt-in Postgres tests are skipped and the existing
Starlette/httpx deprecation warning remains. No provider call, browser run or
actual Pipecat/other-framework integration was performed. C2 normalizes observed
metadata; C3A now derives service latency, while audio segment mapping, remote
playout and provider readiness remain unestablished.

## C3A causal-analysis local candidate (2026-09-27)

Scope: [#19](https://github.com/engkimo/voxbench/issues/19), implemented locally,
not committed or published. The issue remains open for review/publication.
[cascade-analysis.md](cascade-analysis.md) records the measurement, clock,
critical-path, coverage and SLO contract.

Required new files:

- `src/voxbench/observability/cascade_analysis.py`
- `tests/test_cascade_analysis.py` (10 focused acceptance cases)
- `docs/cascade-analysis.md`

Required existing-source changes: two additional closed service boundaries and
`output_kind` validation in `observability/service_events.py`; C3 projections,
intervals and SLO incidents in `control_plane/run_api.py`; safe component
projection in `registry/config_views.py`; v2-only explicit latency SLO contracts
in `schemas_v2.py`; generated v2/service-event schemas; and an opt-in real
Postgres restart case in `test_postgres_integration.py`.

| Check | Final result | Scope |
| --- | --- | --- |
| Publication-tree Ruff and full Python suite | Passed; 444 passed / 5 skipped | `ae575b6` plus C1–C3; includes the committed click/pop coverage and 10 C3 focused tests |
| Isolated `git archive ef7deec` plus C1–C3 | Ruff passed; 436 passed / 5 skipped | UI/SSE modules/tests and personal files excluded; imports resolve from isolated source |
| Multi-segment critical path | Passed | End-to-end local write wait uses linked first segment; overlapping lifecycle durations are not added |
| Answer/tool/retry/epoch grouping | Passed | First output differs from first spoken answer; continuations are separate operations; old playback is not reused |
| Missing/clock evidence | Passed | Missing boundary is unobserved; unmatched parent stays explicit; cross-domain/negative time is indeterminate |
| Persistence/reconstruction | Passed with SQLAlchemy SQLite | Safe components, grouping, measurements and evidence refs reproduce after repository reload |
| Explicit latency SLO | Passed | Incident exists only for an observed applicable configured threshold; no default quality/latency inference |
| Real Postgres restart | Test added; skipped locally | Requires `VOXBENCH_TEST_POSTGRES_URL`; local run did not claim a Postgres deployment result |

Both runs use Python 3.12.3 and existing installed dependencies. Five opt-in
Postgres tests are skipped and the existing Starlette/httpx warning remains.
No provider call, browser run, Pipecat runtime or other framework was exercised.
The next user-designated goal is to build matched deterministic Cascade test
projects with and without Pipecat, feed both through this common contract, and
use VoxBench to compare, tune and evaluate them.
