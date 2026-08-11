"""多个紧凑指标、一个核心指标和主要操作。"""

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
    select_field,
    validate_numeric_paths,
)


class CompactMetricArg(BaseModel):
    label: str
    icon: str = "battery"
    value: BindingRef
    total: float = 100.0


class Invocation(BaseModel):
    title: str = "状态与优化"
    description: str = "查看多个状态指标并执行主要操作"
    compact_metrics: list[CompactMetricArg] = Field(min_length=2, max_length=4)
    primary_label: str
    primary_icon: str = "memory"
    primary_value: BindingRef
    primary_total: float = 100.0
    warning_threshold: float = 80.0
    action: ActionRef


SPEC = ComponentSpec(
    component_id="compact-metrics-primary-action",
    description="多个紧凑指标、一个核心百分比指标和一个主要操作。",
    supported_sizes=["2x2"],
    required_signals={"percentage": 4.0, "metrics": 2.0, "action": 2.0},
    preferred_signals={"repeated-metrics": 4.0, "monitoring-intent": 2.0},
    domains=["device"],
    scenarios=["resource-monitoring"],
    status_semantics=["warning", "active"],
    content_semantics=["metric", "percentage", "status"],
    action_semantics=["primary-action"],
    temporalities=["now"],
    min_actions=1,
)


