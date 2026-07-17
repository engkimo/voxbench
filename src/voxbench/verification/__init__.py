"""Signal invariant verification APIs."""

from voxbench.verification.core import VerificationResult, verify_recordings
from voxbench.verification.full_reference import (
    FullReferenceBlock,
    FullReferenceCandidate,
    FullReferenceSelection,
    select_full_reference_candidates,
)
from voxbench.verification.scoring import (
    FullReferenceScorer,
    FullReferenceScorerContract,
    FullReferenceScoreResult,
    FullReferenceScorerReadiness,
    FullReferenceScoreState,
    FullReferenceScoringReport,
    full_reference_scores_to_metrics,
    score_full_reference_selection,
)

__all__ = [
    "FullReferenceBlock",
    "FullReferenceCandidate",
    "FullReferenceScoreResult",
    "FullReferenceScoreState",
    "FullReferenceScorer",
    "FullReferenceScorerContract",
    "FullReferenceScorerReadiness",
    "FullReferenceScoringReport",
    "FullReferenceSelection",
    "VerificationResult",
    "full_reference_scores_to_metrics",
    "score_full_reference_selection",
    "select_full_reference_candidates",
    "verify_recordings",
]
