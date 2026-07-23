"""Shared validation for externally managed application identifiers."""

from typing import Annotated

from pydantic import StringConstraints

ApplicationIdentifier = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=255),
]