def build_rows(
    invocation: Invocation,
    tokens: dict[str, object],
    task_spec: TaskSpec,
) -> list[list[object]]:
    metric_ids = [f"metric-{index}" for index in range(len(invocation.compact_metrics))]
    palette = tokens.get("metricPalette", [tokens["accent"]])
    rows = [
        ["root", "Column", root_props(tokens), ["compact-row", "hero", "action"]],
        ["compact-row", "Row", {"itemMargin": 4, "alignItems": "center"}, metric_ids],
    ]
    for index, (component_id, metric) in enumerate(
        zip(metric_ids, invocation.compact_metrics, strict=True)
    ):
        progress_id = f"{component_id}-progress"
        icon_id = f"{component_id}-icon"
        rows.extend(
            [
                [
                    component_id,
                    "Stack",
                    {"width": 39, "height": 39, "alignContent": "center"},
                    [progress_id, icon_id],
                ],
                [
                    progress_id,
                    "Progress",
                    {
                        "value": binding(metric.value),
                        "total": metric.total,
                        "design": "ring",
                        "color": palette[index % len(palette)],
                    },
                ],
                [
                    icon_id,
                    "Text",
                    {"content": metric.icon, "design": "caption-m", "fontColor": tokens["primary"]},
                ],
            ]
        )
    rows.extend(
        [
            [
                "hero",
                "Column",
                {
                    "layoutWeight": 1,
                    "backgroundColor": tokens["surface"],
                    "borderRadius": 18,
                    "padding": 6,
                },
                ["hero-label", "hero-progress"],
            ],
            [
                "hero-label",
                "Text",
                {
                    "content": invocation.primary_label,
                    "design": "caption-m",
                    "fontColor": tokens["secondary"],
                },
            ],
            [
                "hero-progress",
                "Progress",
                {
                    "value": binding(invocation.primary_value),
                    "total": invocation.primary_total,
                    "design": "linear-bar",
                    "color": tokens["accent"],
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
    )
    return rows


def build_a2ui(invocation: Invocation, tokens: dict[str, object], task_spec: TaskSpec) -> str:
    """aesthetic_plan_a/compact_metrics_primary_action.py 原始 build()。"""
    palette = tokens.get(
        "metricPalette", [tokens["accent"], tokens["accentSecondary"], "#FF38C9A0"]
    )
    root = root_styles(tokens)
    root["padding"] = 6
    metric_ids = [f"compact-metric-{index}" for index in range(len(invocation.compact_metrics))]
    components: list[dict[str, object]] = [
        {
            "id": "root",
            "component": "Column",
            "itemMargin": 3,
            "suppressResourceBackdrop": True,
            "styles": root,
            "children": ["compact-rings-row", "hero-card", "action"],
        },
        {
            "id": "compact-rings-row",
            "component": "Row",
            "itemMargin": 4,
            "styles": {
                "height": 43,
                "justifyContent": "spaceAround",
                "alignItems": "center",
                "flexShrink": 0,
            },
            "children": metric_ids,
        },
    ]
    for index, (component_id, metric) in enumerate(
        zip(metric_ids, invocation.compact_metrics, strict=True)
    ):
        color = palette[index % len(palette)]
        components.extend(
            [
                {
                    "id": component_id,
                    "component": "Stack",
                    "styles": {"width": 39, "height": 39, "alignContent": "center"},
                    "children": [f"metric-{index}-progress", f"metric-{index}-icon"],
                    "accessibility": {"label": metric.label},
                },
                {
                    "id": f"metric-{index}-progress",
                    "component": "Progress",
                    "value": binding_expression(metric.value),
                    "total": metric.total,
                    "styles": {
                        "type": "ring",
                        "width": 37,
                        "height": 37,
                        "strokeWidth": 3,
                        "color": color,
                        "secondaryColor": color,
                        "trackColor": tokens["track"],
                        "centerColor": "#16FFFFFF",
                    },
                    "accessibility": {"label": metric.label},
                },
                {
                    "id": f"metric-{index}-icon",
                    "component": "Text",
                    "content": metric.icon,
                    "iconChrome": False,
                    "styles": {"fontSize": 15, "fontColor": tokens["textPrimary"]},
                },
            ]
        )
    primary_expr = binding_expression(invocation.primary_value)[3:-3]
    components.extend(
        [
            {
                "id": "hero-card",
                "component": "Row",
                "itemMargin": 8,
                "styles": {
                    "height": 62,
                    "padding": 5,
                    "alignItems": "center",
                    "flexShrink": 0,
                    "backgroundColor": tokens["surface"],
                    "borderRadius": 18,
                    "borderWidth": 1,
                    "borderColor": tokens["surfaceBorder"],
                },
                "children": ["hero-ring", "hero-copy"],
            },
            {
                "id": "hero-ring",
                "component": "Stack",
                "styles": {"width": 52, "height": 52, "alignContent": "center"},
                "children": ["hero-progress", "hero-icon"],
            },
            {
                "id": "hero-progress",
                "component": "Progress",
                "value": binding_expression(invocation.primary_value),
                "total": invocation.primary_total,
                "styles": {
                    "type": "ring",
                    "width": 50,
                    "height": 50,
                    "strokeWidth": 5,
                    "color": (
                        "{{{{ {} >= {} ? '{}' : '{}' }}}}".format(
                            primary_expr,
                            invocation.warning_threshold,
                            tokens["danger"],
                            tokens["accent"],
                        )
                    ),
                    "secondaryColor": tokens["danger"],
                    "trackColor": tokens["track"],
                    "centerColor": "#16FFFFFF",
                },
                "accessibility": {"label": invocation.primary_label},
            },
            {
                "id": "hero-icon",
                "component": "Text",
                "content": invocation.primary_icon,
                "iconChrome": False,
                "styles": {"fontSize": 17, "fontColor": tokens["textPrimary"]},
            },
            {
                "id": "hero-copy",
                "component": "Column",
                "itemMargin": 1,
                "styles": {"layoutWeight": 1, "justifyContent": "center", "alignItems": "start"},
                "children": ["hero-label", "hero-value"],
            },
            {
                "id": "hero-label",
                "component": "Text",
                "content": invocation.primary_label,
                "styles": {
                    "fontSize": 10,
                    "fontWeight": 500,
                    "fontColor": tokens["textSecondary"],
                    "maxLines": 1,
                },
            },
            {
                "id": "hero-value",
                "component": "Text",
                "content": f"{{{{ {primary_expr} + '%' }}}}",
                "styles": {
                    "fontSize": 22,
                    "fontWeight": 700,
                    "fontColor": tokens["textPrimary"],
                    "maxLines": 1,
                },
            },
            {
                "id": "action",
                "component": "Button",
                "label": f"{invocation.action.icon or 'clean'} {invocation.action.label}",
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
    )
    return make_a2ui(components, task_spec)


def map_offline(task_spec: TaskSpec, data_shape: DataShape) -> Invocation:
    numeric_fields = [
        field for field in data_shape.fields if field.data_type in {"integer", "number"}
    ]
    if not numeric_fields:
        raise ValueError("metric component requires numeric fields")
    primary = select_field(data_shape, roles=("percentage",), numeric=True)
    metrics = [
        CompactMetricArg(label=field.name, value=BindingRef(path=field.path))
        for field in numeric_fields[:4]
    ]
    while len(metrics) < 2:
        metrics.append(metrics[0].model_copy(deep=True))
    return Invocation(
        compact_metrics=metrics,
        primary_label="当前占用",
        primary_value=BindingRef(path=primary.path),
        action=primary_action(task_spec, "一键处理", "clean"),
    )


def validate(invocation: Invocation, task_spec: TaskSpec) -> None:
    validate_numeric_paths(
        [invocation.primary_value.path, *(item.value.path for item in invocation.compact_metrics)],
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

__all__ = ["CompactMetricArg", "Invocation", "PLUGIN", "build_rows"]
