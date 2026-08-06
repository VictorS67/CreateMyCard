# -*- coding: utf-8 -*-
# ruff: noqa: E402
"""高级组件两轮模型、回退和模板编译测试。"""

import importlib
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLOUD_ROOT = PROJECT_ROOT / "cloud"
if str(CLOUD_ROOT) not in sys.path:
    sys.path.insert(0, str(CLOUD_ROOT))

from api.schemas import GenerateWidgetCardRequest
from core.errors import GenerationStatus
from custom.a2ui_model_client import A2UIModelClient
from models.generation import EventAction, TaskSpec
from models.service import ArtifactSaveResult
from services.advanced_component_pipeline import AdvancedComponentPipeline
from services.advanced_component_pipeline.argument_mapper import validate_invocation
from services.advanced_component_pipeline.compiler import build_terse_nested2
from services.advanced_component_pipeline.component_registry import component_plugins
from services.advanced_component_pipeline.components.base import sample_data
from services.advanced_component_pipeline.components.compact_metrics_primary_action.plugin import (
    CompactMetricArg,
)
from services.advanced_component_pipeline.components.compact_metrics_primary_action.plugin import (
    Invocation as CompactMetricsInvocation,
)
from services.advanced_component_pipeline.components.ring_split_metric_action.plugin import (
    Invocation as RingSplitMetricInvocation,
)
from services.advanced_component_pipeline.components.schedule_detail_action.plugin import (
    Invocation as ScheduleDetailInvocation,
)
from services.advanced_component_pipeline.models import (
    ActionRef,
    AdvancedPipelineOutput,
    BindingRef,
    UIBrief,
)
from services.advanced_component_pipeline.styles import STYLE_TOKENS
from services.artifact_store import ArtifactStore
from services.compact_dsl_a2ui_converter import convert_compact_dsl_to_a2ui
from services.generation_pipeline import (
    DslProcessorKind,
    GenerationRoutePolicy,
)
from services.protocol_registry import (
    TERSE_DSL_NESTED2_PROFILE_ID,
)
from services.source_artifact_repository import SourceArtifactLoadResult
from services.terse_dsl_nested2_converter import convert_terse_dsl_nested2_to_a2ui
from services.validator import ArtifactValidator
from services.widget_generation_service import WidgetGenerationService


def _metric_task_spec(with_action: bool = True) -> TaskSpec:
    events = [EventAction(id="event.clean.memory", call="clickToApi", args={})]
    return TaskSpec(
        userQuery="清理内存",
        size="2x2",
        eventCandidates=events if with_action else [],
        dataModelSchema={
            "data": {
                "memory": {
                    "usedPercent": {
                        "type": "number",
                        "description": "内存占用百分比",
                        "sampleValue": 78,
                    },
                    "available": {
                        "type": "number",
                        "description": "可用内存容量",
                        "sampleValue": 4.5,
                    },
                }
            }
        },
    )


def test_component_plugins_are_discovered_from_component_directories():
    plugins = component_plugins()

    assert {plugin.component_id for plugin in plugins} == {
        "compact-metrics-primary-action",
        "ring-split-metric-action",
        "schedule-detail-action",
    }
    assert all(plugin.invocation_model for plugin in plugins)
    assert all(callable(plugin.build) for plugin in plugins)
    assert all(callable(plugin.map_offline) for plugin in plugins)
    assert all(callable(plugin.validate) for plugin in plugins)


class OfflineModelClient:
    async def generate_json(self, _prompt, *, phase):
        raise RuntimeError(f"offline: {phase}")


