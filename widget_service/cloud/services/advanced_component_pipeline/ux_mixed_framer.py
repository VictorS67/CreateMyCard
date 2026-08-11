"""Deterministic framing repairs that are exclusive to the new UX mixed entry."""

from __future__ import annotations

import json
from typing import Any

from models.generation import WidgetSize
from services.advanced_component_pipeline.models import UX_LAYOUT_COMPONENT_IDS
from services.cardplan_template.parser import (
    ParsedCall,
    normalize_hybrid_source,
    parse_hybrid_card,
    parse_ux_layout_card,
)
from services.cardplan_template.registry import CardPlanRegistry

_UX_ACTION_COMPONENTS = frozenset({"PillAction", "IconAction", "ActionTile"})


def frame_ux_layout_root_children(
    source: str,
    *,
    size: WidgetSize,
    registry: CardPlanRegistry,
) -> tuple[str, bool]:
    """Frame overflow for the direct layout-root protocol without touching Action."""
    normalized = normalize_hybrid_source(source)
    normalized, trailing_delimiters_repaired = _close_trailing_delimiters(normalized)
    root = parse_ux_layout_card(normalized)
    maximum = registry.require_ux_layout_component(root.name).max_children_by_size[size]
    actions = tuple(
        child
        for child in root.children
        if child.kind == "component" and child.name in _UX_ACTION_COMPONENTS
    )
    content = tuple(child for child in root.children if child not in actions)
    if len(content) <= maximum:
        return normalized, trailing_delimiters_repaired
    retained = content[: max(maximum - 1, 0)]
    overflow = content[max(maximum - 1, 0) :]
    grouped = ParsedCall(
        kind="component",
        name="Column",
        values=("section",),
        children=overflow,
        span=root.span,
    )
    framed_root = ParsedCall(
        kind=root.kind,
        name=root.name,
        values=root.values,
        children=(*retained, grouped, *actions),
        span=root.span,
    )
    return _serialize_call(framed_root) + ";", True


def frame_ux_layout_children(
    source: str,
    *,
    size: WidgetSize,
    registry: CardPlanRegistry,
) -> tuple[str, bool]:
    """Group layout overflow into a standard Column without changing any facts."""
    normalized = normalize_hybrid_source(source)
    normalized, trailing_delimiters_repaired = _close_trailing_delimiters(normalized)
    root = parse_hybrid_card(normalized)
    layout = root.children[0]
    if layout.kind != "component" or layout.name not in UX_LAYOUT_COMPONENT_IDS:
        return normalized, trailing_delimiters_repaired
    maximum = registry.require_ux_layout_component(layout.name).max_children_by_size[size]
    if len(layout.children) <= maximum:
        return normalized, trailing_delimiters_repaired
    retained = layout.children[: max(maximum - 1, 0)]
    overflow = layout.children[max(maximum - 1, 0) :]
    grouped = ParsedCall(
        kind="component",
        name="Column",
        values=("section",),
        children=overflow,
        span=layout.span,
    )
    framed_layout = ParsedCall(
        kind=layout.kind,
        name=layout.name,
        values=layout.values,
        children=(*retained, grouped),
        span=layout.span,
    )
    framed_root = ParsedCall(
        kind=root.kind,
        name=root.name,
        values=root.values,
        children=(framed_layout,),
        span=root.span,
    )
    return _serialize_call(framed_root) + ";", True


def _close_trailing_delimiters(source: str) -> tuple[str, bool]:
    """Close a small, typed EOF-only delimiter suffix; never repair crossed input."""
    stripped = source.strip()
    if not stripped.endswith(";"):
        return source, False
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = set(pairs.values())
    stack: list[str] = []
    in_string: str | None = None
    escaped = False
    for char in stripped[:-1]:
        if in_string is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {'"', "'"}:
            in_string = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in closers:
            if not stack or stack[-1] != char:
                return source, False
            stack.pop()
    if in_string is not None or not stack or len(stack) > 4:
        return source, False
    return stripped[:-1] + "".join(reversed(stack)) + ";", True


def _serialize_call(call: ParsedCall) -> str:
    values: list[str]
    if call.kind == "template":
        if call.name == "card@1":
            values = [_literal(call.name), _literal(call.values[0])]
        else:
            values = [_literal(call.name), *(_literal(value) for value in call.values)]
        values.extend(_serialize_call(child) for child in call.children)
        return f"Template({', '.join(values)})"
    values = [_literal(value) for value in call.values]
    values.extend(_serialize_call(child) for child in call.children)
    return f"{call.name}({', '.join(values)})"


def _literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
