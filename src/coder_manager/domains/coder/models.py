"""Small typed representations used by Coder template synchronization."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CoderTemplate:
    """Remote Coder template identity."""

    id: UUID


@dataclass(frozen=True, slots=True)
class CoderTemplateVersion:
    """Remote Coder template version and import state."""

    id: UUID
    status: str
    archived: bool