class StructuredModelClient:
    def __init__(self):
        self.phases = []

    async def generate_json(self, _prompt, *, phase):
        self.phases.append(phase)
        if phase == "advanced-ui-brief":
            return {
                "purpose": "resource-monitoring",
                "primaryInformation": ["内存占用"],
                "informationHierarchy": ["指标", "操作"],
                "density": "compact",
                "temporality": "now",
                "interaction": "one-primary-action",
                "attention": "warning-capable",
                "visualTone": "technical-efficient",
                "contentPriorities": ["占用优先"],
                "reason": "突出当前资源状态。",
            }
        return {
            "compact_metrics": [
                {
                    "label": "已用",
                    "value": {"path": "/data/memory/usedPercent"},
                },
                {
                    "label": "可用",
                    "value": {"path": "/data/memory/available"},
                },
            ],
            "primary_label": "内存已用",
            "primary_value": {"path": "/data/memory/usedPercent"},
            "action": {"event_id": "event.clean.memory", "label": "一键清理"},
        }


@pytest.mark.asyncio
async def test_pipeline_uses_two_structured_model_calls_and_builds_template():
    model_client = StructuredModelClient()
    output = await AdvancedComponentPipeline().generate(_metric_task_spec(), model_client)

    assert output is not None
    assert output.component_id == "compact-metrics-primary-action"
    assert output.planner_mode == "llm"
    assert output.mapper_mode == "llm"
    assert model_client.phases == ["advanced-ui-brief", "advanced-argument-map"]
    assert output.source_dsl.startswith('Column("card"')
    assert "\ndata = " in output.source_dsl
    assert '"onClick":[{"call":"clickToApi","args":{}}]' in output.source_dsl


@pytest.mark.asyncio
async def test_pipeline_uses_offline_fallback_when_structured_model_fails():
    output = await AdvancedComponentPipeline().generate(
        _metric_task_spec(),
        OfflineModelClient(),
    )

    assert output is not None
    assert output.planner_mode == "offline"
    assert output.mapper_mode == "offline"


@pytest.mark.asyncio
async def test_pipeline_returns_none_without_required_action():
    output = await AdvancedComponentPipeline().generate(
        _metric_task_spec(with_action=False),
        OfflineModelClient(),
    )

    assert output is None


def test_invocation_rejects_string_binding_for_progress():
    task_spec = _metric_task_spec()
    invocation = RingSplitMetricInvocation(
        caption=BindingRef(path="/data/memory/available"),
        progress=BindingRef(path="/data/memory/missingText"),
        major_value=BindingRef(path="/data/memory/usedPercent"),
        major_unit="%",
        minor_value=BindingRef(path="/data/memory/available"),
        minor_unit="GB",
        action=ActionRef(event_id="event.clean.memory", label="清理"),
    )
    task_spec.dataModelSchema["data"]["memory"]["missingText"] = {
        "type": "string",
        "description": "展示文本",
        "sampleValue": "78%",
    }

    with pytest.raises(ValueError, match="must be numeric"):
        validate_invocation("ring-split-metric-action", invocation, task_spec)


@pytest.mark.asyncio
async def test_advanced_template_converts_to_standard_a2ui():
    output = await AdvancedComponentPipeline().generate(
        _metric_task_spec(),
        OfflineModelClient(),
    )
    assert output is not None
    profile = {
        "version": "v0.9",
        "sizes": {"2x2": {"width": 160, "height": 160}},
    }

    genui = convert_terse_dsl_nested2_to_a2ui(
        output.source_dsl,
        size="2x2",
        protocol_profile=profile,
    )

    assert len(genui.splitlines()) == 3
    assert '"createSurface"' in genui


