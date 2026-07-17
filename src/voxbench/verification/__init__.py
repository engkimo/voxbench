"""Signal invariant verification APIs."""

from voxbench.verification.aggregation import (
    FullReferenceAggregateState,
    FullReferenceStageAggregate,
    FullReferenceTreatmentReport,
    aggregate_full_reference_reports,
)
from voxbench.verification.core import VerificationResult, verify_recordings
from voxbench.verification.full_reference import (
    FullReferenceBlock,
    FullReferenceCandidate,
    FullReferenceSelection,
    select_full_reference_candidates,
)
from voxbench.verification.scoring import (
    FullReferenceMeasurement,
    FullReferenceScorer,
    FullReferenceScorerContract,
    FullReferenceScoreResult,
    FullReferenceScorerReadiness,
    FullReferenceScoreState,
    FullReferenceScoringReport,
    full_reference_scores_to_metrics,
    score_full_reference_selection,
)
from voxbench.verification.visqol import (
    VISQOL_CONTRACT,
    VisqolCliScorer,
    VisqolMode,
    build_visqol_candidate,
)

__all__ = [
    "VISQOL_CONTRACT",
    "FullReferenceAggregateState",
    "FullReferenceBlock",
    "FullReferenceCandidate",
    "FullReferenceMeasurement",
    "FullReferenceScoreResult",
    "FullReferenceScoreState",
    "FullReferenceScorer",
    "FullReferenceScorerContract",
    "FullReferenceScorerReadiness",
    "FullReferenceScoringReport",
    "FullReferenceSelection",
    "FullReferenceStageAggregate",
    "FullReferenceTreatmentReport",
    "VerificationResult",
    "VisqolCliScorer",
    "VisqolMode",
    "aggregate_full_reference_reports",
    "build_visqol_candidate",
    "full_reference_scores_to_metrics",
    "score_full_reference_selection",
    "select_full_reference_candidates",
    "verify_recordings",
]
