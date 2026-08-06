"""高级组件 Invocation 到受控 TerseDSL-Nested-2 的分派器。"""

from pydantic import BaseModel

from models.generation import TaskSpec

from .component_registry import get_component
from .styles import STYLE_TOKENS


def build_terse_nested2(
    component_id: str,
    invocation: BaseModel,
    task_spec: TaskSpec,
    style_id: str,
) -> str:
    plugin = get_component(component_id)
    if not isinstance(invocation, plugin.invocation_model):
        raise ValueError(f"invocation does not match component {component_id}")
    return plugin.build(invocation, STYLE_TOKENS[style_id], task_spec)
