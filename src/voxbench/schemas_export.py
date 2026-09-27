"""Export JSON Schema documents for Phase 0 contracts."""

from __future__ import annotations

import json
from pathlib import Path

from voxbench.observability.service_events import ServiceEvent
from voxbench.schemas import CapabilityManifest, VoiceConfig
from voxbench.schemas_v2 import CapabilityManifestV2, VoiceConfigV2


def export_schemas(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_schema(output_dir / "config.schema.json", VoiceConfig.model_json_schema())
    _write_schema(output_dir / "manifest.schema.json", CapabilityManifest.model_json_schema())
    _write_schema(output_dir / "config.v2.schema.json", VoiceConfigV2.model_json_schema())
    _write_schema(output_dir / "manifest.v2.schema.json", CapabilityManifestV2.model_json_schema())
    _write_schema(
        output_dir / "service-event.v1.schema.json",
        ServiceEvent.model_json_schema(),
    )


def _write_schema(path: Path, schema: dict[str, object]) -> None:
    path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    export_schemas(Path("schemas"))
