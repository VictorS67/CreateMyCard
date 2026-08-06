"""近期日程、时间范围、提醒和主要操作。"""

from pydantic import BaseModel

from models.generation import TaskSpec

from ...component_registry import ComponentPlugin, register_component
from ...models import ActionRef, BindingRef, ComponentSpec, DataShape
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
        map_offline=map_offline,
        validate=validate,
    )
)

__all__ = ["Invocation", "PLUGIN", "build_rows"]
