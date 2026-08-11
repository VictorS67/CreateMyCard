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
from services.advanced_component_pipeline.compiler import (
    build_standard_a2ui,
    build_terse_nested2,
)
from services.advanced_component_pipeline.component_registry import component_plugins, get_component
from services.advanced_component_pipeline.component_selector import select_component
from services.advanced_component_pipeline.components.status_ring_action.plugin import (
    Invocation as LowPowerInvocation,
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
    events = [EventAction(id="event.enable.power", call="clickToApi", args={})]
    return TaskSpec(
        userQuery="设备电量低于20%，开启省电模式",
        size="2x2",
        eventCandidates=events if with_action else [],
        dataModelSchema={
            "data": {
                "battery": {
                    "status": {
                        "type": "string",
                        "description": "低电量状态和省电建议",
                        "sampleValue": "电量低于20%，开启省电模式",
                    },
                    "batteryPercent": {
                        "type": "integer",
                        "description": "电池电量百分比",
                        "sampleValue": 18,
                    },
                }
            }
        },
        assetCandidates=[
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
        ],
    )


def test_component_plugins_are_discovered_from_component_directories():
    plugins = component_plugins()

    assert {plugin.component_id for plugin in plugins} == {
        "dual-duration-action",
        "dual-ring-primary-action",
        "hero-countdown",
        "status-ring-action",
        "timeline-event-action",
        "upcoming-event-action",
        "usage-summary-action",
        "hero-metric-action",
        "hero-metric-icon-action",
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
                    "memoryUsedPercent": field("integer", 87, "内存占用百分比"),
                    "storageUsedPercent": field("integer", 72, "设备存储占用百分比"),
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
            {
                "id": "asset.memory",
                "src": "resources/base/media/memory.svg",
                "description": "内存图标",
            },
            {
                "id": "asset.storage",
                "src": "resources/base/media/storage.svg",
                "description": "设备存储图标",
            },
            {
                "id": "asset.rain",
                "src": "resources/base/media/rain.svg",
                "description": "降雨天气图标",
            },
            {
                "id": "asset.taxi",
                "src": "resources/base/media/taxi.svg",
                "description": "打车图标",
            },
        ],
    )


