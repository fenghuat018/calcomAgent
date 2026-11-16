"""Register SDK function tools with workflow phase checks."""

from inspect import isawaitable
from typing import Callable

from agents import Agent, RunContextWrapper, function_tool

from workflow_context import Role, WorkflowContext


def enabled(role: Role, step: str):
    def check(wrapper: RunContextWrapper[WorkflowContext], agent: Agent[WorkflowContext]) -> bool:
        task = wrapper.context.task
        return agent.name == role and task.role == role and task.step == step and task.status == "active"

    return check


def checked(wrapper: RunContextWrapper[WorkflowContext], role: Role, step: str) -> WorkflowContext:
    task = wrapper.context.task
    if task.role != role or task.step != step or task.status != "active":
        raise ValueError("The tool does not belong to the active workflow phase")
    return wrapper.context


def local_tool(role: Role, step: str, handler: Callable, description: str):
    async def run(wrapper: RunContextWrapper[WorkflowContext]) -> str:
        result = handler(checked(wrapper, role, step))
        if isawaitable(result):
            await result
        return "Workflow state saved."

    return function_tool(
        run, name_override=step, description_override=description,
        is_enabled=enabled(role, step), failure_error_function=None,
    )
