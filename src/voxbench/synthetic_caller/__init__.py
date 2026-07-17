"""Offline synthetic caller fixtures for Phase 2 verification."""

from voxbench.synthetic_caller.offline import (
    StageReferenceArtifact,
    SyntheticArtifacts,
    SyntheticAudioSpec,
    SyntheticStageDegradation,
    generate_synthetic_artifacts,
)
from voxbench.synthetic_caller.verification import (
    SyntheticTreatmentRun,
    SyntheticTreatmentState,
    SyntheticVerificationRun,
    SyntheticVerificationState,
    run_synthetic_treatment,
    run_synthetic_verification,
    write_synthetic_treatment_report,
    write_synthetic_verification_report,
)

__all__ = [
    "StageReferenceArtifact",
    "SyntheticArtifacts",
    "SyntheticAudioSpec",
    "SyntheticStageDegradation",
    "SyntheticTreatmentRun",
    "SyntheticTreatmentState",
    "SyntheticVerificationRun",
    "SyntheticVerificationState",
    "generate_synthetic_artifacts",
    "run_synthetic_treatment",
    "run_synthetic_verification",
    "write_synthetic_treatment_report",
    "write_synthetic_verification_report",
]
