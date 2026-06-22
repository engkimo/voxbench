"""Registry exceptions."""


class RegistryError(ValueError):
    """Base error for config registry failures."""


class ManifestNotFoundError(RegistryError):
    """Raised when a config references an unknown plugin manifest."""


class ConfigNotFoundError(RegistryError):
    """Raised when a config or parent overlay is unknown."""


class ConfigValidationError(RegistryError):
    """Raised when config validation must hard-fail."""

