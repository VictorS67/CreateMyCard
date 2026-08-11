"""第五接口的新第一层 LLM：只选择 Theme 和业务高级组件范围。"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from itertools import combinations
from typing import Any

from pydantic import ValidationError

from models.generation import TaskSpec, WidgetSize
from services.cardplan_template.registry import CardPlanRegistry

from .models import AdvancedScopeBrief, DataShape, UxBusinessComponentCapability

_REDUNDANT_2X2_SUPPORTS = {
    frozenset(("WeatherOverview", "LocationOverview")): "LocationOverview",
    frozenset(("ScheduleOverview", "DateOverview")): "DateOverview",
}


def build_advanced_scope_prompt(
    task_spec: TaskSpec,
    data_shape: DataShape,
    registry: CardPlanRegistry,
    available_capability_ids: tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    """构造不含 Template、布局源码和整卡置信度信息的新第一层 Prompt。"""
    effective_ids = resolve_available_capability_ids(
        task_spec,
        registry,
        available_capability_ids,
    )
    component_candidates = _component_candidates(task_spec, data_shape, registry, effective_ids)
    if not component_candidates:
        raise ValueError("no provider-backed UX Business Component candidate")
    candidate_ids = {item.name for item in component_candidates}
    user_payload = {
        "userQuery": task_spec.userQuery,
        "size": task_spec.size,
        "dataShape": data_shape.model_dump(exclude={"fields"}),
        "fields": [
            {
                "path": field.path,
                "name": field.name,
                "dataType": field.data_type,
                "description": field.description,
                "roles": field.roles,
            }
            for field in data_shape.fields
        ],
        "themes": [
            {
                "id": theme.theme_profile_id,
                "description": theme.description,
            }
            for theme in registry.themes.values()
        ],
        "advancedComponents": [
            {
                "id": capability.name,
                "description": capability.description,
                "variants": capability.enabled_variants(effective_ids),
                "themeIds": _theme_ids_for_components((capability,), registry),
                "compatibleWith": _compatible_component_ids(
                    capability,
                    candidate_ids,
                    task_spec.size,
                    task_spec.userQuery,
                    registry,
                ),
            }
            for capability in component_candidates
        ],
        "maxAdvancedComponents": registry.ux_size_budgets[task_spec.size].max_business_components,
    }
    schema = AdvancedScopeBrief.model_json_schema(by_alias=True)
    return [
        {
            "role": "system",
            "content": (
                "你是第五接口独立的 Advanced Scope Planner。只输出 JSON，且只决定 themeId "
                "与 advancedComponentIds；scopeVersion 固定为 advanced-scope-brief/1。不得输出"
                "整卡置信度、整卡参数、局部模板候选、布局选择、组件参数、颜色、尺寸、"
                "Action、理由或任何额外字段。advancedComponentIds 只能从 "
                "advancedComponents 选择，"
                "必须覆盖用户主要业务语义，并遵守 maxAdvancedComponents；选择多个组件时必须"
                "互相出现在 compatibleWith 中。themeId 只能从 themes 选择，并且必须出现在每个"
                "所选高级组件的 themeIds 合集中。\n" + json.dumps(schema, ensure_ascii=False)
            ),
        },
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


async def plan_advanced_scope_with_llm(
    task_spec: TaskSpec,
    data_shape: DataShape,
    generate_json: Callable[[list[dict[str, str]], str], Awaitable[dict[str, Any]]],
    registry: CardPlanRegistry,
    available_capability_ids: tuple[str, ...] | None = None,
) -> AdvancedScopeBrief:
    prompt = build_advanced_scope_prompt(
        task_spec,
        data_shape,
        registry,
        available_capability_ids,
    )
    raw = await generate_json(prompt, "advanced-component-scope")
    raw = _normalize_empty_component_scope(
        raw,
        task_spec,
        registry,
        available_capability_ids,
    )
    try:
        scope = AdvancedScopeBrief.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"invalid AdvancedScopeBrief: {exc}") from exc
    scope = _normalize_redundant_2x2_support(scope, task_spec)
    try:
        validate_advanced_scope(
            scope,
            task_spec,
            data_shape,
            registry,
            available_capability_ids,
        )
    except ValueError as exc:
        if str(exc) not in {
            "AdvancedScopeBrief selected a Theme outside component palettes",
            "AdvancedScopeBrief has no compatible UX layout",
        }:
            raise
        try:
            scope = _normalize_scope_to_compatible_layout(scope, task_spec, registry)
        except ValueError:
            if str(exc) == "AdvancedScopeBrief selected a Theme outside component palettes":
                raise exc from None
            raise
        validate_advanced_scope(
            scope,
            task_spec,
            data_shape,
            registry,
            available_capability_ids,
        )
    return scope


def plan_advanced_scope_offline(
    task_spec: TaskSpec,
    data_shape: DataShape,
    registry: CardPlanRegistry,
    available_capability_ids: tuple[str, ...] | None = None,
) -> AdvancedScopeBrief:
    """仅供显式离线兼容测试；第五接口生产主链路默认不启用。"""
    effective_ids = resolve_available_capability_ids(
        task_spec,
        registry,
        available_capability_ids,
    )
    candidates = _component_candidates(task_spec, data_shape, registry, effective_ids)
    if not candidates:
        raise ValueError("no provider-backed UX Business Component candidate")
    primary = candidates[0]
    theme_ids = _theme_ids_for_components((primary,), registry)
    scope = AdvancedScopeBrief(
        themeId=theme_ids[0],
        advancedComponentIds=(primary.name,),
    )
    validate_advanced_scope(
        scope,
        task_spec,
        data_shape,
        registry,
        available_capability_ids,
    )
    return scope


def validate_advanced_scope(
    scope: AdvancedScopeBrief,
    task_spec: TaskSpec,
    data_shape: DataShape,
    registry: CardPlanRegistry,
    available_capability_ids: tuple[str, ...] | None = None,
) -> None:
    del data_shape
    effective_ids = resolve_available_capability_ids(
        task_spec,
        registry,
        available_capability_ids,
    )
    candidates = _component_candidates(
        task_spec,
        extract_shape=None,
        registry=registry,
        available_capability_ids=effective_ids,
    )
    candidate_ids = {item.name for item in candidates}
    selected_ids = set(scope.advanced_component_ids)
    if not selected_ids.issubset(candidate_ids):
        raise ValueError("AdvancedScopeBrief selected a component outside trusted candidates")
    budget = registry.ux_size_budgets[task_spec.size]
    if len(scope.advanced_component_ids) > budget.max_business_components:
        raise ValueError("AdvancedScopeBrief exceeds the size component budget")
    components = tuple(
        registry.require_ux_business_component(item) for item in scope.advanced_component_ids
    )
    if any(not item.enabled_variants(effective_ids) for item in components):
        raise ValueError("AdvancedScopeBrief selected a component without a production provider")
    if any(task_spec.size not in item.supported_card_sizes for item in components):
        raise ValueError("AdvancedScopeBrief selected a component unsupported by card size")
    allowed_themes = set(_theme_ids_for_components(components, registry))
    if scope.theme_id not in allowed_themes:
        raise ValueError("AdvancedScopeBrief selected a Theme outside component palettes")
    if not resolve_scope_layout_ids(scope, task_spec, registry):
        raise ValueError("AdvancedScopeBrief has no compatible UX layout")


def _normalize_scope_to_compatible_layout(
    scope: AdvancedScopeBrief,
    task_spec: TaskSpec,
    registry: CardPlanRegistry,
) -> AdvancedScopeBrief:
    """Drop the least-prioritized scope items only when no common layout exists."""
    values = scope.advanced_component_ids
    for size in range(len(values) - 1, 0, -1):
        for candidate_ids in combinations(values, size):
            candidate = scope.model_copy(update={"advanced_component_ids": tuple(candidate_ids)})
            components = tuple(
                registry.require_ux_business_component(item) for item in candidate_ids
            )
            if scope.theme_id not in set(_theme_ids_for_components(components, registry)):
                continue
            if resolve_scope_layout_ids(candidate, task_spec, registry):
                return candidate
    raise ValueError("AdvancedScopeBrief has no compatible UX layout")


def _normalize_redundant_2x2_support(
    scope: AdvancedScopeBrief,
    task_spec: TaskSpec,
) -> AdvancedScopeBrief:
    """Keep one atomic owner when a 2x2 content component already owns its context."""
    if task_spec.size != "2x2":
        return scope
    selected = list(scope.advanced_component_ids)
    selected_set = set(selected)
    for pair, redundant_id in _REDUNDANT_2X2_SUPPORTS.items():
        if redundant_id == "DateOverview" and _query_explicitly_requests_date(task_spec.userQuery):
            continue
        if pair.issubset(selected_set):
            selected.remove(redundant_id)
            selected_set.remove(redundant_id)
    if tuple(selected) == scope.advanced_component_ids:
        return scope
    return scope.model_copy(update={"advanced_component_ids": tuple(selected)})


def _query_explicitly_requests_date(query: str) -> bool:
    normalized = _normalize(query)
    return any(term in normalized for term in ("date", "日期", "星期", "日历", "几号"))


def _normalize_empty_component_scope(
    raw: dict[str, Any],
    task_spec: TaskSpec,
    registry: CardPlanRegistry,
    available_capability_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    if raw.get("advancedComponentIds") != []:
        return raw
    theme_id = raw.get("themeId")
    if not isinstance(theme_id, str):
        return raw
    selected = next(
        (
            item.name
            for item in _component_candidates(
                task_spec,
                extract_shape=None,
                registry=registry,
                available_capability_ids=resolve_available_capability_ids(
                    task_spec,
                    registry,
                    available_capability_ids,
                ),
            )
            if theme_id in _theme_ids_for_components((item,), registry)
        ),
        None,
    )
    if selected is None:
        return raw
    normalized = dict(raw)
    normalized["advancedComponentIds"] = [selected]
    return normalized


def resolve_scope_layout_ids(
    scope: AdvancedScopeBrief,
    task_spec: TaskSpec,
    registry: CardPlanRegistry,
) -> tuple[str, ...]:
    components = tuple(
        registry.require_ux_business_component(item) for item in scope.advanced_component_ids
    )
    count = len(components)
    action_count = len(task_spec.eventCandidates)
    has_action = action_count > 0
    common = set(registry.ux_layout_components)
    for capability in components:
        common &= set(capability.supported_layouts)
    allowed: list[str] = []
    for layout_id in common:
        layout = registry.require_ux_layout_component(layout_id)
        if task_spec.size not in layout.supported_card_sizes:
            continue
        if (
            not layout.minimum_children(task_spec.size)
            <= count
            <= layout.max_children_by_size[task_spec.size]
        ):
            continue
        if action_count < layout.min_action_children_by_size[task_spec.size]:
            continue
        allowed.append(layout_id)
    return tuple(sorted(allowed, key=lambda item: _layout_rank(item, count, has_action)))


def scope_template_ids(
    scope: AdvancedScopeBrief,
    registry: CardPlanRegistry,
    task_spec: TaskSpec | None = None,
) -> tuple[str, ...]:
    template_ids = tuple(
        dict.fromkeys(
            template_id
            for component_id in scope.advanced_component_ids
            for template_id in registry.require_ux_business_component(
                component_id
            ).local_template_ids
        )
    )[:12]
    if task_spec is None:
        return template_ids
    return tuple(
        template_id
        for template_id in template_ids
        if _template_has_satisfiable_variant(template_id, task_spec, registry)
    )


def _template_has_satisfiable_variant(
    template_id: str,
    task_spec: TaskSpec,
    registry: CardPlanRegistry,
) -> bool:
    definition = registry.require_template(template_id)
    field_names = _schema_field_names(task_spec.dataModelSchema)
    has_assets = any(item.get("src") for item in task_spec.assetCandidates)
    has_actions = bool(task_spec.eventCandidates)
    has_numbers = any(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in _schema_values(task_spec.dataModelSchema)
    )
    for variant in definition.variants:
        properties = variant.parameters_schema.get("properties", {})
        required = variant.parameters_schema.get("required", ())
        if all(
            _required_parameter_is_satisfiable(
                name,
                properties.get(name, {}),
                field_names=field_names,
                has_assets=has_assets,
                has_actions=has_actions,
                has_numbers=has_numbers,
            )
            for name in required
        ):
            return True
    return False


def _required_parameter_is_satisfiable(
    name: str,
    schema: dict[str, Any],
    *,
    field_names: set[str],
    has_assets: bool,
    has_actions: bool,
    has_numbers: bool,
) -> bool:
    semantic = _normalize(f"{name} {schema.get('description', '')}")
    if any(
        token in semantic
        for token in ("icon", "image", "asset", "source", "src", "图标", "图片", "素材", "资源")
    ):
        return has_assets
    if any(token in semantic for token in ("action", "event", "操作", "事件")):
        return has_actions
    if schema.get("type") in {"number", "integer"}:
        return has_numbers
    normalized_name = _normalize(name)
    return any(
        normalized_name == field
        or (len(normalized_name) >= 4 and normalized_name in field)
        or (len(field) >= 4 and field in normalized_name)
        for field in field_names
    )


def _schema_field_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            names.add(_normalize(str(key)))
            names.update(_schema_field_names(item))
    elif isinstance(value, list):
        for item in value:
            names.update(_schema_field_names(item))
    return names


def _schema_values(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _schema_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _schema_values(item)
    else:
        yield value


def resolve_available_capability_ids(
    task_spec: TaskSpec,
    registry: CardPlanRegistry,
    explicit_ids: tuple[str, ...] | None = None,
) -> set[str]:
    """Resolve trusted providers from CardSpec IDs or legacy test schema keys."""
    known_ids = {
        capability_id
        for component in registry.ux_business_components.values()
        for capability_id in component.data_capability_ids
    }
    if explicit_ids is not None:
        return set(explicit_ids) & known_ids

    discovered: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in known_ids:
                    discovered.add(key)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(task_spec.dataModelSchema)
    return discovered


def _component_candidates(
    task_spec: TaskSpec,
    extract_shape: DataShape | None,
    registry: CardPlanRegistry,
    available_capability_ids: set[str],
) -> tuple[UxBusinessComponentCapability, ...]:
    schema_parts = [json.dumps(task_spec.dataModelSchema, ensure_ascii=False)]
    if extract_shape is not None:
        schema_parts.append(
            " ".join(
                f"{field.path} {field.name} {field.description} {' '.join(field.roles)}"
                for field in extract_shape.fields
            )
        )
    schema_text = _normalize(" ".join(schema_parts))
    query_text = _normalize(task_spec.userQuery)
    scored = [
        (
            sum(_detection_term_matches(term, schema_text) for term in item.detection_terms),
            sum(_detection_term_matches(term, query_text) for term in item.detection_terms),
            item,
        )
        for item in registry.ux_business_components.values()
        if task_spec.size in item.supported_card_sizes
        and bool(item.enabled_variants(available_capability_ids))
    ]
    ranked = sorted(scored, key=lambda pair: (-pair[0], -pair[1], pair[2].name))
    schema_positive = [item for schema_score, _query_score, item in ranked if schema_score > 0]
    query_positive = [item for _schema_score, query_score, item in ranked if query_score > 0]
    fallback = [item for _schema_score, _query_score, item in ranked]
    matched_by_name = {item.name: item for item in [*schema_positive, *query_positive]}
    matched = tuple(matched_by_name.values())
    return tuple((matched or tuple(fallback))[:8])


def _compatible_component_ids(
    capability: UxBusinessComponentCapability,
    candidate_ids: set[str],
    size: WidgetSize,
    user_query: str,
    registry: CardPlanRegistry,
) -> tuple[str, ...]:
    compatible: list[str] = []
    own_layouts = set(capability.supported_layouts)
    for component_id in sorted(candidate_ids):
        if component_id == capability.name:
            continue
        if size == "2x2":
            pair = frozenset((capability.name, component_id))
            redundant_id = _REDUNDANT_2X2_SUPPORTS.get(pair)
            if redundant_id is not None and not (
                redundant_id == "DateOverview" and _query_explicitly_requests_date(user_query)
            ):
                continue
        candidate = registry.require_ux_business_component(component_id)
        shared = own_layouts & set(candidate.supported_layouts)
        if any(
            registry.require_ux_layout_component(layout_id).minimum_children(size)
            <= 2
            <= registry.require_ux_layout_component(layout_id).max_children_by_size[size]
            for layout_id in shared
        ):
            compatible.append(component_id)
    return tuple(compatible)


def _theme_ids_for_components(
    components: tuple[UxBusinessComponentCapability, ...],
    registry: CardPlanRegistry,
) -> tuple[str, ...]:
    per_component = [
        tuple(
            dict.fromkeys(
                theme_id
                for scene in component.palette_scenes
                for theme_id in registry.palette_scene_theme_ids[scene]
            )
        )
        for component in components
    ]
    if not per_component:
        return ()
    common = set(per_component[0])
    for theme_ids in per_component[1:]:
        common &= set(theme_ids)
    return tuple(theme_id for theme_id in per_component[0] if theme_id in common)


def _layout_rank(layout_id: str, count: int, has_action: bool) -> tuple[int, str]:
    preferred: dict[tuple[int, bool], tuple[str, ...]] = {
        (1, False): ("SingleFocusLayout", "ListActionLayout"),
        (1, True): ("HeroActionLayout", "ListActionLayout", "SingleFocusLayout"),
        (2, False): ("HeroSupportLayout", "PeerPairLayout", "EqualItemsLayout"),
        (2, True): ("HeroSupportActionLayout", "HeroSupportLayout", "PeerPairLayout"),
    }
    order = preferred.get((count, has_action), ("SequentialSummaryLayout", "EqualItemsLayout"))
    return (order.index(layout_id) if layout_id in order else len(order), layout_id)


def _normalize(value: str) -> str:
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return re.sub(r"[\s_./:-]+", " ", camel_split.casefold())


def _detection_term_matches(term: str, normalized_text: str) -> bool:
    """Match Latin detection terms by token boundary and CJK terms by phrase."""
    normalized_term = _normalize(term).strip()
    if not normalized_term:
        return False
    if re.search(r"[\u3400-\u9fff]", normalized_term):
        return normalized_term.replace(" ", "") in normalized_text.replace(" ", "")
    term_tokens = tuple(re.findall(r"[a-z0-9]+", normalized_term))
    text_tokens = set(re.findall(r"[a-z0-9]+", normalized_text))
    return bool(term_tokens) and all(
        any(
            text_token == term_token
            or (len(term_token) >= 4 and text_token in {f"{term_token}s", f"{term_token}es"})
            for text_token in text_tokens
        )
        for term_token in term_tokens
    )
