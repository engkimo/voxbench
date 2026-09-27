# Cascade and framework-independent issue plan

Recorded 2026-09-18. GitHub issues were created using the verified `engkimo`
connector account. Issue publication is complete. C1–C3A are committed on
[Draft PR #25](https://github.com/engkimo/voxbench/pull/25), pending review and
merge; this does not make them available on `main`.

Parent: [#15: Support framework-independent realtime and STT → LLM → TTS diagnostics](https://github.com/engkimo/voxbench/issues/15)

## Agreed direction

Support both integrated realtime and independent STT → LLM → TTS, using Pipecat,
other frameworks/middleware, or direct SDK/HTTP/WebSocket application code.
Observation support comes first, optional test-call execution follows. Core
contracts and analysis must not require framework SDKs or frame/context types.

Current boundaries and verification: [implementation-status.md](implementation-status.md).
Contract details and fixtures: [cascade-design.md](cascade-design.md).
The detailed design is currently local; public issue bodies are self-contained.

## Issue inventory and prerequisites

| Slice | Issue | Scope | Prerequisites |
| --- | --- | --- | --- |
| S0 | [#16](https://github.com/engkimo/voxbench/issues/16) | Stabilize the typed diagnostic UI/SSE candidate as a complete change | Independent |
| C1 | [#17](https://github.com/engkimo/voxbench/issues/17) | Add versioned realtime/cascade configs and role-specific service manifests | Independent |
| C2 | [#18](https://github.com/engkimo/voxbench/issues/18) | Add metadata-only cascade service observations with causal identities | #17 |
| C3A | [#19](https://github.com/engkimo/voxbench/issues/19) | Derive cascade latency from explicitly correlated service boundaries | #18 |
| C3B | [#20](https://github.com/engkimo/voxbench/issues/20) | Map cascade recording segments to run time and verify audio chains independently | #17, #18 |
| C4 | [#21](https://github.com/engkimo/voxbench/issues/21) | Show cascade components, causal waits and evidence coverage in the call inspector | #19, #20 |
| C5A | [#22](https://github.com/engkimo/voxbench/issues/22) | Add a no-middleware cascade example and shared observation conformance suite | #18 |
| C5B | [#23](https://github.com/engkimo/voxbench/issues/23) | Add optional framework adapters that share the direct cascade observation contract | #22 |
| C6 | [#24](https://github.com/engkimo/voxbench/issues/24) | Add a framework-independent runtime launcher for cascade test calls | #17, #18, #22 |

S0 stabilizes the existing uncommitted UI/SSE candidate. It is an additional
prerequisite only if C4 integrates that optional agent dispatcher; C1/common
observation can proceed independently.

## First work and remaining choices

C1/#17, C2/#18 and C3A/#19 are review branch candidates in Draft PR #25. See
[the v2 config contract](cascade-config-v2.md) and
[the service observation contract](cascade-observation-api.md),
[the causal analysis contract](cascade-analysis.md), and
[the implementation inventory](implementation-status.md) for checks and precise
scope. V2 execution and audio recording ingestion remain planned; the GitHub
issues remain open until review and merge. S0 and all other slices retain their
previous status.

Start with #17 for versioned schemas/registry compatibility; develop
#16 independently to stabilize the diagnostic UI candidate. Then
#18 establishes the common metadata-only service observation path; #19 derives
endpoint-backed latency and critical paths from it.

The next user-designated goal is to create two matched deterministic fake
Cascade test projects, one without middleware and one with Pipecat, then ingest
both into VoxBench for tuning and comparison. This advances #22 and the Pipecat
portion of #23 after #19; it does not select real providers or make Pipecat a
required core dependency.

Exact provider/model combinations, deployment regions and the second framework
are not selected. These choices belong to adapter/live validation and do not
block common contracts or framework-free fake fixtures.

Existing packet ingestion and loudness issues remain separate; this plan does not
claim to complete them or silently expand their scope.
