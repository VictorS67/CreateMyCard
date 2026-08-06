"""环形核心进度、双数值摘要和主要操作。"""

from pydantic import BaseModel

from models.generation import TaskSpec

from ...component_registry import ComponentPlugin, register_component
from ...models import ActionRef, BindingRef, ComponentSpec, DataShape
from ..base import (
    binding,
    event_handler,
    primary_action,
    root_props,
    serialize,
    validate_numeric_paths,
)


class Invocation(BaseModel):
    title: str = "状态摘要"
    description: str = "查看核心状态并执行改善操作"
    caption: BindingRef
    caption_icon: str = "bell"
    progress: BindingRef
    progress_total: float = 100.0
    center_icon: str = "moon"
    major_value: BindingRef
    major_unit: str
    minor_value: BindingRef
    minor_unit: str
    action: ActionRef


SPEC = ComponentSpec(
    component_id="ring-split-metric-action",
    description="环形核心指标、双数值摘要和主要操作。",
    supported_sizes=["2x2"],
    required_signals={"metrics": 2.0, "action": 2.0},
    preferred_signals={"duration": 3.0, "percentage": 2.0},
    min_actions=1,
)


def build(invocation: Invocation, tokens: dict[str, object], task_spec: TaskSpec) -> str:
    rows = [
        ["root", "Column", root_props(tokens), ["caption-row", "metrics", "action"]],
        [
            "caption-row",
            "Row",
            {"itemMargin": 4, "alignItems": "center"},
            ["caption-icon", "caption"],
        ],
        [
            "caption-icon",
            "Text",
            {
                "content": invocation.caption_icon,
                "design": "caption-m",
                "fontColor": tokens["secondary"],
            },
        ],
        [
            "caption",
            "Text",
            {
                "content": binding(invocation.caption),
                "design": "caption-m",
                "fontColor": tokens["secondary"],
            },
        ],
        [
            "metrics",
            "Row",
            {"layoutWeight": 1, "alignItems": "center", "itemMargin": 8},
            ["ring-stack", "values"],
        ],
        [
            "ring-stack",
            "Stack",
            {"width": 76, "height": 76, "alignContent": "center"},
            ["ring", "ring-icon"],
        ],
        [
            "ring",
            "Progress",
            {
                "value": binding(invocation.progress),
                "total": invocation.progress_total,
                "design": "ring",
                "color": tokens["accent"],
            },
        ],
        [
            "ring-icon",
            "Text",
            {
                "content": invocation.center_icon,
                "design": "subtitle-s",
                "fontColor": tokens["primary"],
            },
        ],
        ["values", "Column", {"layoutWeight": 1, "itemMargin": 2}, ["major-row", "minor-row"]],
        ["major-row", "Row", {"itemMargin": 3, "alignItems": "end"}, ["major", "major-unit"]],
        [
            "major",
            "Text",
            {
                "content": binding(invocation.major_value),
                "design": "title-s",
                "fontColor": tokens["primary"],
            },
        ],
        [
            "major-unit",
            "Text",
            {
                "content": invocation.major_unit,
                "design": "caption-m",
                "fontColor": tokens["primary"],
            },
        ],
        ["minor-row", "Row", {"itemMargin": 3, "alignItems": "end"}, ["minor", "minor-unit"]],
        [
            "minor",
            "Text",
            {
                "content": binding(invocation.minor_value),
                "design": "subtitle-s",
                "fontColor": tokens["secondary"],
            },
        ],
        [
            "minor-unit",
            "Text",
            {
                "content": invocation.minor_unit,
                "design": "caption-m",
                "fontColor": tokens["primary"],
            },
        ],
        [
            "action",
            "Button",
            {
                "label": invocation.action.label,
                "design": "capsule",
                "onClick": event_handler(invocation.action, task_spec),
                "fontColor": tokens["accent"],
                "backgroundColor": tokens["button"],
                "borderColor": tokens["buttonBorder"],
                "borderWidth": 1,
            },
        ],
    ]
    return serialize(rows, task_spec)


def map_offline(task_spec: TaskSpec, data_shape: DataShape) -> Invocation:
    numeric_fields = [
        field for field in data_shape.fields if field.data_type in {"integer", "number"}
    ]
    if not numeric_fields:
        raise ValueError("metric component requires numeric fields")
    caption = next(
        (field for field in data_shape.fields if field.data_type == "string"), numeric_fields[0]
    )
    return Invocation(
        caption=BindingRef(path=caption.path),
        progress=BindingRef(path=numeric_fields[0].path),
        major_value=BindingRef(path=numeric_fields[0].path),
        major_unit="",
        minor_value=BindingRef(path=numeric_fields[min(1, len(numeric_fields) - 1)].path),
        minor_unit="",
        action=primary_action(task_spec, "立即操作", "star"),
    )


def validate(invocation: Invocation, task_spec: TaskSpec) -> None:
    validate_numeric_paths(
        [invocation.progress.path, invocation.major_value.path, invocation.minor_value.path],
        task_spec,
    )


PLUGIN = register_component(
    ComponentPlugin(
        component_id=SPEC.component_id,
        spec=SPEC,
        invocation_model=Invocation,
        build=build,
        map_offline=map_offline,
        validate=validate,
    )
)

__all__ = ["Invocation", "PLUGIN"]
