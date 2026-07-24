"""Sanitized failures raised while retrieving a template source."""


class TemplateSourceError(Exception):
    """Raised when a Git source cannot be safely resolved or archived."""
