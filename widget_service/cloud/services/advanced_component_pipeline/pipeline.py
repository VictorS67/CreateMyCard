"""高级组件分支的独立两轮模型编排。"""

from __future__ import annotations

import inspect
from typing import Any, Literal

from app.logger import logger
from config.config import get_settings
from custom.a2ui_model_client import A2UIModelClient
from custom.deepseek_call_budget import DeepSeekCallBudgetExceeded
from models.generation import TaskSpec
from services.cardplan_template.compiler import compile_hybrid_card
from services.cardplan_template.prompt import build_hybrid_prompt
from services.cardplan_template.registry import get_cardplan_registry
from services.protocol_registry import TERSE_DSL_NESTED2_PROFILE_ID, A2UIProtocolRegistry

from .argument_mapper import map_arguments_offline, map_arguments_with_llm
from .compiler import build_component_output
from .component_selector import select_component
from .data_shape import extract_data_shape
from .models import AdvancedPipelineOutput, SelectionConstraints
from .styles import select_style
from .ui_planner import plan_ui_offline, plan_ui_with_llm

_MODULE = "[Advanced Component Pipeline]"


class AdvancedComponentPipeline:
    """模型负责语义规划和参数映射，服务端负责选择与模板编译。"""

    async def generate(
        self,
        task_spec: TaskSpec,
        model_client: A2UIModelClient,
        card_spec: dict[str, Any] | None = None,
        *,
        force_hybrid: bool = False,
        allow_offline_fallback: bool = True,
    ) -> AdvancedPipelineOutput | None:
        data_shape = extract_data_shape(task_spec)
        registry = get_cardplan_registry()

        async def generate_json(
            prompt: list[dict[str, str]],
            phase: str,
        ) -> dict[str, Any]:
            if phase == "advanced-ui-brief":
                logger.info(
                    f"{_MODULE} ui_planner_prompt_built phase={phase} "
                    f"message_count={len(prompt)} "
                    f"prompt_chars={sum(len(item['content']) for item in prompt)}"
                )
            elif phase == "advanced-argument-map":
                logger.info(
                    f"{_MODULE} argument_mapper_prompt_built "
                    f"phase={phase} message_count={len(prompt)} "
                    f"prompt_chars={sum(len(item['content']) for item in prompt)}"
                )
            response = await model_client.generate_json(prompt, phase=phase)
            logger.info(
                f"{_MODULE} model_response_received phase={phase} field_count={len(response)}"
            )
            return response

        planner_mode: Literal["llm", "offline"] = "llm"
        try:
            ui_brief = await plan_ui_with_llm(task_spec, data_shape, generate_json)
        except DeepSeekCallBudgetExceeded:
            raise
        except (RuntimeError, ValueError) as exc:
            if not allow_offline_fallback:
                raise
            planner_mode = "offline"
            ui_brief = plan_ui_offline(task_spec, data_shape)
            logger.warning(f"{_MODULE} ui_brief_fallback exception_type={type(exc).__name__}")
        logger.info(
            f"{_MODULE} ui_brief_resolved mode={planner_mode} "
            f"template_candidate_count={len(ui_brief.local_template_ids)} "
            f"theme_selected={ui_brief.theme_id is not None}"
        )

        selection = select_component(
            data_shape,
            ui_brief,
            SelectionConstraints(
                size=task_spec.size,
                action_count=len(task_spec.eventCandidates),
                asset_count=len(task_spec.assetCandidates),
            ),
        )
        selection_candidates = selection.candidates if selection is not None else []
        confidence = selection.confidence if selection is not None else 0.0
        threshold = getattr(
            get_settings(),
            "advanced_whole_card_confidence_threshold",
            0.75,
        )
        use_hybrid = force_hybrid or selection is None or confidence < threshold
        selected_component = selection.component_id if selection is not None else "none"
        logger.info(
            f"{_MODULE} component_selection_completed selected_component_id={selected_component} "
            f"confidence={confidence} threshold={threshold} force_hybrid={force_hybrid} "
            f"route={'hybrid-template' if use_hybrid else 'whole-card-template'} "
            f"candidate_count={len(selection_candidates)}"
        )

        if use_hybrid and getattr(model_client, "use_mock", False) and not force_hybrid:
            logger.info(f"{_MODULE} hybrid_route_skipped reason=legacy-mock-compatibility")
            return None

        if use_hybrid:
            if card_spec is None:
                card_spec = {
                    "title": task_spec.userQuery[:8],
                    "description": task_spec.userQuery[:12],
                    "suggestSize": task_spec.size,
                }
            projection = build_hybrid_prompt(
                task_spec=task_spec,
                card_spec=card_spec,
                ui_brief=ui_brief,
                registry=registry,
            )
            logger.info(
                f"{_MODULE} hybrid_prompt_built message_count={len(projection.messages)} "
                f"prompt_chars={sum(len(item['content']) for item in projection.messages)} "
                f"template_candidate_count={len(projection.requested_template_ids)}"
            )
            raw_output = await _generate_hybrid_body(
                model_client,
                projection.messages,
            )
            protocol_profile = A2UIProtocolRegistry.read_design_protocol_profile(
                TERSE_DSL_NESTED2_PROFILE_ID
            )
            compilation = compile_hybrid_card(
                raw_output,
                task_spec=task_spec,
                contract=projection.contract,
                protocol_profile=protocol_profile,
                registry=registry,
            )
            logger.info(
                f"{_MODULE} hybrid_generation_completed template_call_count="
                f"{compilation.stats.template_call_count} expanded_component_count="
                f"{compilation.stats.expanded_component_count} fallback_used=false"
            )
            return AdvancedPipelineOutput(
                component_id="cardplan-template-hybrid",
                style_id=projection.theme_id,
                source_dsl=compilation.raw_output,
                source_format="a2ui",
                ui_brief=ui_brief,
                invocation={"requestedTemplateIds": projection.requested_template_ids},
                planner_mode=planner_mode,
                mapper_mode="llm",
                route="hybrid-template",
                whole_card_confidence=confidence,
                whole_card_candidates=selection_candidates,
                confidence_bypassed=force_hybrid,
                raw_output=compilation.raw_output,
                effective_output=compilation.effective_output,
                compiled_a2ui=compilation.a2ui,
                fallback_used=False,
                template_call_count=compilation.stats.template_call_count,
                template_used_ids=list(compilation.stats.template_used_ids),
                expanded_component_count=compilation.stats.expanded_component_count,
            )

        if selection is None:
            return None

        style_id, _tokens = select_style(ui_brief)
        mapper_mode: Literal["llm", "offline"] = "llm"
        try:
            invocation = await map_arguments_with_llm(
                selection.component_id,
                task_spec,
                data_shape,
                ui_brief,
                generate_json,
            )
        except (RuntimeError, ValueError) as exc:
            if not allow_offline_fallback:
                raise
            mapper_mode = "offline"
            try:
                invocation = map_arguments_offline(
                    selection.component_id,
                    task_spec,
                    data_shape,
                )
            except ValueError:
                logger.warning(
                    f"{_MODULE} invocation_fallback_failed "
                    f"exception_type={type(exc).__name__} fallback=terse"
                )
                return None
            logger.warning(f"{_MODULE} invocation_fallback exception_type={type(exc).__name__}")

        logger.info(
            f"{_MODULE} invocation_resolved mode={mapper_mode} "
            f"component_id={selection.component_id} "
            f"invocation_field_count={len(invocation.model_dump())}"
        )

        output_format = get_settings().advanced_component_output_format
        source_dsl = build_component_output(
            selection.component_id,
            invocation,
            task_spec,
            style_id,
            output_format,
        )
        logger.info(
            f"{_MODULE} generation_completed component_id={selection.component_id} "
            f"style_id={style_id} output_format={output_format} "
            f"planner_mode={planner_mode} mapper_mode={mapper_mode}"
        )
        return AdvancedPipelineOutput(
            component_id=selection.component_id,
            style_id=style_id,
            source_dsl=source_dsl,
            source_format=output_format,
            ui_brief=ui_brief,
            invocation=invocation.model_dump(mode="json"),
            planner_mode=planner_mode,
            mapper_mode=mapper_mode,
            route="whole-card-template",
            whole_card_confidence=selection.confidence,
            whole_card_candidates=selection.candidates,
            raw_output=source_dsl,
            effective_output=source_dsl,
        )


async def _generate_hybrid_body(
    model_client: A2UIModelClient,
    messages: list[dict[str, str]],
) -> str:
    profile = {"id": TERSE_DSL_NESTED2_PROFILE_ID, "format": "hybrid-card"}
    generate = model_client.generate
    parameters = inspect.signature(generate).parameters
    accepts_keywords = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    if accepts_keywords or "suppress_prompt_log" in parameters:
        result = generate(
            messages,
            profile,
            suppress_prompt_log=True,
            phase="hybrid-body",
        )
    else:
        result = generate(messages, profile)
    if inspect.isawaitable(result):
        return await result
    return result
