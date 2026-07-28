## Summary

<!-- What does this change, and why does it matter? -->

Closes #

## Evidence contract

<!-- What is directly observed? What is derived or inferred? What remains unobserved? -->

- Observed:
- Derived/inferred:
- Explicitly unobserved:

## Changes

-

## Validation

<!-- Include exact commands and relevant manual workflows. -->

- [ ] `ruff check .`
- [ ] `pytest`
- [ ] `npm --prefix web run build` when Web code changes
- [ ] Relevant manual or integration workflow

## Privacy, security, and compatibility

- [ ] No credentials, raw SIP/SDP, packet payloads, caller identifiers, or private recordings are included.
- [ ] New observations and collections are bounded.
- [ ] External failures use safe aliases rather than raw exception messages.
- [ ] Realtime audio paths do not perform blocking observation I/O.
- [ ] Library and schema compatibility is preserved, or the migration is documented.

## UI evidence

<!-- Add redacted screenshots for visible changes. Remove this section otherwise. -->
