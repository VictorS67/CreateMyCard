"""Project UIBrief and TaskSpec into a bounded Hybrid Body prompt."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from models.generation import TaskSpec

from .generated.prompts import BODY_SYSTEM_PROMPT_KERNEL
from .models import ActionBinding, Fact, HybridBodyContract, HybridLimits
from .registry import CardPlanRegistry

_PLAIN_DESIGNS = ("title", "body", "subtitle", "success", "warning", "primary")
_PLAIN_LAYOUTS = ("card", "section", "compact", "between", "actions", "list", "dense", "overlay")
_ACTION_LABELS = {
    "event.call.phone": "联系家人",
    "event.clean.memory": "一键清理",
    "event.enter.meeting": "加入会议",
    "event.open.settings.dnd": "专注模式",
    "event.open.settings.bluetooth": "蓝牙设置",
    "event.open.settings.battery": "电池设置",
    "event.open.settings.batteryHealth": "电池健康",
    "event.open.settings.parentControl": "家长控制",
    "event.open.settings.storage": "存储设置",
    "event.open.weather": "天气详情",
    "event.open.clock.alarm": "设置闹钟",
    "event.open.music.daily": "每日推荐",
    "event.open.music.favorite": "心动歌单",
    "event.open.health.sport": "今日训练",
    "event.open.health.sleep": "睡眠详情",
    "event.viewCalendarEvent": "查看日程",
    "event.startNavigate": "开始导航",
    "event.setPowerSavingMode": "省电模式",
}


@dataclass(frozen=True)
class HybridPromptProjection:
    messages: list[dict[str, str]]
    contract: HybridBodyContract
    facts: tuple[Fact, ...]
    requested_template_ids: tuple[str, ...]
    theme_id: str


def build_hybrid_prompt(
    *,
    task_spec: TaskSpec,
    card_spec: dict[str, Any],
    ui_brief: Any,
    registry: CardPlanRegistry,
) -> HybridPromptProjection:
    facts = tuple(_collect_facts(task_spec.dataModelSchema))
    theme_id = _resolve_theme(task_spec, ui_brief, registry)
    requested = _resolve_templates(task_spec, ui_brief, theme_id, registry)
    asset_sources = tuple(
        str(item["src"])
        for item in task_spec.assetCandidates
        if isinstance(item, dict) and isinstance(item.get("src"), str)
    )
    trusted_literals = _unique(
        [
            task_spec.userQuery,
            str(card_spec.get("title", "")),
            str(card_spec.get("description", "")),
            *(str(fact.value) for fact in facts if isinstance(fact.value, str)),
            *(_action_label(event) for event in task_spec.eventCandidates),
        ]
    )
    trusted_numbers = tuple(
        dict.fromkeys(
            fact.value
            for fact in facts
            if isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool)
        )
    )
    actions = tuple(
        ActionBinding(
            action_id=event.id or "",
            display_label=_action_label(event),
            call=event.call,
            args=event.args,
        )
        for event in task_spec.eventCandidates
        if event.id
    )
    if getattr(ui_brief, "action_placement", "auto") == "none":
        actions = ()
    selected_definitions = [registry.require_template(wire_id) for wire_id in requested]
    content_action_ids = _resolve_content_action_ids(
        ui_brief=ui_brief,
        actions=actions,
        definitions=selected_definitions,
    )
    design_tokens = _unique(
        [
            *_PLAIN_DESIGNS,
            *(token for item in selected_definitions for token in item.allowed_design_tokens),
        ]
    )
    layout_tokens = _unique(
        [
            *_PLAIN_LAYOUTS,
            *(token for item in selected_definitions for token in item.allowed_layout_tokens),
            *(item.recommended_container_layout_token or "" for item in selected_definitions),
            *(
                item.recommended_variant_layout.inline_layout_token
                for item in selected_definitions
                if item.recommended_variant_layout is not None
            ),
        ]
    )
    string_facts = [
        str(fact.value)
        for fact in facts
        if isinstance(fact.value, str) and fact.value.strip() and fact.value != "示例"
    ]
    limits = HybridLimits(
        max_raw_components=18 if task_spec.size == "2x2" else 28,
        max_expanded_components=36 if task_spec.size == "2x2" else 52,
        max_nesting_depth=7,
        vertical_budget_vp=126,
    )
    contract = HybridBodyContract(
        theme_profile_id=theme_id,
        allowed_components=(
            "Text",
            "Image",
            "Divider",
            "Progress",
            "Button",
            "Checkbox",
            "Row",
            "Column",
            "List",
            "Stack",
        ),
        allowed_design_tokens=design_tokens,
        allowed_layout_tokens=layout_tokens,
        allowed_template_ids=requested,
        allowed_asset_sources=asset_sources,
        trusted_literals=trusted_literals,
        trusted_numbers=trusted_numbers,
        required_literals=tuple(dict.fromkeys(string_facts)),
        protected_literals=tuple(dict.fromkeys(string_facts)),
        action_bindings=actions,
        content_action_ids=content_action_ids,
        limits=limits,
    )
    system = _system_prompt(contract, requested, registry)
    card_composition = {
        "titleCandidates": [
            {"role": "title", "text": card_spec.get("title")},
            {"role": "subtitle", "text": card_spec.get("description")},
        ],
        "titleIconCandidates": [
            {"src": item.get("src"), "description": item.get("description", "")}
            for item in task_spec.assetCandidates
            if item.get("src")
        ],
        "cardActionCandidates": [
            {
                "id": action.action_id,
                "label": action.display_label,
                "importance": action.importance,
                "materialHint": action.material_hint,
            }
            for action in actions
            if action.action_id not in content_action_ids
        ],
        "contentActionCandidates": [
            {
                "id": action.action_id,
                "label": action.display_label,
                "importance": action.importance,
                "materialHint": action.material_hint,
            }
            for action in actions
            if action.action_id in content_action_ids
        ],
        "required": (
            {"actionId": actions[0].action_id}
            if len(actions) == 1 and not content_action_ids
            else {}
        ),
        "cardParamsPolicy": (
            "independent-chrome-without-action" if content_action_ids else "candidate-chrome"
        ),
    }
    user = "\n".join(
        (
            f"request={json.dumps(task_spec.userQuery, ensure_ascii=False)}",
            f"card={json.dumps({'size': task_spec.size, 'theme': theme_id}, ensure_ascii=False)}",
            f"requestedTemplate={json.dumps(requested, ensure_ascii=False)}",
            f"cardComposition={json.dumps(card_composition, ensure_ascii=False)}",
            f"dataFacts={json.dumps([fact.model_dump() for fact in facts], ensure_ascii=False)}",
            f"mustKeep={json.dumps(contract.required_literals, ensure_ascii=False)}",
            "advancedComposition="
            + json.dumps(
                {
                    "primaryDomain": getattr(ui_brief, "primary_domain", None),
                    "adaptiveTemplateId": getattr(ui_brief, "adaptive_template_id", None),
                    "advancedComponentIds": getattr(ui_brief, "advanced_component_ids", []),
                },
                ensure_ascii=False,
            ),
            '只输出一个以分号结束、以 Template("card@1", ...) 为根的完整 Card。',
        )
    )
    if len(system) + len(user) > 80_000:
        raise ValueError("Hybrid Body Prompt exceeds the service input budget")
    return HybridPromptProjection(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        contract=contract,
        facts=facts,
        requested_template_ids=requested,
        theme_id=theme_id,
    )


def selection_candidates(task_spec: TaskSpec, registry: CardPlanRegistry) -> dict[str, Any]:
    semantic_text = _semantic_text(task_spec, None)
    ranked = _ranked_templates(semantic_text, registry)
    if task_spec.eventCandidates:
        action_templates = [item for item in ranked if item.action_policy != "none"][:6]
        content_templates = [item for item in ranked if item.action_policy == "none"]
        templates = [*action_templates, *content_templates[: 12 - len(action_templates)]]
        templates.sort(key=ranked.index)
    else:
        templates = ranked[:12]
    return {
        "themes": [
            {
                "id": theme.theme_profile_id,
                "description": theme.description,
                "density": theme.density,
            }
            for theme in registry.themes.values()
        ],
        "localTemplates": [
            {
                "id": definition.wire_id,
                "description": definition.description,
                "domainTags": definition.domain_tags,
                "supportedSizes": definition.supported_sizes,
                "compatibleThemeIds": definition.compatible_theme_profile_ids,
                "actionPolicy": definition.action_policy,
                "variants": [
                    {
                        "size": variant.size,
                        "requiredParameters": [
                            {
                                "name": name,
                                "description": variant.parameters_schema.get("properties", {})
                                .get(name, {})
                                .get("description", ""),
                                "valueKind": _parameter_value_kind(
                                    name,
                                    variant.parameters_schema.get("properties", {}).get(name, {}),
                                ),
                            }
                            for name in variant.parameters_schema.get("required", [])
                        ],
                    }
                    for variant in definition.variants
                ],
            }
            for definition in templates
        ],
    }


def _system_prompt(
    contract: HybridBodyContract,
    requested: tuple[str, ...],
    registry: CardPlanRegistry,
) -> str:
    signatures: list[str] = []
    for wire_id in requested:
        definition = registry.require_template(wire_id)
        for variant in definition.variants:
            if not contract.content_action_ids and _variant_requires_action(variant):
                continue
            properties = variant.parameters_schema.get("properties", {})
            params = {
                name: {
                    "type": value.get("type", "value"),
                    "description": value.get("description", ""),
                    "valueKind": _parameter_value_kind(name, value),
                }
                for name, value in properties.items()
            }
            signatures.append(
                f"- Template({wire_id!r}, {variant.size!r}, params): "
                f"{definition.description}; params={json.dumps(params, ensure_ascii=False)}"
            )
    content_actions = [
        item for item in contract.action_bindings if item.action_id in contract.content_action_ids
    ]
    action_templates = [
        registry.require_template(wire_id).wire_id
        for wire_id in requested
        if registry.require_template(wire_id).action_policy != "none"
    ]
    if content_actions:
        content_action_json = json.dumps(
            [item.model_dump() for item in content_actions],
            ensure_ascii=False,
        )
        action_rule = (
            "card params 必须省略 action。title、subtitle、titleIcon 仅在表达未被 content "
            "消费的独立上下文时使用；否则一并省略。"
            f"contentActionCandidates={content_action_json}；"
            "每个批准 ID 必须由一个 actionPolicy!=none 的局部 Template 恰好消费一次，"
            f"可用 Action Template={json.dumps(action_templates, ensure_ascii=False)}；"
            "content 禁止标准 Button。"
        )
    elif contract.action_bindings:
        action_rule = (
            "card action 必须从 cardActionCandidates 的批准 label/id 对中选择；"
            "content 禁止 Button 和事件。"
        )
    else:
        action_rule = "本次没有批准 Action；card params 省略 action，content 禁止 Button 和事件。"
    return "\n".join(
        (
            BODY_SYSTEM_PROMPT_KERNEL,
            "",
            "标准组件投影：Text/Image/Button 使用批准 DesignToken；"
            "Column/Row/List/Stack 使用批准 LayoutToken；Progress 使用字面量对象。",
            "基础容器的规范组合为 Column(section|compact)、Row(between|actions)、"
            "List(list|dense)、Stack(overlay)；Registry 展开的专用别名由服务端静态归一化。",
            'Text 严格写成 Text("可见文字", "designToken")，可见文字在前、DesignToken '
            "在后，禁止交换两个位置。",
            '容器严格写成 Column("layoutToken", child1, child2)；不要把 layoutToken '
            "包装成对象，不要用数组包装 children。",
            "Template 参数必须逐项遵守签名中的 JSON type；看起来像数字的 string 仍需加引号。",
            'Card 外壳必须是 Template("card@1", cardParams, content)。',
            "cardParams 只允许 title、subtitle、titleIcon、action；禁止 icon 等别名。"
            "title/subtitle 必须逐字来自候选或 dataFacts，否则省略。",
            '整卡 Action 直接写成 action: { label: "批准文案", id: "批准ID" }；'
            "禁止写成 action: { action: {...} } 或增加任何包装层。",
            "素材 src 只能填入参数名或描述明确表示 icon/image/asset/source/src 的字段；"
            "symbol、文字、标签和数值字段禁止使用素材。",
            f"允许 DesignToken={json.dumps(contract.allowed_design_tokens)}",
            f"允许 LayoutToken={json.dumps(contract.allowed_layout_tokens)}",
            f"允许素材 src={json.dumps(contract.allowed_asset_sources, ensure_ascii=False)}",
            action_rule,
            "局部 Template：",
            *signatures,
            f"预算：raw<={contract.limits.max_raw_components}, "
            f"expanded<={contract.limits.max_expanded_components}, "
            f"depth<={contract.limits.max_nesting_depth}, "
            f"body<={contract.limits.vertical_budget_vp}vp。",
        )
    )


def _resolve_theme(task_spec: TaskSpec, ui_brief: Any, registry: CardPlanRegistry) -> str:
    requested = getattr(ui_brief, "theme_id", None)
    requested_templates = [
        registry.templates[item]
        for item in (getattr(ui_brief, "local_template_ids", []) or [])
        if item in registry.templates
    ]
    themed_templates = [item for item in requested_templates if item.compatible_theme_profile_ids]
    if themed_templates:
        support = {
            theme_id: sum(
                theme_id in definition.compatible_theme_profile_ids
                for definition in themed_templates
            )
            for theme_id in registry.themes
        }
        best_support = max(support.values(), default=0)
        requested_support = support.get(requested, 0) if isinstance(requested, str) else 0
        if best_support > requested_support:
            return min(theme_id for theme_id, score in support.items() if score == best_support)
    if isinstance(requested, str) and requested in registry.themes:
        return requested
    text = _semantic_text(task_spec, ui_brief)
    ranked = sorted(
        registry.themes.values(),
        key=lambda item: (
            -_token_overlap(text, f"{item.theme_profile_id} {item.description}"),
            item.theme_profile_id,
        ),
    )
    return ranked[0].theme_profile_id


def _resolve_templates(
    task_spec: TaskSpec,
    ui_brief: Any,
    theme_id: str,
    registry: CardPlanRegistry,
) -> tuple[str, ...]:
    requested = getattr(ui_brief, "local_template_ids", []) or []
    allowed: list[str] = []
    for wire_id in requested:
        definition = registry.templates.get(wire_id)
        if definition is None:
            continue
        themes = definition.compatible_theme_profile_ids
        if not themes or theme_id in themes:
            allowed.append(wire_id)
    if not allowed:
        text = _semantic_text(task_spec, ui_brief)
        for definition in _ranked_templates(text, registry):
            themes = definition.compatible_theme_profile_ids
            if not themes or theme_id in themes:
                allowed.append(definition.wire_id)
            if len(allowed) >= 6:
                break
    resolved = tuple(dict.fromkeys(allowed))
    resolved = _supplement_action_only_templates(
        resolved,
        task_spec=task_spec,
        ui_brief=ui_brief,
        theme_id=theme_id,
        registry=registry,
    )
    return _prune_redundant_action_templates(resolved, registry)


def _supplement_action_only_templates(
    requested: tuple[str, ...],
    *,
    task_spec: TaskSpec,
    ui_brief: Any,
    theme_id: str,
    registry: CardPlanRegistry,
) -> tuple[str, ...]:
    if not requested:
        return requested
    definitions = [registry.require_template(item) for item in requested]
    if any(definition.action_policy == "none" for definition in definitions):
        return requested
    text = _semantic_text(task_spec, ui_brief)
    supplement = next(
        (
            definition.wire_id
            for definition in _ranked_templates(text, registry)
            if definition.action_policy == "none"
            and theme_id in definition.compatible_theme_profile_ids
        ),
        None,
    )
    return (*requested, supplement) if supplement is not None else requested


def _prune_redundant_action_templates(
    requested: tuple[str, ...],
    registry: CardPlanRegistry,
) -> tuple[str, ...]:
    definitions = [registry.require_template(item) for item in requested]
    covered_content_parameters = {
        name.casefold()
        for definition in definitions
        if definition.action_policy == "none"
        for variant in definition.variants
        for name in variant.parameters_schema.get("required", [])
        if isinstance(name, str)
    }
    covered_domain_tags = {
        tag.casefold()
        for definition in definitions
        if definition.action_policy == "none"
        for tag in definition.domain_tags
    }
    retained: list[str] = []
    for definition in definitions:
        if definition.action_policy == "none" or definition.compatible_theme_profile_ids:
            retained.append(definition.wire_id)
            continue
        semantic_parameters = {
            name.casefold()
            for variant in definition.variants
            for name in variant.parameters_schema.get("required", [])
            if isinstance(name, str) and not _is_action_or_asset_parameter(name)
        }
        semantic_tags = {
            tag.casefold() for tag in definition.domain_tags if tag.casefold() != "action"
        }
        content_is_covered = (
            semantic_parameters and semantic_parameters <= covered_content_parameters
        ) or (semantic_tags and semantic_tags <= covered_domain_tags)
        if content_is_covered:
            continue
        retained.append(definition.wire_id)
    return tuple(retained)


def _is_action_or_asset_parameter(name: str) -> bool:
    normalized = name.casefold()
    return any(
        token in normalized
        for token in ("action", "event", "icon", "image", "asset", "source", "src")
    )


def _parameter_value_kind(name: str, schema: dict[str, Any]) -> str:
    semantic_text = f"{name} {schema.get('description', '')}".casefold()
    if any(
        token in semantic_text
        for token in (
            "icon",
            "image",
            "asset",
            "source",
            "src",
            "图标",
            "图片",
            "素材",
            "资源",
        )
    ):
        return "asset-source"
    if any(token in semantic_text for token in ("action", "event", "操作", "事件")):
        return "action-id"
    return "literal"


def _variant_requires_action(variant: Any) -> bool:
    required = variant.parameters_schema.get("required", [])
    return any(
        _parameter_value_kind(name, variant.parameters_schema.get("properties", {}).get(name, {}))
        == "action-id"
        for name in required
    )


def _ranked_templates(text: str, registry: CardPlanRegistry):
    return sorted(
        registry.templates.values(),
        key=lambda item: (
            -_token_overlap(
                text,
                " ".join((item.template_id, item.description, *item.domain_tags)),
            ),
            item.wire_id,
        ),
    )


def _semantic_text(task_spec: TaskSpec, ui_brief: Any) -> str:
    values = [task_spec.userQuery, json.dumps(task_spec.dataModelSchema, ensure_ascii=False)]
    if ui_brief is not None:
        values.append(json.dumps(ui_brief.model_dump(by_alias=True), ensure_ascii=False))
    return " ".join(values).casefold()


def _token_overlap(left: str, right: str) -> int:
    overlap = _semantic_tokens(left) & _semantic_tokens(right)
    return sum(max(1, len(token) - 1) for token in overlap)


def _semantic_tokens(value: str) -> set[str]:
    normalized = value.casefold()
    tokens = set(re.findall(r"[a-z0-9]+", normalized))
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        for width in range(2, min(4, len(run)) + 1):
            tokens.update(run[index : index + width] for index in range(len(run) - width + 1))
    return tokens


def _resolve_content_action_ids(
    *,
    ui_brief: Any,
    actions: tuple[ActionBinding, ...],
    definitions: list[Any],
) -> tuple[str, ...]:
    if not actions:
        return ()
    placement = getattr(ui_brief, "action_placement", "auto")
    if placement in {"card", "none"}:
        return ()
    has_action_template = any(definition.action_policy != "none" for definition in definitions)
    if has_action_template and placement in {"content", "auto"}:
        return tuple(action.action_id for action in actions)
    return ()


def _collect_facts(value: Any, path: str = "", source: str = "task") -> list[Fact]:
    if isinstance(value, dict) and "sampleValue" in value:
        sample = value["sampleValue"]
        if sample is None or isinstance(sample, (str, int, float, bool)):
            return [Fact(source=source, path=path or "/", value=sample)]
    if isinstance(value, dict):
        result: list[Fact] = []
        for key, child in value.items():
            result.extend(_collect_facts(child, f"{path}/{key}", source))
        return result
    if isinstance(value, list) and value:
        return _collect_facts(value[0], f"{path}/0", source)
    return []


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _action_label(event: Any) -> str:
    display_label = getattr(event, "displayLabel", None)
    if isinstance(display_label, str) and display_label.strip():
        return display_label.strip()
    return _ACTION_LABELS.get(getattr(event, "id", "") or "", "打开详情")
