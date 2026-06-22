"""VoxBench Phase 0 CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from voxbench.registry.service import RegistryService

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """VoxBench Phase 0 command line tools."""


@app.command("resolve-config")
def resolve_config(
    config: Annotated[
        list[Path],
        typer.Option("--config", "-c", exists=True, file_okay=True, dir_okay=False),
    ],
    manifest: Annotated[
        list[Path],
        typer.Option("--manifest", "-m", exists=True, file_okay=True, dir_okay=False),
    ],
    name: Annotated[str | None, typer.Option("--name")] = None,
) -> None:
    """Resolve and validate a config, then print its hash and resolved JSON."""

    service = RegistryService.from_files(config_paths=config, manifest_paths=manifest)
    target = name or _single_config_name(config)
    resolved = service.resolve_config(target)
    typer.echo(f"hash: {resolved.hash}")
    typer.echo(json.dumps(resolved.resolved, indent=2, sort_keys=True))


def _single_config_name(config_paths: list[Path]) -> str:
    if len(config_paths) != 1:
        raise typer.BadParameter("--name is required when multiple --config files are provided")
    with config_paths[0].open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return str(raw["meta"]["name"])
