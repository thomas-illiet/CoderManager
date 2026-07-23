"""API schemas."""

from coder_manager.schemas.application_identifier import ApplicationIdentifier
from coder_manager.schemas.instance import (
    InstanceAdminCredentialsRead,
    InstanceArgoCdStatusRead,
    InstanceCreate,
    InstancePage,
    InstanceRead,
)
from coder_manager.schemas.instance_kubernetes import (
    InstanceKubernetesCreate,
    InstanceKubernetesRead,
    InstanceKubernetesUpdate,
)
from coder_manager.schemas.job_execution import JobRead, JobResourceResponse, JobResponse
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
    "ApplicationIdentifier",
    "DatabaseCreate",
    "DatabaseItemStatistics",
    "DatabaseListQuery",
    "DatabasePage",
    "DatabaseRead",
    "DatabaseRegionStatistics",
    "DatabaseStatistics",
    "DatabaseUpdate",
    "DatabaseUsageStatistics",
    "InstanceAdminCredentialsRead",
    "InstanceArgoCdStatusRead",
    "InstanceCreate",
    "InstanceKubernetesCreate",
    "InstanceKubernetesRead",
    "InstanceKubernetesUpdate",
    "InstancePage",
    "InstanceRead",
    "JobRead",
    "JobResourceResponse",
    "JobResponse",
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
