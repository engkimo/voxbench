"""Signal invariant verification APIs."""

from voxbench.verification.aggregation import (
    FullReferenceAggregateState,
    FullReferenceStageAggregate,
    FullReferenceTreatmentReport,
    aggregate_full_reference_reports,
    load_full_reference_treatment_report,
    write_full_reference_treatment_report,
)
from voxbench.verification.core import VerificationResult, verify_recordings
from voxbench.verification.full_reference import (
    FullReferenceBlock,
    FullReferenceCandidate,
    FullReferenceSelection,
    select_full_reference_candidates,
)
from voxbench.verification.regression import (
    FullReferenceRegressionPolicy,
    FullReferenceRegressionReport,
    FullReferenceRegressionState,
    FullReferenceStageRegression,
    compare_full_reference_treatments,
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
    "FullReferenceRegressionPolicy",
    "FullReferenceRegressionReport",
    "FullReferenceRegressionState",
    "FullReferenceScoreResult",
    "FullReferenceScoreState",
    "FullReferenceScorer",
    "FullReferenceScorerContract",
    "FullReferenceScorerReadiness",
    "FullReferenceScoringReport",
    "FullReferenceSelection",
    "FullReferenceStageAggregate",
    "FullReferenceStageRegression",
    "FullReferenceTreatmentReport",
    "VerificationResult",
    "VisqolCliScorer",
    "VisqolMode",
    "aggregate_full_reference_reports",
    "build_visqol_candidate",
    "compare_full_reference_treatments",
    "full_reference_scores_to_metrics",
    "load_full_reference_treatment_report",
    "score_full_reference_selection",
    "select_full_reference_candidates",
    "verify_recordings",
    "write_full_reference_treatment_report",
]
