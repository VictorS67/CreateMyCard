"""高级组件分支的独立两轮模型编排。"""

from __future__ import annotations

from typing import Any

from app.logger import logger
from config.config import get_settings
from custom.a2ui_model_client import A2UIModelClient
from models.generation import TaskSpec

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
    ) -> AdvancedPipelineOutput | None:
        data_shape = extract_data_shape(task_spec)

        async def generate_json(
            prompt: list[dict[str, str]],
            phase: str,
        ) -> dict[str, Any]:
            return await model_client.generate_json(prompt, phase=phase)

        planner_mode = "llm"
        try:
            ui_brief = await plan_ui_with_llm(task_spec, data_shape, generate_json)
        except (RuntimeError, ValueError) as exc:
            planner_mode = "offline"
            ui_brief = plan_ui_offline(task_spec, data_shape)
            logger.warning(f"{_MODULE} ui_brief_fallback exception_type={type(exc).__name__}")

        selection = select_component(
            data_shape,
            ui_brief,
            SelectionConstraints(
                size=task_spec.size,
                action_count=len(task_spec.eventCandidates),
            ),
        )
        if selection is None:
            logger.info(f"{_MODULE} component_not_selected fallback=terse")
            return None

        style_id, _tokens = select_style(ui_brief)
        mapper_mode = "llm"
        try:
            invocation = await map_arguments_with_llm(
                selection.component_id,
                task_spec,
                data_shape,
                ui_brief,
                generate_json,
            )
        except (RuntimeError, ValueError) as exc:
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
        )
