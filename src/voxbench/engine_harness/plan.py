"""Resolved-config to harness plan conversion."""

from __future__ import annotations

from typing import Any

from voxbench.engine_harness.models import StagePlan


def build_stage_plan(resolved_config: dict[str, Any]) -> list[StagePlan]:
    stages = resolved_config["spec"]["media"]["pipeline"]
    return [
        StagePlan(
            stage=stage["type"],
            plugin=stage["plugin"],
            format=_stage_format(stage),
        )
        for stage in stages
    ]


def _stage_format(stage: dict[str, Any]) -> dict[str, Any]:
    io = stage.get("io")
    if io is not None:
        return dict(io.get("produces") or io.get("accepts") or {})

    params = stage.get("params", {})
    return {
        key: params[key]
        for key in ("encoding", "rate", "channels", "output_rate")
        if key in params
    }

