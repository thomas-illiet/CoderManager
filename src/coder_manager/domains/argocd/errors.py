"""Errors raised by the Argo CD domain."""


class ArgoCdConfigurationError(RuntimeError):
    """Raised when required Argo CD configuration is absent or invalid."""


class ArgoCdRequestError(RuntimeError):
    """Raised when Argo CD rejects a request or returns an invalid response."""


class ArgoCdApplicationOwnershipError(ArgoCdRequestError):
    """Raised when an Application is not owned by the expected instance."""


class ArgoCdApplicationNotFoundError(RuntimeError):
    """Raised when a managed Application does not exist in Argo CD."""
