# -*- coding: utf-8 -*-
# ruff: noqa: E402
"""高级组件两轮模型、回退和模板编译测试。"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLOUD_ROOT = PROJECT_ROOT / "cloud"
if str(CLOUD_ROOT) not in sys.path:
    sys.path.insert(0, str(CLOUD_ROOT))

import services.advanced_component_pipeline.pipeline as advanced_pipeline_module
from api.schemas import GenerateWidgetCardRequest
from config.config import get_settings
from core.errors import GenerationStatus
from custom.a2ui_model_client import A2UIModelClient
from models.artifact import ArtifactMeta, WidgetArtifact
from models.generation import EventAction, TaskSpec
from models.service import ArtifactSaveResult
from services.advanced_component_pipeline import AdvancedComponentPipeline
from services.advanced_component_pipeline.argument_mapper import validate_invocation
from services.advanced_component_pipeline.compiler import (
    build_standard_a2ui,
    build_terse_nested2,
)
from services.advanced_component_pipeline.component_registry import component_plugins, get_component
from services.advanced_component_pipeline.component_selector import select_component
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
from services.advanced_component_pipeline.data_shape import extract_data_shape
from services.advanced_component_pipeline.models import (
    ActionRef,
    AdvancedPipelineOutput,
    BindingRef,
    SelectionConstraints,
    UIBrief,
)
from services.artifact_store import ArtifactStore
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
        assetCandidates=[
            {
                "id": "bell",
                "src": "resources/base/media/bell.svg",
                "description": "状态图标",
            },
            {
                "id": "moon",
                "src": "resources/base/media/moon.svg",
                "description": "睡眠图标",
            },
        ],
    )


def test_component_plugins_are_discovered_from_component_directories():
    plugins = component_plugins()

    assert {plugin.component_id for plugin in plugins} == {
        "compact-metrics-primary-action",
        "current-meeting",
        "digital-wellbeing",
        "family-care",
        "focus-mode",
        "low-power",
        "race-countdown",
        "ring-split-metric-action",
        "schedule-detail-action",
        "sleep-coach",
    }
    assert all(plugin.invocation_model for plugin in plugins)
    assert all(callable(plugin.build_rows) for plugin in plugins)
    assert all(callable(plugin.map_offline) for plugin in plugins)
    assert all(callable(plugin.validate) for plugin in plugins)


def _seven_scene_task_spec() -> TaskSpec:
    def field(data_type, sample, description):
        return {"type": data_type, "description": description, "sampleValue": sample}

    return TaskSpec(
        userQuery="生成场景卡片",
        size="2x2",
        eventCandidates=[EventAction(id="event.scene.action", call="clickToApi", args={})],
        dataModelSchema={
            "data": {
                "scene": {
                    "prefectureName": field("string", "深圳", "城市名称"),
                    "temperatureText": field("string", "38°", "当前温度"),
                    "condition": field("string", "晴", "天气状态"),
                    "temperatureRangeText": field("string", "26°/16°", "最高最低温度范围"),
                    "eventTitle": field("string", "UI需求评审会", "赛事或会议标题"),
                    "remainingDays": field("integer", 32, "赛事剩余天数"),
                    "sleepHours": field("integer", 5, "睡眠小时数"),
                    "sleepMinutes": field("integer", 45, "睡眠分钟数"),
                    "appName": field("string", "抖音使用时长", "应用名称"),
                    "durationText": field("string", "3小时45分钟", "应用使用时长"),
                    "status": field("string", "电量低于20%，开启省电模式", "电池状态"),
                    "batteryPercent": field("integer", 18, "电池电量百分比"),
                    "startTime": field("string", "14:00", "会议开始时间"),
                    "endTime": field("string", "15:30", "会议结束时间"),
                    "date": field("string", "27日", "日期"),
                    "weekday": field("string", "星期一", "星期"),
                    "location": field("string", "深圳市龙岗区", "会议地点"),
                }
            }
        },
        assetCandidates=[
            {
                "id": "asset.location",
                "src": "resources/base/media/location.svg",
                "description": "定位天气图标",
            },
            {
                "id": "asset.run",
                "src": "resources/base/media/run.svg",
                "description": "运动跑步图标",
            },
            {
                "id": "asset.alarm",
                "src": "resources/base/media/alarm.svg",
                "description": "闹钟图标",
            },
            {
                "id": "asset.tiktok",
                "src": "resources/base/media/tiktok.png",
                "description": "应用图标",
            },
            {
                "id": "asset.timing",
                "src": "resources/base/media/timing.svg",
                "description": "计时图标",
            },
            {
                "id": "asset.electricity",
                "src": "resources/base/media/battery.svg",
                "description": "电池图标",
            },
            {
                "id": "asset.save_power",
                "src": "resources/base/media/power.svg",
                "description": "省电图标",
            },
            {
                "id": "asset.meeting",
                "src": "resources/base/media/meeting.svg",
                "description": "会议图标",
            },
        ],
    )


@pytest.mark.parametrize(
    ("purpose", "component_id"),
    [
        ("family-care", "family-care"),
        ("race-countdown", "race-countdown"),
        ("sleep-coach", "sleep-coach"),
        ("digital-wellbeing", "digital-wellbeing"),
        ("low-power", "low-power"),
        ("focus-mode", "focus-mode"),
        ("current-meeting", "current-meeting"),
    ],
)
def test_seven_visual_scene_plugins_select_and_compile(purpose, component_id):
    task_spec = _seven_scene_task_spec()
    data_shape = extract_data_shape(task_spec)
    brief = UIBrief(
        purpose=purpose,
        primaryInformation=[purpose],
        informationHierarchy=["主信息", "操作"],
        visualTone=purpose,
        contentPriorities=[purpose],
        reason="测试场景选择",
    )
    selection = select_component(
        data_shape,
        brief,
        SelectionConstraints(size="2x2", action_count=1),
    )
    assert selection is not None
    assert selection.component_id == component_id

    plugin = get_component(component_id)
    invocation = plugin.map_offline(task_spec, data_shape)
    plugin.validate(invocation, task_spec)
    terse = build_terse_nested2(component_id, invocation, task_spec, "night-violet")
    a2ui = build_standard_a2ui(component_id, invocation, task_spec, "night-violet")
    converted_a2ui = convert_terse_dsl_nested2_to_a2ui(
        terse,
        size="2x2",
        protocol_profile={"version": "v0.9", "sizes": {"2x2": {"width": 160, "height": 160}}},
    )

    assert terse.startswith('Column("card"')
    assert len(a2ui.splitlines()) == 3
    assert len(converted_a2ui.splitlines()) == 3
    assert '"root":"root"' in a2ui
    artifact = WidgetArtifact(
        genui=a2ui,
        cardSpec={"title": purpose, "description": purpose, "suggestSize": "2x2"},
        taskSpec=task_spec.model_dump(mode="json"),
        effectiveCapabilities={"asset": task_spec.assetCandidates},
        meta=ArtifactMeta(
            protocolProfileId="a2ui-form-rom6.0-v1",
            capabilityRegistryVersion="app-11.7.5.205_rom-6.0",
            createdAt=1,
        ),
    )
    errors = ArtifactValidator().validate(
        artifact,
        {"id": "a2ui-form-rom6.0-v1"},
    )
    assert [error for error in errors if not error.startswith("EFFECTIVE_")] == []


def test_schedule_dnd_ui_brief_selects_focus_mode():
    task_spec = _seven_scene_task_spec()
    brief = UIBrief(
        purpose="以紧凑卡片形式展示未来日程概览，提示用户当前处于免打扰状态，并允许一键进入设置。",
        primaryInformation=["今日及近期日程数量", "近期日程的时间与标题", "免打扰开启状态"],
        informationHierarchy=["免打扰状态", "近期日程", "设置入口"],
        density="compact",
        temporality="upcoming",
        interaction="one-primary-action",
        attention="normal",
        visualTone="简洁、高效，强调日程时间性与免打扰的静默感",
        contentPriorities=["免打扰状态", "日程时间准确性", "日程标题", "进入设置"],
        reason="2x2 卡片突出未来日程和免打扰设置。",
    )

    selection = select_component(
        extract_data_shape(task_spec),
        brief,
        SelectionConstraints(size="2x2", action_count=1),
    )

    assert selection is not None
    assert selection.component_id == "focus-mode"
    assert selection.confidence >= 0.75


class OfflineModelClient:
    async def generate_json(self, _prompt, *, phase):
        raise RuntimeError(f"offline: {phase}")

    async def generate(self, *_args, **_kwargs):
        return (
            'Template("card@1", {}, '
            'Column("section", Text("清理内存", "body")));'
        )


class StructuredModelClient:
    def __init__(self):
        self.phases = []
        self.prompts = {}

    async def generate_json(self, prompt, *, phase):
        self.phases.append(phase)
        self.prompts[phase] = prompt
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
    task_spec = _metric_task_spec()
    task_spec.assetCandidates = [
        {
            "id": "asset.memory",
            "src": "resources/base/media/memory.svg",
            "description": "内存状态图标",
        }
    ]
    output = await AdvancedComponentPipeline().generate(task_spec, model_client)

    assert output is not None
    assert output.component_id == "compact-metrics-primary-action"
    assert output.planner_mode == "llm"
    assert output.mapper_mode == "llm"
    assert model_client.phases == ["advanced-ui-brief", "advanced-argument-map"]
    argument_payload = json.loads(model_client.prompts["advanced-argument-map"][1]["content"])
    assert argument_payload["assetCandidates"] == task_spec.assetCandidates
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
async def test_pipeline_can_disable_offline_fallback_for_strict_evaluation():
    with pytest.raises(RuntimeError, match="offline: advanced-ui-brief"):
        await AdvancedComponentPipeline().generate(
            _metric_task_spec(),
            OfflineModelClient(),
            allow_offline_fallback=False,
        )


@pytest.mark.asyncio
async def test_pipeline_output_format_switch_can_emit_standard_a2ui(monkeypatch):
    monkeypatch.setattr(
        advanced_pipeline_module,
        "get_settings",
        lambda: SimpleNamespace(advanced_component_output_format="a2ui"),
    )

    output = await AdvancedComponentPipeline().generate(
        _metric_task_spec(),
        OfflineModelClient(),
    )

    assert output is not None
    assert output.source_format == "a2ui"
    assert len(output.source_dsl.splitlines()) == 3
    assert '"createSurface"' in output.source_dsl


@pytest.mark.asyncio
async def test_pipeline_uses_hybrid_route_without_reliable_whole_card_candidate():
    output = await AdvancedComponentPipeline().generate(
        _metric_task_spec(with_action=False),
        OfflineModelClient(),
    )

    assert output is not None
    assert output.route == "hybrid-template"
    assert output.whole_card_confidence == 0.0
    assert output.fallback_used is False
    assert "Template" not in output.compiled_a2ui


def test_invocation_rejects_string_binding_for_progress():
    task_spec = _metric_task_spec()
    invocation = RingSplitMetricInvocation(
        caption=BindingRef(path="/data/memory/available"),
        caption_icon="bell",
        progress=BindingRef(path="/data/memory/missingText"),
        center_icon="moon",
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


def test_ring_split_invocation_allows_string_display_values():
    task_spec = _metric_task_spec()
    task_spec.dataModelSchema["data"]["memory"]["durationText"] = {
        "type": "string",
        "description": "格式化时长文本",
        "sampleValue": "7小时30分钟",
    }
    invocation = RingSplitMetricInvocation(
        caption=BindingRef(path="/data/memory/durationText"),
        caption_icon="bell",
        progress=BindingRef(path="/data/memory/usedPercent"),
        center_icon="moon",
        major_value=BindingRef(path="/data/memory/usedPercent"),
        major_unit="分",
        minor_value=BindingRef(path="/data/memory/durationText"),
        minor_unit="时长",
        action=ActionRef(event_id="event.clean.memory", label="查看详情"),
    )

    validate_invocation("ring-split-metric-action", invocation, task_spec)


def test_ring_split_invocation_schema_describes_binding_semantics():
    schema = RingSplitMetricInvocation.model_json_schema()
    properties = schema["properties"]

    assert "number 或 integer" in properties["progress"]["description"]
    assert "格式化的字符串" in properties["minor_value"]["description"]
    assert "已包含单位" in properties["minor_unit"]["description"]
    assert "assetCandidates" in properties["caption_icon"]["description"]
    assert "assetCandidates" in properties["center_icon"]["description"]
    assert properties["progress_total"]["exclusiveMinimum"] == 0


def test_ring_split_invocation_rejects_unknown_icon_asset():
    task_spec = _metric_task_spec()
    invocation = RingSplitMetricInvocation(
        caption=BindingRef(path="/data/memory/available"),
        caption_icon="asset.unknown",
        progress=BindingRef(path="/data/memory/usedPercent"),
        center_icon="moon",
        major_value=BindingRef(path="/data/memory/usedPercent"),
        major_unit="%",
        minor_value=BindingRef(path="/data/memory/available"),
        minor_unit="GB",
        action=ActionRef(event_id="event.clean.memory", label="查看详情"),
    )

    with pytest.raises(ValueError, match="asset is not in TaskSpec"):
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
                caption_icon="bell",
                progress=BindingRef(path="/data/metric/progress"),
                center_icon="moon",
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
        assetCandidates=[
            {
                "id": "bell",
                "src": "resources/base/media/bell.svg",
                "description": "状态图标",
            },
            {
                "id": "moon",
                "src": "resources/base/media/moon.svg",
                "description": "睡眠图标",
            },
        ],
    )
    source_dsl = build_terse_nested2(component_id, invocation, task_spec, style_id)

    genui = convert_terse_dsl_nested2_to_a2ui(
        source_dsl,
        size="2x2",
        protocol_profile={"version": "v0.9", "sizes": {"2x2": {"width": 160, "height": 160}}},
    )

    assert len(genui.splitlines()) == 3
    assert '"linearGradient"' in genui
    if component_id == "ring-split-metric-action":
        assert 'Image("resources/base/media/bell.svg"' in source_dsl
        assert 'Image("resources/base/media/moon.svg"' in source_dsl


@pytest.mark.parametrize(
    ("component_id", "invocation", "style_id"),
    [
        (
            "compact-metrics-primary-action",
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
            RingSplitMetricInvocation(
                caption=BindingRef(path="/data/metric/caption"),
                caption_icon="bell",
                progress=BindingRef(path="/data/metric/progress"),
                center_icon="moon",
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
def test_direct_a2ui_templates_use_original_aesthetic_component_tree(
    component_id,
    invocation,
    style_id,
):
    task_spec = _template_task_spec()
    output = build_standard_a2ui(
        component_id,
        invocation,
        task_spec,
        style_id,
    )
    messages = [json.loads(line) for line in output.splitlines()]
    update = messages[1]["updateComponents"]
    ids = {component["id"] for component in update["components"]}
    expected_original_ids = {
        "compact-metrics-primary-action": {"compact-rings-row", "hero-card"},
        "ring-split-metric-action": {"hero-ring", "hero-icon", "split-values"},
        "schedule-detail-action": {"time-range", "flex-spacer"},
    }
    assert update["root"] == "root"
    assert expected_original_ids[component_id] <= ids
    if component_id == "ring-split-metric-action":
        components = {component["id"]: component for component in update["components"]}
        assert components["caption-icon"]["component"] == "Image"
        assert components["caption-icon"]["src"] == "resources/base/media/bell.svg"
        assert components["hero-icon"]["component"] == "Image"
        assert components["hero-icon"]["src"] == "resources/base/media/moon.svg"
        assert components["hero-ring"]["styles"]["alignContent"] == "center"
        assert components["hero-ring"]["children"] == ["hero-progress", "hero-icon"]


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
        assetCandidates=[
            {
                "id": "bell",
                "src": "resources/base/media/bell.svg",
                "description": "状态图标",
            },
            {
                "id": "moon",
                "src": "resources/base/media/moon.svg",
                "description": "睡眠图标",
            },
        ],
    )


@pytest.mark.parametrize("output_format", ["terse", "a2ui"])
@pytest.mark.asyncio
async def test_terse_endpoint_runs_advanced_pipeline_end_to_end(
    monkeypatch,
    output_format,
):
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
    monkeypatch.setattr(
        get_settings(),
        "advanced_component_output_format",
        output_format,
    )
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
    if output_format == "terse":
        assert saved_design_tokens[0].startswith('Column("card"')
    else:
        assert saved_design_tokens[0].startswith('{"version"')
        assert saved_design_tokens[0] == saved_genui[0]


@pytest.mark.asyncio
async def test_invalid_advanced_template_falls_back_to_original_terse(monkeypatch):
    calls: list[str] = []

    async def invalid_advanced(_pipeline, _task_spec, _model_client, *_args, **_kwargs):
        return AdvancedPipelineOutput(
            component_id="ring-split-metric-action",
            style_id="night-violet",
            source_dsl='["broken","Text",{}]',
            source_format="terse",
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
