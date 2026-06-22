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

This implementation intentionally does not include the Phase 2 verification engine,
Synthetic Caller, timeline UI, live monitoring, or scale profile.

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

## Run the Phase 1 API slice

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

## Verification

```bash
ruff check .
pytest
```
