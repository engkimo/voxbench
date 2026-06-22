"""Engine harness exceptions."""


class HarnessError(RuntimeError):
    """Base error for harness execution failures."""


class HarnessDependencyError(HarnessError):
    """Raised when an optional runtime dependency is unavailable."""

