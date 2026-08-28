"""Template publication, synchronization, and deletion tasks."""

from coder_manager.tasks.template.create import step_01_create_template
from coder_manager.tasks.template.delete import step_01_delete_template
from coder_manager.tasks.template.sync import step_01_sync_template

__all__ = [
    "step_01_create_template",
    "step_01_delete_template",
    "step_01_sync_template",
]
