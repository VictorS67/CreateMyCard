"""环形核心进度、双数值摘要和主要操作。"""

from pydantic import BaseModel

from models.generation import TaskSpec

from ...component_registry import ComponentPlugin, register_component
from ...models import ActionRef, BindingRef, ComponentSpec, DataShape
from ..a2ui_base import (
    binding_expression,
    make_a2ui,
    root_styles,
)
from ..a2ui_base import (
    event_handler as a2ui_event_handler,
)
from ..base import (
    binding,
    event_handler,
    primary_action,
    root_props,
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


def build_rows(
    invocation: Invocation,
    tokens: dict[str, object],
    task_spec: TaskSpec,
) -> list[list[object]]:
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
    return rows


def build_a2ui(invocation: Invocation, tokens: dict[str, object], task_spec: TaskSpec) -> str:
    """aesthetic_plan_a/ring_split_metric_action.py 原始 build()。"""
    root = root_styles(tokens)
    root["padding"] = {"top": 14, "right": 8, "bottom": 6, "left": 8}
    components: list[dict[str, object]] = [
        {
            "id": "root",
            "component": "Column",
            "itemMargin": 4,
            "suppressResourceBackdrop": True,
            "styles": root,
            "children": ["caption-row", "metric-content", "action"],
        },
        {
            "id": "caption-row",
            "component": "Row",
            "itemMargin": 4,
            "styles": {
                "height": 15,
                "alignItems": "center",
                "flexShrink": 0,
                "margin": {"left": 7},
            },
            "children": ["caption-icon", "caption-text"],
        },
        {
            "id": "caption-icon",
            "component": "Text",
            "content": invocation.caption_icon,
            "styles": {"fontSize": 11, "fontColor": tokens["textSecondary"]},
        },
        {
            "id": "caption-text",
            "component": "Text",
            "content": binding_expression(invocation.caption),
            "styles": {
                "fontSize": 10,
                "fontWeight": 500,
                "fontColor": tokens["textSecondary"],
                "maxLines": 1,
                "textOverflow": "ellipsis",
            },
        },
        {
            "id": "metric-content",
            "component": "Row",
            "itemMargin": 8,
            "styles": {"layoutWeight": 1, "alignItems": "center", "justifyContent": "center"},
            "children": ["hero-ring", "split-values"],
        },
        {
            "id": "hero-ring",
            "component": "Stack",
            "styles": {"width": 76, "height": 76, "alignContent": "center"},
            "children": ["hero-progress", "hero-icon-disc"],
        },
        {
            "id": "hero-progress",
            "component": "Progress",
            "value": binding_expression(invocation.progress),
            "total": invocation.progress_total,
            "styles": {
                "type": "ring",
                "width": 72,
                "height": 72,
                "strokeWidth": 10,
                "startAngle": -20,
                "color": tokens["accent"],
                "secondaryColor": tokens["accentSecondary"],
                "trackColor": tokens["track"],
                "centerColor": "#FF5630A4",
            },
            "accessibility": {"label": "核心进度"},
        },
        {
            "id": "hero-icon-disc",
            "component": "Column",
            "styles": {
                "width": 24,
                "height": 24,
                "borderRadius": 12,
                "backgroundColor": "#FFFFFFFF",
                "alignItems": "center",
                "justifyContent": "center",
            },
            "children": ["hero-icon"],
        },
        {
            "id": "hero-icon",
            "component": "Text",
            "content": "sleep-moon"
            if invocation.center_icon in {"moon", "sleep", "sleep-moon"}
            else invocation.center_icon,
            "iconChrome": False,
            "iconUseStyleColor": True,
            "styles": {"fontSize": 16, "fontColor": "#FF5630A4"},
        },
        {
            "id": "split-values",
            "component": "Column",
            "itemMargin": 0,
            "styles": {"width": 54, "alignItems": "start", "justifyContent": "center"},
            "children": ["major-row", "minor-row"],
        },
        {
            "id": "major-row",
            "component": "Row",
            "itemMargin": 3,
            "styles": {"alignItems": "end"},
            "children": ["major-value", "major-unit"],
        },
        {
            "id": "major-value",
            "component": "Text",
            "content": binding_expression(invocation.major_value),
            "styles": {
                "fontSize": 22,
                "fontWeight": 700,
                "fontColor": tokens["textPrimary"],
                "maxLines": 1,
            },
        },
        {
            "id": "major-unit",
            "component": "Text",
            "content": invocation.major_unit,
            "styles": {
                "fontSize": 10,
                "fontWeight": 600,
                "fontColor": tokens["textPrimary"],
                "maxLines": 1,
            },
        },
        {
            "id": "minor-row",
            "component": "Row",
            "itemMargin": 3,
            "styles": {"alignItems": "end"},
            "children": ["minor-value", "minor-unit"],
        },
        {
            "id": "minor-value",
            "component": "Text",
            "content": binding_expression(invocation.minor_value),
            "styles": {
                "fontSize": 22,
                "fontWeight": 700,
                "fontColor": tokens["textPrimary"],
                "maxLines": 1,
            },
        },
        {
            "id": "minor-unit",
            "component": "Text",
            "content": invocation.minor_unit,
            "styles": {
                "fontSize": 10,
                "fontWeight": 600,
                "fontColor": tokens["textPrimary"],
                "maxLines": 1,
            },
        },
        {
            "id": "action",
            "component": "Button",
            "label": f"{invocation.action.icon or 'alarm'} {invocation.action.label}",
            "onClick": a2ui_event_handler(invocation.action, task_spec),
            "styles": {
                "height": 34,
                "width": "matchParent",
                "flexShrink": 0,
                "fontSize": 12,
                "fontWeight": 600,
                "fontColor": tokens["textPrimary"],
                "iconSize": 14,
                "backgroundColor": tokens["button"],
                "borderColor": tokens["buttonBorder"],
                "borderWidth": 1,
                "borderRadius": 18,
            },
        },
    ]
    return make_a2ui(components, task_spec)


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
        build_rows=build_rows,
        build_a2ui=build_a2ui,
        map_offline=map_offline,
        validate=validate,
    )
)

__all__ = ["Invocation", "PLUGIN", "build_rows"]
