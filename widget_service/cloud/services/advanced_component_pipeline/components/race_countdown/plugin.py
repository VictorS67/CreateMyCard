"""赛事陪伴：赛事标题、倒计时和训练动作。"""

from pydantic import BaseModel, Field

from models.generation import TaskSpec

from ...component_registry import ComponentPlugin, register_component
from ...models import ActionRef, BindingRef, ComponentSpec, DataShape
from ..a2ui_base import rows_to_a2ui
from ..base import binding, event_handler, root_props, validate_numeric_paths
from ..scene_helpers import asset_src, field_by_terms, first_action, first_asset_id, ref


class Invocation(BaseModel):
    event_title: BindingRef = Field(description="赛事名称或目标名称的数据绑定。")
    remaining_days: BindingRef = Field(description="剩余天数，必须绑定 number 或 integer 字段。")
    unit: str = Field(default="天剩余", description="倒计时数值后的短说明。")
    action_icon: str = Field(description="必须填写 assetCandidates 中的运动资源 id。")
    action: ActionRef = Field(description="训练计划动作，event_id 必须来自 eventCandidates。")


SPEC = ComponentSpec(
    component_id="race-countdown",
    description="高饱和赛事倒计时和主要训练动作。",
    supported_sizes=["2x2"],
    required_signals={"race-countdown-intent": 20.0, "action": 1.0},
    min_actions=1,
)


def build_rows(invocation: Invocation, tokens: dict[str, object], task_spec: TaskSpec):
    root = root_props(tokens)
    root.update({"padding": 12, "itemMargin": 8, "justifyContent": "spaceBetween"})
    return [
        ["root", "Column", root, ["title", "hero", "action-row"]],
        [
            "title",
            "Text",
            {
                "content": binding(invocation.event_title),
                "fontSize": 14,
                "fontColor": tokens["secondary"],
                "maxLines": 1,
            },
        ],
        ["hero", "Row", {"alignItems": "end", "itemMargin": 4}, ["days", "unit"]],
        [
            "days",
            "Text",
            {
                "content": binding(invocation.remaining_days),
                "fontSize": 40,
                "fontWeight": 700,
                "fontColor": tokens["primary"],
            },
        ],
        [
            "unit",
            "Text",
            {"content": invocation.unit, "fontSize": 13, "fontColor": tokens["secondary"]},
        ],
        [
            "action-row",
            "Row",
            {
                "width": "matchParent",
                "height": 36,
                "borderRadius": 18,
                "backgroundColor": "#F2FFFFFF",
                "alignItems": "center",
                "justifyContent": "center",
                "itemMargin": 6,
            },
            ["action-icon", "action"],
        ],
        [
            "action-icon",
            "Image",
            {
                "src": asset_src(invocation.action_icon, task_spec),
                "width": 18,
                "height": 18,
                "objectFit": "contain",
            },
        ],
        [
            "action",
            "Button",
            {
                "label": invocation.action.label,
                "onClick": event_handler(invocation.action, task_spec),
                "height": 32,
                "backgroundColor": "#00FFFFFF",
                "fontColor": tokens["accent"],
                "fontSize": 13,
            },
        ],
    ]


def build_a2ui(invocation: Invocation, tokens: dict[str, object], task_spec: TaskSpec) -> str:
    return rows_to_a2ui(build_rows(invocation, tokens, task_spec), task_spec)


def map_offline(task_spec: TaskSpec, data_shape: DataShape) -> Invocation:
    return Invocation(
        event_title=ref(field_by_terms(data_shape, "title", "赛事", numeric=False)),
        remaining_days=ref(field_by_terms(data_shape, "days", "剩余", numeric=True)),
        action_icon=first_asset_id(task_spec, "run", "运动"),
        action=first_action(task_spec, "今日训练"),
    )


def validate(invocation: Invocation, task_spec: TaskSpec) -> None:
    validate_numeric_paths([invocation.remaining_days.path], task_spec)
    asset_src(invocation.action_icon, task_spec)


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
__all__ = ["Invocation", "PLUGIN", "build_a2ui", "build_rows"]
