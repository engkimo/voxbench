# Security Policy

## Supported versions

VoxBench is pre-1.0 software. Security fixes are applied to the latest `main`
branch. Older commits, local demo credentials, and example deployment files are
not maintained as supported releases.

The Asterisk, Postgres, and Web demo configurations bind services to loopback and
use intentionally low-value development credentials. They are not production
deployment templates.

## Report a vulnerability privately

Do not open a public issue for a suspected vulnerability. Use GitHub's private
vulnerability reporting page:

<https://github.com/engkimo/voxbench/security/advisories/new>

If private vulnerability reporting is unavailable, contact the maintainer
through a private contact method listed on the maintainer's GitHub profile. Do
not put exploit details or sensitive call data in a public request for contact.

Include:

- affected commit or version;
- impacted component and deployment shape;
- reproduction steps using synthetic or redacted data;
- security impact;
- whether credentials, call media, metadata, or external services are involved;
  and
- a proposed mitigation, if known.

Do not send real API keys, bearer tokens, database passwords, private caller
audio, transcripts, raw SIP/SDP, or packet payloads. Use placeholders and safe
aliases.

## Security-sensitive areas

Reports are especially useful for:

- remote recording authorization and session cookies;
- path traversal or object-storage key handling;
- secret leakage through errors, readiness, telemetry, or Web responses;
- unbounded observation ingestion or denial of service;
- Postgres job lease and fenced-commit bypasses;
- unsafe deserialization or schema bypasses;
- provider or AMI credential exposure;
- unintended network exposure in local deployment examples; and
- retention of call content contrary to the documented evidence boundary.

## Response and disclosure

This community project has no response-time SLA. Maintainers will validate the
report, establish a private remediation plan, and coordinate disclosure when
practical. Please allow time for a fix before public disclosure.

Security fixes should include regression tests and documentation without
embedding exploit secrets or private data.
