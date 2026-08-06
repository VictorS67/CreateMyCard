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

from .models import DataShape, UIBrief


def build_ui_planner_prompt(task_spec: TaskSpec, data_shape: DataShape) -> list[dict[str, str]]:
    """构造第一轮模型消息，不暴露组件实现和模板选择规则。"""
    user_payload = {
        "userQuery": task_spec.userQuery,
        "size": task_spec.size,
        "dataShape": data_shape.model_dump(exclude={"fields"}),
        "fields": [field.model_dump() for field in data_shape.fields],
        "eventIds": [event.id for event in task_spec.eventCandidates if event.id],
    }
    return [
        {
            "role": "system",
            "content": (
                "你只负责输出抽象 UI 意图 JSON，不能选择组件、布局或主题。"
                "必须严格符合给出的 UIBrief JSON Schema，不得输出颜色、圆角、"
                "组件名、布局名、主题名或 DesignToken。\n"
                + json.dumps(UIBrief.model_json_schema(by_alias=True), ensure_ascii=False)
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


async def plan_ui_with_llm(
    task_spec: TaskSpec,
    data_shape: DataShape,
    generate_json: Callable[[list[dict[str, str]], str], Awaitable[dict[str, Any]]],
) -> UIBrief:
    """第一轮模型只生成抽象 UIBrief；结构不合法时由调用方回退离线规划。"""
    raw = await generate_json(build_ui_planner_prompt(task_spec, data_shape), "advanced-ui-brief")
    try:
        return UIBrief.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid UIBrief: {exc}") from exc


def plan_ui_offline(task_spec: TaskSpec, data_shape: DataShape) -> UIBrief:
    """根据数据语义给出可预测的保守意图，保证选择器无需模型也能安全运行。"""
    query = task_spec.userQuery.lower()
    if data_shape.time_range_count:
        return UIBrief(
            purpose="schedule-management",
            primaryInformation=["近期事项", "开始和结束时间"],
            informationHierarchy=["事项", "时间", "主要操作"],
            temporality="upcoming",
            visualTone="warm-focused",
            contentPriorities=["时间清晰", "操作直接"],
            reason="数据包含同一事项的时间范围。",
        )
    is_monitoring = data_shape.percentage_count or data_shape.repeated_metric_group_count
    if is_monitoring or any(word in query for word in ("内存", "电量", "存储", "状态")):
        return UIBrief(
            purpose="resource-monitoring",
            primaryInformation=["核心占用", "关联指标"],
            informationHierarchy=["状态", "核心指标", "主要操作"],
            density="compact",
            attention="warning-capable",
            visualTone="technical-efficient",
            contentPriorities=["异常可识别", "指标可扫读"],
            reason="数据包含资源百分比或重复指标。",
        )
    return UIBrief(
        purpose="wellbeing-coaching",
        primaryInformation=["当前状态", "核心时长"],
        informationHierarchy=["状态", "时长", "主要操作"],
        temporality="historical" if data_shape.duration_count else "now",
        attention="prominent",
        visualTone="calm-night",
        contentPriorities=["状态先被感知", "时长快速理解"],
        reason="数据适合形成可行动的状态摘要。",
    )
