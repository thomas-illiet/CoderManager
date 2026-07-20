"""Failure helpers kept separate for explicit task responsibilities."""

from coder_manager.tasks.common.execution import ExecutionClaim, fail_execution

__all__ = ["ExecutionClaim", "fail_execution"]
