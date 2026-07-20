"""Instance deletion steps."""

from coder_manager.tasks.instance.delete.step_01_remove_workspaces import (
    step_01_remove_workspaces,
)
from coder_manager.tasks.instance.delete.step_02_remove_instance import step_02_remove_instance
from coder_manager.tasks.instance.delete.step_03_remove_schema import step_03_remove_schema
from coder_manager.tasks.instance.delete.step_04_remove_local_configuration import (
    step_04_remove_local_configuration,
)

__all__ = [
    "step_01_remove_workspaces",
    "step_02_remove_instance",
    "step_03_remove_schema",
    "step_04_remove_local_configuration",
]
