"""Shared fixtures for direct health overview component tests."""

from __future__ import annotations

from typing import Any

from models.generation import EventAction, TaskSpec
from services.advanced_component_pipeline.content_selectors import (
    project_content_component_facts,
)
from services.advanced_component_pipeline.models import AdvancedScopeBrief
from services.advanced_component_pipeline.ux_mixed_prompt import build_ux_mixed_prompt
from services.cardplan_template.compiler import compile_ux_layout_card
from services.cardplan_template.registry import get_cardplan_registry
from services.protocol_registry import TERSE_DSL_NESTED2_PROFILE_ID, A2UIProtocolRegistry


def field(value: Any, data_type: str) -> dict[str, Any]:
    return {"type": data_type, "sampleValue": value}


def sport_action(action_id: str = "event.open.health.sport") -> EventAction:
    return EventAction(
        id=action_id,
        displayLabel="打开运动健康",
        call="clickToIntent",
        args={"intentName": "HealthSport"},
    )


def compile_health_scope(
    task_spec: TaskSpec,
    component_ids: tuple[str, ...],
    capability_ids: set[str],
    source: str,
):
    projected = project_content_component_facts(
        task_spec,
        capability_ids,
        component_ids,
    )
    scope = AdvancedScopeBrief(
        themeId="meeting-paper-neutral",
        advancedComponentIds=component_ids,
    )
    projection = build_ux_mixed_prompt(
        task_spec=projected,
        card_spec={
            "suggestSize": task_spec.size,
            "dataBindings": [
                {"capabilityId": capability_id}
                for capability_id in sorted(capability_ids)
            ],
        },
        scope=scope,
        registry=get_cardplan_registry(),
    )
    compiled = compile_ux_layout_card(
        source,
        task_spec=projected,
        contract=projection.contract,
        protocol_profile=A2UIProtocolRegistry.read_design_protocol_profile(
            TERSE_DSL_NESTED2_PROFILE_ID
        ),
        registry=get_cardplan_registry(),
    )
    return compiled, projection, projected
