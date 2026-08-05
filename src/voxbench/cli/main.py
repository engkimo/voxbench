"""VoxBench Phase 0 CLI."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Annotated
from uuid import UUID

import typer

from voxbench.live_demo.observed_run import (
    LiveDemoProvider,
    build_audiosocket_observed_run_payload,
    normalize_experiment_condition,
)
from voxbench.observability import HttpObservationTransport, VoxBenchObserver
from voxbench.realtime_providers import (
    GeminiLiveProvider,
    OpenAIRealtimeProvider,
    ProviderConnectionError,
    connect_with_retry,
)
from voxbench.registry.service import RegistryService
from voxbench.synthetic_caller import (
    SyntheticAudioSpec,
    run_synthetic_treatment,
    run_synthetic_verification,
    write_synthetic_treatment_report,
    write_synthetic_verification_report,
)
from voxbench.telephony import (
    AmiError,
    AmiRtcpCollector,
    AudioSocketLoopbackServer,
    AudioSocketRealtimeServer,
    LoopbackCallSession,
    RealtimeCallSession,
)
from voxbench.verification import (
    FullReferenceRegressionPolicy,
    FullReferenceSelection,
    VisqolCliScorer,
    VisqolMode,
    analyze_full_reference_repeatability,
    build_visqol_candidate,
    compare_full_reference_treatments,
    load_full_reference_treatment_report,
    score_full_reference_selection,
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
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Provider model ID; defaults to the selected adapter's pinned model.",
        ),
    ] = None,
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
    experiment_condition: Annotated[
        str | None,
        typer.Option(
            "--experiment-condition",
            help="Safe alias recorded on each run, such as no-interruption.",
        ),
    ] = None,
    collect_rtcp: Annotated[
        bool,
        typer.Option(
            "--collect-rtcp",
            help="Attach aggregate Asterisk AMI RTCP observations to each call run.",
        ),
    ] = False,
    ami_host: Annotated[str, typer.Option("--ami-host")] = "127.0.0.1",
    ami_port: Annotated[int, typer.Option("--ami-port", min=1, max=65535)] = 5038,
    ami_clock_rate_hz: Annotated[
        int,
        typer.Option("--ami-clock-rate-hz", min=1, max=384_000),
    ] = 8_000,
    ami_username_env: Annotated[
        str,
        typer.Option("--ami-username-env"),
    ] = "VOXBENCH_AMI_USERNAME",
    ami_secret_env: Annotated[
        str,
        typer.Option("--ami-secret-env"),
    ] = "VOXBENCH_AMI_SECRET",
) -> None:
    """Bridge Asterisk AudioSocket PCM to a realtime AI provider."""

    provider_adapter = (
        OpenAIRealtimeProvider(**({"model": model} if model is not None else {}))
        if provider == "openai-realtime"
        else GeminiLiveProvider(**({"model": model} if model is not None else {}))
    )
    selected_model = provider_adapter.model
    readiness = provider_adapter.readiness(dry_run=False)
    if not readiness.ready:
        env_vars = ", ".join((readiness.env_var, *readiness.alternate_env_vars))
        detail = f"set {env_vars} and install the live extra: pip install -e '.[live]'"
        raise typer.BadParameter(detail)

    if experiment_condition is not None:
        try:
            experiment_condition = normalize_experiment_condition(experiment_condition)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None

    ami_username: str | None = None
    ami_secret: str | None = None
    if collect_rtcp:
        ami_username = os.environ.get(ami_username_env)
        ami_secret = os.environ.get(ami_secret_env)
        missing = [
            name
            for name, value in (
                (ami_username_env, ami_username),
                (ami_secret_env, ami_secret),
            )
            if not value
        ]
        if missing:
            raise typer.BadParameter(
                f"set required environment variable(s): {', '.join(missing)}"
            )

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
            model=selected_model,
            experiment_condition=experiment_condition,
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
            typer.echo(
                f"Provider connection exhausted: {exc.reason_alias} "
                f"({exc.error_type})",
                err=True,
            )
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
        await asyncio.to_thread(observer.flush)
        typer.echo(
            f"AudioSocket call {call_id} -> {provider}/{selected_model} "
            f"-> run {run['run_id']}"
        )
        if experiment_condition is not None:
            typer.echo(f"Experiment condition: {experiment_condition}")
        background_tasks: tuple[asyncio.Task[object], ...] = ()
        if collect_rtcp:
            assert ami_username is not None
            assert ami_secret is not None
            rtcp_observer = VoxBenchObserver(run["run_id"], transport)
            collector = AmiRtcpCollector(
                host=ami_host,
                port=ami_port,
                username=ami_username,
                secret=ami_secret,
                clock_rate_hz=ami_clock_rate_hz,
            )

            async def collect_call_rtcp() -> None:
                try:
                    await collector.collect(rtcp_observer)
                except asyncio.CancelledError:
                    raise
                except AmiError as exc:
                    rtcp_observer.observe_metric("asterisk_ami_rtcp_failures", 1.0)
                    with suppress(Exception):
                        await asyncio.to_thread(rtcp_observer.flush)
                    typer.echo(
                        f"Asterisk RTCP collection failed for run {run['run_id']}: {exc}",
                        err=True,
                    )

            background_tasks = (asyncio.create_task(collect_call_rtcp()),)
            typer.echo(
                f"Asterisk RTCP collection attached to run {run['run_id']} "
                f"on {ami_host}:{ami_port}"
            )
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
            background_tasks=background_tasks,
        )

    server = AudioSocketRealtimeServer(
        session_factory=create_session,
        host=host,
        port=port,
        on_failure=lambda reason_alias, error_type: typer.echo(
            f"Provider session failed: {reason_alias} ({error_type})",
            err=True,
        ),
    )
    typer.echo(
        f"Listening for {provider}/{selected_model} AudioSocket calls on {host}:{port}"
    )
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        typer.echo("AudioSocket realtime bridge stopped")


@app.command("gemini-live-preflight")
def gemini_live_preflight(
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Gemini Live model ID; defaults to the adapter's current pinned model.",
        ),
    ] = None,
) -> None:
    """Validate Gemini credentials and Live model access without sending audio."""

    provider = GeminiLiveProvider(**({"model": model} if model is not None else {}))
    readiness = provider.readiness(dry_run=False)
    if not readiness.ready:
        env_vars = ", ".join((readiness.env_var, *readiness.alternate_env_vars))
        detail = f"set {env_vars} and install the live extra: pip install -e '.[live]'"
        raise typer.BadParameter(detail)

    async def preflight() -> None:
        connection = await connect_with_retry(provider, attempts=1)
        await connection.session.close()

    try:
        asyncio.run(preflight())
    except ProviderConnectionError as exc:
        typer.echo(
            f"Gemini Live preflight failed: {exc.reason_alias} ({exc.error_type})",
            err=True,
        )
        raise typer.Exit(code=2) from None

    typer.echo(f"Gemini Live preflight passed: {provider.model}")


@app.command("asterisk-ami-rtcp")
def asterisk_ami_rtcp(
    run_id: Annotated[str, typer.Option("--run-id")],
    control_plane_url: Annotated[
        str,
        typer.Option("--control-plane-url"),
    ] = "http://127.0.0.1:8000",
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 5038,
    clock_rate_hz: Annotated[
        int,
        typer.Option("--clock-rate-hz", min=1, max=384_000),
    ] = 8_000,
    username_env: Annotated[
        str,
        typer.Option("--username-env"),
    ] = "VOXBENCH_AMI_USERNAME",
    secret_env: Annotated[
        str,
        typer.Option("--secret-env"),
    ] = "VOXBENCH_AMI_SECRET",
) -> None:
    """Collect aggregate RTCP quality from a read-only Asterisk AMI account."""

    username = os.environ.get(username_env)
    secret = os.environ.get(secret_env)
    missing = [
        name for name, value in ((username_env, username), (secret_env, secret)) if not value
    ]
    if missing:
        raise typer.BadParameter(f"set required environment variable(s): {', '.join(missing)}")

    transport = HttpObservationTransport(control_plane_url)
    observer = VoxBenchObserver(run_id, transport)
    collector = AmiRtcpCollector(
        host=host,
        port=port,
        username=username,
        secret=secret,
        clock_rate_hz=clock_rate_hz,
    )
    typer.echo(f"Collecting Asterisk RTCP quality on {host}:{port} for run {run_id}")
    try:
        asyncio.run(collector.collect(observer))
    except KeyboardInterrupt:
        typer.echo("Asterisk RTCP collection stopped")
    except AmiError as exc:
        observer.observe_metric("asterisk_ami_rtcp_failures", 1.0)
        with suppress(Exception):
            observer.flush()
        typer.echo(f"Asterisk RTCP collection failed: {exc}", err=True)
        raise typer.Exit(code=1) from None


@app.command("visqol-score")
def visqol_score(
    reference: Annotated[
        Path,
        typer.Option("--reference", exists=True, file_okay=True, dir_okay=False),
    ],
    degraded: Annotated[
        Path,
        typer.Option("--degraded", exists=True, file_okay=True, dir_okay=False),
    ],
    binary: Annotated[Path, typer.Option("--binary")],
    mode: Annotated[VisqolMode, typer.Option("--mode")] = "speech",
    stage: Annotated[str, typer.Option("--stage")] = "full-reference",
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout-seconds", min=0.1, max=600.0),
    ] = 120.0,
) -> None:
    """Score matching local WAVs with an optional official ViSQOL binary."""

    try:
        candidate = build_visqol_candidate(
            stage=stage,
            reference_path=reference,
            degraded_path=degraded,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(
            "reference and degraded must be matching uncompressed mono PCM16 WAV files"
        ) from exc
    report = score_full_reference_selection(
        FullReferenceSelection(candidates=(candidate,), blocked=()),
        VisqolCliScorer(
            binary=binary,
            mode=mode,
            timeout_seconds=timeout_seconds,
        ),
    )
    result = report.results[0]
    typer.echo(
        json.dumps(
            {
                "metric_name": result.metric_name,
                "reason_alias": result.reason_alias,
                "score": result.score,
                "scorer": result.scorer,
                "stage": result.stage,
                "state": result.state,
                "transformations": list(result.transformations),
            },
            sort_keys=True,
        )
    )
    if result.state == "unavailable":
        raise typer.Exit(code=2)
    if result.state != "scored":
        raise typer.Exit(code=1)


@app.command("synthetic-visqol")
def synthetic_visqol(
    config: Annotated[
        list[Path],
        typer.Option("--config", "-c", exists=True, file_okay=True, dir_okay=False),
    ],
    manifest: Annotated[
        list[Path],
        typer.Option("--manifest", "-m", exists=True, file_okay=True, dir_okay=False),
    ],
    output_root: Annotated[Path, typer.Option("--output-root")],
    binary: Annotated[Path, typer.Option("--binary")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    mode: Annotated[VisqolMode, typer.Option("--mode")] = "speech",
    sample_rate_hz: Annotated[
        int,
        typer.Option("--sample-rate-hz", min=1),
    ] = 24_000,
    channels: Annotated[int, typer.Option("--channels", min=1)] = 1,
    duration_seconds: Annotated[
        float,
        typer.Option("--duration-seconds", min=0.01, max=60.0),
    ] = 5.0,
    amplitude: Annotated[
        int,
        typer.Option("--amplitude", min=1, max=32_767),
    ] = 10_000,
    frequency_hz: Annotated[
        float,
        typer.Option("--frequency-hz", min=0.01),
    ] = 1_000.0,
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout-seconds", min=0.1, max=600.0),
    ] = 120.0,
) -> None:
    """Generate and score every eligible synthetic stage with ViSQOL."""

    service = RegistryService.from_files(config_paths=config, manifest_paths=manifest)
    target = name or _single_config_name(config)
    resolved = service.resolve_config(target)
    run = run_synthetic_verification(
        resolved_config=resolved.resolved,
        output_root=output_root,
        audio_spec=SyntheticAudioSpec(
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            duration_seconds=duration_seconds,
            amplitude=amplitude,
            frequency_hz=frequency_hz,
        ),
        scorer=VisqolCliScorer(
            binary=binary,
            mode=mode,
            timeout_seconds=timeout_seconds,
        ),
    )
    report_path = output_root / "verification-report.json"
    write_synthetic_verification_report(run, report_path)
    typer.echo(json.dumps(run.safe_payload(), sort_keys=True))
    if run.state == "partial":
        raise typer.Exit(code=2)
    if run.state == "failed":
        raise typer.Exit(code=1)


@app.command("synthetic-visqol-treatment")
def synthetic_visqol_treatment(
    config: Annotated[
        list[Path],
        typer.Option("--config", "-c", exists=True, file_okay=True, dir_okay=False),
    ],
    manifest: Annotated[
        list[Path],
        typer.Option("--manifest", "-m", exists=True, file_okay=True, dir_okay=False),
    ],
    output_root: Annotated[Path, typer.Option("--output-root")],
    binary: Annotated[Path, typer.Option("--binary")],
    treatment: Annotated[str, typer.Option("--treatment")],
    name: Annotated[str | None, typer.Option("--name")] = None,
    mode: Annotated[VisqolMode, typer.Option("--mode")] = "speech",
    sample_count: Annotated[int, typer.Option("--sample-count", min=2, max=50)] = 3,
    minimum_samples: Annotated[
        int,
        typer.Option("--minimum-samples", min=2, max=50),
    ] = 3,
    duration_seconds: Annotated[
        float,
        typer.Option("--duration-seconds", min=0.01, max=60.0),
    ] = 5.0,
    frequency_hz: Annotated[
        float,
        typer.Option("--frequency-hz", min=0.01),
    ] = 800.0,
    frequency_step_hz: Annotated[
        float,
        typer.Option("--frequency-step-hz", min=0.0),
    ] = 100.0,
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout-seconds", min=0.1, max=600.0),
    ] = 120.0,
) -> None:
    """Run and aggregate several comparable synthetic ViSQOL samples."""

    service = RegistryService.from_files(config_paths=config, manifest_paths=manifest)
    target = name or _single_config_name(config)
    resolved = service.resolve_config(target)
    try:
        run = run_synthetic_treatment(
            treatment=treatment,
            sample_count=sample_count,
            minimum_samples=minimum_samples,
            resolved_config=resolved.resolved,
            output_root=output_root,
            audio_spec=SyntheticAudioSpec(
                sample_rate_hz=24_000,
                channels=1,
                duration_seconds=duration_seconds,
                amplitude=10_000,
                frequency_hz=frequency_hz,
            ),
            scorer=VisqolCliScorer(
                binary=binary,
                mode=mode,
                timeout_seconds=timeout_seconds,
            ),
            frequency_step_hz=frequency_step_hz,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    write_synthetic_treatment_report(run, output_root / "treatment-report.json")
    typer.echo(json.dumps(run.safe_payload(), sort_keys=True))
    if run.state == "partial":
        raise typer.Exit(code=2)
    if run.state == "failed":
        raise typer.Exit(code=1)


@app.command("visqol-compare-treatments")
def visqol_compare_treatments(
    baseline: Annotated[
        Path,
        typer.Option("--baseline", exists=True, file_okay=True, dir_okay=False),
    ],
    current: Annotated[
        Path,
        typer.Option("--current", exists=True, file_okay=True, dir_okay=False),
    ],
    stable_tolerance: Annotated[
        float,
        typer.Option("--stable-tolerance", min=0.0),
    ],
    lower_is_better: Annotated[bool, typer.Option("--lower-is-better")] = False,
) -> None:
    """Compare two persisted aggregate treatment reports."""

    try:
        baseline_report = load_full_reference_treatment_report(baseline)
        current_report = load_full_reference_treatment_report(current)
        report = compare_full_reference_treatments(
            baseline=baseline_report,
            current=current_report,
            policy=FullReferenceRegressionPolicy(
                stable_tolerance=stable_tolerance,
                higher_is_better=not lower_is_better,
            ),
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter("treatment report is invalid or unsafe") from exc
    typer.echo(json.dumps(report.safe_payload(), sort_keys=True))
    if any(stage.state == "regressed" for stage in report.stages):
        raise typer.Exit(code=1)
    if any(stage.state == "indeterminate" for stage in report.stages):
        raise typer.Exit(code=2)


@app.command("visqol-calibrate-repeatability")
def visqol_calibrate_repeatability(
    report: Annotated[
        list[Path],
        typer.Option("--report", exists=True, file_okay=True, dir_okay=False),
    ],
    minimum_repeats: Annotated[
        int,
        typer.Option("--minimum-repeats", min=3, max=50),
    ] = 3,
) -> None:
    """Describe repeated baseline variation without selecting a tolerance."""

    try:
        calibration = analyze_full_reference_repeatability(
            reports=tuple(load_full_reference_treatment_report(path) for path in report),
            minimum_repeats=minimum_repeats,
        )
    except (OSError, ValueError) as exc:
        raise typer.BadParameter("treatment report is invalid or unsafe") from exc
    typer.echo(json.dumps(calibration.safe_payload(), sort_keys=True))
    if any(stage.state == "indeterminate" for stage in calibration.stages):
        raise typer.Exit(code=2)


def _single_config_name(config_paths: list[Path]) -> str:
    if len(config_paths) != 1:
        raise typer.BadParameter("--name is required when multiple --config files are provided")
    with config_paths[0].open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return str(raw["meta"]["name"])
