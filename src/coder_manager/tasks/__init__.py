"""Durable background task steps registered by the Celery worker."""

from coder_manager.tasks.check_instance_states import check_instance_states
from coder_manager.tasks.database.sync import step_01_sync_database
from coder_manager.tasks.healthcheck import healthcheck
from coder_manager.tasks.instance.create import (
    step_01_create_schema,
    step_02_create_instance,
    step_03_bootstrap_admin,
    step_04_sync_templates,
)
from coder_manager.tasks.instance.delete import (
    step_01_remove_workspaces,
    step_02_remove_instance,
    step_03_remove_schema,
    step_04_remove_local_configuration,
)
from coder_manager.tasks.instance.start import step_01_start_instance
from coder_manager.tasks.instance.stop import step_01_stop_workspaces, step_02_stop_instance
from coder_manager.tasks.instance.update import step_01_update_instance, step_02_cleanup_users
from coder_manager.tasks.retry_job_executions import retry_job_executions
from coder_manager.tasks.template.sync import step_01_sync_template
from coder_manager.tasks.workspace.create import step_01_create_workspace
from coder_manager.tasks.workspace.delete import step_01_delete_workspace
from coder_manager.tasks.workspace.update import step_01_update_workspace

__all__ = [
    "check_instance_states",
    "healthcheck",
    "retry_job_executions",
    "step_01_create_schema",
    "step_01_create_workspace",
    "step_01_delete_workspace",
    "step_01_remove_workspaces",
    "step_01_start_instance",
    "step_01_stop_workspaces",
    "step_01_sync_database",
    "step_01_sync_template",
    "step_01_update_instance",
    "step_01_update_workspace",
    "step_02_cleanup_users",
    "step_02_create_instance",
    "step_02_remove_instance",
    "step_02_stop_instance",
    "step_03_bootstrap_admin",
    "step_03_remove_schema",
    "step_04_remove_local_configuration",
    "step_04_sync_templates",
]
