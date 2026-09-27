"""Read versioned resolved configs without rewriting stored v1 objects."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from voxbench.registry.errors import ConfigValidationError


def ai_mode(config: dict[str, Any]) -> str:
    return config["spec"]["ai"].get("mode", "realtime")


def service_components(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    ai = config["spec"]["ai"]
    if config.get("apiVersion", "voxbench/v1") == "voxbench/v1":
        component = deepcopy(ai)
        component["id"] = "ai"
        component["plugin"] = component.pop("provider")
        return {"realtime": component}
    roles = ("realtime",) if ai["mode"] == "realtime" else ("stt", "llm", "tts")
    return {role: deepcopy(ai[role]) for role in roles}


def safe_ai_components(config: dict[str, Any]) -> list[dict[str, str]]:
    """Return stable component identity without params, prompts, tools, or endpoints."""

    components = service_components(config)
    if config.get("apiVersion", "voxbench/v1") == "voxbench/v2":
        aggregation = config["spec"]["ai"].get("text_aggregation")
        if isinstance(aggregation, dict):
            components["aggregation"] = aggregation
    result: list[dict[str, str]] = []
    for role, component in components.items():
        item = {
            "role": role,
            "component_id": component["id"],
            "plugin": component["plugin"],
        }
        for name in ("manifest_version", "model"):
            value = component.get(name)
            if isinstance(value, str):
                item[name] = value
        result.append(item)
    roles = ("realtime", "stt", "llm", "aggregation", "tts")
    role_order = {role: index for index, role in enumerate(roles)}
    return sorted(result, key=lambda item: role_order.get(item["role"], len(role_order)))


def pipeline_chains(config: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    media = config["spec"]["media"]
    if config.get("apiVersion", "voxbench/v1") == "voxbench/v1":
        return {"legacy": deepcopy(media["pipeline"])}
    return {
        direction: deepcopy(media[f"{direction}_pipeline"])
        for direction in ("input", "output")
    }


def require_v1_workflow(config: dict[str, Any], *, operation: str) -> None:
    version = config.get("apiVersion", "voxbench/v1")
    if version != "voxbench/v1":
        raise ConfigValidationError(
            f"{operation} does not support {version} yet; v2 currently supports "
            "configuration resolution only"
        )
