"""Signal invariant verification APIs."""

from voxbench.verification.core import VerificationResult, verify_recordings
from voxbench.verification.full_reference import (
    FullReferenceBlock,
    FullReferenceCandidate,
    FullReferenceSelection,
    select_full_reference_candidates,
)

__all__ = [
    "FullReferenceBlock",
    "FullReferenceCandidate",
    "FullReferenceSelection",
    "VerificationResult",
    "select_full_reference_candidates",
    "verify_recordings",
]