@pytest.mark.parametrize(
    ("component_id", "invocation", "style_id"),
    [
        (
            "ring-split-metric-action",
            RingSplitMetricInvocation(
                caption=BindingRef(path="/data/metric/caption"),
                progress=BindingRef(path="/data/metric/progress"),
                major_value=BindingRef(path="/data/metric/major"),
                major_unit="小时",
                minor_value=BindingRef(path="/data/metric/minor"),
                minor_unit="分钟",
                action=ActionRef(event_id="event.go", label="立即开始"),
            ),
            "night-violet",
        ),
        (
            "schedule-detail-action",
            ScheduleDetailInvocation(
                caption="下一个日程",
                entity_title=BindingRef(path="/data/metric/title"),
                start_time=BindingRef(path="/data/metric/start"),
                end_time=BindingRef(path="/data/metric/end"),
                reminder_value=BindingRef(path="/data/metric/reminder"),
                action=ActionRef(event_id="event.go", label="专注模式"),
            ),
            "warm-copper",
        ),
    ],
)
def test_other_advanced_templates_convert_to_standard_a2ui(
    component_id,
    invocation,
    style_id,
):
    task_spec = TaskSpec(
        userQuery="生成状态卡片",
        size="2x2",
        eventCandidates=[EventAction(id="event.go", call="clickToApi", args={})],
        dataModelSchema={
            "data": {
                "metric": {
                    name: {
                        "type": "number"
                        if name in {"progress", "major", "minor", "reminder"}
                        else "string",
                        "description": name,
                        "sampleValue": 10
                        if name in {"progress", "major", "minor", "reminder"}
                        else name,
                    }
                    for name in (
                        "caption",
                        "progress",
                        "major",
                        "minor",
                        "title",
                        "start",
                        "end",
                        "reminder",
                    )
                }
            }
        },
    )
    source_dsl = build_terse_nested2(component_id, invocation, task_spec, style_id)

    genui = convert_terse_dsl_nested2_to_a2ui(
        source_dsl,
        size="2x2",
        protocol_profile={"version": "v0.9", "sizes": {"2x2": {"width": 160, "height": 160}}},
    )

    assert len(genui.splitlines()) == 3
    assert '"linearGradient"' in genui


@pytest.mark.parametrize(
    ("component_id", "module_name", "builder_name", "invocation", "style_id"),
    [
        (
            "compact-metrics-primary-action",
            "services.advanced_component_pipeline.components.compact_metrics_primary_action.plugin",
            "build",
            CompactMetricsInvocation(
                compact_metrics=[
                    CompactMetricArg(
                        label="主指标", value=BindingRef(path="/data/metric/progress")
                    ),
                    CompactMetricArg(label="次指标", value=BindingRef(path="/data/metric/major")),
                ],
                primary_label="核心指标",
                primary_value=BindingRef(path="/data/metric/progress"),
                action=ActionRef(event_id="event.go", label="立即处理"),
            ),
            "system-teal",
        ),
        (
            "ring-split-metric-action",
            "services.advanced_component_pipeline.components.ring_split_metric_action.plugin",
            "build",
            RingSplitMetricInvocation(
                caption=BindingRef(path="/data/metric/caption"),
                progress=BindingRef(path="/data/metric/progress"),
                major_value=BindingRef(path="/data/metric/major"),
                major_unit="小时",
                minor_value=BindingRef(path="/data/metric/minor"),
                minor_unit="分钟",
                action=ActionRef(event_id="event.go", label="立即开始"),
            ),
            "night-violet",
        ),
        (
            "schedule-detail-action",
            "services.advanced_component_pipeline.components.schedule_detail_action.plugin",
            "build",
            ScheduleDetailInvocation(
                caption="下一个日程",
                entity_title=BindingRef(path="/data/metric/title"),
                start_time=BindingRef(path="/data/metric/start"),
                end_time=BindingRef(path="/data/metric/end"),
                reminder_value=BindingRef(path="/data/metric/reminder"),
                action=ActionRef(event_id="event.go", label="专注模式"),
            ),
            "warm-copper",
        ),
    ],
)
def test_terse_templates_produce_same_a2ui_as_previous_compact_templates(
    monkeypatch,
    component_id,
    module_name,
    builder_name,
    invocation,
    style_id,
):
    task_spec = _template_task_spec()
    profile = {"version": "v0.9", "sizes": {"2x2": {"width": 160, "height": 160}}}
    module = importlib.import_module(module_name)
    original_serialize = module.serialize

    def serialize_compact(rows, current_task_spec):
        rows.append(["/data", sample_data(current_task_spec.dataModelSchema["data"])])
        return "\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows)

    monkeypatch.setattr(module, "serialize", serialize_compact)
    compact_source = getattr(module, builder_name)(
        invocation,
        STYLE_TOKENS[style_id],
        task_spec,
    )
    expected = convert_compact_dsl_to_a2ui(
        compact_source,
        size="2x2",
        protocol_profile=profile,
    )
    monkeypatch.setattr(module, "serialize", original_serialize)

    terse_source = build_terse_nested2(component_id, invocation, task_spec, style_id)
    actual = convert_terse_dsl_nested2_to_a2ui(
        terse_source,
        size="2x2",
        protocol_profile=profile,
    )

    assert [json.loads(line) for line in actual.splitlines()] == [
        json.loads(line) for line in expected.splitlines()
    ]


