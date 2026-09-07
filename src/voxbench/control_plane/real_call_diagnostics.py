"""Bounded diagnostics for the guided real-call reproduction experiment."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RealCallExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_run_id: str = Field(min_length=1, max_length=128)
    compare_run_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("primary_run_id", "compare_run_id")
    @classmethod
    def normalize_run_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or any(
            marker in normalized.lower() for marker in ("http://", "https://")
        ):
            raise ValueError("run IDs must be safe identifiers")
        return normalized


class ExperimentCriterion(BaseModel):
    key: str
    label: str
    status: Literal["pass", "fail", "missing"]
    detail: str


class ExperimentFinding(BaseModel):
    classification: Literal["observed", "derived", "unknown", "recommended"]
    title: str
    detail: str
    run_role: Literal["primary", "compare", "both"]
    evidence_refs: list[str] = Field(default_factory=list)


class RunExperimentSnapshot(BaseModel):
    run_id: str
    role: Literal["no-interruption", "intentional-barge-in", "unknown"]
    observed_call: bool
    duration_ms: float
    provider: str
    config_hash: str
    rtp_report_count: int
    incident_count: int
    barge_in_count: int
    recording_stages: list[str]


class RealCallExperimentResponse(BaseModel):
    status: Literal["needs-compare", "ready", "inconclusive"]
    summary: str
    primary: RunExperimentSnapshot
    compare: RunExperimentSnapshot | None = None
    criteria: list[ExperimentCriterion]
    findings: list[ExperimentFinding]
    next_actions: list[str]


def analyze_real_call_experiment(
    primary: Any,
    compare: Any | None,
) -> RealCallExperimentResponse:
    primary_snapshot = _snapshot(primary)
    if compare is None:
        return RealCallExperimentResponse(
            status="needs-compare",
            summary=(
                "Baseline run selected. Add the intentional barge-in run to compare the experiment."
            ),
            primary=primary_snapshot,
            criteria=[
                _criterion(
                    "primary-role",
                    "Baseline condition",
                    primary_snapshot.role == "no-interruption",
                    f"Detected role: {primary_snapshot.role}",
                ),
                ExperimentCriterion(
                    key="compare-present",
                    label="Barge-in comparison",
                    status="missing",
                    detail="Select the intentional-barge-in run as Compare.",
                ),
            ],
            findings=[],
            next_actions=[
                "Complete one intentional-barge-in call, then select its run as Compare."
            ],
        )

    compare_snapshot = _snapshot(compare)
    same_provider = primary.provider == compare.provider
    same_config = primary.config_hash == compare.config_hash
    criteria = [
        _criterion(
            "different-runs",
            "Two distinct runs",
            primary.run_id != compare.run_id,
            "Primary and Compare must differ.",
        ),
        _criterion(
            "primary-role",
            "Primary is no-interruption",
            primary_snapshot.role == "no-interruption",
            f"Detected role: {primary_snapshot.role}",
        ),
        _criterion(
            "compare-role",
            "Compare is intentional-barge-in",
            compare_snapshot.role == "intentional-barge-in",
            f"Detected role: {compare_snapshot.role}",
        ),
        _criterion(
            "observed-calls",
            "Both are observed calls",
            primary_snapshot.observed_call and compare_snapshot.observed_call,
            "Synthetic runs cannot prove the real-call path.",
        ),
        _criterion(
            "duration",
            "Both calls are at least 20 seconds",
            primary_snapshot.duration_ms >= 20_000 and compare_snapshot.duration_ms >= 20_000,
            (
                f"Primary {primary_snapshot.duration_ms / 1000:.1f}s · "
                f"Compare {compare_snapshot.duration_ms / 1000:.1f}s"
            ),
        ),
        _criterion(
            "provider", "Same provider", same_provider, f"{primary.provider} · {compare.provider}"
        ),
        _criterion(
            "config",
            "Same configuration",
            same_config,
            "Configuration hashes match."
            if same_config
            else "Configuration hashes differ; treat the comparison as confounded.",
        ),
        _criterion(
            "rtcp",
            "RTCP evidence on both runs",
            primary_snapshot.rtp_report_count > 0 and compare_snapshot.rtp_report_count > 0,
            (
                f"Primary {primary_snapshot.rtp_report_count} · "
                f"Compare {compare_snapshot.rtp_report_count} reports"
            ),
        ),
        _criterion(
            "baseline-clear",
            "No barge-in in baseline",
            primary_snapshot.barge_in_count == 0,
            f"Primary {primary_snapshot.barge_in_count} barge-in incident(s)",
        ),
        _criterion(
            "compare-barge-in",
            "Barge-in reproduced in Compare",
            compare_snapshot.barge_in_count > 0,
            f"Compare {compare_snapshot.barge_in_count} barge-in incident(s)",
        ),
    ]
    findings = _findings(primary_snapshot, compare_snapshot)
    required = {
        "different-runs",
        "primary-role",
        "compare-role",
        "observed-calls",
        "duration",
        "provider",
        "rtcp",
        "baseline-clear",
        "compare-barge-in",
    }
    ready = all(item.status == "pass" for item in criteria if item.key in required)
    next_actions: list[str] = []
    if compare_snapshot.barge_in_count == 0:
        next_actions.append("Repeat the Compare call and interrupt once after the second sentence.")
    if primary_snapshot.barge_in_count > 0:
        next_actions.append(
            "Check headset isolation and microphone sensitivity before repeating the baseline."
        )
    if primary_snapshot.rtp_report_count == 0 or compare_snapshot.rtp_report_count == 0:
        next_actions.append(
            "Repeat with --collect-rtcp and keep each call active for 20-30 seconds."
        )
    if not same_config:
        next_actions.append(
            "Repeat both calls with the same provider model and pipeline configuration."
        )
    if not next_actions:
        next_actions.append(
            "Inspect the Compare barge-in incident and listen to each stage at the shared cursor."
        )
    return RealCallExperimentResponse(
        status="ready" if ready else "inconclusive",
        summary=(
            "The controlled real-call comparison has the required evidence."
            if ready
            else "The runs were compared, but one or more required controls are missing."
        ),
        primary=primary_snapshot,
        compare=compare_snapshot,
        criteria=criteria,
        findings=findings,
        next_actions=next_actions,
    )


def _snapshot(run: Any) -> RunExperimentSnapshot:
    timeline = run.to_timeline()
    tags = {tag.lower() for tag in run.environment.tags}
    role: Literal["no-interruption", "intentional-barge-in", "unknown"] = "unknown"
    if "intentional-barge-in" in tags or "experiment-intentional-barge-in" in tags:
        role = "intentional-barge-in"
    elif "no-interruption" in tags or "experiment-no-interruption" in tags:
        role = "no-interruption"
    incidents = timeline.lanes.incidents
    barge_incidents = [item for item in incidents if item.rule_id == "barge_in_sequence"]
    duration_ms = max(
        [item.duration_ms for item in timeline.lanes.recordings]
        + (
            [max(0.0, (run.ended_at - run.started_at).total_seconds() * 1000)]
            if run.ended_at
            else [0.0]
        )
    )
    return RunExperimentSnapshot(
        run_id=run.run_id,
        role=role,
        observed_call=(
            "audiosocket" in tags
            or (run.environment.started_from or "").startswith("voxbench-audiosocket-")
        ),
        duration_ms=duration_ms,
        provider=run.provider,
        config_hash=run.config_hash,
        rtp_report_count=len(timeline.lanes.rtp_quality),
        incident_count=len(incidents),
        barge_in_count=len(barge_incidents),
        recording_stages=[item.stage for item in timeline.lanes.recordings],
    )


def _criterion(key: str, label: str, passed: bool, detail: str) -> ExperimentCriterion:
    return ExperimentCriterion(
        key=key, label=label, status="pass" if passed else "fail", detail=detail
    )


def _findings(
    primary: RunExperimentSnapshot, compare: RunExperimentSnapshot
) -> list[ExperimentFinding]:
    findings = [
        ExperimentFinding(
            classification="derived",
            title="Barge-in contrast",
            detail=(
                f"Primary has {primary.barge_in_count} barge-in incident(s); "
                f"Compare has {compare.barge_in_count}."
            ),
            run_role="both",
        ),
        ExperimentFinding(
            classification="observed"
            if primary.rtp_report_count and compare.rtp_report_count
            else "unknown",
            title="Transport coverage",
            detail=(
                f"RTCP reports: Primary {primary.rtp_report_count}, "
                f"Compare {compare.rtp_report_count}."
            ),
            run_role="both",
        ),
    ]
    if compare.barge_in_count > 0 and primary.barge_in_count == 0:
        findings.append(
            ExperimentFinding(
                classification="derived",
                title="Controlled interruption reproduced",
                detail="Barge-in evidence appears only in the intentional interruption condition.",
                run_role="both",
            )
        )
    return findings
