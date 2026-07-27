# Local Asterisk for VoxBench

This development-only container gives a macOS SIP softphone one authenticated
PJSIP account and routes extension `7000` to the VoxBench AudioSocket bridge.
SIP, RTP, and AMI are published on macOS loopback only.

From the repository root:

```bash
./scripts/asterisk-local up
```

The command builds Asterisk, waits for PJSIP and AudioSocket to be ready, then
prints every value required by the macOS Telephone app. The default local-only
values are:

| Telephone field | Value |
| --- | --- |
| Account / description | `VoxBench` |
| Full name / display name | `VoxBench 6001` |
| Domain / SIP server | `127.0.0.1` |
| Port | `5060` |
| User name | `6001` |
| Authorization user | `6001` |
| Password | `voxbench-6001-local-only` |
| Transport | `UDP` |
| Outbound proxy | blank |
| STUN | off |
| Preferred codec | `PCMU` / G.711 mu-law |

Start the VoxBench bridge in another terminal, using the actual Control Plane
port:

```bash
voxbench audiosocket-loopback \
  --control-plane-url http://127.0.0.1:8001
```

When it prints `Listening for Asterisk AudioSocket on 127.0.0.1:9019`, call
`7000` in Telephone. Speak for several seconds; the processed audio should come
back to the call and the bridge prints the correlated VoxBench run ID.

The command above is an audio loopback and does not contact an AI provider. To
talk to Gemini Live instead, set the key only in the current shell and run the
checked realtime launcher:

```fish
set -gx GOOGLE_API_KEY 'your-key'
./scripts/asterisk-local gemini
```

For zsh/bash, use `export GOOGLE_API_KEY='your-key'`. The launcher verifies
Asterisk, the Control Plane, the Gemini SDK, and the key before starting. It
performs a no-audio Live connection preflight so an invalid key, unavailable
model, quota limit, or permission failure is reported before Telephone places a
call. The current pinned model is `gemini-3.1-flash-live-preview`. The launcher
does not print or store the key.

Useful operations:

```bash
./scripts/asterisk-local settings
./scripts/asterisk-local status
./scripts/asterisk-local logs
./scripts/asterisk-local cli
./scripts/asterisk-local gemini
./scripts/asterisk-local down
```

The optional RTCP collector can connect to AMI at `127.0.0.1:5038` with user
`voxbench-rtcp` and default password `voxbench-ami-local-only`.

Override development credentials before `up` when needed:

```bash
export VOXBENCH_SIP_PASSWORD='choose-a-local-value'
export VOXBENCH_AMI_PASSWORD='choose-another-local-value'
./scripts/asterisk-local up
```

These defaults are intentionally low-value development credentials. The
container is loopback-only and is not a production Asterisk configuration:
there is no SIP TLS, SRTP, rate limiting, or Internet exposure.