def _template_task_spec():
    numeric = {"type": "number", "description": "数值", "sampleValue": 10}
    text = {"type": "string", "description": "文本", "sampleValue": "示例"}
    return TaskSpec(
        userQuery="生成状态卡片",
        size="2x2",
        eventCandidates=[EventAction(id="event.go", call="clickToApi", args={})],
        dataModelSchema={
            "data": {
                "metric": {
                    "caption": text,
                    "progress": numeric,
                    "major": numeric,
                    "minor": numeric,
                    "title": text,
                    "start": text,
                    "end": text,
                    "reminder": numeric,
                }
            }
        },
    )


@pytest.mark.asyncio
async def test_terse_endpoint_runs_advanced_pipeline_end_to_end(monkeypatch):
    saved_genui: list[str] = []
    saved_design_tokens: list[str | None] = []

    async def unavailable_json(_client, _prompt, *, phase):
        raise RuntimeError(f"offline: {phase}")

    async def must_not_generate_terse(*_args, **_kwargs):
        raise AssertionError("advanced success must not call the Terse generator")

    def save_artifact(store, artifact):
        saved_genui.append(artifact.genui)
        saved_design_tokens.append(store.design_token)
        return ArtifactSaveResult(
            artifactUrl="https://artifact.test/advanced",
            artifactDigest="sha256:advanced",
        )

    monkeypatch.setattr(A2UIModelClient, "generate_json", unavailable_json)
    monkeypatch.setattr(A2UIModelClient, "generate", must_not_generate_terse)
    monkeypatch.setattr(ArtifactStore, "save", save_artifact)
    request = GenerateWidgetCardRequest(
        uid="advanced-e2e",
        prdVer="11.7.5.205",
        device={"romVersion": "CLS-AL30 6.0.0.328"},
        userQuery="生成天气状态卡片",
        title="天气",
        description="天气状态",
        candidateDataBindings=[
            {
                "capabilityId": "ViewWeather",
                "arguments": {"districtName": "上海"},
                "writeResultTo": "/data/weather",
            }
        ],
        candidateEventCandidates=[
            {
                "capabilityId": "event.open.weather",
                "action": {
                    "id": "event.open.weather",
                    "call": "clickToDeeplink",
                    "args": {
                        "intentName": "Weather_CityCode",
                        "bundleName": "",
                        "abilityName": "",
                        "uri": "hww://www.huawei.com/totemweather?enterType=share&cityCode=",
                    },
                },
            }
        ],
    )

    response = await WidgetGenerationService().generate_widget_card_terse_dsl_nested2(request)

    assert response.status == GenerationStatus.SUCCESS
    assert response.artifactUrl == "https://artifact.test/advanced"
    assert len(saved_genui[0].splitlines()) == 3
    assert saved_design_tokens[0] is not None
    assert saved_design_tokens[0].startswith('Column("card"')


