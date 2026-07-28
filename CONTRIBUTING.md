# Contributing to VoxBench

Thank you for helping make AI voice calls easier to understand and debug.
Contributions are welcome from telephony engineers, audio and signal-processing
practitioners, Voice AI builders, observability engineers, frontend developers,
technical writers, and first-time open-source contributors.

## Start with the product contract

VoxBench aligns evidence from an AI voice call on one common time axis so an
operator can move from an audible symptom to its likely cause. The most important
correctness rule is:

> Unobserved is not healthy, and correlation is not proof of remote playout.

Before making a large change, read:

- [README.md](README.md) for the runnable walkthrough
- [ROADMAP.md](ROADMAP.md) for current priorities and non-goals
- [DESIGN.md](DESIGN.md) for the architecture and evidence model
- [docs/library-integration.md](docs/library-integration.md) for observation
  boundaries
- [docs/provider-adapter-guide.md](docs/provider-adapter-guide.md) for provider
  integrations

## Ways to contribute

- Reproduce an audio-quality problem with a minimal, privacy-safe fixture.
- Improve the common-time-axis inspector or stage playback experience.
- Add a provider or telephony adapter behind the existing protocols.
- Add bounded audio-quality evidence such as click/pop, loudness, or true-peak
  observations.
- Improve RTP capture-health and packet evidence.
- Add tests, examples, diagrams, or troubleshooting notes.
- Review a pull request and test the change against a real Voice AI stack.

Use the
[good first issue](https://github.com/engkimo/voxbench/labels/good%20first%20issue)
label for bounded starter work and
[help wanted](https://github.com/engkimo/voxbench/labels/help%20wanted) for work
that benefits from domain experience.

## Before opening an issue

Search existing issues first. For a bug, include:

- the smallest safe reproduction;
- what you expected and what you observed;
- the exact VoxBench commit or version;
- Python, operating system, provider alias, codec, and sample rate;
- which evidence layers were actually connected; and
- whether the problem is audible in `resampler`, `agc`, `limiter`, or
  `serializer`.

Do not attach API keys, bearer tokens, database URLs with credentials, raw SIP
messages, SDP, packet payloads, caller identifiers, external service URLs, or
recordings containing personal data. Prefer synthetic audio and safe aliases.
Follow [SECURITY.md](SECURITY.md) for vulnerabilities.

## Claim an issue

Comment on an open issue before doing substantial work. Describe the slice you
intend to implement and any contract questions. This avoids duplicate work and
lets maintainers clarify evidence and privacy boundaries before code is written.

For an unlisted feature, open a feature request before a large implementation.
Small documentation and test corrections can go directly to a pull request.

## Development setup

VoxBench requires Python 3.12 or newer. Docker Desktop and Node.js/npm are needed
for the complete Postgres and Web workflow.

```bash
git clone https://github.com/engkimo/voxbench.git
cd voxbench
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev,postgres]"
npm --prefix web ci
```

Run the safe local diagnostic demo:

```bash
./scripts/dev-demo
```

It starts loopback-only Postgres, applies migrations, starts the API and Web UI,
creates a three-second synthetic run, and prints the selected run URL. No
provider key or Asterisk installation is required.

Provider-backed work needs the optional live dependencies:

```bash
python -m pip install -e ".[dev,postgres,live]"
```

Keep provider credentials in environment variables. Never add them to fixtures,
configs, screenshots, logs, or commits.

## Run the checks

Run the Python checks from the repository root:

```bash
ruff check .
pytest
```

Run the Web production build:

```bash
npm --prefix web run build
```

Postgres integration tests are opt-in and require an isolated disposable
database:

```bash
export VOXBENCH_TEST_POSTGRES_URL='postgresql+psycopg://user:password@127.0.0.1:55432/voxbench_test'
pytest -m postgres_integration
```

Never point the integration test variable at a production or shared database.

## Architecture entry points

| Area | Start here |
| --- | --- |
| Provider sessions | `src/voxbench/realtime_providers/providers.py` |
| AudioSocket and barge-in | `src/voxbench/telephony/audiosocket.py` |
| RTP/RTCP collection | `src/voxbench/telephony/ami_rtcp.py` |
| Observation library | `src/voxbench/observability/observer.py` |
| Timeline projection and incidents | `src/voxbench/control_plane/run_api.py` |
| Persistent run and job state | `src/voxbench/control_plane/` |
| Web inspector | `web/src/App.tsx`, `web/src/styles.css`, `web/src/types.ts` |
| Schemas and manifests | `schemas/`, `src/voxbench/schemas.py` |
| Verification | `src/voxbench/verification/` |

## Engineering rules

### Preserve evidence semantics

- Record what was observed, not what probably happened.
- Use explicit states such as `unobserved`, `unknown`, or `not_applicable`.
- Keep confidence and capture-health boundaries with derived incidents.
- Do not label aggregate RTCP loss as a verified RTP sequence gap.
- Do not claim caller-side playout unless that boundary was observed.
- Keep timestamps and media-time positions distinct.

### Keep observations bounded and privacy-safe

- Never persist raw provider frames, RTP payloads, SIP bodies, or SDP by default.
- Store safe aliases instead of hostnames, external URLs, channel IDs, or
  provider item IDs.
- Bound collections, payload sizes, retry counts, and timeouts.
- Classify external failures into safe reason aliases; do not return raw provider
  exceptions.

### Protect realtime paths

- Do not perform blocking I/O in audio callbacks or paced media writes.
- Flush observation batches outside the audio write path.
- Preserve the 20 ms AudioSocket pacing contract unless a change explicitly
  revises it with tests.
- Add deterministic tests for queue disposal, partial frames, lifecycle events,
  and failure cleanup.

### Maintain library compatibility

The `voxbench.observability` and provider protocols are library boundaries.
Prefer additive changes. If a breaking change is unavoidable, explain the
migration in the issue and pull request and update every reference and example.

## Pull request expectations

Keep pull requests focused. Include:

- the problem and why it matters;
- the observed/expected contract;
- implementation notes;
- tests and commands run;
- screenshots for visible Web changes;
- privacy and compatibility impact; and
- the issue closed by the change, when applicable.

Update documentation when behavior, environment variables, CLI flags, schemas,
or evidence meanings change. Do not mix unrelated formatting or refactoring into
a functional change.

By contributing, you agree that source-code contributions are licensed under
Apache-2.0 and documentation contributions are licensed under MIT, matching the
repository's [license statement](README.md#license).

## Community

Be respectful and constructive. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
Questions that may expose a vulnerability or sensitive call data must use the
private route in [SECURITY.md](SECURITY.md), not a public issue.
