"""同一高级组件模板的 TerseDSL 与标准 A2UI 双输出编译器。"""

from typing import Literal

from pydantic import BaseModel

from models.generation import TaskSpec
from services.compact_dsl_a2ui_converter import convert_compact_dsl_to_a2ui
from services.protocol_registry import DESIGN_COMPACT_PROFILE_ID, A2UIProtocolRegistry

from .component_registry import get_component
from .components.base import serialize, serialize_compact
from .styles import STYLE_TOKENS

AdvancedOutputFormat = Literal["terse", "a2ui"]


def _build_rows(
    component_id: str,
    invocation: BaseModel,
    task_spec: TaskSpec,
    style_id: str,
) -> list[list[object]]:
    plugin = get_component(component_id)
    if not isinstance(invocation, plugin.invocation_model):
        raise ValueError(f"invocation does not match component {component_id}")
    return plugin.build_rows(invocation, STYLE_TOKENS[style_id], task_spec)


def build_terse_nested2(
    component_id: str,
    invocation: BaseModel,
    task_spec: TaskSpec,
    style_id: str,
) -> str:
    return serialize(_build_rows(component_id, invocation, task_spec, style_id), task_spec)


def build_standard_a2ui(
    component_id: str,
    invocation: BaseModel,
    task_spec: TaskSpec,
    style_id: str,
) -> str:
    """绕过 Terse 转换器，恢复原 Design Compact 模板到 A2UI 的输出路径。"""
    rows = _build_rows(component_id, invocation, task_spec, style_id)
    compact_dsl = serialize_compact(rows, task_spec)
    profile = A2UIProtocolRegistry.read_design_protocol_profile(DESIGN_COMPACT_PROFILE_ID)
    return convert_compact_dsl_to_a2ui(
        compact_dsl,
        size=task_spec.size,
        protocol_profile=profile,
    )


def build_component_output(
    component_id: str,
    invocation: BaseModel,
    task_spec: TaskSpec,
    style_id: str,
    output_format: AdvancedOutputFormat,
) -> str:
    if output_format == "a2ui":
        return build_standard_a2ui(component_id, invocation, task_spec, style_id)
    return build_terse_nested2(component_id, invocation, task_spec, style_id)
