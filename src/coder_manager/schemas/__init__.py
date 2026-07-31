"""API schemas."""

from coder_manager.schemas.application_identifier import ApplicationIdentifier
from coder_manager.schemas.instance import (
    InstanceAdminCredentialsRead,
    InstanceArgoCdStatusRead,
    InstanceCreate,
    InstancePage,
    InstanceRead,
)
from coder_manager.schemas.instance_kubernetes import InstanceKubernetesRead
from coder_manager.schemas.job_execution import JobRead, JobResourceResponse, JobResponse
from coder_manager.schemas.managed_database import (
    DatabaseCreate,
    DatabaseItemStatistics,
    DatabaseListQuery,
    DatabasePage,
    DatabaseRead,
    DatabaseStatistics,
    DatabaseUpdate,
    DatabaseUsageStatistics,
)
from coder_manager.schemas.member import MemberCreate, MemberPage, MemberRead, MemberRoleUpdate
from coder_manager.schemas.template import (
    TemplateCreate,
    TemplateDeploymentStatistics,
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
from coder_manager.schemas.template_parameter import (
    SystemTemplateParameterCreate,
    SystemTemplateParameterUpdate,
    TemplateParameterCreate,
    TemplateParameterPage,
    TemplateParameterRead,
    TemplateParameterUpdate,
    UserTemplateParameterCreate,
    UserTemplateParameterUpdate,
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
    "DatabaseStatistics",
    "DatabaseUpdate",
    "DatabaseUsageStatistics",
    "InstanceAdminCredentialsRead",
    "InstanceArgoCdStatusRead",
    "InstanceCreate",
    "InstanceKubernetesRead",
    "InstancePage",
    "InstanceRead",
    "JobRead",
    "JobResourceResponse",
    "JobResponse",
    "MemberCreate",
    "MemberPage",
    "MemberRead",
    "MemberRoleUpdate",
    "SystemTemplateParameterCreate",
    "SystemTemplateParameterUpdate",
    "TemplateCreate",
    "TemplateDeploymentStatistics",
    "TemplateImageCreate",
    "TemplateImagePage",
    "TemplateImageRead",
    "TemplateListQuery",
    "TemplatePage",
    "TemplateParameterCreate",
    "TemplateParameterPage",
    "TemplateParameterRead",
    "TemplateParameterUpdate",
    "TemplateRead",
    "TemplateUpdate",
    "UserTemplateParameterCreate",
    "UserTemplateParameterUpdate",
    "WorkspaceCreate",
    "WorkspaceListQuery",
    "WorkspacePage",
    "WorkspaceRead",
    "WorkspaceUpdate",
]
