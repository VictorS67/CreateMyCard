"""近期日程、时间范围、提醒和主要操作。"""

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
from ..base import binding, event_handler, primary_action, root_props, select_field


class Invocation(BaseModel):
    title: str = "近期日程"
    description: str = "查看下一项安排并进入专注状态"
    caption: str
    caption_icon: str = "calendar"
    entity_title: BindingRef
    start_time: BindingRef
    end_time: BindingRef
    reminder_value: BindingRef
    reminder_prefix: str = "还有"
    reminder_suffix: str = "分钟后开启"
    reminder_icon: str = "bell"
    action: ActionRef


SPEC = ComponentSpec(
    component_id="schedule-detail-action",
    description="即将发生事项的时间范围、详情和提醒操作。",
    supported_sizes=["2x2"],
    required_signals={"time-range": 4.0, "action": 2.0},
    preferred_signals={"schedule-intent": 3.0},
    domains=["schedule", "productivity"],
    scenarios=["schedule-detail"],
    content_semantics=["event-title", "time-range", "countdown"],
    action_semantics=["open-event", "open-details"],
    temporalities=["upcoming"],
    min_actions=1,
)


def build_rows(
    invocation: Invocation,
    tokens: dict[str, object],
    task_spec: TaskSpec,
) -> list[list[object]]:
    rows = [
        [
            "root",
            "Column",
            root_props(tokens),
            ["caption-row", "title", "time", "reminder", "action"],
        ],
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
                "content": invocation.caption,
                "design": "caption-m",
                "fontColor": tokens["secondary"],
            },
        ],
        [
            "title",
            "Text",
            {
                "content": binding(invocation.entity_title),
                "design": "subtitle-s",
                "fontColor": tokens["primary"],
                "maxLines": 1,
            },
        ],
        ["time", "Row", {"itemMargin": 3}, ["start", "separator", "end"]],
        [
            "start",
            "Text",
            {
                "content": binding(invocation.start_time),
                "design": "title-s",
                "fontColor": tokens["primary"],
            },
        ],
        [
            "separator",
            "Text",
            {"content": "-", "design": "title-s", "fontColor": tokens["primary"]},
        ],
        [
            "end",
            "Text",
            {
                "content": binding(invocation.end_time),
                "design": "title-s",
                "fontColor": tokens["primary"],
            },
        ],
        [
            "reminder",
            "Row",
            {"itemMargin": 2, "alignItems": "center"},
            ["reminder-icon", "reminder-prefix", "reminder-value", "reminder-suffix"],
        ],
        [
            "reminder-icon",
            "Text",
            {
                "content": invocation.reminder_icon,
                "design": "caption-m",
                "fontColor": tokens["accent"],
            },
        ],
        [
            "reminder-prefix",
            "Text",
            {
                "content": invocation.reminder_prefix,
                "design": "body-s",
                "fontColor": tokens["secondary"],
            },
        ],
        [
            "reminder-value",
            "Text",
            {
                "content": binding(invocation.reminder_value),
                "design": "body-s",
                "fontColor": tokens["accent"],
            },
        ],
        [
            "reminder-suffix",
            "Text",
            {
                "content": invocation.reminder_suffix,
                "design": "body-s",
                "fontColor": tokens["secondary"],
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
    """aesthetic_plan_a/schedule_detail_action.py 原始 build()。"""
    root = root_styles(tokens)
    root["padding"] = {"top": 14, "right": 9, "bottom": 9, "left": 9}
    components: list[dict[str, object]] = [
        {
            "id": "root",
            "component": "Column",
            "itemMargin": 4,
            "suppressResourceBackdrop": True,
            "styles": root,
            "children": [
                "caption-row",
                "entity-title",
                "time-range",
                "reminder-row",
                "flex-spacer",
                "action",
            ],
        },
        {
            "id": "caption-row",
            "component": "Row",
            "itemMargin": 4,
            "styles": {"height": 15, "alignItems": "center", "flexShrink": 0},
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
            "content": invocation.caption,
            "styles": {
                "fontSize": 10,
                "fontWeight": 500,
                "fontColor": tokens["textSecondary"],
                "maxLines": 1,
            },
        },
        {
            "id": "entity-title",
            "component": "Text",
            "content": binding_expression(invocation.entity_title),
            "styles": {
                "fontSize": 12,
                "fontWeight": 600,
                "fontColor": tokens["textPrimary"],
                "maxLines": 1,
                "textOverflow": "ellipsis",
                "width": "matchParent",
            },
        },
        {
            "id": "time-range",
            "component": "Row",
            "itemMargin": 2,
            "styles": {"alignItems": "center", "height": 28, "flexShrink": 0},
            "children": ["start-time", "time-separator", "end-time"],
        },
        {
            "id": "start-time",
            "component": "Text",
            "content": binding_expression(invocation.start_time),
            "styles": {
                "fontSize": 21,
                "fontWeight": 700,
                "fontColor": tokens["textPrimary"],
                "maxLines": 1,
            },
        },
        {
            "id": "time-separator",
            "component": "Text",
            "content": "-",
            "styles": {
                "fontSize": 20,
                "fontWeight": 600,
                "fontColor": tokens["textPrimary"],
                "maxLines": 1,
            },
        },
        {
            "id": "end-time",
            "component": "Text",
            "content": binding_expression(invocation.end_time),
            "styles": {
                "fontSize": 21,
                "fontWeight": 700,
                "fontColor": tokens["textPrimary"],
                "maxLines": 1,
            },
        },
        {
            "id": "reminder-row",
            "component": "Row",
            "itemMargin": 3,
            "styles": {"height": 14, "alignItems": "center", "flexShrink": 0},
            "children": ["reminder-icon", "reminder-prefix", "reminder-value", "reminder-suffix"],
        },
        {
            "id": "reminder-icon",
            "component": "Text",
            "content": invocation.reminder_icon,
            "styles": {"fontSize": 10, "fontColor": tokens["accent"]},
        },
        {
            "id": "reminder-prefix",
            "component": "Text",
            "content": invocation.reminder_prefix,
            "styles": {"fontSize": 9, "fontColor": tokens["accentSecondary"], "maxLines": 1},
        },
        {
            "id": "reminder-value",
            "component": "Text",
            "content": binding_expression(invocation.reminder_value),
            "styles": {
                "fontSize": 9,
                "fontWeight": 700,
                "fontColor": tokens["accent"],
                "maxLines": 1,
            },
        },
        {
            "id": "reminder-suffix",
            "component": "Text",
            "content": invocation.reminder_suffix,
            "styles": {"fontSize": 9, "fontColor": tokens["accentSecondary"], "maxLines": 1},
        },
        {"id": "flex-spacer", "component": "Column", "styles": {"layoutWeight": 1}, "children": []},
        {
            "id": "action",
            "component": "Button",
            "label": f"{invocation.action.icon or 'moon'} {invocation.action.label}",
            "onClick": a2ui_event_handler(invocation.action, task_spec),
            "styles": {
                "height": 31,
                "width": "matchParent",
                "flexShrink": 0,
                "fontSize": 12,
                "fontWeight": 600,
                "fontColor": tokens["textPrimary"],
                "backgroundColor": tokens["button"],
                "borderColor": tokens["buttonBorder"],
                "borderWidth": 1,
                "borderRadius": 16,
            },
        },
    ]
    return make_a2ui(components, task_spec)


def map_offline(task_spec: TaskSpec, data_shape: DataShape) -> Invocation:
    time_fields = [field for field in data_shape.fields if "time" in field.roles]
    if len(time_fields) < 2:
        raise ValueError("schedule component requires two time fields")
    entity = select_field(data_shape, terms=("title", "name", "标题", "名称"))
    reminder = select_field(data_shape, roles=("metric",), numeric=True)
    return Invocation(
        caption="下一个日程",
        entity_title=BindingRef(path=entity.path),
        start_time=BindingRef(path=time_fields[0].path),
        end_time=BindingRef(path=time_fields[1].path),
        reminder_value=BindingRef(path=reminder.path),
        action=primary_action(task_spec, "专注模式", "moon"),
    )


def validate(invocation: Invocation, task_spec: TaskSpec) -> None:
    del invocation, task_spec


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
