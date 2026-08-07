"""Trusted Template expansion, Hybrid Contract checks, and A2UI lowering."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

from models.generation import TaskSpec
from services.terse_dsl_nested2_converter import (
    Nested2Node,
    TerseDslNested2ConversionError,
    convert_terse_dsl_nested2_to_a2ui,
)

from .models import ExpansionStats, HybridBodyContract, TemplateNode, TemplateValue
from .parser import ParsedCall, parse_hybrid_card
from .registry import CardPlanRegistry

_CONTAINERS = frozenset({"Row", "Column", "List", "Stack"})
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
    ("Text", "ux-title-compact"): "title",
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
    raw_count = _count_calls(composition.children[0])
    if raw_count > contract.limits.max_raw_components:
        raise TerseDslNested2ConversionError("Hybrid raw component budget exceeded.")
    _reject_direct_events(composition.children[0])
    _validate_raw_components(composition.children[0], contract)
    state = _ExpansionState(template_ids=[], action_ids=[], action_occurrences=[])
    content = _expand_call(
        composition.children[0],
        parent="$root",
        contract=contract,
        registry=registry,
        state=state,
    )
    content = _lower_capsule_progress(content)
    card_params = _drop_duplicate_chrome(card_params, content)
    root = _compile_card_shell(card_params, content, task_spec, contract, registry)
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
    content_height = _estimate_height(content)
    body_budget = _body_budget(card_params, contract)
    space_constrained = content_height > body_budget
    if space_constrained:
        content = _constrain_content_height(content, body_budget)
        root = _compile_card_shell(card_params, content, task_spec, contract, registry)
    effective = _serialize_node(root) + ";"
    a2ui = convert_terse_dsl_nested2_to_a2ui(
        effective,
        size=task_spec.size,
        protocol_profile=protocol_profile,
    )
    if "Template" in a2ui:
        raise TerseDslNested2ConversionError("Template leaked into final A2UI.")
    return HybridCompilation(
        raw_output=source,
        effective_output=effective,
        a2ui=a2ui,
        stats=ExpansionStats(
            template_call_count=state.template_calls + 1,
            template_used_ids=tuple(state.template_ids),
            expanded_component_count=count,
            raw_component_count=raw_count,
            max_depth=depth,
            estimated_height_vp=content_height,
            vertical_budget_vp=body_budget,
            space_constrained=space_constrained,
            action_used_ids=tuple(state.action_ids),
        ),
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
        children = tuple(
            _expand_call(
                child,
                parent=call.name,
                contract=contract,
                registry=registry,
                state=state,
            )
            for child in call.children
        )
        return Nested2Node(
            call.name,
            _normalize_component_values(call.name, call.values),
            children,
        )
    wire_id = call.name
    if wire_id not in contract.allowed_template_ids:
        raise TerseDslNested2ConversionError(f"Template is not allowed: {wire_id}")
    definition = registry.require_template(wire_id)
    if parent not in definition.allowed_parent_components:
        raise TerseDslNested2ConversionError(f"Template parent is not allowed: {wire_id}/{parent}")
    size, params = call.values
    variant = registry.require_variant(wire_id, str(size))
    errors = sorted(Draft202012Validator(variant.parameters_schema).iter_errors(params), key=str)
    if errors:
        raise TerseDslNested2ConversionError(
            f"Template params are invalid for {wire_id}/{size}: {errors[0].message}"
        )
    _validate_template_params(params, definition.asset_parameter_semantic_tags, contract)
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


def _validate_template_params(
    params: dict[str, Any],
    asset_tags: dict[str, tuple[str, ...]],
    contract: HybridBodyContract,
) -> None:
    del asset_tags
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
            elif isinstance(item, str) and item not in contract.trusted_literals:
                action_ids = {binding.action_id for binding in contract.action_bindings}
                if item not in action_ids:
                    raise TerseDslNested2ConversionError(f"Template literal is not trusted: {item}")
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                if item not in contract.trusted_numbers and item not in {0, 1, 100}:
                    raise TerseDslNested2ConversionError(f"Template number is not trusted: {item}")


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
    if theme.root_component == "Stack" and "alignContent" in root_options:
        root_options["alignItems"] = root_options.pop("alignContent")
    root_options.pop("width", None)
    root_options.pop("height", None)
    root_options["_id"] = "root"
    children: list[Nested2Node] = []
    header_children: list[Nested2Node] = []
    if "titleIcon" in params:
        header_children.append(Nested2Node("Image", (params["titleIcon"], "icon"), ()))
    if "title" in params:
        header_children.append(Nested2Node("Text", (params["title"], "title"), ()))
    if "subtitle" in params:
        header_children.append(Nested2Node("Text", (params["subtitle"], "subtitle"), ()))
    if header_children:
        children.append(Nested2Node("Row", ("between",), tuple(header_children)))
    children.append(content)
    action = params.get("action")
    if isinstance(action, dict):
        binding = next(item for item in contract.action_bindings if item.action_id == action["id"])
        event = next(item for item in task_spec.eventCandidates if item.id == binding.action_id)
        options = {
            "width": "100%",
            "height": 30,
            "padding": 2,
            "borderRadius": 15,
            "backgroundColor": "#24FFFFFF",
            "alignContent": "center",
            "onClick": [{"call": event.call, "args": event.args}],
        }
        label = Nested2Node("Text", (binding.display_label, "body"), ())
        row = Nested2Node("Row", ("actions",), (label,))
        children.append(Nested2Node("Stack", ("overlay", options), (row,)))
    return Nested2Node("Column", ("card", root_options), tuple(children))


def _drop_duplicate_chrome(
    params: dict[str, Any],
    content: Nested2Node,
) -> dict[str, Any]:
    visible = {
        value
        for node in _walk_nodes(content)
        for raw in node.values
        for value in _primitive_values(raw)
        if isinstance(value, str)
    }
    normalized = dict(params)
    for key in ("subtitle", "title"):
        if normalized.get(key) in visible:
            normalized.pop(key, None)
    return normalized


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
    if node.name == "Button":
        raise TerseDslNested2ConversionError("Direct Buttons are forbidden in Hybrid content.")
    approved_strings = {
        *contract.trusted_literals,
        *contract.allowed_design_tokens,
        *contract.allowed_layout_tokens,
        *contract.allowed_asset_sources,
    }
    approved_numbers = {*contract.trusted_numbers, 0, 1, 100}
    for value in node.values:
        for item in _primitive_values(value):
            if isinstance(item, str) and item not in approved_strings:
                raise TerseDslNested2ConversionError(f"Raw literal is not trusted: {item}")
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                if item not in approved_numbers:
                    raise TerseDslNested2ConversionError(f"Raw number is not trusted: {item}")
    for child in node.children:
        _validate_raw_components(child, contract)


def _contains_key(value: Any, keys: frozenset[str]) -> bool:
    if isinstance(value, dict):
        return any(key in keys or _contains_key(child, keys) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_key(child, keys) for child in value)
    return False


def _validate_expanded_tree(root: Nested2Node, contract: HybridBodyContract) -> None:
    seen_assets: set[str] = set()
    seen_strings: list[str] = []

    def visit(node: Nested2Node, trusted_blueprint: bool = False) -> None:
        if node.component_type not in contract.allowed_components:
            raise TerseDslNested2ConversionError(
                f"Expanded component is not allowed: {node.component_type}"
            )
        if node.component_type == "Image" and node.values:
            source = node.values[0]
            if source not in contract.allowed_asset_sources:
                raise TerseDslNested2ConversionError(f"Image source is not approved: {source}")
            seen_assets.add(str(source))
        for value in node.values:
            seen_strings.extend(item for item in _primitive_values(value) if isinstance(item, str))
        for child in node.children:
            visit(child, trusted_blueprint)

    visit(root)
    missing_assets = set(contract.required_asset_sources) - seen_assets
    if missing_assets:
        raise TerseDslNested2ConversionError(
            f"Required assets are missing: {sorted(missing_assets)}"
        )
    visible = "\n".join(seen_strings)
    missing_literals = [item for item in contract.required_literals if item not in visible]
    if missing_literals:
        raise TerseDslNested2ConversionError(
            f"Required literals are missing: {missing_literals[:3]}"
        )


def _count_calls(node: ParsedCall) -> int:
    return 1 + sum(_count_calls(child) for child in node.children)


def _shape(node: Nested2Node) -> tuple[int, int]:
    if not node.children:
        return 1, 1
    child_shapes = [_shape(child) for child in node.children]
    return 1 + sum(item[0] for item in child_shapes), 1 + max(item[1] for item in child_shapes)


def _body_budget(params: dict[str, Any], contract: HybridBodyContract) -> int:
    header = 24 if any(key in params for key in ("title", "subtitle", "titleIcon")) else 0
    action = 36 if "action" in params else 0
    return max(24, contract.limits.vertical_budget_vp - header - action)


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
