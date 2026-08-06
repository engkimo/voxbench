"""Safe observed-run payloads shared by live demo bridge entry points."""

from __future__ import annotations

import json
import re
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
EXPERIMENT_CONDITION_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def normalize_experiment_condition(value: str) -> str:
    """Return a safe, stable alias for a matched live-call condition."""

    normalized = value.strip().lower()
    if EXPERIMENT_CONDITION_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            "experiment_condition must be a 1-64 character lowercase alias using "
            "letters, digits, dot, underscore, or dash"
        )
    return normalized


def build_audiosocket_observed_run_payload(
    *,
    provider: LiveDemoProvider,
    call_id: str,
    target_rms: float,
    max_gain: float,
    noise_floor: float,
    mode: AudioSocketDemoMode = "loopback",
    model: str | None = None,
    experiment_condition: str | None = None,
) -> dict[str, Any]:
    config = deepcopy(_load_json(f"configs/live-demo-{provider}.json"))
    selected_model = model or str(config["spec"]["ai"]["model"])
    config["spec"]["ai"]["model"] = selected_model
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
    provider_note = f"{provider_note} Provider model: {selected_model}."
    tags = ["live-demo", "audiosocket", mode, provider]
    if experiment_condition is not None:
        experiment_condition = normalize_experiment_condition(experiment_condition)
        provider_note = (
            f"{provider_note} Experiment condition: {experiment_condition}."
        )
        tags.append(f"experiment-{experiment_condition}")
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
            "integration_target_alias": f"{provider}:{selected_model}:{mode}",
            "started_from": f"voxbench-audiosocket-{mode}",
            "operator_note": provider_note,
            "tags": tags,
            "secret_ref_names": (
                ["OPENAI_API_KEY"] if provider == "openai-realtime" else ["GOOGLE_API_KEY"]
            )
            if mode == "provider"
            else [],
        },
    }


def _load_json(relative_path: str) -> dict[str, Any]:
    return json.loads((EXAMPLE_ROOT / relative_path).read_text(encoding="utf-8"))
