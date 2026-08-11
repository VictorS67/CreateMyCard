"""环形核心进度、双数值摘要和主要操作。"""

from pydantic import BaseModel, Field

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
    title: str = Field(
        default="状态摘要",
        description="卡片标题元数据，简短概括当前状态，不直接作为模板中的大标题展示。",
    )
    description: str = Field(
        default="查看核心状态并执行改善操作",
        description="卡片用途说明，描述用户能查看的信息和执行的操作。",
    )
    caption: BindingRef = Field(
        description="顶部状态说明文本的数据绑定，例如睡眠状态；绑定字段可以是字符串或数值。"
    )
    caption_icon: str = Field(
        description="顶部状态说明旁的图片资源，必须填写 assetCandidates 中的资源 id。",
    )
    progress: BindingRef = Field(
        description="环形进度值的数据绑定，必须选择 fields 中的 number 或 integer 字段。"
    )
    progress_total: float = Field(
        default=100.0,
        gt=0,
        description="环形进度的最大值，必须大于 0；百分制评分通常填写 100。",
    )
    center_icon: str = Field(
        description="环形进度中央的图片资源，必须填写 assetCandidates 中的资源 id。",
    )
    major_value: BindingRef = Field(
        description="右侧第一行的主要展示值绑定，可选择数值或已格式化的字符串字段。"
    )
    major_unit: str = Field(
        description="主要展示值后的短单位；若 major_value 本身已包含单位，必须填写空字符串。"
    )
    minor_value: BindingRef = Field(
        description="右侧第二行的次要展示值绑定，可选择数值或已格式化的字符串字段。"
    )
    minor_unit: str = Field(
        description="次要展示值后的短单位；若 minor_value 本身已包含单位，必须填写空字符串。"
    )
    action: ActionRef = Field(
        description=(
            "底部主操作；event_id 必须来自 eventCandidates，label 使用面向用户的简短按钮文案。"
        )
    )


SPEC = ComponentSpec(
    component_id="ring-split-metric-action",
    description="环形核心指标、双数值摘要和主要操作。",
    supported_sizes=["2x2"],
    required_signals={"metrics": 2.0, "action": 2.0},
    preferred_signals={"duration": 3.0, "percentage": 2.0},
    domains=["health", "device"],
    scenarios=["status-summary"],
    status_semantics=["sleep-quality", "warning", "active"],
    content_semantics=["metric", "percentage", "duration", "status"],
    action_semantics=["open-details", "primary-action"],
    temporalities=["now", "historical"],
    min_actions=1,
)


def _asset_src(asset_id: str, task_spec: TaskSpec) -> str:
    for candidate in task_spec.assetCandidates:
        if candidate.get("id") == asset_id and candidate.get("src"):
            return str(candidate["src"])
    raise ValueError(f"asset is not in TaskSpec or has no src: {asset_id}")


def _offline_asset_ids(task_spec: TaskSpec) -> tuple[str, str]:
    asset_ids = [
        str(candidate["id"])
        for candidate in task_spec.assetCandidates
        if candidate.get("id") and candidate.get("src")
    ]
    if not asset_ids:
        raise ValueError("ring split component requires at least one asset candidate")
    return asset_ids[0], asset_ids[min(1, len(asset_ids) - 1)]


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
            "Image",
            {
                "src": _asset_src(invocation.caption_icon, task_spec),
                "width": 11,
                "height": 11,
                "objectFit": "contain",
                "flexShrink": 0,
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
            "Image",
            {
                "src": _asset_src(invocation.center_icon, task_spec),
                "width": 24,
                "height": 24,
                "objectFit": "contain",
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
            "component": "Image",
            "src": _asset_src(invocation.caption_icon, task_spec),
            "styles": {"width": 11, "height": 11, "objectFit": "contain", "flexShrink": 0},
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
            "children": ["hero-progress", "hero-icon"],
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
            "id": "hero-icon",
            "component": "Image",
            "src": _asset_src(invocation.center_icon, task_spec),
            "styles": {"width": 24, "height": 24, "objectFit": "contain"},
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
    caption_icon, center_icon = _offline_asset_ids(task_spec)
    return Invocation(
        caption=BindingRef(path=caption.path),
        caption_icon=caption_icon,
        progress=BindingRef(path=numeric_fields[0].path),
        center_icon=center_icon,
        major_value=BindingRef(path=numeric_fields[0].path),
        major_unit="",
        minor_value=BindingRef(path=numeric_fields[min(1, len(numeric_fields) - 1)].path),
        minor_unit="",
        action=primary_action(task_spec, "立即操作", "star"),
    )


def validate(invocation: Invocation, task_spec: TaskSpec) -> None:
    # Progress 参与环形进度计算，必须是数值；左右摘要仅用于 Text 展示，
    # 可以绑定数值，也可以绑定诸如“7小时30分钟”的格式化字符串。
    validate_numeric_paths([invocation.progress.path], task_spec)
    _asset_src(invocation.caption_icon, task_spec)
    _asset_src(invocation.center_icon, task_spec)


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