@pytest.mark.parametrize(
    (
        "purpose",
        "component_id",
        "layout",
        "domain",
        "scenario",
        "content",
        "action",
        "status",
        "temporality",
    ),
    [
        (
            "亲人关怀",
            "hero-metric-action",
            "hero-metric-action",
            "weather",
            "family-care",
            ["location", "temperature"],
            ["call-contact"],
            [],
            "now",
        ),
        (
            "赛事陪伴",
            "hero-countdown",
            "hero-countdown",
            "sports",
            "race-countdown",
            ["countdown"],
            ["open-event"],
            [],
            "upcoming",
        ),
        (
            "睡眠监测",
            "dual-duration-action",
            "dual-duration-action",
            "health",
            "sleep-summary",
            ["duration"],
            ["remind-sleep"],
            ["sleep-quality"],
            "historical",
        ),
        (
            "防沉迷",
            "usage-summary-action",
            "usage-summary-action",
            "digital-wellbeing",
            "usage-control",
            ["app-usage", "duration"],
            ["manage-usage"],
            [],
            "now",
        ),
        (
            "设备电量",
            "status-ring-action",
            "status-ring-action",
            "device",
            "low-power",
            ["battery-level", "percentage"],
            ["enable-power-saving"],
            ["low-power"],
            "now",
        ),
        (
            "专注模式",
            "upcoming-event-action",
            "upcoming-event-action",
            "schedule",
            "upcoming-event",
            ["event-title", "time-range"],
            ["enable-focus"],
            ["do-not-disturb"],
            "upcoming",
        ),
        (
            "当前会议",
            "timeline-event-action",
            "timeline-event-action",
            "schedule",
            "ongoing-event",
            ["event-title", "time-range"],
            ["join-meeting"],
            ["active"],
            "now",
        ),
        (
            "内存不足",
            "dual-ring-primary-action",
            "dual-ring-primary-action",
            "device",
            "memory-cleanup",
            ["memory-usage", "storage-usage", "percentage"],
            ["clean-memory"],
            ["warning"],
            "now",
        ),
        (
            "雨天打车回家",
            "hero-metric-icon-action",
            "hero-metric-icon-action",
            "weather",
            "bad-weather-commute",
            ["location", "temperature", "status"],
            ["hail-taxi"],
            ["warning"],
            "now",
        ),
    ],
)
def test_visual_scene_plugins_select_and_compile(
    purpose, component_id, layout, domain, scenario, content, action, status, temporality
):
    task_spec = _seven_scene_task_spec()
    data_shape = extract_data_shape(task_spec)
    brief = UIBrief(
        purpose=purpose,
        domain=domain,
        scenario=scenario,
        layoutArchetype=layout,
        contentSemantics=content,
        actionSemantics=action,
        statusSemantics=status,
        temporality=temporality,
        primaryInformation=[purpose],
        informationHierarchy=["主信息", "操作"],
        visualTone=purpose,
        contentPriorities=[purpose],
        reason="测试场景选择",
    )
    selection = select_component(
        data_shape,
        brief,
        SelectionConstraints(
            size="2x2",
            action_count=1,
            asset_count=len(task_spec.assetCandidates),
        ),
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


def test_schedule_dnd_ui_brief_selects_upcoming_event_layout():
    task_spec = _seven_scene_task_spec()
    brief = UIBrief(
        purpose="以紧凑卡片形式展示未来日程概览，提示用户当前处于免打扰状态，并允许一键进入设置。",
        domain="schedule",
        scenario="upcoming-event",
        layoutArchetype="upcoming-event-action",
        statusSemantics=["do-not-disturb"],
        contentSemantics=["event-title", "time-range", "event-count"],
        actionSemantics=["open-dnd-settings"],
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
        SelectionConstraints(
            size="2x2",
            action_count=1,
            asset_count=len(task_spec.assetCandidates),
        ),
    )

    assert selection is not None
    assert selection.component_id == "upcoming-event-action"
    assert selection.confidence >= 0.75


def test_structural_weather_hero_selection_does_not_require_business_name_or_asset():
    task_spec = TaskSpec(
        userQuery="生成一张状态概览卡片",
        size="2x2",
        eventCandidates=[EventAction(id="event.open", call="clickToApi", args={})],
        dataModelSchema={
            "data": {
                "summary": {
                    "city": {"type": "string", "description": "地点", "sampleValue": "深圳"},
                    "temperatureText": {
                        "type": "string",
                        "description": "当前温度",
                        "sampleValue": "28°",
                    },
                    "condition": {
                        "type": "string",
                        "description": "当前状态",
                        "sampleValue": "晴",
                    },
                    "rangeText": {
                        "type": "string",
                        "description": "范围摘要",
                        "sampleValue": "26°/18°",
                    },
                }
            }
        },
        assetCandidates=[],
    )
    brief = UIBrief(
        purpose="突出一个主指标，并在底部展示摘要和快捷操作",
        domain="general",
        scenario="general",
        layoutArchetype="hero-metric-action",
        primaryInformation=["地点", "主指标", "状态"],
        informationHierarchy=["地点", "主指标", "摘要和操作"],
        visualTone="清晰简洁",
        contentPriorities=["主指标"],
        reason="使用单个大指标布局。",
    )

    selection = select_component(
        extract_data_shape(task_spec),
        brief,
        SelectionConstraints(size="2x2", action_count=1, asset_count=0),
    )

    assert selection is not None
    assert selection.component_id == "hero-metric-action"
    plugin = get_component(selection.component_id)
    invocation = plugin.map_offline(task_spec, extract_data_shape(task_spec))
    assert invocation.location_icon is None
    plugin.validate(invocation, task_spec)
    assert "Image(" not in build_terse_nested2(
        selection.component_id, invocation, task_spec, "system-teal"
    )


def test_countdown_template_supports_one_field_without_action():
    task_spec = TaskSpec(
        userQuery="做个运动会倒数日卡片",
        size="2x2",
        eventCandidates=[],
        dataModelSchema={
            "data": {
                "countdown": {
                    "countdownDays": {
                        "type": "integer",
                        "description": "距离目标日期的剩余天数",
                        "sampleValue": 35,
                    }
                }
            }
        },
        assetCandidates=[],
    )
    data_shape = extract_data_shape(task_spec)
    brief = UIBrief(
        purpose="运动会倒数日",
        domain="sports",
        scenario="race-countdown",
        layoutArchetype="hero-countdown",
        contentSemantics=["countdown"],
        actionSemantics=[],
        primaryInformation=["剩余天数"],
        informationHierarchy=["标题", "倒计时"],
        temporality="upcoming",
        interaction="none",
        visualTone="活力运动感",
        contentPriorities=["倒计时"],
        reason="突出剩余天数。",
    )

    selection = select_component(
        data_shape,
        brief,
        SelectionConstraints(size="2x2", action_count=0, asset_count=0),
    )

    assert selection is not None
    assert selection.component_id == "hero-countdown"
    plugin = get_component(selection.component_id)
    invocation = plugin.map_offline(task_spec, data_shape)
    assert invocation.action is None
    terse = build_terse_nested2(
        selection.component_id,
        invocation,
        task_spec,
        "race-orange",
    )
    assert "Button(" not in terse
    assert '"path":"/data/countdown/countdownDays"' in terse


class OfflineModelClient:
    async def generate_json(self, _prompt, *, phase):
        raise RuntimeError(f"offline: {phase}")

    async def generate(self, *_args, **_kwargs):
        return (
            'Template("card@1", {}, Column("section", '
            'Text("设备电量低于20", "body"), '
            'Text("电量低于20%，开启省电模式", "body")));'
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
                "purpose": "低电量状态和省电操作",
                "domain": "device",
                "scenario": "low-power",
                "layoutArchetype": "status-ring-action",
                "statusSemantics": ["low-power", "warning"],
                "contentSemantics": ["battery-level", "percentage", "status"],
                "actionSemantics": ["enable-power-saving"],
                "primaryInformation": ["设备电量"],
                "informationHierarchy": ["指标", "操作"],
                "density": "compact",
                "temporality": "now",
                "interaction": "one-primary-action",
                "attention": "warning-capable",
                "visualTone": "technical-efficient",
                "contentPriorities": ["低电量状态优先"],
                "reason": "突出电量状态和省电入口。",
            }
        return {
            "status_text": {"path": "/data/battery/status"},
            "percentage": {"path": "/data/battery/batteryPercent"},
            "battery_icon": "asset.electricity",
            "action_icon": "asset.save_power",
            "action": {"event_id": "event.enable.power", "label": "开启省电"},
        }


@pytest.mark.asyncio
async def test_pipeline_uses_two_structured_model_calls_and_builds_template():
    model_client = StructuredModelClient()
    task_spec = _metric_task_spec()
    output = await AdvancedComponentPipeline().generate(task_spec, model_client)

    assert output is not None
    assert output.component_id == "status-ring-action"
    assert output.planner_mode == "llm"
    assert output.mapper_mode == "llm"
    assert model_client.phases == ["advanced-ui-brief", "advanced-argument-map"]
    planner_payload = json.loads(model_client.prompts["advanced-ui-brief"][1]["content"])
    assert planner_payload["eventCandidates"] == [
        event.model_dump(exclude_none=True) for event in task_spec.eventCandidates
    ]
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
            "status-ring-action",
            LowPowerInvocation(
                status_text=BindingRef(path="/data/metric/caption"),
                percentage=BindingRef(path="/data/metric/progress"),
                battery_icon="bell",
                action_icon="moon",
                action=ActionRef(event_id="event.go", label="开启省电"),
            ),
            "system-teal",
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
    assert 'Image("resources/base/media/bell.svg"' in source_dsl
    assert 'Image("resources/base/media/moon.svg"' in source_dsl


@pytest.mark.parametrize(
    ("component_id", "invocation", "style_id"),
    [
        (
            "status-ring-action",
            LowPowerInvocation(
                status_text=BindingRef(path="/data/metric/caption"),
                percentage=BindingRef(path="/data/metric/progress"),
                battery_icon="bell",
                action_icon="moon",
                action=ActionRef(event_id="event.go", label="开启省电"),
            ),
            "system-teal",
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
        "status-ring-action": {"battery-stack", "battery-progress", "action-wrap"},
    }
    assert update["root"] == "root"
    assert expected_original_ids[component_id] <= ids
    components = {component["id"]: component for component in update["components"]}
    assert components["battery-icon"]["component"] == "Image"
    assert components["battery-icon"]["src"] == "resources/base/media/bell.svg"
    assert components["action-icon"]["component"] == "Image"
    assert components["action-icon"]["src"] == "resources/base/media/moon.svg"


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
    saved_task_specs: list[dict] = []

    async def unavailable_json(_client, _prompt, *, phase):
        raise RuntimeError(f"offline: {phase}")

    async def must_not_generate_terse(*_args, **_kwargs):
        raise AssertionError("advanced success must not call the Terse generator")

    def save_artifact(store, artifact):
        saved_genui.append(artifact.genui)
        saved_design_tokens.append(store.design_token)
        saved_task_specs.append(artifact.taskSpec)
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
        userQuery="设备电量低于20%，开启省电模式",
        title="设备电量",
        description="低电量状态和省电操作",
        candidateDataBindings=[
            {
                "capabilityId": "GetPhoneBatteryInfo",
                "arguments": {},
                "writeResultTo": "/data/phoneBattery",
            }
        ],
        candidateEventCandidates=[
            {
                "capabilityId": "event.setPowerSavingMode",
                "action": {
                    "id": "event.setPowerSavingMode",
                    "call": "clickToIntent",
                    "args": {
                        "intentName": "SetSettingSwitch",
                        "params": {
                            "appBundleName": "com.huawei.hmos.settings",
                            "itemName": "battery_saving_mode",
                            "switchFlag": 0,
                        },
                    },
                },
            }
        ],
        candidateAssetIds=["asset.icon_electricity", "asset.icon_save_power"],
    )

    response = await WidgetGenerationService().generate_widget_card_terse_dsl_nested2(request)

    assert response.status == GenerationStatus.SUCCESS
    assert response.artifactUrl == "https://artifact.test/advanced"
    assert len(saved_genui[0].splitlines()) == 3
    assert saved_design_tokens[0] is not None
    assert saved_task_specs[0]["selectedTemplateId"] == "status-ring-action"
    if output_format == "terse":
        assert saved_design_tokens[0].startswith('Column("card"')
    else:
        assert saved_design_tokens[0].startswith('{"version"')
        assert saved_design_tokens[0] == saved_genui[0]


@pytest.mark.asyncio
async def test_invalid_advanced_template_falls_back_to_original_terse(monkeypatch):
    calls: list[str] = []
    saved_task_specs: list[dict] = []

    async def invalid_advanced(_pipeline, _task_spec, _model_client, *_args, **_kwargs):
        return AdvancedPipelineOutput(
            component_id="status-ring-action",
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

    def save_fallback_artifact(_store, artifact):
        saved_task_specs.append(artifact.taskSpec)
        return ArtifactSaveResult(
            artifactUrl="https://artifact.test/fallback",
            artifactDigest="sha256:fallback",
        )

    monkeypatch.setattr(AdvancedComponentPipeline, "generate", invalid_advanced)
    monkeypatch.setattr(A2UIModelClient, "generate", generate_terse)
    monkeypatch.setattr(ArtifactValidator, "validate", lambda *_args: [])
    monkeypatch.setattr(
        ArtifactStore,
        "save",
        save_fallback_artifact,
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
    assert "selectedTemplateId" not in saved_task_specs[0]


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
