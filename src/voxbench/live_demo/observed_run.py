"""Safe observed-run payloads shared by live demo bridge entry points."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

LiveDemoProvider = Literal["openai-realtime", "gemini-live"]
AudioSocketDemoMode = Literal["loopback", "provider"]

EXAMPLE_ROOT = Path(__file__).resolve().parents[3] / "examples"
PROCESSOR_MANIFESTS = (
    "manifests/engine/asterisk.json",
    "manifests/processor/resampler.json",
    "manifests/processor/agc.json",
    "manifests/processor/limiter.json",
    "manifests/processor/serializer.json",
)


def build_audiosocket_observed_run_payload(
    *,
    provider: LiveDemoProvider,
    call_id: str,
    target_rms: float,
    max_gain: float,
    noise_floor: float,
    mode: AudioSocketDemoMode = "loopback",
) -> dict[str, Any]:
    config = deepcopy(_load_json(f"configs/live-demo-{provider}.json"))
    config["spec"]["engine"]["params"].update(
        {
            "websocket_url": "alias:local-asterisk-audiosocket",
            "media_profile": "8khz-pcm16-audiosocket",
        }
    )
    for stage in config["spec"]["media"]["pipeline"]:
        if stage["type"] == "agc":
            stage["params"].update(
                {
                    "target_rms": target_rms,
                    "max_gain": max_gain,
                    "noise_floor": noise_floor,
                }
            )
        elif stage["type"] == "serializer":
            stage["params"]["encoding"] = "pcm16"

    provider_note = (
        "Local PCM loopback; provider network session is not opened."
        if mode == "loopback"
        else "Bidirectional AudioSocket media connected to the selected provider."
    )
    return {
        "config_name": config["meta"]["name"],
        "configs": [config],
        "manifests": [
            _load_json(f"manifests/provider/{provider}.json"),
            *[_load_json(path) for path in PROCESSOR_MANIFESTS],
        ],
        "call_id": call_id,
        "environment": {
            "environment_profile": "demo",
            "server_alias": "local-audiosocket-bridge",
            "integration_target_alias": f"{provider}-{mode}",
            "started_from": f"voxbench-audiosocket-{mode}",
            "operator_note": provider_note,
            "tags": ["live-demo", "audiosocket", mode, provider],
            "secret_ref_names": (
                ["OPENAI_API_KEY"] if provider == "openai-realtime" else ["GOOGLE_API_KEY"]
            )
            if mode == "provider"
            else [],
        },
    }


def _load_json(relative_path: str) -> dict[str, Any]:
    return json.loads((EXAMPLE_ROOT / relative_path).read_text(encoding="utf-8"))
