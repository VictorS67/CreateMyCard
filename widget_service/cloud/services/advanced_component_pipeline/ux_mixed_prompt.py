"""第二层高级组件与基础组件混合生成 Prompt。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from models.generation import TaskSpec
from services.cardplan_template.models import Fact, HybridBodyContract
from services.cardplan_template.prompt import build_hybrid_prompt
from services.cardplan_template.registry import CardPlanRegistry

from .models import AdvancedScopeBrief
from .scope_planner import (
    resolve_available_capability_ids,
    resolve_scope_layout_ids,
    scope_template_ids,
)


class _ScopePromptBridge(BaseModel):
    """仅把新 Scope 投影给现有可信 Contract 构造器，不触发旧 UI Planner。"""

    model_config = ConfigDict(frozen=True)

    theme_id: str
    local_template_ids: tuple[str, ...]
    action_placement: str = "content"
    primary_domain: str
    adaptive_template_id: None = None
    advanced_component_ids: tuple[str, ...]
    disable_template_fallback: bool = True


@dataclass(frozen=True)
class UxMixedPromptProjection:
    messages: list[dict[str, str]]
    contract: HybridBodyContract
    facts: tuple[Fact, ...]
    requested_template_ids: tuple[str, ...]
    allowed_layout_ids: tuple[str, ...]
    theme_id: str


def build_ux_mixed_validation_retry_prompt(
    messages: list[dict[str, str]],
    raw_output: str,
    error: ValueError,
) -> list[dict[str, str]]:
    """Ask only the second layer to regenerate after strict contract rejection."""
    return [
        *messages,
        {"role": "assistant", "content": raw_output},
        {
            "role": "user",
            "content": (
                "上一输出未通过服务端严格契约校验："
                f"{error}。不要解释；保持同一个 uxAdvancedScope，重新输出完整布局根 DSL。"
                "只能逐字使用原请求 trustedStringLiterals/trustedAssetSources，"
                "不得新增标签、单位、颜色、尺寸、Action 或未批准 Template；"
                "必须逐组补齐 requiredLocalTemplateGroups。"
            ),
        },
    ]


def build_ux_mixed_prompt(
    *,
    task_spec: TaskSpec,
    card_spec: dict[str, Any],
    scope: AdvancedScopeBrief,
    registry: CardPlanRegistry,
) -> UxMixedPromptProjection:
    """复用事实、Action 和 Template 安全契约，替换旧候选与布局决策入口。"""
    available_capability_ids = _card_spec_capability_ids(card_spec)
    effective_capability_ids = resolve_available_capability_ids(
        task_spec,
        registry,
        available_capability_ids,
    )
    allowed_layout_ids = resolve_scope_layout_ids(scope, task_spec, registry)
    if not allowed_layout_ids:
        raise ValueError("Advanced Scope has no compatible UX layout")
    components = tuple(
        registry.require_ux_business_component(item) for item in scope.advanced_component_ids
    )
    bridge = _ScopePromptBridge(
        theme_id=scope.theme_id,
        local_template_ids=scope_template_ids(scope, registry, task_spec),
        primary_domain=components[0].domain_id,
        advanced_component_ids=scope.advanced_component_ids,
    )
    base = build_hybrid_prompt(
        task_spec=task_spec,
        card_spec=card_spec,
        ui_brief=bridge,
        registry=registry,
        ux_layout_root_ids=allowed_layout_ids,
    )
    required_template_groups = tuple(
        _required_template_group(component.local_template_ids, base.requested_template_ids)
        for component in components
    )
    if any(not group for group in required_template_groups):
        raise ValueError("Advanced Scope component has no satisfiable trusted Template")
    contract = base.contract.model_copy(
        update={"required_template_groups": required_template_groups}
    )
    layout_lines = [
        (
            f"- {layout.name}([config], child1, ...): {layout.description}; "
            f"businessChildren={layout.minimum_children(task_spec.size)}.."
            f"{layout.max_children_by_size[task_spec.size]}（不含 Action）; "
            f"actions={layout.min_action_children_by_size[task_spec.size]}.."
            f"{layout.max_action_children_by_size[task_spec.size]}; "
            "Action 必须是连续末尾直接 children；configSchema="
            + json.dumps(layout.parameters_schema, ensure_ascii=False)
        )
        for layout_id in allowed_layout_ids
        for layout in (registry.require_ux_layout_component(layout_id),)
    ]
    business_lines = [
        (
            f"- {component.name}: {component.description}; "
            f"variants={list(component.enabled_variants(effective_capability_ids))}; "
            f"roles={list(component.roles)}; "
            f"maxItems={component.max_items_by_size[task_spec.size]}; "
            "可信局部Template="
            + json.dumps(
                [
                    item
                    for item in component.local_template_ids
                    if item in base.requested_template_ids
                ],
                ensure_ascii=False,
            )
        )
        for component in components
    ]
    ux_override = "\n".join(
        (
            "",
            "UX Token=" + json.dumps(registry.ux_tokens, ensure_ascii=False),
            "允许的布局高级组件：",
            *layout_lines,
            "已批准的业务高级组件范围：",
            *business_lines,
            "每个业务高级组件必须从对应 requiredLocalTemplateGroups 中至少使用一个可信局部"
            " Template；标准组件只能补充未覆盖事实，不能完整替代所选业务高级组件。",
            "最终输出必须直接以唯一批准的布局高级组件为根并以分号结束；禁止 card@1。",
        )
    )
    user_suffix = "\n".join(
        (
            "trustedStringLiterals=" + json.dumps(contract.trusted_literals, ensure_ascii=False),
            "trustedAssetSources=" + json.dumps(contract.allowed_asset_sources, ensure_ascii=False),
            "uxAdvancedScope=" + json.dumps(scope.model_dump(by_alias=True), ensure_ascii=False),
            "allowedUxLayouts=" + json.dumps(allowed_layout_ids, ensure_ascii=False),
            "requiredLocalTemplateGroups="
            + json.dumps(required_template_groups, ensure_ascii=False),
            "只输出混合 DSL，不输出说明。",
        )
    )
    messages = [
        {"role": "system", "content": base.messages[0]["content"] + ux_override},
        {"role": "user", "content": base.messages[1]["content"] + "\n" + user_suffix},
    ]
    if sum(len(item["content"]) for item in messages) > 80_000:
        raise ValueError("UX Mixed Prompt exceeds the service input budget")
    return UxMixedPromptProjection(
        messages=messages,
        contract=contract,
        facts=base.facts,
        requested_template_ids=base.requested_template_ids,
        allowed_layout_ids=allowed_layout_ids,
        theme_id=base.theme_id,
    )


def _required_template_group(
    component_template_ids: tuple[str, ...],
    requested_template_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Prefer the current UX generation when compatibility Templates coexist."""
    eligible = tuple(
        template_id
        for template_id in component_template_ids
        if template_id in requested_template_ids
    )
    current = tuple(template_id for template_id in eligible if template_id.endswith("@2"))
    return current or eligible


def _card_spec_capability_ids(card_spec: dict[str, Any]) -> tuple[str, ...] | None:
    bindings = card_spec.get("dataBindings")
    if bindings is None:
        return None
    if not isinstance(bindings, list):
        return ()
    return tuple(
        capability_id
        for binding in bindings
        if isinstance(binding, dict)
        for capability_id in (binding.get("capabilityId"),)
        if isinstance(capability_id, str)
    )