@pytest.mark.asyncio
async def test_invalid_advanced_template_falls_back_to_original_terse(monkeypatch):
    calls: list[str] = []

    async def invalid_advanced(_pipeline, _task_spec, _model_client):
        return AdvancedPipelineOutput(
            component_id="ring-split-metric-action",
            style_id="night-violet",
            source_dsl='["broken","Text",{}]',
            ui_brief=UIBrief(
                purpose="wellbeing-coaching",
                primaryInformation=["状态"],
                informationHierarchy=["状态", "操作"],
                visualTone="calm-night",
                contentPriorities=["状态优先"],
                reason="测试高级失败回退。",
            ),
            invocation={},
            planner_mode="offline",
            mapper_mode="offline",
        )

    async def generate_terse(_client, _prompt, _profile):
        calls.append("terse")
        return 'Column("card", Text("回退成功", "title"));'

    monkeypatch.setattr(AdvancedComponentPipeline, "generate", invalid_advanced)
    monkeypatch.setattr(A2UIModelClient, "generate", generate_terse)
    monkeypatch.setattr(ArtifactValidator, "validate", lambda *_args: [])
    monkeypatch.setattr(
        ArtifactStore,
        "save",
        lambda *_args: ArtifactSaveResult(
            artifactUrl="https://artifact.test/fallback",
            artifactDigest="sha256:fallback",
        ),
    )
    request = GenerateWidgetCardRequest(
        uid="fallback-e2e",
        prdVer="11.7.5.205",
        device={"romVersion": "CLS-AL30 6.0.0.328"},
        userQuery="生成静态摘要",
        title="摘要",
        description="回退测试",
    )

    response = await WidgetGenerationService().generate_widget_card_terse_dsl_nested2(request)

    assert response.status == GenerationStatus.SUCCESS
    assert response.artifactUrl == "https://artifact.test/fallback"
    assert calls == ["terse"]


@pytest.mark.asyncio
async def test_advanced_design_token_is_valid_for_terse_edit_route():
    task_spec = _metric_task_spec()
    output = await AdvancedComponentPipeline().generate(task_spec, OfflineModelClient())
    assert output is not None
    conversion_profile = {
        "version": "v0.9",
        "sizes": {"2x2": {"width": 160, "height": 160}},
    }
    genui = convert_terse_dsl_nested2_to_a2ui(
        output.source_dsl,
        size="2x2",
        protocol_profile=conversion_profile,
    )
    service = WidgetGenerationService()
    artifact = service._build_artifact(
        genui,
        {
            "title": "内存",
            "description": "内存状态",
            "suggestSize": "2x2",
            "dataBindings": [
                {
                    "capabilityId": "ViewMemory",
                    "arguments": {},
                    "writeResultTo": "/data/memory",
                }
            ],
        },
        task_spec.model_dump(mode="json"),
        [],
        task_spec.eventCandidates,
        [],
        [],
        "a2ui-form-rom6.0-v1",
        "v0.9",
        "app-11.7.5.205_rom-6.0",
    )
    source = SourceArtifactLoadResult(
        artifact=artifact,
        design_token=output.source_dsl,
        artifact_digest="sha256:source",
        url_hash="source-url",
        read_latency_ms=1.0,
        parse_latency_ms=1.0,
        download_mode="test",
    )
    policy = GenerationRoutePolicy(
        operation="generateWidgetCardTerseDslNested2",
        protocol_profile_id="a2ui-form-rom6.0-v1",
        backend="openai",
        processor_kind=DslProcessorKind.TERSE_NESTED2,
        source_format=TERSE_DSL_NESTED2_PROFILE_ID,
        model_profile_id=TERSE_DSL_NESTED2_PROFILE_ID,
        model_format=TERSE_DSL_NESTED2_PROFILE_ID,
        design_profile_id=TERSE_DSL_NESTED2_PROFILE_ID,
    )

    valid = await service._validate_source_design_token(
        output.source_dsl,
        source,
        policy,
        conversion_profile,
    )

    assert valid is True
