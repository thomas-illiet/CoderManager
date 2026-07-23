"""Sanitized Coder bootstrap domain errors."""


class CoderRequestError(Exception):
    """Raised when a Coder bootstrap request cannot be completed safely."""


class CoderFirstUserConflictError(CoderRequestError):
    """Raised when the existing first user does not match prepared credentials."""
