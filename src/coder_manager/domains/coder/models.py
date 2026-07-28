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


@dataclass(frozen=True, slots=True)
class CoderWorkspace:
    """Remote workspace identity and latest build status."""

    id: UUID
    status: str
    latest_build_id: UUID


@dataclass(frozen=True, slots=True)
class CoderWorkspacePage:
    """One page of remote Coder workspaces."""

    items: tuple[CoderWorkspace, ...]
    count: int


@dataclass(frozen=True, slots=True)
class CoderWorkspaceBuild:
    """Remote workspace stop build state."""

    id: UUID
    status: str
