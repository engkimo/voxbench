"""VoxBench Phase 0 CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from voxbench.live_demo.observed_run import (
    LiveDemoProvider,
    build_audiosocket_observed_run_payload,
)
from voxbench.observability import HttpObservationTransport, VoxBenchObserver
from voxbench.realtime_providers import (
    GeminiLiveProvider,
    OpenAIRealtimeProvider,
    ProviderConnectionError,
    connect_with_retry,
)
from voxbench.registry.service import RegistryService
from voxbench.telephony import (
    AudioSocketLoopbackServer,
    AudioSocketRealtimeServer,
    LoopbackCallSession,
    RealtimeCallSession,
)

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


@app.command("audiosocket-loopback")
def audiosocket_loopback(
    control_plane_url: Annotated[
        str,
        typer.Option("--control-plane-url"),
    ] = "http://127.0.0.1:8000",
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 9019,
    provider: Annotated[
        LiveDemoProvider,
        typer.Option("--provider"),
    ] = "openai-realtime",
    target_rms: Annotated[float, typer.Option("--target-rms", min=1.0)] = 3000.0,
    max_gain: Annotated[float, typer.Option("--max-gain", min=0.01)] = 8.0,
    noise_floor: Annotated[float, typer.Option("--noise-floor", min=0.0)] = 200.0,
) -> None:
    """Echo Asterisk AudioSocket PCM through observed AGC/limiter stages."""

    transport = HttpObservationTransport(control_plane_url)

    async def create_session(call_uuid: UUID) -> LoopbackCallSession:
        call_id = str(call_uuid)
        payload = build_audiosocket_observed_run_payload(
            provider=provider,
            call_id=call_id,
            target_rms=target_rms,
            max_gain=max_gain,
            noise_floor=noise_floor,
        )
        run = await asyncio.to_thread(transport.start_run, payload)
        typer.echo(f"AudioSocket call {call_id} -> VoxBench run {run['run_id']}")
        observer = VoxBenchObserver(run["run_id"], transport)
        return LoopbackCallSession(
            call_id=call_id,
            observer=observer,
            complete_run=lambda: transport.complete_run(run["run_id"]),
            target_rms=target_rms,
            max_gain=max_gain,
            noise_floor=noise_floor,
        )

    server = AudioSocketLoopbackServer(
        session_factory=create_session,
        host=host,
        port=port,
    )
    typer.echo(f"Listening for Asterisk AudioSocket on {host}:{port}")
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        typer.echo("AudioSocket loopback stopped")


@app.command("audiosocket-realtime")
def audiosocket_realtime(
    control_plane_url: Annotated[
        str,
        typer.Option("--control-plane-url"),
    ] = "http://127.0.0.1:8000",
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 9019,
    provider: Annotated[
        LiveDemoProvider,
        typer.Option("--provider"),
    ] = "openai-realtime",
    target_rms: Annotated[float, typer.Option("--target-rms", min=1.0)] = 3000.0,
    max_gain: Annotated[float, typer.Option("--max-gain", min=0.01)] = 8.0,
    noise_floor: Annotated[float, typer.Option("--noise-floor", min=0.0)] = 200.0,
    connect_attempts: Annotated[
        int,
        typer.Option("--connect-attempts", min=1, max=10),
    ] = 3,
    connect_backoff_seconds: Annotated[
        float,
        typer.Option("--connect-backoff-seconds", min=0.0, max=30.0),
    ] = 0.5,
) -> None:
    """Bridge Asterisk AudioSocket PCM to a realtime AI provider."""

    provider_adapter = (
        OpenAIRealtimeProvider() if provider == "openai-realtime" else GeminiLiveProvider()
    )
    readiness = provider_adapter.readiness(dry_run=False)
    if not readiness.ready:
        env_vars = ", ".join((readiness.env_var, *readiness.alternate_env_vars))
        detail = f"set {env_vars} and install the live extra: pip install -e '.[live]'"
        raise typer.BadParameter(detail)

    transport = HttpObservationTransport(control_plane_url)

    async def create_session(call_uuid: UUID) -> RealtimeCallSession:
        call_id = str(call_uuid)
        payload = build_audiosocket_observed_run_payload(
            provider=provider,
            call_id=call_id,
            target_rms=target_rms,
            max_gain=max_gain,
            noise_floor=noise_floor,
            mode="provider",
        )
        run = await asyncio.to_thread(transport.start_run, payload)
        observer = VoxBenchObserver(run["run_id"], transport)

        def report_retry(failed_attempt: int, delay: float) -> None:
            typer.echo(
                f"Provider connection attempt {failed_attempt}/{connect_attempts} "
                f"failed; retrying in {delay:.2f}s"
            )

        try:
            connection = await connect_with_retry(
                provider_adapter,
                attempts=connect_attempts,
                initial_backoff_seconds=connect_backoff_seconds,
                on_retry=report_retry,
            )
        except ProviderConnectionError as exc:
            observer.observe_metric("provider_connect_attempts", float(exc.attempts))
            observer.observe_metric("provider_connect_retries", float(exc.attempts - 1))
            observer.observe_metric("provider_connect_failures", float(exc.attempts))
            observer.observe_metric("provider_connect_exhausted", 1.0)
            try:
                await asyncio.to_thread(observer.flush)
            finally:
                await asyncio.to_thread(
                    transport.fail_run,
                    run["run_id"],
                    "provider-connect-error",
                )
            raise

        observer.observe_metric(
            "provider_connect_attempts",
            float(connection.attempts),
        )
        observer.observe_metric(
            "provider_connect_retries",
            float(connection.attempts - 1),
        )
        if connection.attempts > 1:
            observer.observe_metric(
                "provider_connect_failures",
                float(connection.attempts - 1),
            )
        typer.echo(f"AudioSocket call {call_id} -> {provider} -> run {run['run_id']}")
        return RealtimeCallSession(
            call_id=call_id,
            observer=observer,
            provider_session=connection.session,
            complete_run=lambda: transport.complete_run(run["run_id"]),
            fail_run=lambda failure_alias: transport.fail_run(
                run["run_id"],
                failure_alias,
            ),
            target_rms=target_rms,
            max_gain=max_gain,
            noise_floor=noise_floor,
        )

    server = AudioSocketRealtimeServer(
        session_factory=create_session,
        host=host,
        port=port,
    )
    typer.echo(f"Listening for {provider} AudioSocket calls on {host}:{port}")
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        typer.echo("AudioSocket realtime bridge stopped")


def _single_config_name(config_paths: list[Path]) -> str:
    if len(config_paths) != 1:
        raise typer.BadParameter("--name is required when multiple --config files are provided")
    with config_paths[0].open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return str(raw["meta"]["name"])
