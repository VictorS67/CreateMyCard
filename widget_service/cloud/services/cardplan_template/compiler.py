"""Trusted Template expansion, Hybrid Contract checks, and A2UI lowering."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from jsonschema import Draft202012Validator

from models.generation import TaskSpec
from services.advanced_component_pipeline.models import (
    UX_LAYOUT_COMPONENT_IDS,
    UxLayoutComponentCapability,
)
from services.terse_dsl_nested2_converter import (
    Nested2Node,
    TerseDslNested2ConversionError,
    convert_terse_dsl_nested2_to_a2ui,
)

from .models import (
    ExpansionStats,
    HybridBodyContract,
    TemplateNode,
    TemplateParameterRelation,
    TemplateValue,
)
from .parser import ParsedCall, parse_hybrid_card, parse_ux_layout_card
from .registry import CardPlanRegistry

_STANDARD_CONTAINERS = frozenset({"Row", "Column", "List", "Stack"})
_CONTAINERS = _STANDARD_CONTAINERS | UX_LAYOUT_COMPONENT_IDS
_UX_ACTION_COMPONENTS = frozenset({"PillAction", "IconAction", "ActionTile"})
_DANGEROUS_EVENT_KEYS = frozenset({"onClick", "call", "args", "action"})
_LAYOUT_ALIASES = {
    ("Column", "card"): "section",
    ("Column", "section-relaxed"): "section",
    ("Column", "metric-stack"): "section",
    ("Column", "dense"): "compact",
    ("Column", "action-bottom-compact"): "compact",
    ("Row", "icon-control-group"): "between",
    ("Row", "inline"): "between",
    ("Row", "section"): "between",
    ("Row", "compact"): "between",
    ("Stack", "ux-ring-medium"): "overlay",
    ("Stack", "ux-ring-small"): "overlay",
}
_DESIGN_ALIASES = {
    ("Text", "caption"): "subtitle",
    ("Text", "metric-hero"): "title",
    ("Text", "status-alert"): "warning",
    ("Text", "ux-status-alert"): "warning",
    ("Text", "ux-time-compact"): "subtitle",
    ("Text", "ux-title-compact"): "compact-title",
    ("Image", "ux-glyph-sm"): "icon",
    ("Image", "ux-glyph-xs"): "icon",
    ("Button", "action-frosted"): "primary",
}
_COLOR_MODE_LITERAL = re.compile(
    r"^\{\{\s*\$__colorMode\s*==\s*'dark'\s*\?\s*'([^']+)'\s*:\s*'([^']+)'\s*\}\}$"
)


@dataclass(frozen=True)
class HybridCompilation:
    raw_output: str
    effective_output: str
    a2ui: str
    stats: ExpansionStats
    fallback_used: bool = False


@dataclass
class _ExpansionState:
    template_ids: list[str]
    action_ids: list[str]
    action_occurrences: list[str]
    template_calls: int = 0
    template_variant_normalizations: int = 0
    template_provider_param_normalizations: int = 0
    template_relation_number_normalizations: int = 0
    expanded_components: int = 0


def compile_hybrid_card(
    source: str,
    *,
    task_spec: TaskSpec,
    contract: HybridBodyContract,
    protocol_profile: dict[str, Any],
    registry: CardPlanRegistry,
) -> HybridCompilation:
    composition = parse_hybrid_card(source)
    raw_card_params = composition.values[0]
    if not isinstance(raw_card_params, dict):
        raise TerseDslNested2ConversionError("card@1 params must be an object.")
    card_params = _normalize_card_params(raw_card_params)
    _validate_card_params(card_params, task_spec, contract)
    _validate_ux_layout_root(
        composition.children[0],
        contract,
        size=task_spec.size,
        registry=registry,
    )
    raw_count = _count_calls(composition.children[0])
    if raw_count > contract.limits.max_raw_components:
        raise TerseDslNested2ConversionError("Hybrid raw component budget exceeded.")
    _reject_direct_events(composition.children[0])
    _validate_raw_components(composition.children[0], contract)
    normalized_content, provider_param_normalizations = _normalize_template_provider_params(
        composition.children[0],
        task_spec,
        contract,
        registry,
    )
    normalized_content, relation_number_normalizations = _normalize_template_relation_numbers(
        normalized_content,
        contract,
        registry,
    )
    composition = ParsedCall(
        composition.kind,
        composition.name,
        composition.values,
        (normalized_content,),
        composition.span,
    )
    _validate_required_numbers(composition.children[0], contract)
    content_call = _strip_direct_card_chrome_from_call(
        composition.children[0],
        card_params,
    )
    content_call = _normalize_recommended_variant_order(content_call, registry)
    state = _ExpansionState(
        template_ids=[],
        action_ids=[],
        action_occurrences=[],
        template_provider_param_normalizations=provider_param_normalizations,
        template_relation_number_normalizations=relation_number_normalizations,
    )
    content = _expand_call(
        content_call,
        parent="$root",
        contract=contract,
        registry=registry,
        state=state,
    )
    content = _lower_ux_layouts(
        content,
        size=task_spec.size,
        has_action="action" in card_params,
        registry=registry,
    )
    content = _lower_capsule_progress(content)
    card_params = _drop_redundant_card_chrome(card_params, content)
    content = _deduplicate_visible_text(content, task_spec)
    content = _append_missing_required_literals(
        content,
        contract,
        already_visible=tuple(
            item for item in _primitive_values(card_params) if isinstance(item, str)
        ),
    )
    # Missing facts appended above can make an earlier composite subtitle fully
    # redundant. Re-run chrome ownership before measuring the body budget.
    card_params = _drop_redundant_card_chrome(card_params, content)
    card_params = _reclaim_optional_chrome_for_content(
        card_params,
        content,
        contract,
        registry,
    )
    content_height = _estimate_height(content)
    root = _compile_card_shell(card_params, content, task_spec, contract, registry)
    text_role = registry.require_theme(contract.theme_profile_id).text_role
    root = _apply_theme_text_role(root, text_role)
    card_action = card_params.get("action")
    if isinstance(card_action, dict):
        if card_action["id"] not in state.action_ids:
            state.action_ids.append(card_action["id"])
        state.action_occurrences.append(card_action["id"])
    expected_content_actions = Counter({item: 1 for item in contract.content_action_ids})
    actual_content_actions = Counter(state.action_occurrences)
    if isinstance(card_action, dict):
        actual_content_actions.subtract({card_action["id"]: 1})
        actual_content_actions += Counter()
    if actual_content_actions != expected_content_actions:
        raise TerseDslNested2ConversionError("Hybrid content Actions do not match the contract.")
    count, depth = _shape(root)
    if count > contract.limits.max_expanded_components:
        raise TerseDslNested2ConversionError("Hybrid expanded component budget exceeded.")
    if depth > contract.limits.max_nesting_depth:
        raise TerseDslNested2ConversionError("Hybrid component depth budget exceeded.")
    _validate_expanded_tree(root, contract)
    body_budget = _body_budget(card_params, contract, registry)
    space_constrained = content_height > body_budget
    if space_constrained:
        content = _constrain_content_height(content, body_budget)
        root = _compile_card_shell(card_params, content, task_spec, contract, registry)
        root = _apply_theme_text_role(root, text_role)
    effective = _serialize_node(root) + ";"
    a2ui = convert_terse_dsl_nested2_to_a2ui(
        effective,
        size=task_spec.size,
        protocol_profile=protocol_profile,
    )
    if "Template" in a2ui:
        raise TerseDslNested2ConversionError("Template leaked into final A2UI.")
    if any(layout_id in a2ui for layout_id in UX_LAYOUT_COMPONENT_IDS):
        raise TerseDslNested2ConversionError("UX Layout leaked into final A2UI.")
    return HybridCompilation(
        raw_output=source,
        effective_output=effective,
        a2ui=a2ui,
        stats=ExpansionStats(
            template_call_count=state.template_calls + 1,
            template_used_ids=tuple(state.template_ids),
            template_variant_normalization_count=state.template_variant_normalizations,
            template_provider_param_normalization_count=(
                state.template_provider_param_normalizations
            ),
            template_relation_number_normalization_count=(
                state.template_relation_number_normalizations
            ),
            expanded_component_count=count,
            raw_component_count=raw_count,
            max_depth=depth,
            estimated_height_vp=content_height,
            vertical_budget_vp=body_budget,
            space_constrained=space_constrained,
            action_used_ids=tuple(state.action_ids),
        ),
    )


def compile_ux_layout_card(
    source: str,
    *,
    task_spec: TaskSpec,
    contract: HybridBodyContract,
    protocol_profile: dict[str, Any],
    registry: CardPlanRegistry,
    business_title: str | None = None,
) -> HybridCompilation:
    """Compile the fifth-interface layout root without invoking ``card@1``.

    The model chooses one approved UX Layout plus trusted local Templates and a
    bounded Action node. The service still owns root geometry, event binding,
    Theme lowering, validation, and the final standard A2UI conversion.
    """
    composition = parse_ux_layout_card(source)
    composition = _normalize_trusted_composite_text_calls(composition, contract)
    _validate_ux_layout_root(
        composition,
        contract,
        size=task_spec.size,
        registry=registry,
        embedded_actions=True,
    )
    raw_count = _count_calls(composition)
    if raw_count > contract.limits.max_raw_components:
        raise TerseDslNested2ConversionError("Hybrid raw component budget exceeded.")
    _reject_direct_events(composition)
    _validate_raw_components(composition, contract)
    composition, provider_param_normalizations = _normalize_template_provider_params(
        composition,
        task_spec,
        contract,
        registry,
    )
    composition, relation_number_normalizations = _normalize_template_relation_numbers(
        composition,
        contract,
        registry,
    )
    _validate_required_numbers(composition, contract)
    composition = _normalize_recommended_variant_order(composition, registry)
    state = _ExpansionState(
        template_ids=[],
        action_ids=[],
        action_occurrences=[],
        template_provider_param_normalizations=provider_param_normalizations,
        template_relation_number_normalizations=relation_number_normalizations,
    )
    expanded = _expand_call(
        composition,
        parent="$root",
        contract=contract,
        registry=registry,
        state=state,
    )
    _validate_required_template_groups(state, contract)
    embedded_action_count = sum(
        child.kind == "component" and child.name in _UX_ACTION_COMPONENTS
        for child in composition.children
    )
    if len(state.action_occurrences) != embedded_action_count:
        raise TerseDslNested2ConversionError(
            "UX Layout Actions must use the dedicated Action nodes."
        )
    if any(action_id not in contract.content_action_ids for action_id in state.action_occurrences):
        raise TerseDslNested2ConversionError("UX Layout used an unapproved Action.")
    actual_actions = Counter(state.action_occurrences)
    if any(count != 1 for count in actual_actions.values()):
        raise TerseDslNested2ConversionError("UX Layout cannot repeat the same Action.")
    expanded = _append_missing_required_literals_to_ux_layout(expanded, contract)
    expanded = _inject_ux_business_title(expanded, business_title, contract)
    content = _lower_ux_layout_root(
        expanded,
        size=task_spec.size,
        contract=contract,
        registry=registry,
    )
    content = _deduplicate_ux_business_title_fragments(content, business_title)
    content = _lower_capsule_progress(content)
    content = _deduplicate_visible_text(content, task_spec)
    content_height = _estimate_height(content)
    body_budget = _ux_layout_body_budget(registry)
    if content_height > body_budget:
        content = _constrain_content_height(content, body_budget)
    root = _compile_ux_layout_shell(content, contract, registry)
    text_role = registry.require_theme(contract.theme_profile_id).text_role
    root = _apply_theme_text_role(root, text_role)
    count, depth = _shape(root)
    if count > contract.limits.max_expanded_components:
        raise TerseDslNested2ConversionError("Hybrid expanded component budget exceeded.")
    if depth > contract.limits.max_nesting_depth:
        raise TerseDslNested2ConversionError("Hybrid component depth budget exceeded.")
    _validate_expanded_tree(root, contract)
    effective = _serialize_node(root) + ";"
    a2ui = convert_terse_dsl_nested2_to_a2ui(
        effective,
        size=task_spec.size,
        protocol_profile=protocol_profile,
    )
    if "Template" in a2ui:
        raise TerseDslNested2ConversionError("Template leaked into final A2UI.")
    if any(layout_id in a2ui for layout_id in UX_LAYOUT_COMPONENT_IDS):
        raise TerseDslNested2ConversionError("UX Layout leaked into final A2UI.")
    if any(action_id in a2ui for action_id in _UX_ACTION_COMPONENTS):
        raise TerseDslNested2ConversionError("UX Action leaked into final A2UI.")
    return HybridCompilation(
        raw_output=source,
        effective_output=effective,
        a2ui=a2ui,
        stats=ExpansionStats(
            template_call_count=state.template_calls,
            template_used_ids=tuple(state.template_ids),
            template_variant_normalization_count=state.template_variant_normalizations,
            template_provider_param_normalization_count=(
                state.template_provider_param_normalizations
            ),
            template_relation_number_normalization_count=(
                state.template_relation_number_normalizations
            ),
            expanded_component_count=count,
            raw_component_count=raw_count,
            max_depth=depth,
            estimated_height_vp=content_height,
            vertical_budget_vp=body_budget,
            space_constrained=content_height > body_budget,
            action_used_ids=tuple(state.action_ids),
        ),
    )


def _validate_required_template_groups(
    state: _ExpansionState,
    contract: HybridBodyContract,
) -> None:
    """Require one trusted content Template for each selected UX component."""
    used = set(state.template_ids)
    for group in contract.required_template_groups:
        if not used.intersection(group):
            choices = ", ".join(group)
            raise TerseDslNested2ConversionError(
                f"UX business component requires one trusted Template from: {choices}"
            )


def _normalize_trusted_composite_text_calls(
    call: ParsedCall,
    contract: HybridBodyContract,
) -> ParsedCall:
    """Split only delimiter-joined Text values made entirely of trusted facts.

    Models commonly render high/low values as ``26°/16°``. The wire grammar
    intentionally forbids synthesized literals, but this exact decomposition
    is lossless and remains closed over the trusted literal allowlist.
    """
    children = tuple(
        _normalize_trusted_composite_text_calls(child, contract) for child in call.children
    )
    normalized = ParsedCall(call.kind, call.name, call.values, children, call.span)
    if (
        call.kind != "component"
        or call.name != "Text"
        or len(call.values) != 2
        or not isinstance(call.values[0], str)
        or call.values[0] in contract.trusted_literals
        or not isinstance(call.values[1], str)
    ):
        return normalized
    parts = tuple(part.strip() for part in re.split(r"[|｜/·•]+", call.values[0]))
    if not 2 <= len(parts) <= 4 or any(
        not part or part not in contract.trusted_literals for part in parts
    ):
        return normalized
    return ParsedCall(
        "component",
        "Row",
        ("between",),
        tuple(
            ParsedCall("component", "Text", (part, call.values[1]), (), call.span) for part in parts
        ),
        call.span,
    )


def _validate_card_params(
    params: dict[str, Any],
    task_spec: TaskSpec,
    contract: HybridBodyContract,
) -> None:
    unknown = set(params) - {"title", "subtitle", "titleIcon", "action"}
    if unknown:
        raise TerseDslNested2ConversionError(f"Unknown card@1 params: {sorted(unknown)}")
    for key in ("title", "subtitle"):
        if key not in params:
            continue
        value = params[key]
        if not isinstance(value, str) or value not in contract.trusted_literals:
            raise TerseDslNested2ConversionError(f"card@1 {key} is not trusted.")
    if "titleIcon" in params and params["titleIcon"] not in contract.allowed_asset_sources:
        raise TerseDslNested2ConversionError("card@1 titleIcon is not approved.")
    action = params.get("action")
    if action is None:
        return
    if not isinstance(action, dict) or set(action) != {"label", "id"}:
        raise TerseDslNested2ConversionError("card@1 action must contain label and id.")
    pair = (action.get("label"), action.get("id"))
    approved = {(item.display_label, item.action_id) for item in contract.action_bindings}
    if pair not in approved:
        raise TerseDslNested2ConversionError("card@1 action label/id pair is not approved.")
    if action["id"] in contract.content_action_ids:
        raise TerseDslNested2ConversionError("content Action cannot be used by card@1.")
    event_ids = {item.id for item in task_spec.eventCandidates}
    if action["id"] not in event_ids:
        raise TerseDslNested2ConversionError("card@1 action is not in TaskSpec.")


def _normalize_card_params(params: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(params)
    if "icon" not in normalized:
        return normalized
    if "titleIcon" in normalized:
        raise TerseDslNested2ConversionError("card@1 cannot contain icon and titleIcon.")
    normalized["titleIcon"] = normalized.pop("icon")
    return normalized


def _expand_call(
    call: ParsedCall,
    *,
    parent: str,
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
    state: _ExpansionState,
) -> Nested2Node:
    if call.kind == "component":
        if call.name in _UX_ACTION_COMPONENTS:
            return _expand_ux_action_call(call, contract, state)
        child_parent = "Column" if call.name in UX_LAYOUT_COMPONENT_IDS else call.name
        children = tuple(
            _expand_call(
                child,
                parent=child_parent,
                contract=contract,
                registry=registry,
                state=state,
            )
            for child in call.children
        )
        values = _normalize_component_values(call.name, call.values)
        if call.name in {"Column", "List"} and values and values[0] == "compact":
            children = tuple(
                child
                if contract.allowed_layout_component_ids and source.kind == "template"
                else _compact_text_roles(child)
                for source, child in zip(call.children, children, strict=True)
            )
        return Nested2Node(
            call.name,
            values,
            children,
        )
    wire_id = call.name
    if wire_id not in contract.allowed_template_ids:
        raise TerseDslNested2ConversionError(f"Template is not allowed: {wire_id}")
    definition = registry.require_template(wire_id)
    if parent not in definition.allowed_parent_components:
        raise TerseDslNested2ConversionError(f"Template parent is not allowed: {wire_id}/{parent}")
    size, params = call.values
    try:
        variant = registry.require_variant(wire_id, str(size))
    except ValueError as exc:
        if len(definition.variants) != 1:
            raise TerseDslNested2ConversionError(
                f"Template variant is not allowed: {wire_id}/{size}"
            ) from exc
        variant = definition.variants[0]
        state.template_variant_normalizations += 1
    errors = sorted(Draft202012Validator(variant.parameters_schema).iter_errors(params), key=str)
    if errors:
        raise TerseDslNested2ConversionError(
            f"Template params are invalid for {wire_id}/{size}: {errors[0].message}"
        )
    params = _normalize_template_asset_params(
        params,
        definition.asset_parameter_semantic_tags,
        contract,
    )
    _validate_template_params(params, definition.asset_parameter_semantic_tags, contract)
    _validate_template_parameter_relations(params, variant.parameter_relations)
    root = _instantiate_blueprint(variant.root, params)
    root, action_ids = _bind_template_actions(root, contract)
    node_count, depth = _shape(root)
    if node_count > variant.expanded_node_budget or depth > variant.expanded_depth_budget:
        raise TerseDslNested2ConversionError(f"Template blueprint budget drift: {wire_id}/{size}")
    state.template_calls += 1
    state.expanded_components += node_count
    if wire_id not in state.template_ids:
        state.template_ids.append(wire_id)
    for action_id in action_ids:
        state.action_occurrences.append(action_id)
        if action_id not in state.action_ids:
            state.action_ids.append(action_id)
    return root


def _expand_ux_action_call(
    call: ParsedCall,
    contract: HybridBodyContract,
    state: _ExpansionState,
) -> Nested2Node:
    if len(call.values) != 1 or not isinstance(call.values[0], dict):
        raise TerseDslNested2ConversionError(f"{call.name} requires one object argument.")
    params = dict(call.values[0])
    allowed_keys = {"actionId", "icon"}
    if set(params) - allowed_keys:
        raise TerseDslNested2ConversionError(f"{call.name} contains unknown fields.")
    action_id = params.get("actionId")
    if not isinstance(action_id, str):
        raise TerseDslNested2ConversionError(f"{call.name} actionId is invalid.")
    binding = next(
        (item for item in contract.action_bindings if item.action_id == action_id),
        None,
    )
    if binding is None or action_id not in contract.content_action_ids:
        raise TerseDslNested2ConversionError(f"{call.name} Action is not approved.")
    icon = params.get("icon")
    if icon is not None and (
        not isinstance(icon, str) or icon not in contract.allowed_asset_sources
    ):
        raise TerseDslNested2ConversionError(f"{call.name} icon is not approved.")
    if call.name == "IconAction" and not isinstance(icon, str):
        raise TerseDslNested2ConversionError("IconAction requires an approved icon.")
    if action_id not in state.action_ids:
        state.action_ids.append(action_id)
    state.action_occurrences.append(action_id)
    return Nested2Node(call.name, (params,), ())


def _validate_template_params(
    params: dict[str, Any],
    asset_tags: dict[str, tuple[str, ...]],
    contract: HybridBodyContract,
) -> None:
    for key, value in params.items():
        values = _primitive_values(value)
        is_asset_parameter = any(
            token in key.casefold() for token in ("icon", "image", "asset", "source", "src")
        )
        for item in values:
            if item == "":
                continue
            if isinstance(item, str) and is_asset_parameter:
                if item not in contract.allowed_asset_sources:
                    raise TerseDslNested2ConversionError(f"Template asset is not approved: {item}")
                required_tags = set(asset_tags.get(key, ()))
                actual_tags = set(contract.asset_semantic_tags_by_source.get(item, ()))
                if required_tags and not required_tags.issubset(actual_tags):
                    raise TerseDslNested2ConversionError(
                        f"Template asset semantics do not match {key}: {item}"
                    )
            elif isinstance(item, str) and not _is_trusted_template_literal(
                item,
                contract.trusted_literals,
            ):
                action_ids = {binding.action_id for binding in contract.action_bindings}
                if item not in action_ids:
                    raise TerseDslNested2ConversionError(f"Template literal is not trusted: {item}")
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                if item not in contract.trusted_numbers and item not in {0, 1, 100}:
                    raise TerseDslNested2ConversionError(f"Template number is not trusted: {item}")


def _validate_template_parameter_relations(
    params: dict[str, Any],
    relations: tuple[TemplateParameterRelation, ...],
) -> None:
    for relation in relations:
        number = params[relation.number_parameter]
        text = params[relation.text_parameter]
        if relation.kind != "number-matches-text":
            raise TerseDslNested2ConversionError("Unknown Template parameter relation.")
        if not isinstance(number, (int, float)) or isinstance(number, bool):
            raise TerseDslNested2ConversionError("Template relation number is invalid.")
        if not isinstance(text, str):
            raise TerseDslNested2ConversionError("Template relation text is invalid.")
        canonical = str(int(number)) if float(number).is_integer() else str(number)
        candidates = {canonical + suffix for suffix in relation.allowed_suffixes}
        if text not in candidates:
            raise TerseDslNested2ConversionError(
                "Template number/text parameter relation does not match."
            )


def _is_trusted_template_literal(value: str, trusted_literals: tuple[str, ...]) -> bool:
    """允许模板把多个已信任的叶子字面量拼接为一个展示字符串。

    拼接必须完全由 Contract 已提供的非空原子覆盖；不允许从原字符串中
    任意截取，也不增加单位、标点或业务文案。这样可以安全表示例如
    ``"6" + "小时" + "45" + "分"`` 的紧凑组合，同时继续拒绝模型自行派生的文本。
    """
    if value in trusted_literals:
        return True
    atoms = tuple(
        sorted(
            {
                literal
                for literal in trusted_literals
                if literal and literal != value and len(literal) <= len(value)
            },
            key=lambda item: (-len(item), item),
        )
    )
    if not value or not atoms:
        return False
    reachable = {0}
    for index in range(len(value)):
        if index not in reachable:
            continue
        for atom in atoms:
            if value.startswith(atom, index):
                reachable.add(index + len(atom))
    return len(value) in reachable


def _normalize_template_asset_params(
    params: dict[str, Any],
    asset_tags: dict[str, tuple[str, ...]],
    contract: HybridBodyContract,
) -> dict[str, Any]:
    normalized = dict(params)
    for key, value in params.items():
        is_asset_parameter = any(
            token in key.casefold() for token in ("icon", "image", "asset", "source", "src")
        )
        if not is_asset_parameter or not isinstance(value, str) or value == "":
            continue
        if value not in contract.allowed_asset_sources:
            raise TerseDslNested2ConversionError(f"Template asset is not approved: {value}")
        required_tags = set(asset_tags.get(key, ()))
        actual_tags = set(contract.asset_semantic_tags_by_source.get(value, ()))
        if not required_tags or required_tags.issubset(actual_tags):
            continue
        candidates = [
            source
            for source in contract.allowed_asset_sources
            if required_tags.issubset(set(contract.asset_semantic_tags_by_source.get(source, ())))
        ]
        if len(candidates) != 1:
            raise TerseDslNested2ConversionError(
                f"Template asset semantics do not match {key}: {value}"
            )
        normalized[key] = candidates[0]
    return normalized


def _primitive_values(value: Any) -> list[Any]:
    if isinstance(value, dict):
        result: list[Any] = []
        for child in value.values():
            result.extend(_primitive_values(child))
        return result
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_primitive_values(child))
        return result
    return [value]


def _instantiate_blueprint(node: TemplateNode, params: dict[str, Any]) -> Nested2Node:
    values = [_template_value(item, params) for item in node.values]
    normalized = _normalize_blueprint_values(node.component, values)
    return Nested2Node(
        component_type=node.component,
        values=tuple(normalized),
        children=tuple(_instantiate_blueprint(child, params) for child in node.children),
    )


def _template_value(value: TemplateValue, params: dict[str, Any]) -> Any:
    if value.kind == "literal":
        return value.value
    if value.kind == "parameter":
        if value.name not in params:
            raise TerseDslNested2ConversionError(f"Template parameter is missing: {value.name}")
        return params[value.name]
    if value.kind == "array":
        return [_template_value(item, params) for item in value.items]
    return {key: _template_value(item, params) for key, item in value.properties.items()}


def _normalize_blueprint_values(component: str, values: list[Any]) -> list[Any]:
    if (
        component == "Text"
        and values
        and isinstance(values[0], (int, float))
        and not isinstance(values[0], bool)
    ):
        values[0] = str(values[0])
    normalized: list[Any] = []
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("styles"), dict):
            flattened = dict(value["styles"])
            flattened.update({key: child for key, child in value.items() if key != "styles"})
            normalized.append(flattened)
        else:
            normalized.append(value)
    return list(_normalize_component_values(component, tuple(normalized)))


def _bind_template_actions(
    node: Nested2Node,
    contract: HybridBodyContract,
) -> tuple[Nested2Node, tuple[str, ...]]:
    values = list(node.values)
    used: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            continue
        action_id = _template_action_placeholder(value)
        if action_id is None:
            continue
        binding = next(
            (
                item
                for item in contract.action_bindings
                if item.action_id == action_id and item.action_id in contract.content_action_ids
            ),
            None,
        )
        if binding is None:
            raise TerseDslNested2ConversionError(f"Template Action is not approved: {action_id}")
        bound = dict(value)
        bound.pop("action", None)
        bound["onClick"] = [{"call": binding.call, "args": binding.args}]
        values[index] = bound
        used.append(action_id)
    children: list[Nested2Node] = []
    for child in node.children:
        bound_child, child_ids = _bind_template_actions(child, contract)
        children.append(bound_child)
        used.extend(child_ids)
    return Nested2Node(node.component_type, tuple(values), tuple(children)), tuple(used)


def _template_action_placeholder(value: dict[str, Any]) -> str | None:
    if "onClick" in value:
        handlers = value["onClick"]
        if not isinstance(handlers, list) or len(handlers) != 1:
            raise TerseDslNested2ConversionError("Template Action placeholder is invalid.")
        handler = handlers[0]
        args = handler.get("args") if isinstance(handler, dict) else None
        action_id = args.get("eventName") if isinstance(args, dict) else None
        if (
            not isinstance(args, dict)
            or not isinstance(action_id, str)
            or set(args) != {"eventName"}
            or handler.get("call") != "sendToAssistant"
        ):
            raise TerseDslNested2ConversionError("Template Action ID is invalid.")
        return action_id
    if "action" not in value:
        return None
    action = value["action"]
    event = action.get("event") if isinstance(action, dict) else None
    action_id = event.get("name") if isinstance(event, dict) else None
    if (
        not isinstance(action, dict)
        or set(action) != {"event"}
        or not isinstance(event, dict)
        or set(event) != {"name"}
        or not isinstance(action_id, str)
    ):
        raise TerseDslNested2ConversionError("Template Action ID is invalid.")
    return action_id


def _compile_card_shell(
    params: dict[str, Any],
    content: Nested2Node,
    task_spec: TaskSpec,
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> Nested2Node:
    theme = registry.require_theme(contract.theme_profile_id)
    root_options = _normalize_theme_styles(theme.root_styles)
    ux_mixed = bool(contract.allowed_layout_component_ids)
    if ux_mixed:
        root_options.update(
            {
                "padding": registry.ux_tokens["safeInset"],
                "borderRadius": registry.ux_tokens["radius"],
                "itemMargin": registry.ux_tokens["sectionGap"],
            }
        )
    if theme.root_component == "Stack" and "alignContent" in root_options:
        root_options["alignItems"] = root_options.pop("alignContent")
    root_options.pop("width", None)
    root_options.pop("height", None)
    root_options["_id"] = "root"
    children: list[Nested2Node] = []
    header_children: list[Nested2Node] = []
    if "titleIcon" in params:
        title_icon_values: tuple[Any, ...] = (params["titleIcon"], "compact-icon")
        if ux_mixed:
            icon_size = registry.ux_tokens["titleSourceIconSize"]
            title_icon_values = (*title_icon_values, {"width": icon_size, "height": icon_size})
        header_children.append(Nested2Node("Image", title_icon_values, ()))
    header_text: list[Nested2Node] = []
    if "title" in params:
        header_text.append(Nested2Node("Text", (params["title"], "compact-title"), ()))
    if "subtitle" in params:
        header_text.append(Nested2Node("Text", (params["subtitle"], "subtitle"), ()))
    header_height = 34 if len(header_text) > 1 else 18
    if len(header_text) > 1:
        header_children.append(
            Nested2Node(
                "Column",
                (
                    "compact",
                    {
                        "height": 32,
                        "itemMargin": 0,
                        "justifyContent": "start",
                        "layoutWeight": 1,
                    },
                ),
                tuple(header_text),
            )
        )
    else:
        header_children.extend(header_text)
    if header_children:
        children.append(
            Nested2Node(
                "Row",
                (
                    "between",
                    {
                        "height": header_height,
                        "itemMargin": 4,
                        "justifyContent": "start",
                    },
                ),
                tuple(header_children),
            )
        )
    children.append(content)
    action = params.get("action")
    if isinstance(action, dict):
        binding = next(item for item in contract.action_bindings if item.action_id == action["id"])
        event = next(item for item in task_spec.eventCandidates if item.id == binding.action_id)
        action_style = theme.action_style
        action_height = (
            action_style.height
            if action_style is not None
            else registry.ux_tokens["pillActionHeight"]
            if ux_mixed
            else 30
        )
        options = {
            "width": "100%",
            "height": action_height,
            "padding": 2,
            "borderRadius": (
                action_style.border_radius if action_style is not None else action_height / 2
            ),
            "backgroundColor": (
                action_style.background_color if action_style is not None else "#24FFFFFF"
            ),
            "alignContent": "center",
            "onClick": [{"call": event.call, "args": event.args}],
        }
        label_values: tuple[Any, ...] = (binding.display_label, "compact-action")
        if action_style is not None:
            label_values = (
                *label_values,
                {
                    "fontColor": action_style.font_color,
                    "fontSize": action_style.font_size,
                    "fontWeight": action_style.font_weight,
                },
            )
        label = Nested2Node("Text", label_values, ())
        row = Nested2Node("Row", ("actions", {"justifyContent": "center"}), (label,))
        children.append(Nested2Node("Stack", ("overlay", options), (row,)))
    return Nested2Node("Column", ("card", root_options), tuple(children))


def _compile_ux_layout_shell(
    content: Nested2Node,
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> Nested2Node:
    theme = registry.require_theme(contract.theme_profile_id)
    root_options = _normalize_theme_styles(theme.root_styles)
    root_options.update(
        {
            "padding": registry.ux_tokens["safeInset"],
            "borderRadius": registry.ux_tokens["radius"],
            "itemMargin": registry.ux_tokens["sectionGap"],
        }
    )
    if "alignContent" in root_options:
        root_options["alignItems"] = root_options.pop("alignContent")
    root_options.pop("width", None)
    root_options.pop("height", None)
    root_options["_id"] = "root"
    return Nested2Node("Column", ("card", root_options), (content,))


def _strip_direct_card_chrome_from_call(
    content: ParsedCall,
    params: dict[str, Any],
) -> ParsedCall:
    """Remove only model-authored duplicate Text; trusted Templates stay atomic."""
    chrome_literals = {
        value
        for key in ("title", "subtitle")
        if isinstance((value := params.get(key)), str) and value.strip()
    }
    action = params.get("action")
    if isinstance(action, dict):
        label = action.get("label")
        if isinstance(label, str) and label.strip():
            chrome_literals.add(label)
    if not chrome_literals:
        return content
    title = params.get("title")
    title_fragment = _semantic_text_fragment(title) if isinstance(title, str) else ""

    def visit(current: ParsedCall) -> ParsedCall | None:
        if current.kind == "template":
            return current
        text_fragment = (
            _semantic_text_fragment(current.values[0])
            if current.name == "Text" and current.values and isinstance(current.values[0], str)
            else ""
        )
        if (
            current.name == "Text"
            and current.values
            and isinstance(current.values[0], str)
            and (
                current.values[0] in chrome_literals
                or (
                    len(text_fragment) >= 2
                    and bool(title_fragment)
                    and text_fragment in title_fragment
                )
            )
        ):
            return None
        children = tuple(child for item in current.children if (child := visit(item)) is not None)
        if current.children and not children and current.name in _CONTAINERS:
            return None
        return ParsedCall(current.kind, current.name, current.values, children, current.span)

    return visit(content) or ParsedCall(
        "component",
        "Column",
        ("section",),
        (),
        content.span,
    )


def _drop_redundant_card_chrome(
    params: dict[str, Any],
    content: Nested2Node,
) -> dict[str, Any]:
    """Let atomic local Templates own matching title facts and reclaim header space."""
    visible = tuple(
        node.values[0]
        for node in _walk_nodes(content)
        if node.component_type == "Text"
        and node.values
        and isinstance(node.values[0], str)
        and node.values[0].strip()
    )
    visible_blob = "".join(_semantic_text_fragment(item) for item in visible)
    normalized = dict(params)
    for key in ("title", "subtitle"):
        value = params.get(key)
        if not isinstance(value, str):
            continue
        fragments = tuple(
            _semantic_text_fragment(item)
            for item in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+|[%°]", value.casefold())
            if _semantic_text_fragment(item)
        )
        covered = (
            _is_trusted_template_literal(value, visible)
            or _semantic_text_fragment(value) in visible_blob
            or bool(fragments)
            and all(fragment in visible_blob for fragment in fragments)
        )
        if covered:
            normalized.pop(key, None)
    return normalized


def _semantic_text_fragment(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9\u4e00-\u9fff°%]+", value.casefold()))


def _deduplicate_visible_text(node: Nested2Node, task_spec: TaskSpec) -> Nested2Node:
    """Remove model-created duplicate Text while preserving equal independent facts."""
    allowed = Counter(_string_fact_values(task_spec.dataModelSchema))
    seen: Counter[str] = Counter()

    def visit(current: Nested2Node) -> Nested2Node | None:
        if (
            current.component_type == "Text"
            and current.values
            and isinstance(current.values[0], str)
            and current.values[0].strip()
        ):
            literal = current.values[0]
            limit = max(1, allowed[literal])
            seen[literal] += 1
            if seen[literal] > limit:
                return None
        children = tuple(child for item in current.children if (child := visit(item)) is not None)
        if current.children and not children and current.component_type in _CONTAINERS:
            return None
        return Nested2Node(current.component_type, current.values, children)

    return visit(node) or Nested2Node("Column", ("compact",), ())


def _string_fact_values(value: Any) -> list[str]:
    if isinstance(value, dict) and "sampleValue" in value:
        sample = value["sampleValue"]
        return [sample] if isinstance(sample, str) and sample.strip() else []
    if isinstance(value, dict):
        return [item for child in value.values() for item in _string_fact_values(child)]
    if isinstance(value, list):
        return [item for child in value for item in _string_fact_values(child)]
    return []


def _append_missing_required_literals(
    content: Nested2Node,
    contract: HybridBodyContract,
    *,
    already_visible: tuple[str, ...] = (),
) -> Nested2Node:
    """Deterministically preserve mustKeep facts without a third model call."""
    visible_values = tuple(
        node.values[0]
        for node in _walk_nodes(content)
        if node.component_type == "Text" and node.values and isinstance(node.values[0], str)
    )
    visible = "\n".join(visible_values)
    visible_blob = "".join(_semantic_text_fragment(item) for item in visible_values)
    chrome_fragments = tuple(
        _semantic_text_fragment(item) for item in already_visible if _semantic_text_fragment(item)
    )
    missing = [
        literal
        for literal in contract.required_literals
        if literal not in visible
        and _semantic_text_fragment(literal) not in visible_blob
        and literal not in already_visible
        and not (
            len(_semantic_text_fragment(literal)) >= 2
            and any(
                _semantic_text_fragment(literal) in chrome_fragment
                for chrome_fragment in chrome_fragments
            )
        )
    ]
    if not missing:
        return content
    additions = tuple(Nested2Node("Text", (literal, "body"), ()) for literal in missing)
    if 2 <= len(additions) <= 4 and all(
        len(_semantic_text_fragment(literal)) <= 8 for literal in missing
    ):
        additions = (
            Nested2Node(
                "Row",
                (
                    "between",
                    {
                        "width": "100%",
                        "height": 18,
                        "itemMargin": 4,
                        "justifyContent": "spaceBetween",
                        "alignItems": "center",
                    },
                ),
                additions,
            ),
        )
    if content.component_type == "Column":
        return Nested2Node(content.component_type, content.values, (*content.children, *additions))
    return Nested2Node("Column", ("section",), (content, *additions))


def _reclaim_optional_chrome_for_content(
    params: dict[str, Any],
    content: Nested2Node,
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> dict[str, Any]:
    """Drop only a non-required subtitle when it is stealing body space."""
    content_height = _estimate_height(content)
    normalized = dict(params)
    visible_blob = "".join(
        _semantic_text_fragment(node.values[0])
        for node in _walk_nodes(content)
        if node.component_type == "Text" and node.values and isinstance(node.values[0], str)
    )
    action = normalized.get("action")
    if isinstance(action, dict) and isinstance(action.get("label"), str):
        visible_blob += _semantic_text_fragment(action["label"])

    # The title remains owned by card@1. Only the secondary line is eligible
    # for deterministic reclamation; the model may omit an optional title at
    # generation time, but trusted compilation never silently removes one.
    for key in ("subtitle",):
        if content_height <= _body_budget(normalized, contract, registry):
            break
        value = normalized.get(key)
        if not isinstance(value, str):
            continue
        value_fragment = _semantic_text_fragment(value)
        owns_required_fact = any(
            (required_fragment := _semantic_text_fragment(required))
            and required_fragment in value_fragment
            and required_fragment not in visible_blob
            for required in contract.required_literals
        )
        if owns_required_fact:
            continue
        normalized.pop(key, None)
    return normalized


def _apply_theme_text_role(node: Nested2Node, text_role: str) -> Nested2Node:
    """Apply theme foreground semantics to standard Text without overriding alerts."""
    children = tuple(_apply_theme_text_role(child, text_role) for child in node.children)
    if node.component_type != "Text" or text_role != "text-on-accent":
        return Nested2Node(node.component_type, node.values, children)
    values = list(node.values)
    if any(isinstance(value, dict) and "fontColor" in value for value in values[1:]):
        return Nested2Node(node.component_type, tuple(values), children)
    design = values[1] if len(values) > 1 and isinstance(values[1], str) else None
    if design in {"warning", "success"}:
        return Nested2Node(node.component_type, tuple(values), children)
    if values and isinstance(values[-1], dict):
        options = dict(values[-1])
        options["fontColor"] = "#FFFFFFFF"
        values[-1] = options
    else:
        values.append({"fontColor": "#FFFFFFFF"})
    return Nested2Node(node.component_type, tuple(values), children)


def _lower_capsule_progress(node: Nested2Node) -> Nested2Node:
    children = tuple(_lower_capsule_progress(child) for child in node.children)
    if node.component_type != "Progress" or not node.values:
        return Nested2Node(node.component_type, node.values, children)
    options = next((value for value in node.values if isinstance(value, dict)), None)
    if options is None or options.get("type") != "capsule":
        return Nested2Node(node.component_type, node.values, children)
    total = options.get("total")
    value = options.get("value")
    if not isinstance(total, (int, float)) or total <= 0 or not isinstance(value, (int, float)):
        return Nested2Node(node.component_type, node.values, children)
    ratio = max(0.0, min(1.0, value / total))
    height = options.get("height", options.get("strokeWidth", 8))
    width = options.get("width", "100%")
    fill_width: int | float | str
    if isinstance(width, (int, float)):
        fill_width = round(width * ratio, 2)
    else:
        fill_width = f"{round(ratio * 100, 2)}%"
    fill = Nested2Node(
        "Text",
        (
            " ",
            {
                "width": fill_width,
                "height": height,
                "backgroundColor": options.get("color"),
                "borderRadius": height / 2 if isinstance(height, (int, float)) else 4,
                "maxLines": 1,
            },
        ),
        (),
    )
    return Nested2Node(
        "Row",
        (
            {
                "width": width,
                "height": height,
                "justifyContent": "start",
                "alignItems": "center",
            },
        ),
        (fill,),
    )


def _walk_nodes(node: Nested2Node) -> Iterator[Nested2Node]:
    yield node
    for child in node.children:
        yield from _walk_nodes(child)


def _reject_direct_events(node: ParsedCall) -> None:
    for value in node.values:
        if isinstance(value, dict) and _contains_key(value, _DANGEROUS_EVENT_KEYS):
            raise TerseDslNested2ConversionError("Direct events are forbidden in Hybrid content.")
    for child in node.children:
        _reject_direct_events(child)


def _validate_raw_components(node: ParsedCall, contract: HybridBodyContract) -> None:
    if node.kind == "template":
        return
    if node.name not in contract.allowed_components:
        raise TerseDslNested2ConversionError(f"Raw component is not allowed: {node.name}")
    if node.name in _UX_ACTION_COMPONENTS:
        _validate_raw_ux_action(node, contract)
        return
    if node.name in _CONTAINERS and not node.children:
        raise TerseDslNested2ConversionError(
            f"Raw container must contain at least one child: {node.name}"
        )
    if node.name == "Button":
        raise TerseDslNested2ConversionError("Direct Buttons are forbidden in Hybrid content.")
    approved_strings = {
        *contract.trusted_literals,
        *contract.allowed_design_tokens,
        *contract.allowed_layout_tokens,
        *contract.allowed_asset_sources,
    }
    approved_numbers = {*contract.trusted_numbers, 0, 1, 100}
    # Layout configuration is validated against the closed, versioned Registry
    # schema before this generic literal pass. Its enum strings and small
    # integer choices are control-plane values, not business literals.
    values = () if node.name in UX_LAYOUT_COMPONENT_IDS else node.values
    for value in values:
        for item in _primitive_values(value):
            if isinstance(item, str) and item not in approved_strings:
                raise TerseDslNested2ConversionError(f"Raw literal is not trusted: {item}")
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                if item not in approved_numbers:
                    raise TerseDslNested2ConversionError(f"Raw number is not trusted: {item}")
    for child in node.children:
        _validate_raw_components(child, contract)


def _validate_raw_ux_action(node: ParsedCall, contract: HybridBodyContract) -> None:
    if node.children or len(node.values) != 1 or not isinstance(node.values[0], dict):
        raise TerseDslNested2ConversionError(f"{node.name} must be one leaf object call.")
    params = node.values[0]
    if set(params) - {"actionId", "icon"}:
        raise TerseDslNested2ConversionError(f"{node.name} contains unknown fields.")
    action_id = params.get("actionId")
    approved_ids = set(contract.content_action_ids)
    if not isinstance(action_id, str) or action_id not in approved_ids:
        raise TerseDslNested2ConversionError(f"{node.name} Action is not approved.")
    icon = params.get("icon")
    if icon is not None and icon not in contract.allowed_asset_sources:
        raise TerseDslNested2ConversionError(f"{node.name} icon is not approved.")
    if node.name == "IconAction" and not isinstance(icon, str):
        raise TerseDslNested2ConversionError("IconAction requires an approved icon.")


def _validate_ux_layout_root(
    node: ParsedCall,
    contract: HybridBodyContract,
    *,
    size: Literal["2x2", "2x4"],
    registry: CardPlanRegistry,
    embedded_actions: bool = False,
) -> None:
    allowed = set(contract.allowed_layout_component_ids)
    if not allowed:
        return
    if node.kind != "component" or node.name not in allowed:
        raise TerseDslNested2ConversionError(
            "UX Mixed content root must be one approved Layout Component."
        )
    layout = registry.require_ux_layout_component(node.name)
    if len(node.values) > 1 or (node.values and not isinstance(node.values[0], dict)):
        raise TerseDslNested2ConversionError(
            "UX Layout configuration must be one optional object argument."
        )
    parameters = node.values[0] if node.values else {}
    parameter_errors = sorted(
        Draft202012Validator(layout.parameters_schema).iter_errors(parameters),
        key=str,
    )
    if parameter_errors:
        raise TerseDslNested2ConversionError(
            f"UX Layout parameters are invalid for {node.name}: {parameter_errors[0].message}"
        )
    maximum = layout.max_children_by_size[size]
    action_children = tuple(
        child
        for child in node.children
        if child.kind == "component" and child.name in _UX_ACTION_COMPONENTS
    )
    content_children = tuple(child for child in node.children if child not in action_children)
    counted_children = content_children if embedded_actions else node.children
    minimum = layout.minimum_children(size)
    if not minimum <= len(counted_children) <= maximum:
        raise TerseDslNested2ConversionError(
            f"UX Layout child count is invalid: {node.name}/{len(counted_children)}"
        )
    if embedded_actions:
        _validate_ux_layout_action_slot(node, layout, size, action_children)

    def reject_nested_layout(current: ParsedCall) -> None:
        for child in current.children:
            if child.kind == "component" and child.name in UX_LAYOUT_COMPONENT_IDS:
                raise TerseDslNested2ConversionError("UX Layout Components cannot be nested.")
            reject_nested_layout(child)

    reject_nested_layout(node)


def _validate_ux_layout_action_slot(
    node: ParsedCall,
    layout: UxLayoutComponentCapability,
    size: Literal["2x2", "2x4"],
    action_children: tuple[ParsedCall, ...],
) -> None:
    minimum = layout.min_action_children_by_size[size]
    maximum = layout.max_action_children_by_size[size]
    if not minimum <= len(action_children) <= maximum:
        if minimum == maximum == 1 and not action_children:
            raise TerseDslNested2ConversionError("UX Layout requires one embedded Action.")
        if maximum == 0 and action_children:
            raise TerseDslNested2ConversionError("UX Layout does not accept an Action.")
        raise TerseDslNested2ConversionError(
            f"UX Layout Action count is invalid: {node.name}/{len(action_children)}"
        )
    if action_children and node.children[-len(action_children) :] != action_children:
        raise TerseDslNested2ConversionError("UX Layout Actions must be contiguous final children.")
    action_ids = tuple(
        child.values[0].get("actionId")
        for child in action_children
        if child.values and isinstance(child.values[0], dict)
    )
    if len(action_ids) != len(set(action_ids)):
        raise TerseDslNested2ConversionError("UX Layout cannot repeat the same Action.")
    matrix_has_non_tiles = node.name == "ActionMatrixLayout" and any(
        child.name != "ActionTile" for child in action_children
    )
    if matrix_has_non_tiles:
        raise TerseDslNested2ConversionError("ActionMatrixLayout requires ActionTile controls.")


def _lower_ux_layouts(
    node: Nested2Node,
    *,
    size: Literal["2x2", "2x4"],
    has_action: bool,
    registry: CardPlanRegistry,
) -> Nested2Node:
    children = tuple(
        _lower_ux_layouts(
            child,
            size=size,
            has_action=has_action,
            registry=registry,
        )
        for child in node.children
    )
    if node.component_type not in UX_LAYOUT_COMPONENT_IDS:
        return Nested2Node(node.component_type, node.values, children)
    layout = registry.require_ux_layout_component(node.component_type)
    if size not in layout.supported_card_sizes:
        raise TerseDslNested2ConversionError("UX Layout does not support the target card size.")
    maximum = layout.max_children_by_size[size]
    # The raw tree already passed the strict layout contract. Trusted chrome
    # de-duplication may remove one child before lowering, but cannot add or
    # reorder business content.
    minimum = layout.minimum_children(size)
    if not minimum <= len(children) <= maximum:
        raise TerseDslNested2ConversionError(
            f"UX Layout child count is invalid: {node.component_type}/{len(children)}"
        )
    if layout.action_policy == "required" and not has_action:
        raise TerseDslNested2ConversionError("UX Layout requires the card@1 primary Action.")
    gap = registry.ux_tokens["moduleGap"]
    direction = layout.lowering_by_size[size]
    if direction == "column":
        token = "compact" if len(children) == 1 else "section"
        options = {"itemMargin": registry.ux_tokens["denseInnerGap"] if len(children) == 1 else gap}
        return Nested2Node("Column", (token, options), children)
    weighted_children = tuple(
        Nested2Node(
            "Column",
            ("compact", {"layoutWeight": 1, "itemMargin": 0}),
            (child,),
        )
        for child in children
    )
    return Nested2Node(
        "Row",
        ("between", {"itemMargin": gap, "alignItems": "center"}),
        weighted_children,
    )


def _append_missing_required_literals_to_ux_layout(
    node: Nested2Node,
    contract: HybridBodyContract,
) -> Nested2Node:
    content, actions = _split_ux_layout_children(node)
    if not content:
        return node
    already_visible = tuple(
        descendant.values[0]
        for child in content[:-1]
        for descendant in _walk_nodes(child)
        if descendant.component_type == "Text"
        and descendant.values
        and isinstance(descendant.values[0], str)
    )
    completed = _append_missing_required_literals(
        content[-1],
        contract,
        already_visible=already_visible,
    )
    return Nested2Node(
        node.component_type,
        node.values,
        (*content[:-1], completed, *actions),
    )


def _split_ux_layout_children(
    node: Nested2Node,
) -> tuple[tuple[Nested2Node, ...], tuple[Nested2Node, ...]]:
    actions = tuple(
        child for child in node.children if child.component_type in _UX_ACTION_COMPONENTS
    )
    content = tuple(
        child for child in node.children if child.component_type not in _UX_ACTION_COMPONENTS
    )
    return content, actions


def _inject_ux_business_title(
    node: Nested2Node,
    title: str | None,
    contract: HybridBodyContract,
) -> Nested2Node:
    """Project the trusted CardSpec title into the business region when useful."""
    if not isinstance(title, str) or not title.strip() or title not in contract.trusted_literals:
        return node
    normalized_title = _semantic_text_fragment(title)
    visible = tuple(
        descendant.values[0]
        for descendant in _walk_nodes(node)
        if descendant.component_type == "Text"
        and descendant.values
        and isinstance(descendant.values[0], str)
    )
    visible_blob = "".join(_semantic_text_fragment(item) for item in visible)
    if normalized_title and normalized_title in visible_blob:
        return node
    content, actions = _split_ux_layout_children(node)
    if not content:
        return node
    title_font_size = 10 if len(normalized_title) > 8 else 14
    title_node = Nested2Node(
        "Text",
        (
            title,
            "compact-title",
            {
                "width": "100%",
                "fontSize": title_font_size,
                "minFontSize": 9,
                "maxLines": 1,
                "textOverflow": "ellipsis",
            },
        ),
        (),
    )
    first = content[0]
    if first.component_type in {"Column", "List"}:
        first = Nested2Node(first.component_type, first.values, (title_node, *first.children))
    else:
        first = Nested2Node("Column", ("compact",), (title_node, first))
    return Nested2Node(node.component_type, node.values, (first, *content[1:], *actions))


def _deduplicate_ux_business_title_fragments(
    node: Nested2Node,
    title: str | None,
) -> Nested2Node:
    """Let one visible business title own its later literal fragments."""
    if not isinstance(title, str) or not title.strip():
        return node
    title_fragment = _semantic_text_fragment(title)
    has_title = any(
        descendant.component_type == "Text" and descendant.values and descendant.values[0] == title
        for descendant in _walk_nodes(node)
    )
    if not has_title:
        return node

    def visit(current: Nested2Node) -> Nested2Node | None:
        if (
            current.component_type == "Text"
            and current.values
            and isinstance(current.values[0], str)
            and current.values[0] != title
        ):
            fragment = _semantic_text_fragment(current.values[0])
            if len(fragment) >= 2 and fragment in title_fragment:
                return None
        children = tuple(child for item in current.children if (child := visit(item)) is not None)
        if current.children and not children and current.component_type in _CONTAINERS:
            return None
        return Nested2Node(current.component_type, current.values, children)

    return visit(node) or node


def _lower_ux_layout_root(
    node: Nested2Node,
    *,
    size: Literal["2x2", "2x4"],
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> Nested2Node:
    if node.component_type not in UX_LAYOUT_COMPONENT_IDS:
        raise TerseDslNested2ConversionError("UX Mixed root is not a Layout Component.")
    layout = registry.require_ux_layout_component(node.component_type)
    configuration = dict(node.values[0]) if node.values else {}
    content, actions = _split_ux_layout_children(node)
    lowered_content = tuple(
        _lower_ux_layouts(
            child,
            size=size,
            has_action=False,
            registry=registry,
        )
        for child in content
    )
    lowered_actions = tuple(
        _lower_ux_action(
            child,
            size=size,
            contract=contract,
            registry=registry,
            allow_action_tile_2x2=node.component_type == "ActionMatrixLayout",
            action_tile_orientation=(
                "vertical" if node.component_type == "ActionMatrixLayout" else "horizontal"
            ),
        )
        for child in actions
    )
    if (
        size == "2x2"
        and node.component_type == "WeatherNowForecastLayout"
        and len(actions) == 1
        and actions[0].component_type == "PillAction"
        and isinstance(actions[0].values[0], dict)
        and isinstance(actions[0].values[0].get("icon"), str)
    ):
        # The UX contract reserves the bottom-right weather control for the
        # compact icon treatment. Normalizing here keeps the event binding and
        # asset checks trusted while preventing a model-selected pill from
        # consuming one third of a 2x2 weather card.
        lowered_actions = (
            _lower_ux_action(
                Nested2Node("IconAction", actions[0].values, ()),
                size=size,
                contract=contract,
                registry=registry,
                allow_action_tile_2x2=False,
                action_tile_orientation="horizontal",
            ),
        )
    if (
        not layout.minimum_children(size)
        <= len(lowered_content)
        <= layout.max_children_by_size[size]
    ):
        raise TerseDslNested2ConversionError("UX Layout content budget changed during expansion.")
    return _lower_registered_ux_layout(
        node.component_type,
        lowered_content,
        lowered_actions,
        configuration=configuration,
        size=size,
        contract=contract,
        registry=registry,
    )


def _lower_registered_ux_layout(
    layout_id: str,
    content: tuple[Nested2Node, ...],
    actions: tuple[Nested2Node, ...],
    *,
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> Nested2Node:
    if layout_id == "SingleFocusLayout":
        return _lower_single_focus_layout(content, actions, configuration, size, registry)
    if layout_id == "HeroActionLayout":
        return _lower_hero_action_layout(content, actions, configuration, size, registry)
    if layout_id == "HeroSupportLayout":
        return _lower_hero_support_layout(
            content,
            configuration,
            size,
            _ux_support_surface_color(contract, registry),
            registry,
        )
    if layout_id == "HeroSupportActionLayout":
        return _lower_hero_support_action_layout(
            content,
            actions,
            configuration,
            size,
            _ux_support_surface_color(contract, registry),
            contract,
            registry,
        )
    if layout_id == "PeerPairLayout":
        return _lower_peer_pair_layout(content, actions, configuration, size, registry)
    if layout_id == "SequentialSummaryLayout":
        return _lower_sequential_summary_layout(
            content,
            configuration,
            size,
            _ux_support_surface_color(contract, registry),
            registry,
        )
    if layout_id == "EqualItemsLayout":
        return _lower_equal_items_layout(content, configuration, size, registry)
    if layout_id == "ListActionLayout":
        return _lower_list_action_layout(content, actions, configuration, size, registry)
    if layout_id == "ActionMatrixLayout":
        return _lower_action_matrix_layout(content, actions, configuration, size, registry)
    if layout_id == "WeatherNowForecastLayout":
        return _lower_weather_now_forecast_layout(
            content,
            actions,
            size,
            _ux_support_surface_color(contract, registry),
            registry,
        )
    raise TerseDslNested2ConversionError(f"Unsupported UX Layout lowering: {layout_id}")


def _lower_single_focus_layout(
    content: tuple[Nested2Node, ...],
    actions: tuple[Nested2Node, ...],
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    registry: CardPlanRegistry,
) -> Nested2Node:
    align = configuration.get("contentAlign", "topStart")
    justify = {"topStart": "start", "centerStart": "center", "bottomStart": "end"}[align]
    base = Nested2Node(
        "Column",
        (
            "compact",
            {
                "width": "100%",
                "height": "100%",
                "itemMargin": registry.ux_tokens["denseInnerGap"],
                "justifyContent": justify,
                "alignItems": "start",
                "clip": True,
            },
        ),
        content,
    )
    return _place_optional_layout_action(base, actions, size=size, registry=registry)


def _lower_hero_action_layout(
    content: tuple[Nested2Node, ...],
    actions: tuple[Nested2Node, ...],
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    registry: CardPlanRegistry,
) -> Nested2Node:
    placement = configuration.get("actionPlacement", "bottom")
    if size == "2x2" and placement == "end":
        raise TerseDslNested2ConversionError(
            "HeroActionLayout actionPlacement=end is only available for 2x4."
        )
    base = _single_region(content[0], justify="start", registry=registry)
    return _place_optional_layout_action(
        base,
        actions,
        size=size,
        registry=registry,
        placement=placement,
    )


def _lower_hero_support_layout(
    content: tuple[Nested2Node, ...],
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    support_surface_color: str,
    registry: CardPlanRegistry,
) -> Nested2Node:
    default_ratio = "balanced" if size == "2x4" else "heroWide"
    ratio = configuration.get("ratio", default_ratio)
    direction = configuration.get("direction", "auto")
    if direction == "auto":
        direction = "horizontal" if size == "2x4" else _auto_pair_direction(content)
    weights = {
        "balanced": (50, 50),
        "heroWide": (56, 44),
        "supportWide": (44, 56),
    }[ratio]
    support = content[1]
    if size == "2x4" and _is_textual_region(support):
        support = _support_panel(support, support_surface_color, registry)
    regions = (content[0], support)
    if direction == "horizontal":
        return _weighted_row(regions, weights, registry)
    return _weighted_column(regions, weights, registry)


def _lower_hero_support_action_layout(
    content: tuple[Nested2Node, ...],
    actions: tuple[Nested2Node, ...],
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    support_surface_color: str,
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> Nested2Node:
    if size == "2x2":
        if _compact_support_overflows(content[0], content[1], actions[0], registry):
            required = set(contract.required_literals)
            support_literals = {
                str(item.values[0])
                for item in _walk_nodes(content[1])
                if item.component_type == "Text" and item.values
            }
            if required & support_literals:
                raise TerseDslNested2ConversionError(
                    "HeroSupportActionLayout cannot drop required Support content on 2x2."
                )
            hero = _single_region(content[0], justify="start", registry=registry)
            return _place_optional_layout_action(
                hero,
                actions,
                size=size,
                registry=registry,
            )
        hero = _with_flex_weight(content[0], 1, axis="vertical")
        support = _merge_node_options(content[1], {"height": 36, "clip": True})
        base = Nested2Node(
            "Column",
            (
                "section",
                {
                    "width": "100%",
                    "height": "100%",
                    "itemMargin": registry.ux_tokens["moduleGap"],
                },
            ),
            (hero, support),
        )
        return _place_optional_layout_action(base, actions, size=size, registry=registry)
    ratio = configuration.get("heroRatio", "wide")
    weights = (56, 44) if ratio == "wide" else (50, 50)
    support = content[1]
    if _is_textual_region(support):
        support = _support_panel(support, support_surface_color, registry)
    support = _with_flex_weight(support, 1, axis="vertical")
    support_action = Nested2Node(
        "Column",
        (
            "section",
            {
                "width": "100%",
                "height": "100%",
                "itemMargin": registry.ux_tokens["moduleGap"],
                "justifyContent": "spaceBetween",
            },
        ),
        (support, actions[0]),
    )
    return _weighted_row((content[0], support_action), weights, registry)


def _compact_support_overflows(
    hero: Nested2Node,
    support: Nested2Node,
    action: Nested2Node,
    registry: CardPlanRegistry,
) -> bool:
    support_line_count = sum(item.component_type == "Text" for item in _walk_nodes(support))
    if support_line_count > 2:
        return True
    support_height = min(_estimate_height(support), registry.ux_tokens["pillActionHeight"])
    if _is_icon_action_node(action, registry):
        action_height = 0
        gap_count = 1
    else:
        action_height = registry.ux_tokens["pillActionHeight"]
        gap_count = 2
    required_height = (
        _estimate_height(hero)
        + support_height
        + action_height
        + registry.ux_tokens["moduleGap"] * gap_count
    )
    return required_height > _ux_layout_body_budget(registry)


def _lower_peer_pair_layout(
    content: tuple[Nested2Node, ...],
    actions: tuple[Nested2Node, ...],
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    registry: CardPlanRegistry,
) -> Nested2Node:
    orientation = configuration.get("orientation", "auto")
    if actions and size == "2x2":
        orientation = "columns"
    elif orientation == "auto":
        orientation = "columns" if size == "2x4" else _auto_peer_orientation(content)
    if orientation == "columns":
        base = _weighted_row(content, (50, 50), registry)
    else:
        base = _weighted_column(content, (50, 50), registry)
    return _place_optional_layout_action(base, actions, size=size, registry=registry)


def _lower_sequential_summary_layout(
    content: tuple[Nested2Node, ...],
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    support_surface_color: str,
    registry: CardPlanRegistry,
) -> Nested2Node:
    primary = _with_flex_weight(content[0], 1, axis="vertical")
    details = tuple(_support_panel(item, support_surface_color, registry) for item in content[1:])
    requested_columns = configuration.get("detailColumns", len(details))
    column_limit = 2 if size == "2x2" else 4
    columns = min(requested_columns, column_limit, len(details))
    detail_grid = _equal_grid(details, columns=max(1, columns), registry=registry)
    detail_grid = _with_flex_weight(detail_grid, 1, axis="vertical")
    return Nested2Node(
        "Column",
        (
            "section",
            {
                "width": "100%",
                "height": "100%",
                "itemMargin": registry.ux_tokens["moduleGap"],
            },
        ),
        (primary, detail_grid),
    )


def _lower_equal_items_layout(
    content: tuple[Nested2Node, ...],
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    registry: CardPlanRegistry,
) -> Nested2Node:
    arrangement = configuration.get("arrangement", "auto")
    if arrangement == "auto":
        arrangement = "grid" if size == "2x4" and len(content) == 4 else "row"
    modules = tuple(_equal_item_panel(item, registry) for item in content)
    columns = 2 if arrangement == "grid" else len(modules)
    return _equal_grid(modules, columns=columns, registry=registry)


def _lower_list_action_layout(
    content: tuple[Nested2Node, ...],
    actions: tuple[Nested2Node, ...],
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    registry: CardPlanRegistry,
) -> Nested2Node:
    placement = configuration.get("actionPlacement", "bottom")
    if size == "2x2" and placement == "end":
        raise TerseDslNested2ConversionError(
            "ListActionLayout actionPlacement=end is only available for 2x4."
        )
    base = _single_region(content[0], justify="start", registry=registry)
    return _place_optional_layout_action(
        base,
        actions,
        size=size,
        registry=registry,
        placement=placement,
    )


def _lower_action_matrix_layout(
    content: tuple[Nested2Node, ...],
    actions: tuple[Nested2Node, ...],
    configuration: dict[str, Any],
    size: Literal["2x2", "2x4"],
    registry: CardPlanRegistry,
) -> Nested2Node:
    primary_index = configuration.get("primaryActionIndex", 0)
    if not 0 <= primary_index < len(actions):
        raise TerseDslNested2ConversionError(
            "ActionMatrixLayout primaryActionIndex exceeds the Action count."
        )
    ordered = actions
    if primary_index != 0:
        primary = actions[primary_index]
        ordered = (primary, *(item for index, item in enumerate(actions) if index != primary_index))
    matrix = _action_matrix_grid(ordered, size=size, registry=registry)
    if not content:
        return matrix
    summary = _single_region(content[0], justify="end", registry=registry)
    if size == "2x2":
        summary = _with_flex_weight(summary, 1, axis="vertical")
        matrix = _with_flex_weight(matrix, 1, axis="vertical")
        return Nested2Node(
            "Column",
            (
                "section",
                {
                    "width": "100%",
                    "height": "100%",
                    "itemMargin": registry.ux_tokens["moduleGap"],
                },
            ),
            (summary, matrix),
        )
    return _weighted_row((summary, matrix), (56, 44), registry)


def _lower_weather_now_forecast_layout(
    content: tuple[Nested2Node, ...],
    actions: tuple[Nested2Node, ...],
    size: Literal["2x2", "2x4"],
    support_surface_color: str,
    registry: CardPlanRegistry,
) -> Nested2Node:
    current = _single_region(content[0], justify="start", registry=registry)
    if size == "2x2":
        return _place_optional_layout_action(current, actions, size=size, registry=registry)
    if len(content) == 1:
        base = current
    else:
        forecast_items = tuple(
            _support_panel(item, support_surface_color, registry) for item in content[1:]
        )
        forecast_row = _equal_grid(
            forecast_items,
            columns=len(forecast_items),
            registry=registry,
        )
        current = _with_flex_weight(current, 3, axis="vertical")
        forecast_row = _with_flex_weight(forecast_row, 2, axis="vertical")
        base = Nested2Node(
            "Column",
            (
                "section",
                {
                    "width": "100%",
                    "height": "100%",
                    "itemMargin": registry.ux_tokens["moduleGap"],
                },
            ),
            (current, forecast_row),
        )
    return _place_optional_layout_action(base, actions, size=size, registry=registry)


def _single_region(
    child: Nested2Node,
    *,
    justify: str,
    registry: CardPlanRegistry,
) -> Nested2Node:
    return Nested2Node(
        "Column",
        (
            "compact",
            {
                "width": "100%",
                "height": "100%",
                "itemMargin": registry.ux_tokens["denseInnerGap"],
                "justifyContent": justify,
                "alignItems": "start",
                "clip": True,
                "constraintSize": {"minWidth": 0, "minHeight": 0},
            },
        ),
        (child,),
    )


def _weighted_row(
    children: tuple[Nested2Node, ...],
    weights: tuple[int, ...],
    registry: CardPlanRegistry,
) -> Nested2Node:
    regions = tuple(
        Nested2Node(
            "Column",
            (
                "compact",
                {
                    "layoutWeight": weight,
                    "itemMargin": 0,
                    "clip": True,
                    "constraintSize": {"minWidth": 0, "minHeight": 0},
                },
            ),
            (child,),
        )
        for child, weight in zip(children, weights, strict=True)
    )
    return Nested2Node(
        "Row",
        (
            "between",
            {
                "width": "100%",
                "height": "100%",
                "itemMargin": registry.ux_tokens["moduleGap"],
                "alignItems": "center",
            },
        ),
        regions,
    )


def _weighted_column(
    children: tuple[Nested2Node, ...],
    weights: tuple[int, ...],
    registry: CardPlanRegistry,
) -> Nested2Node:
    regions = tuple(
        Nested2Node(
            "Column",
            (
                "compact",
                {
                    "layoutWeight": weight,
                    "itemMargin": 0,
                    "clip": True,
                    "constraintSize": {"minWidth": 0, "minHeight": 0},
                },
            ),
            (child,),
        )
        for child, weight in zip(children, weights, strict=True)
    )
    return Nested2Node(
        "Column",
        (
            "section",
            {
                "width": "100%",
                "height": "100%",
                "itemMargin": registry.ux_tokens["moduleGap"],
            },
        ),
        regions,
    )


def _support_panel(
    child: Nested2Node,
    background: str,
    registry: CardPlanRegistry,
) -> Nested2Node:
    padding = registry.ux_tokens["moduleGap"]
    return Nested2Node(
        "Stack",
        (
            "overlay",
            {
                "width": "100%",
                "height": "100%",
                "padding": {
                    "left": 12,
                    "top": padding,
                    "right": 12,
                    "bottom": padding,
                },
                "borderRadius": 8,
                "backgroundColor": background,
                "alignContent": "topStart",
                "clip": True,
            },
        ),
        (child,),
    )


def _equal_item_panel(child: Nested2Node, registry: CardPlanRegistry) -> Nested2Node:
    padding = registry.ux_tokens["moduleGap"]
    return Nested2Node(
        "Stack",
        (
            "overlay",
            {
                "width": "100%",
                "height": "100%",
                "padding": padding,
                "borderRadius": 8,
                "alignContent": "center",
                "clip": True,
            },
        ),
        (child,),
    )


def _equal_grid(
    children: tuple[Nested2Node, ...],
    *,
    columns: int,
    registry: CardPlanRegistry,
) -> Nested2Node:
    rows: list[Nested2Node] = []
    for start in range(0, len(children), columns):
        row_children = children[start : start + columns]
        weights = tuple(1 for _item in row_children)
        rows.append(_weighted_row(row_children, weights, registry))
    if len(rows) == 1:
        return rows[0]
    weighted_rows = tuple(_with_flex_weight(row, 1, axis="vertical") for row in rows)
    return Nested2Node(
        "Column",
        (
            "section",
            {
                "width": "100%",
                "height": "100%",
                "itemMargin": registry.ux_tokens["moduleGap"],
            },
        ),
        weighted_rows,
    )


def _action_matrix_grid(
    actions: tuple[Nested2Node, ...],
    *,
    size: Literal["2x2", "2x4"],
    registry: CardPlanRegistry,
) -> Nested2Node:
    if len(actions) == 3:
        secondary = _equal_grid(actions[1:], columns=2, registry=registry)
        primary = _with_flex_weight(actions[0], 1, axis="vertical")
        secondary = _with_flex_weight(secondary, 1, axis="vertical")
        return Nested2Node(
            "Column",
            (
                "section",
                {
                    "width": "100%",
                    "height": "100%",
                    "itemMargin": registry.ux_tokens["moduleGap"],
                },
            ),
            (primary, secondary),
        )
    columns = 2 if len(actions) == 4 else len(actions)
    if size == "2x2":
        columns = 2
    return _equal_grid(actions, columns=columns, registry=registry)


def _place_optional_layout_action(
    base: Nested2Node,
    actions: tuple[Nested2Node, ...],
    *,
    size: Literal["2x2", "2x4"],
    registry: CardPlanRegistry,
    placement: str = "bottom",
) -> Nested2Node:
    if not actions:
        return base
    action = actions[0]
    if _is_icon_action_node(action, registry):
        return _overlay_icon_action(base, action, registry)
    if size == "2x4" and placement == "end":
        action_region = Nested2Node(
            "Column",
            (
                "compact",
                {
                    "width": "100%",
                    "height": "100%",
                    "itemMargin": 0,
                    "justifyContent": "end",
                    "alignItems": "start",
                },
            ),
            (action,),
        )
        return _weighted_row((base, action_region), (60, 40), registry)
    if size == "2x2":
        base = _compact_2x2_action_content(base, registry)
    base = _with_flex_weight(base, 1, axis="vertical")
    return Nested2Node(
        "Column",
        (
            "section",
            {
                "width": "100%",
                "height": "100%",
                "itemMargin": registry.ux_tokens["moduleGap"],
                "justifyContent": "spaceBetween",
            },
        ),
        (base, action),
    )


def _is_icon_action_node(action: Nested2Node, registry: CardPlanRegistry) -> bool:
    if action.component_type != "Stack":
        return False
    options = next((value for value in action.values if isinstance(value, dict)), {})
    return options.get("width") == registry.ux_tokens["iconActionSize"]


def _auto_pair_direction(children: tuple[Nested2Node, ...]) -> str:
    return "horizontal" if any(_contains_visual_region(item) for item in children) else "vertical"


def _auto_peer_orientation(children: tuple[Nested2Node, ...]) -> str:
    return "columns" if any(_contains_visual_region(item) for item in children) else "rows"


def _contains_visual_region(node: Nested2Node) -> bool:
    return any(item.component_type in {"Image", "Progress"} for item in _walk_nodes(node))


def _is_textual_region(node: Nested2Node) -> bool:
    return not _contains_visual_region(node)


def _ux_support_surface_color(
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> str:
    theme = registry.require_theme(contract.theme_profile_id)
    return "#24FFFFFF" if theme.text_role == "text-on-accent" else "#14000000"


def _merge_node_options(node: Nested2Node, additions: dict[str, Any]) -> Nested2Node:
    values = list(node.values)
    options_index = next(
        (index for index, value in enumerate(values) if isinstance(value, dict)),
        None,
    )
    options = dict(values[options_index]) if options_index is not None else {}
    options.update(additions)
    if options_index is None:
        values.append(options)
    else:
        values[options_index] = options
    return Nested2Node(node.component_type, tuple(values), node.children)


def _with_flex_weight(
    node: Nested2Node,
    weight: int,
    *,
    axis: Literal["horizontal", "vertical"],
) -> Nested2Node:
    values = list(node.values)
    options_index = next(
        (index for index, value in enumerate(values) if isinstance(value, dict)),
        None,
    )
    options = dict(values[options_index]) if options_index is not None else {}
    options.pop("width" if axis == "horizontal" else "height", None)
    options.update(
        {
            "layoutWeight": weight,
            "clip": True,
            "constraintSize": {"minWidth": 0, "minHeight": 0},
        }
    )
    if options_index is None:
        values.append(options)
    else:
        values[options_index] = options
    return Nested2Node(node.component_type, tuple(values), node.children)


def _compact_2x2_action_content(
    node: Nested2Node,
    registry: CardPlanRegistry,
) -> Nested2Node:
    """Compact generic metric rows before reserving the fixed Action slot.

    This is deliberately structural: it does not inspect fixture IDs, business
    domains, labels, or literal values. Adjacent short text-only rows represent
    one inline metric group in the UX grammar and otherwise waste the limited
    vertical budget when emitted as separate rows by a model.
    """
    children = tuple(_compact_2x2_action_content(child, registry) for child in node.children)
    current = Nested2Node(node.component_type, node.values, children)
    if current.component_type not in {"Column", "List"}:
        return current
    normalized_children = list(current.children)
    for index, child in enumerate(normalized_children):
        if (
            child.component_type != "Text"
            or not child.values
            or not isinstance(child.values[0], str)
            or re.fullmatch(r"[+-]?\d+(?:\.\d+)?", child.values[0].strip()) is None
        ):
            continue
        unit_index = next(
            (
                candidate_index
                for candidate_index in range(index + 1, len(normalized_children))
                for candidate in (normalized_children[candidate_index],)
                if candidate.component_type == "Text"
                and candidate.values
                and isinstance(candidate.values[0], str)
                and not re.search(r"\d", candidate.values[0])
                and 1 <= len(_semantic_text_fragment(candidate.values[0])) <= 3
                and len(candidate.values) > 1
                and candidate.values[1] in {"body", "subtitle"}
            ),
            None,
        )
        if unit_index is None:
            continue
        unit = normalized_children.pop(unit_index)
        normalized_children[index] = Nested2Node(
            "Row",
            (
                "between",
                {
                    "width": "100%",
                    "height": 24,
                    "itemMargin": registry.ux_tokens["denseInnerGap"],
                    "alignItems": "bottom",
                },
            ),
            (child, unit),
        )
        break
    merged: list[Nested2Node] = []
    pending_rows: list[Nested2Node] = []

    def flush_rows() -> None:
        if len(pending_rows) < 2:
            merged.extend(pending_rows)
        else:
            merged.append(
                Nested2Node(
                    "Row",
                    (
                        "between",
                        {
                            "width": "100%",
                            "height": 24,
                            "itemMargin": registry.ux_tokens["denseInnerGap"],
                            "alignItems": "bottom",
                        },
                    ),
                    tuple(child for row in pending_rows for child in row.children),
                )
            )
        pending_rows.clear()

    for child in normalized_children:
        is_short_text_row = (
            child.component_type == "Row"
            and 1 <= len(child.children) <= 3
            and all(
                item.component_type == "Text"
                and item.values
                and isinstance(item.values[0], str)
                and len(_semantic_text_fragment(item.values[0])) <= 6
                for item in child.children
            )
        )
        if is_short_text_row:
            pending_rows.append(child)
            continue
        flush_rows()
        merged.append(child)
    flush_rows()
    compacted = (
        current
        if tuple(merged) == current.children
        else Nested2Node(current.component_type, current.values, tuple(merged))
    )
    return _merge_node_options(
        compacted,
        {"itemMargin": registry.ux_tokens["denseInnerGap"]},
    )


def _overlay_icon_action(
    content: Nested2Node,
    action: Nested2Node,
    registry: CardPlanRegistry,
) -> Nested2Node:
    reserved = registry.ux_tokens["iconActionSize"] + registry.ux_tokens["moduleGap"]
    reserved_content = _merge_node_options(
        content,
        {
            "padding": {"right": reserved, "bottom": reserved},
            "clip": True,
        },
    )
    content_layer = Nested2Node(
        "Stack",
        (
            "overlay",
            {
                "width": "100%",
                "height": "100%",
                "alignContent": "topStart",
            },
        ),
        (reserved_content,),
    )
    action_layer = Nested2Node(
        "Stack",
        (
            "overlay",
            {
                "width": "100%",
                "height": "100%",
                "alignContent": "bottomEnd",
            },
        ),
        (action,),
    )
    return Nested2Node(
        "Stack",
        ("overlay", {"width": "100%", "height": "100%"}),
        (content_layer, action_layer),
    )


def _lower_ux_action(
    node: Nested2Node,
    *,
    size: Literal["2x2", "2x4"],
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
    allow_action_tile_2x2: bool = False,
    action_tile_orientation: Literal["horizontal", "vertical"] = "horizontal",
) -> Nested2Node:
    if len(node.values) != 1 or not isinstance(node.values[0], dict):
        raise TerseDslNested2ConversionError("UX Action parameters are invalid.")
    params = node.values[0]
    action_id = params.get("actionId")
    binding = next(
        (item for item in contract.action_bindings if item.action_id == action_id),
        None,
    )
    if binding is None:
        raise TerseDslNested2ConversionError("UX Action binding is unavailable.")
    theme_action = registry.require_theme(contract.theme_profile_id).action_style
    background = theme_action.background_color if theme_action else "#1A0A59F7"
    foreground = theme_action.font_color if theme_action else "#FF0A59F7"
    event = [{"call": binding.call, "args": binding.args}]
    icon = params.get("icon")
    if node.component_type == "IconAction":
        return _lower_icon_action(icon, background, foreground, event, registry)
    if node.component_type == "ActionTile":
        if size != "2x4" and not allow_action_tile_2x2:
            raise TerseDslNested2ConversionError("ActionTile is only available for 2x4.")
        return _lower_action_tile(
            binding.display_label,
            icon,
            background,
            foreground,
            event,
            orientation=action_tile_orientation,
        )
    return _lower_pill_action(
        binding.display_label,
        icon,
        background,
        foreground,
        event,
        registry,
    )


def _lower_pill_action(
    label: str,
    icon: Any,
    background: str,
    foreground: str,
    event: list[dict[str, Any]],
    registry: CardPlanRegistry,
) -> Nested2Node:
    height = registry.ux_tokens["pillActionHeight"]
    action_children: list[Nested2Node] = []
    if isinstance(icon, str):
        icon_size = registry.ux_tokens["pillActionIconSize"]
        action_children.append(
            Nested2Node(
                "Image",
                (
                    icon,
                    "icon",
                    {
                        "width": icon_size,
                        "height": icon_size,
                        "objectFit": "contain",
                        "fillColor": foreground,
                    },
                ),
                (),
            )
        )
    action_children.append(
        Nested2Node(
            "Text",
            (
                label,
                "compact-action",
                {"fontColor": foreground, "fontSize": 14, "fontWeight": 500},
            ),
            (),
        )
    )
    row = Nested2Node(
        "Row",
        ("actions", {"itemMargin": 8, "justifyContent": "center"}),
        tuple(action_children),
    )
    return Nested2Node(
        "Stack",
        (
            "overlay",
            {
                "width": "100%",
                "height": height,
                "padding": 8,
                "borderRadius": height / 2,
                "backgroundColor": background,
                "alignContent": "center",
                "onClick": event,
            },
        ),
        (row,),
    )


def _lower_icon_action(
    icon: Any,
    background: str,
    foreground: str,
    event: list[dict[str, Any]],
    registry: CardPlanRegistry,
) -> Nested2Node:
    if not isinstance(icon, str):
        raise TerseDslNested2ConversionError("IconAction requires an approved icon.")
    if re.search(r"(?:^|[_-])white(?:[_.-]|$)", icon.casefold()):
        background, foreground = foreground, "#FFFFFFFF"
    size = registry.ux_tokens["iconActionSize"]
    icon_size = registry.ux_tokens["iconActionIconSize"]
    image = Nested2Node(
        "Image",
        (
            icon,
            "icon",
            {
                "width": icon_size,
                "height": icon_size,
                "objectFit": "contain",
                "fillColor": foreground,
            },
        ),
        (),
    )
    return Nested2Node(
        "Stack",
        (
            "overlay",
            {
                "width": size,
                "height": size,
                "borderRadius": size / 2,
                "backgroundColor": background,
                "alignContent": "center",
                "onClick": event,
            },
        ),
        (image,),
    )


def _lower_action_tile(
    label: str,
    icon: Any,
    background: str,
    foreground: str,
    event: list[dict[str, Any]],
    *,
    orientation: Literal["horizontal", "vertical"],
) -> Nested2Node:
    children: list[Nested2Node] = []
    if isinstance(icon, str):
        children.append(
            Nested2Node(
                "Image",
                (
                    icon,
                    "icon",
                    {
                        "width": 16,
                        "height": 16,
                        "objectFit": "contain",
                        "fillColor": foreground,
                    },
                ),
                (),
            )
        )
    children.append(
        Nested2Node(
            "Text",
            (
                label,
                "compact-action",
                {"fontColor": foreground, "fontSize": 12, "fontWeight": 500},
            ),
            (),
        )
    )
    container = "Column" if orientation == "vertical" else "Row"
    inner_layout = "compact" if orientation == "vertical" else "actions"
    height: int | str = "100%" if orientation == "vertical" else 36
    return Nested2Node(
        "Stack",
        (
            "overlay",
            {
                "width": "100%",
                "height": height,
                "padding": 8,
                "borderRadius": 8,
                "backgroundColor": background,
                "alignContent": "center",
                "onClick": event,
            },
        ),
        (
            Nested2Node(
                container,
                (inner_layout, {"itemMargin": 4 if orientation == "vertical" else 8}),
                tuple(children),
            ),
        ),
    )


def _contains_key(value: Any, keys: frozenset[str]) -> bool:
    if isinstance(value, dict):
        return any(key in keys or _contains_key(child, keys) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_key(child, keys) for child in value)
    return False


def _validate_expanded_tree(root: Nested2Node, contract: HybridBodyContract) -> None:
    seen_assets: set[str] = set()
    visible_strings: list[str] = []

    def visit(node: Nested2Node, trusted_blueprint: bool = False) -> None:
        if node.component_type not in contract.allowed_components:
            raise TerseDslNested2ConversionError(
                f"Expanded component is not allowed: {node.component_type}"
            )
        if node.component_type in _CONTAINERS and not node.children:
            raise TerseDslNested2ConversionError(
                f"Expanded container must contain at least one child: {node.component_type}"
            )
        if node.component_type == "Image" and node.values:
            source = node.values[0]
            if source not in contract.allowed_asset_sources:
                raise TerseDslNested2ConversionError(f"Image source is not approved: {source}")
            seen_assets.add(str(source))
        if node.component_type == "Text" and node.values and isinstance(node.values[0], str):
            visible_strings.append(node.values[0])
        for child in node.children:
            visit(child, trusted_blueprint)

    visit(root)
    missing_assets = set(contract.required_asset_sources) - seen_assets
    if missing_assets:
        raise TerseDslNested2ConversionError(
            f"Required assets are missing: {sorted(missing_assets)}"
        )
    visible = "\n".join(visible_strings)
    visible_blob = "".join(_semantic_text_fragment(item) for item in visible_strings)
    missing_literals = [
        item
        for item in contract.required_literals
        if item not in visible and _semantic_text_fragment(item) not in visible_blob
    ]
    if missing_literals:
        raise TerseDslNested2ConversionError(
            f"Required literals are missing: {missing_literals[:3]}"
        )


def _count_calls(node: ParsedCall) -> int:
    return 1 + sum(_count_calls(child) for child in node.children)


def _normalize_template_provider_params(
    content: ParsedCall,
    task_spec: TaskSpec,
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> tuple[ParsedCall, int]:
    """Fill only missing params on an already selected trusted Template."""
    values_by_field = _provider_sample_values_by_field(task_spec.dataModelSchema)

    def visit(call: ParsedCall) -> tuple[ParsedCall, int]:
        children: list[ParsedCall] = []
        normalization_count = 0
        for child in call.children:
            normalized_child, child_count = visit(child)
            children.append(normalized_child)
            normalization_count += child_count
        if call.kind != "template" or call.name not in contract.allowed_template_ids:
            return (
                ParsedCall(call.kind, call.name, call.values, tuple(children), call.span),
                normalization_count,
            )
        size, raw_params = call.values
        params = dict(raw_params)
        definition = registry.require_template(call.name)
        try:
            variant = registry.require_variant(call.name, str(size))
        except ValueError:
            if len(definition.variants) != 1:
                return call, normalization_count
            variant = definition.variants[0]
        required = variant.parameters_schema.get("required", [])
        properties = variant.parameters_schema.get("properties", {})
        for parameter_name in required:
            if parameter_name in params or parameter_name not in properties:
                continue
            candidates: list[object]
            if parameter_name in definition.asset_parameter_semantic_tags:
                required_tags = set(definition.asset_parameter_semantic_tags[parameter_name])
                candidates = [
                    source
                    for source in contract.allowed_asset_sources
                    if required_tags.issubset(
                        contract.asset_semantic_tags_by_source.get(source, ())
                    )
                ]
            else:
                candidates = [
                    value
                    for value in values_by_field.get(parameter_name, ())
                    if _trusted_provider_parameter(value, contract)
                ]
            unique_candidates = list(dict.fromkeys(candidates))
            if len(unique_candidates) != 1:
                continue
            params[parameter_name] = unique_candidates[0]
            normalization_count += 1
        return (
            ParsedCall(
                call.kind,
                call.name,
                (size, params),
                tuple(children),
                call.span,
            ),
            normalization_count,
        )

    return visit(content)


def _provider_sample_values_by_field(value: object) -> dict[str, tuple[object, ...]]:
    collected: dict[str, list[object]] = {}

    def visit(current: object, field_name: str | None = None) -> None:
        if isinstance(current, dict) and "sampleValue" in current and field_name:
            sample = current["sampleValue"]
            if sample is None or isinstance(sample, (str, int, float, bool)):
                collected.setdefault(field_name, []).append(sample)
            return
        if isinstance(current, dict):
            for key, child in current.items():
                visit(child, key)
        elif isinstance(current, list):
            for child in current[:1]:
                visit(child, field_name)

    visit(value)
    return {key: tuple(values) for key, values in collected.items()}


def _trusted_provider_parameter(value: object, contract: HybridBodyContract) -> bool:
    if isinstance(value, str):
        return value in contract.trusted_literals
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return value in contract.trusted_numbers
    return False


def _normalize_template_relation_numbers(
    content: ParsedCall,
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> tuple[ParsedCall, int]:
    """Recover a missing numeric relation only from a unique trusted fact.

    This is Registry-driven and scenario-agnostic: the text side must already
    be trusted, the Template must declare ``number-matches-text``, and exactly
    one required number must satisfy the declared suffix relation.
    """
    children: list[ParsedCall] = []
    normalization_count = 0
    for child in content.children:
        normalized_child, child_count = _normalize_template_relation_numbers(
            child,
            contract,
            registry,
        )
        children.append(normalized_child)
        normalization_count += child_count
    if content.kind != "template":
        return (
            ParsedCall(
                content.kind,
                content.name,
                content.values,
                tuple(children),
                content.span,
            ),
            normalization_count,
        )

    size, raw_params = content.values
    params = dict(raw_params)
    if content.name not in contract.allowed_template_ids:
        return (
            ParsedCall(
                content.kind,
                content.name,
                content.values,
                tuple(children),
                content.span,
            ),
            normalization_count,
        )
    definition = registry.require_template(content.name)
    try:
        variant = registry.require_variant(content.name, str(size))
    except ValueError:
        if len(definition.variants) != 1:
            return content, normalization_count
        variant = definition.variants[0]
    required_numbers = tuple(
        number
        for number in contract.required_numbers
        if isinstance(number, (int, float)) and not isinstance(number, bool)
    )
    for relation in variant.parameter_relations:
        current = params.get(relation.number_parameter)
        text = params.get(relation.text_parameter)
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            continue
        if not isinstance(text, str) or text not in contract.trusted_literals:
            continue
        matches = {
            number
            for number in required_numbers
            if text in {_canonical_number(number) + suffix for suffix in relation.allowed_suffixes}
        }
        if len(matches) != 1:
            continue
        params[relation.number_parameter] = matches.pop()
        normalization_count += 1
    return (
        ParsedCall(
            content.kind,
            content.name,
            (size, params),
            tuple(children),
            content.span,
        ),
        normalization_count,
    )


def _canonical_number(number: int | float) -> str:
    return str(int(number)) if float(number).is_integer() else str(number)


def _validate_required_numbers(
    content: ParsedCall,
    contract: HybridBodyContract,
) -> None:
    required = Counter(contract.required_numbers)
    if not required:
        return
    actual: Counter[int | float] = Counter()

    def visit(call: ParsedCall) -> None:
        if call.kind == "template":
            _size, params = call.values
            actual.update(
                item
                for item in _primitive_values(params)
                if isinstance(item, (int, float)) and not isinstance(item, bool)
            )
        elif call.name == "Progress":
            for value in call.values:
                if not isinstance(value, dict):
                    continue
                progress_value = value.get("value")
                if isinstance(progress_value, (int, float)) and not isinstance(
                    progress_value, bool
                ):
                    actual[progress_value] += 1
        for child in call.children:
            visit(child)

    visit(content)
    missing = required - actual
    if missing:
        raise TerseDslNested2ConversionError(
            f"Hybrid content is missing required numeric facts: {list(missing.elements())}"
        )


def _normalize_recommended_variant_order(
    content: ParsedCall,
    registry: CardPlanRegistry,
) -> ParsedCall:
    """Apply Registry multi-size ordering without fixing any business layout."""
    children = tuple(
        _normalize_recommended_variant_order(child, registry) for child in content.children
    )
    if content.kind != "component" or len(children) < 2:
        return ParsedCall(content.kind, content.name, content.values, children, content.span)

    groups: dict[str, list[tuple[int, int]]] = {}
    for index, child in enumerate(children):
        calls = _descendant_template_variants(child)
        families = {wire_id for wire_id, _size in calls}
        if len(families) != 1:
            continue
        wire_id = next(iter(families))
        order = registry.require_template(wire_id).recommended_variant_order
        if not order:
            continue
        ranks = [order.index(size) for _wire_id, size in calls if size in order]
        if len(ranks) != len(calls):
            continue
        groups.setdefault(wire_id, []).append((index, min(ranks)))

    normalized = list(children)
    for units in groups.values():
        if len(units) < 2:
            continue
        positions = [index for index, _rank in units]
        ordered = [
            children[index] for index, _rank in sorted(units, key=lambda item: (item[1], item[0]))
        ]
        for position, child in zip(positions, ordered, strict=True):
            normalized[position] = child
    return ParsedCall(
        content.kind,
        content.name,
        content.values,
        tuple(normalized),
        content.span,
    )


def _descendant_template_variants(content: ParsedCall) -> list[tuple[str, str]]:
    if content.kind == "template":
        size = content.values[0]
        return [(content.name, str(size))]
    return [item for child in content.children for item in _descendant_template_variants(child)]


def _shape(node: Nested2Node) -> tuple[int, int]:
    if not node.children:
        return 1, 1
    child_shapes = [_shape(child) for child in node.children]
    return 1 + sum(item[0] for item in child_shapes), 1 + max(item[1] for item in child_shapes)


def _body_budget(
    params: dict[str, Any],
    contract: HybridBodyContract,
    registry: CardPlanRegistry,
) -> int:
    theme = registry.require_theme(contract.theme_profile_id)
    padding = (
        registry.ux_tokens["safeInset"]
        if contract.allowed_layout_component_ids
        else theme.root_styles.get("padding", 12)
    )
    if isinstance(padding, (int, float)):
        vertical_padding = int(padding) * 2
    elif isinstance(padding, dict):
        top = padding.get("top", 0)
        bottom = padding.get("bottom", 0)
        vertical_padding = int(top if isinstance(top, (int, float)) else 0) + int(
            bottom if isinstance(bottom, (int, float)) else 0
        )
    else:
        vertical_padding = 24
    if "title" in params and "subtitle" in params:
        header = 34
    else:
        header = 18 if any(key in params for key in ("title", "subtitle", "titleIcon")) else 0
    action = (
        registry.ux_tokens["pillActionHeight"]
        if "action" in params and contract.allowed_layout_component_ids
        else 30
        if "action" in params
        else 0
    )
    chrome_count = int(header > 0) + int(action > 0)
    root_gap = 8 * chrome_count
    return max(24, 160 - vertical_padding - header - action - root_gap)


def _ux_layout_body_budget(registry: CardPlanRegistry) -> int:
    return 160 - registry.ux_tokens["safeInset"] * 2


def _estimate_height(node: Nested2Node) -> int:
    options = next((value for value in node.values if isinstance(value, dict)), {})
    explicit = options.get("height")
    if isinstance(explicit, (int, float)):
        return int(explicit)
    child_heights = [_estimate_height(child) for child in node.children]
    if node.component_type in {"Row", "Stack"}:
        return max(child_heights, default=24)
    if node.component_type in {"Column", "List"}:
        margin = options.get("itemMargin", options.get("space", 4))
        margin_value = margin if isinstance(margin, int) else 4
        margin_total = max(0, len(child_heights) - 1) * margin_value
        return sum(child_heights) + margin_total
    return {"Text": 20, "Image": 24, "Progress": 40, "Button": 32}.get(node.component_type, 20)


def _constrain_content_height(node: Nested2Node, budget: int) -> Nested2Node:
    values = list(node.values)
    options_index = next(
        (index for index, value in enumerate(values) if isinstance(value, dict)),
        None,
    )
    options = dict(values[options_index]) if options_index is not None else {}
    options["height"] = budget
    options["clip"] = True
    if options_index is None:
        values.append(options)
    else:
        values[options_index] = options
    return Nested2Node(node.component_type, tuple(values), node.children)


def _normalize_component_values(
    component: str,
    values: tuple[Any, ...],
) -> tuple[Any, ...]:
    normalized = values
    if values and isinstance(values[0], dict):
        first = dict(values[0])
        layout = first.pop("layout", None)
        if isinstance(layout, str):
            remainder = (first,) if first else ()
            normalized = (layout, *remainder, *values[1:])
    if normalized and isinstance(normalized[0], str):
        alias = _LAYOUT_ALIASES.get((component, normalized[0]))
        if alias is not None:
            return (alias, *normalized[1:])
    if len(normalized) > 1 and isinstance(normalized[1], str):
        alias = _DESIGN_ALIASES.get((component, normalized[1]))
        if alias is not None:
            return (normalized[0], alias, *normalized[2:])
    return normalized


def _compact_text_roles(node: Nested2Node) -> Nested2Node:
    children = tuple(_compact_text_roles(child) for child in node.children)
    if node.component_type != "Text":
        return Nested2Node(node.component_type, node.values, children)
    values = list(node.values)
    if len(values) > 1 and values[1] == "title":
        values[1] = "compact-title"
    return Nested2Node(node.component_type, tuple(values), children)


def _normalize_theme_styles(styles: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in styles.items():
        if isinstance(value, str):
            match = _COLOR_MODE_LITERAL.fullmatch(value)
            normalized[key] = match.group(2) if match is not None else value
        else:
            normalized[key] = value
    return normalized


def _serialize_node(node: Nested2Node) -> str:
    arguments = [_serialize_value(value) for value in node.values]
    arguments.extend(_serialize_node(child) for child in node.children)
    return f"{node.component_type}({', '.join(arguments)})"


def _serialize_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
