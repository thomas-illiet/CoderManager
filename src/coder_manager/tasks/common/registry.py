"""Allowlisted task and step names for durable background jobs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from coder_manager.celery_app import celery_app

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

INSTANCE_CREATE_STEP_01 = "step_01_create_schema"
INSTANCE_CREATE_STEP_02 = "step_02_create_instance"
INSTANCE_CREATE_STEP_03 = "step_03_bootstrap_admin"
INSTANCE_CREATE_STEP_04 = "step_04_sync_templates"
INSTANCE_UPDATE_STEP_01 = "step_01_update_instance"
INSTANCE_DELETE_STEP_01 = "step_01_remove_workspaces"
INSTANCE_DELETE_STEP_02 = "step_02_remove_instance"
INSTANCE_DELETE_STEP_03 = "step_03_remove_schema"
INSTANCE_DELETE_STEP_04 = "step_04_remove_local_configuration"
WORKSPACE_CREATE_STEP_01 = "step_01_create_workspace"
WORKSPACE_UPDATE_STEP_01 = "step_01_update_workspace"
WORKSPACE_DELETE_STEP_01 = "step_01_delete_workspace"
DATABASE_SYNC_STEP_01 = "step_01_sync_database"
TEMPLATE_SYNC_STEP_01 = "step_01_sync_template"

INSTANCE_CREATE_STEP_01_TASK = "coder_manager.instance.create.step_01_create_schema"
INSTANCE_CREATE_STEP_02_TASK = "coder_manager.instance.create.step_02_create_instance"
INSTANCE_CREATE_STEP_03_TASK = "coder_manager.instance.create.step_03_bootstrap_admin"
INSTANCE_CREATE_STEP_04_TASK = "coder_manager.instance.create.step_04_sync_templates"
INSTANCE_UPDATE_STEP_01_TASK = "coder_manager.instance.update.step_01_update_instance"
INSTANCE_DELETE_STEP_01_TASK = "coder_manager.instance.delete.step_01_remove_workspaces"
INSTANCE_DELETE_STEP_02_TASK = "coder_manager.instance.delete.step_02_remove_instance"
INSTANCE_DELETE_STEP_03_TASK = "coder_manager.instance.delete.step_03_remove_schema"
INSTANCE_DELETE_STEP_04_TASK = "coder_manager.instance.delete.step_04_remove_local_configuration"
WORKSPACE_CREATE_STEP_01_TASK = "coder_manager.workspace.create.step_01_create_workspace"
WORKSPACE_UPDATE_STEP_01_TASK = "coder_manager.workspace.update.step_01_update_workspace"
WORKSPACE_DELETE_STEP_01_TASK = "coder_manager.workspace.delete.step_01_delete_workspace"
DATABASE_SYNC_STEP_01_TASK = "coder_manager.database.sync.step_01_sync_database"
TEMPLATE_SYNC_STEP_01_TASK = "coder_manager.template.sync.step_01_sync_template"

REGISTERED_STEP_NAMES = frozenset(
    {
        INSTANCE_CREATE_STEP_01_TASK,
        INSTANCE_CREATE_STEP_02_TASK,
        INSTANCE_CREATE_STEP_03_TASK,
        INSTANCE_CREATE_STEP_04_TASK,
        INSTANCE_UPDATE_STEP_01_TASK,
        INSTANCE_DELETE_STEP_01_TASK,
        INSTANCE_DELETE_STEP_02_TASK,
        INSTANCE_DELETE_STEP_03_TASK,
        INSTANCE_DELETE_STEP_04_TASK,
        WORKSPACE_CREATE_STEP_01_TASK,
        WORKSPACE_UPDATE_STEP_01_TASK,
        WORKSPACE_DELETE_STEP_01_TASK,
        DATABASE_SYNC_STEP_01_TASK,
        TEMPLATE_SYNC_STEP_01_TASK,
    }
)


def dispatch_registered_step(task_name: str, job_id: UUID) -> bool:
    """Dispatch one allowlisted task name and report whether it was accepted."""

    if task_name not in REGISTERED_STEP_NAMES:
        logger.warning("Skipping job %s with unknown task %s", job_id, task_name)
        return False
    task = celery_app.tasks.get(task_name)
    if task is None:
        logger.error("Registered task %s is not loaded for job %s", task_name, job_id)
        return False
    try:
        task.delay(str(job_id))
    except Exception:
        logger.exception("Could not dispatch task %s for job %s", task_name, job_id)
        return False
    return True
