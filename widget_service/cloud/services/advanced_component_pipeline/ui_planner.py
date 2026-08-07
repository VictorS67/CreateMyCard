"""高级组件的 UI 意图规划。

真实模型接入时，模型只能输出 :class:`UIBrief`；组件、布局和主题仍由服务端
确定。当前离线规划器用于模型不可用或结果不合格时的安全回退。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import ValidationError

from models.generation import TaskSpec
from services.cardplan_template.prompt import selection_candidates
from services.cardplan_template.registry import CardPlanRegistry

from .models import AdvancedCompositionPlan, DataShape, UIBrief


def build_ui_planner_prompt(
    task_spec: TaskSpec,
    data_shape: DataShape,
    registry: CardPlanRegistry | None = None,
) -> list[dict[str, str]]:
    """构造第一轮模型消息，仅开放版本化局部 Template 能力元数据。"""
    registry = registry or CardPlanRegistry()
    user_payload = {
        "userQuery": task_spec.userQuery,
        "size": task_spec.size,
        "dataShape": data_shape.model_dump(exclude={"fields"}),
        "fields": [field.model_dump() for field in data_shape.fields],
        "eventIds": [event.id for event in task_spec.eventCandidates if event.id],
        "cardPlanCandidates": selection_candidates(task_spec, registry),
    }
    output_schema = UIBrief.model_json_schema(by_alias=True)
    for field_name in ("advancedComponentIds", "adaptiveTemplateId", "primaryDomain"):
        output_schema["properties"].pop(field_name, None)
    return [
        {
            "role": "system",
            "content": (
                "你只负责输出抽象 UI 意图 JSON。themeId 和 localTemplateIds 只能从"
                "cardPlanCandidates 选择；themeSemantics/layoutSemantics 只能表达语义，"
                "不能输出颜色、圆角、组件树、布局源码、参数值或 DesignToken。"
                "局部 Template 是可选能力，不适合时输出空列表。选择 Theme 时优先保证它与"
                "所选局部 Template 的 compatibleThemeIds 一致。actionPlacement 只表达 Action "
                "属于整卡主操作(card)、某个内容摘要/图标控制(content)、无操作(none)，不确定"
                "时用 auto；选择 content 时 localTemplateIds 必须包含 actionPolicy 非 none 的"
                "Template，否则选择 card；选择局部 Template 时优先选择 requiredParameters 能逐项"
                "覆盖独立 fields、素材和 Action 的 variant，不要把多个独立字段拼成一个字符串来"
                "迁就参数较少的 Template；不得借此输出具体组件。advancedComponentIds、"
                "adaptiveTemplateId 和 primaryDomain 是服务端确定性字段，必须省略，服务端会在"
                "解析后注入。\n" + json.dumps(output_schema, ensure_ascii=False)
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


async def plan_ui_with_llm(
    task_spec: TaskSpec,
    data_shape: DataShape,
    generate_json: Callable[[list[dict[str, str]], str], Awaitable[dict[str, Any]]],
    composition_plan: AdvancedCompositionPlan | None = None,
) -> UIBrief:
    """第一轮模型只生成抽象 UIBrief；结构不合法时由调用方回退离线规划。"""
    raw = await generate_json(
        build_ui_planner_prompt(
            task_spec,
            data_shape,
        ),
        "advanced-ui-brief",
    )
    try:
        brief = UIBrief.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid UIBrief: {exc}") from exc
    registry = CardPlanRegistry()
    candidates = selection_candidates(task_spec, registry)
    theme_ids = {item["id"] for item in candidates["themes"]}
    template_ids = {item["id"] for item in candidates["localTemplates"]}
    if brief.theme_id is not None and brief.theme_id not in theme_ids:
        raise ValueError("UIBrief selected a theme outside the trusted candidates")
    if any(item not in template_ids for item in brief.local_template_ids):
        raise ValueError("UIBrief selected a Template outside the trusted candidates")
    if brief.action_placement == "content":
        definitions = [registry.require_template(item) for item in brief.local_template_ids]
        if not any(item.action_policy != "none" for item in definitions):
            brief = brief.model_copy(
                update={"action_placement": "card" if data_shape.action_count else "none"}
            )
    return apply_advanced_composition(brief, composition_plan)


def plan_ui_offline(
    task_spec: TaskSpec,
    data_shape: DataShape,
    composition_plan: AdvancedCompositionPlan | None = None,
) -> UIBrief:
    """根据数据语义给出可预测的保守意图，保证选择器无需模型也能安全运行。"""
    query = task_spec.userQuery.lower()
    if data_shape.time_range_count:
        brief = UIBrief(
            purpose="schedule-management",
            primaryInformation=["近期事项", "开始和结束时间"],
            informationHierarchy=["事项", "时间", "主要操作"],
            temporality="upcoming",
            visualTone="warm-focused",
            contentPriorities=["时间清晰", "操作直接"],
            reason="数据包含同一事项的时间范围。",
        )
        return apply_advanced_composition(brief, composition_plan)
    is_monitoring = data_shape.percentage_count or data_shape.repeated_metric_group_count
    if is_monitoring or any(word in query for word in ("内存", "电量", "存储", "状态")):
        brief = UIBrief(
            purpose="resource-monitoring",
            primaryInformation=["核心占用", "关联指标"],
            informationHierarchy=["状态", "核心指标", "主要操作"],
            density="compact",
            attention="warning-capable",
            visualTone="technical-efficient",
            contentPriorities=["异常可识别", "指标可扫读"],
            reason="数据包含资源百分比或重复指标。",
        )
        return apply_advanced_composition(brief, composition_plan)
    brief = UIBrief(
        purpose="wellbeing-coaching",
        primaryInformation=["当前状态", "核心时长"],
        informationHierarchy=["状态", "时长", "主要操作"],
        temporality="historical" if data_shape.duration_count else "now",
        attention="prominent",
        visualTone="calm-night",
        contentPriorities=["状态先被感知", "时长快速理解"],
        reason="数据适合形成可行动的状态摘要。",
    )
    return apply_advanced_composition(brief, composition_plan)


def apply_advanced_composition(
    brief: UIBrief,
    composition_plan: AdvancedCompositionPlan | None,
) -> UIBrief:
    """把确定性组件计划注入 Brief，不允许模型覆盖计算字段。"""
    if composition_plan is None:
        return brief
    templates = list(
        dict.fromkeys([*composition_plan.local_template_ids, *brief.local_template_ids])
    )[:12]
    return brief.model_copy(
        update={
            "advanced_component_ids": [
                assignment.component_id for assignment in composition_plan.assignments
            ],
            "adaptive_template_id": composition_plan.adaptive_template_id,
            "primary_domain": composition_plan.primary_domain,
            "local_template_ids": templates,
        }
    )
