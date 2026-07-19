"""API schemas."""

from coder_manager.schemas.application import (
    ApplicationCreate,
    ApplicationListQuery,
    ApplicationPage,
    ApplicationRead,
)
from coder_manager.schemas.instance import (
    InstanceArgoCdStatusRead,
    InstanceCreate,
    InstancePage,
    InstanceRead,
)
from coder_manager.schemas.managed_database import (
    DatabaseCreate,
    DatabaseItemStatistics,
    DatabaseListQuery,
    DatabasePage,
    DatabaseRead,
    DatabaseRegionStatistics,
    DatabaseStatistics,
    DatabaseUpdate,
    DatabaseUsageStatistics,
)
from coder_manager.schemas.member import MemberCreate, MemberPage, MemberRead, MemberRoleUpdate
from coder_manager.schemas.template import (
    TemplateCreate,
    TemplateListQuery,
    TemplatePage,
    TemplateRead,
    TemplateUpdate,
)
from coder_manager.schemas.template_image import (
    TemplateImageCreate,
    TemplateImagePage,
    TemplateImageRead,
)
from coder_manager.schemas.workspace import (
    WorkspaceCreate,
    WorkspaceListQuery,
    WorkspacePage,
    WorkspaceRead,
    WorkspaceUpdate,
)

__all__ = [
    "ApplicationCreate",
    "ApplicationListQuery",
    "ApplicationPage",
    "ApplicationRead",
    "DatabaseCreate",
    "DatabaseItemStatistics",
    "DatabaseListQuery",
    "DatabasePage",
    "DatabaseRead",
    "DatabaseRegionStatistics",
    "DatabaseStatistics",
    "DatabaseUpdate",
    "DatabaseUsageStatistics",
    "InstanceArgoCdStatusRead",
    "InstanceCreate",
    "InstancePage",
    "InstanceRead",
    "MemberCreate",
    "MemberPage",
    "MemberRead",
    "MemberRoleUpdate",
    "TemplateCreate",
    "TemplateImageCreate",
    "TemplateImagePage",
    "TemplateImageRead",
    "TemplateListQuery",
    "TemplatePage",
    "TemplateRead",
    "TemplateUpdate",
    "WorkspaceCreate",
    "WorkspaceListQuery",
    "WorkspacePage",
    "WorkspaceRead",
    "WorkspaceUpdate",
]
