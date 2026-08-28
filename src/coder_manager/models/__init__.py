"""ORM models."""

from coder_manager.constants import INSTANCE_SLUG_LENGTH
from coder_manager.models.instance import (
    Instance,
    InstanceState,
    InstanceStatus,
)
from coder_manager.models.instance_kubernetes import InstanceKubernetes
from coder_manager.models.job_execution import JobExecution, JobStatus
from coder_manager.models.managed_database import Database, DatabaseAllocation
from coder_manager.models.member import Member, MemberRole, MemberStatus
from coder_manager.models.template import Template, TemplateSyncStatus
from coder_manager.models.template_assignment import TemplateAssignment, TemplateAssignmentStatus
from coder_manager.models.template_deployment import (
    TemplateDeployment,
    TemplateDeploymentStatus,
)
from coder_manager.models.template_image import TemplateImage
from coder_manager.models.template_parameter import (
    TemplateParameter,
    TemplateParameterSystemValue,
    TemplateParameterType,
)
from coder_manager.models.workspace import Workspace, WorkspaceStatus

__all__ = [
    "INSTANCE_SLUG_LENGTH",
    "Database",
    "DatabaseAllocation",
    "Instance",
    "InstanceKubernetes",
    "InstanceState",
    "InstanceStatus",
    "JobExecution",
    "JobStatus",
    "Member",
    "MemberRole",
    "MemberStatus",
    "Template",
    "TemplateAssignment",
    "TemplateAssignmentStatus",
    "TemplateDeployment",
    "TemplateDeploymentStatus",
    "TemplateImage",
    "TemplateParameter",
    "TemplateParameterSystemValue",
    "TemplateParameterType",
    "TemplateSyncStatus",
    "Workspace",
    "WorkspaceStatus",
]
