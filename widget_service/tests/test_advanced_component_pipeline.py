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
from models.generation import EventAction, TaskSpec
from models.service import ArtifactSaveResult
from services.advanced_component_pipeline import AdvancedComponentPipeline
from services.advanced_component_pipeline.argument_mapper import validate_invocation
from services.advanced_component_pipeline.compiler import (
    build_standard_a2ui,
    build_terse_nested2,
)
from services.advanced_component_pipeline.component_registry import component_plugins
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
from services.advanced_component_pipeline.content_selectors import (
    apply_content_selectors,
    project_content_component_facts,
)
from services.advanced_component_pipeline.data_shape import extract_data_shape
from services.advanced_component_pipeline.models import (
    ActionRef,
    AdvancedPipelineOutput,
    AdvancedScopeBrief,
    BindingRef,
)
from services.advanced_component_pipeline.scope_planner import (
    build_advanced_scope_prompt,
    plan_advanced_scope_with_llm,
    resolve_scope_layout_ids,
    scope_template_ids,
)
from services.advanced_component_pipeline.ux_mixed_framer import frame_ux_layout_children
from services.advanced_component_pipeline.ux_mixed_prompt import build_ux_mixed_prompt
from services.artifact_store import ArtifactStore
from services.cardplan_template.compiler import compile_ux_layout_card
from services.cardplan_template.parser import parse_hybrid_card
from services.cardplan_template.registry import get_cardplan_registry
from services.generation_pipeline import (
    DslProcessorKind,
    GenerationRoutePolicy,
)
from services.protocol_registry import TERSE_DSL_NESTED2_PROFILE_ID, A2UIProtocolRegistry
from services.source_artifact_repository import SourceArtifactLoadResult
from services.terse_dsl_nested2_converter import (
    TerseDslNested2ConversionError,
    convert_terse_dsl_nested2_to_a2ui,
)
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
        "ring-split-metric-action",
        "schedule-detail-action",
    }
    assert all(plugin.invocation_model for plugin in plugins)
    assert all(callable(plugin.build_rows) for plugin in plugins)
    assert all(callable(plugin.map_offline) for plugin in plugins)
    assert all(callable(plugin.validate) for plugin in plugins)


class OfflineModelClient:
    async def generate_json(self, _prompt, *, phase):
        raise RuntimeError(f"offline: {phase}")

    async def generate(self, *_args, **_kwargs):
        return (
            'Template("card@1", {}, '
            'Column("section", Progress({"value":78,"total":100}), '
            'Progress({"value":4.5,"total":100})));'
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


@pytest.mark.asyncio
async def test_server_switch_disables_whole_card_template(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_advanced_whole_card_template", False)

    output = await AdvancedComponentPipeline().generate(
        _metric_task_spec(),
        OfflineModelClient(),
    )

    assert output is not None
    assert output.route == "hybrid-template"
    assert output.confidence_bypassed is True
    assert output.fallback_used is False


_WEATHER_TEMPLATE_BODY = (
    'SingleFocusLayout(Template("ux-weather-overview@2", "medium", '
    '{"city":"深圳","conditionIcon":"resources/base/media/weather.svg",'
    '"temperature":"38°","condition":"晴","airQuality":"空气优",'
    '"temperatureRange":"26° / 16°"}));'
)


class UxMixedModelClient:
    def __init__(self, body: str | None = None):
        self.phases: list[str] = []
        self.prompts: dict[str, list[dict[str, str]]] = {}
        self.body = body or _WEATHER_TEMPLATE_BODY

    async def generate_json(self, prompt, *, phase):
        self.phases.append(phase)
        self.prompts[phase] = prompt
        return {
            "scopeVersion": "advanced-scope-brief/1",
            "themeId": "family-weather-care-blue",
            "advancedComponentIds": ["WeatherOverview"],
        }

    async def generate(self, messages, _profile, **kwargs):
        phase = kwargs.get("phase", "")
        self.phases.append(phase)
        self.prompts[phase] = messages
        return self.body


class RetryingUxMixedModelClient(UxMixedModelClient):
    def __init__(self, bodies: list[str]):
        super().__init__(bodies[-1])
        self.bodies = list(bodies)

    async def generate(self, messages, _profile, **kwargs):
        phase = kwargs.get("phase", "")
        self.phases.append(phase)
        self.prompts[phase] = messages
        return self.bodies.pop(0)


def _weather_scope_task() -> TaskSpec:
    return TaskSpec(
        userQuery="天气",
        size="2x2",
        eventCandidates=[],
        assetCandidates=[
            {
                "src": "resources/base/media/weather.svg",
                "description": "天气状态图标",
                "sceneTags": ["condition", "weather"],
            }
        ],
        dataModelSchema={
            "ViewWeather": {
                "districtName": _sample_field("深圳"),
                "temperatureText": _sample_field("38°"),
                "condition": _sample_field("晴"),
                "airQuality": _sample_field("空气优"),
                "temperatureRangeText": _sample_field("26° / 16°"),
            }
        },
    )


def test_new_scope_prompt_only_exposes_theme_and_advanced_component_output():
    task_spec = _weather_scope_task()
    prompt = build_advanced_scope_prompt(
        task_spec,
        extract_data_shape(task_spec),
        get_cardplan_registry(),
    )
    user_payload = json.loads(prompt[1]["content"])
    output_schema = json.loads(prompt[0]["content"].split("\n", 1)[1])

    assert set(output_schema["properties"]) == {
        "scopeVersion",
        "themeId",
        "advancedComponentIds",
    }
    assert "cardPlanCandidates" not in user_payload
    assert "localTemplates" not in user_payload
    assert "layoutComponents" not in user_payload
    assert len(user_payload["advancedComponents"]) <= 8


def test_scope_prompt_does_not_pad_positive_matches_with_zero_score_components():
    prompt = build_advanced_scope_prompt(
        _weather_scope_task(),
        extract_data_shape(_weather_scope_task()),
        get_cardplan_registry(),
    )
    candidates = {item["id"] for item in json.loads(prompt[1]["content"])["advancedComponents"]}

    assert "WeatherOverview" in candidates
    assert "MemoPreview" not in candidates


def test_scope_candidates_do_not_promote_action_words_over_calendar_data():
    task_spec = TaskSpec(
        userQuery="展示下一日程并进入专注模式",
        size="2x2",
        eventCandidates=[EventAction(id="event.open.settings.dnd", call="clickToApi", args={})],
        dataModelSchema={"GetCalendarEvents": {"title": "UI需求评审会", "time": "14:00 - 15:30"}},
        assetCandidates=[],
    )
    prompt = build_advanced_scope_prompt(
        task_spec,
        extract_data_shape(task_spec),
        get_cardplan_registry(),
    )
    candidates = {item["id"] for item in json.loads(prompt[1]["content"])["advancedComponents"]}

    assert "ScheduleOverview" in candidates
    assert "SystemModeOverview" not in candidates


def test_scope_candidates_match_memory_as_device_resource_not_memo_substring():
    task_spec = TaskSpec(
        userQuery="展示存储和内存占用并支持一键清理",
        size="2x2",
        eventCandidates=[EventAction(id="event.clean.memory", call="clickToApi", args={})],
        dataModelSchema={
            "GetSystemMemInfo": {
                "storageValue": 87,
                "memoryValue": 72,
                "description": "内存不足",
            }
        },
        assetCandidates=[],
    )
    prompt = build_advanced_scope_prompt(
        task_spec,
        extract_data_shape(task_spec),
        get_cardplan_registry(),
    )
    candidates = {item["id"] for item in json.loads(prompt[1]["content"])["advancedComponents"]}

    assert "ResourceUsageOverview" in candidates
    assert "BatteryOverview" not in candidates
    assert "MemoPreview" not in candidates


def test_scope_provider_gate_hides_components_without_effective_data_capability():
    task_spec = TaskSpec(
        userQuery="展示待办并打开设置",
        size="2x2",
        eventCandidates=[EventAction(id="event.open.settings", call="clickToApi", args={})],
        dataModelSchema={"data": {"label": "待办"}},
        assetCandidates=[],
    )

    with pytest.raises(ValueError, match="no provider-backed"):
        build_advanced_scope_prompt(
            task_spec,
            extract_data_shape(task_spec),
            get_cardplan_registry(),
            available_capability_ids=(),
        )


@pytest.mark.parametrize(
    ("capability_id", "query", "component_id", "variants"),
    [
        (
            "GetAppUsageDuration",
            "展示应用使用时长",
            "AppUsageOverview",
            ["singleApp"],
        ),
        (
            "GetHealthAndSportSummary",
            "展示运动平均心率",
            "HeartRateOverview",
            ["average"],
        ),
        (
            "GetSystemMemInfo",
            "展示内存占用",
            "ResourceUsageOverview",
            ["memory"],
        ),
    ],
)
def test_scope_exposes_only_provider_backed_component_variants(
    capability_id: str,
    query: str,
    component_id: str,
    variants: list[str],
) -> None:
    task_spec = TaskSpec(
        userQuery=query,
        size="2x2",
        dataModelSchema={capability_id: {}},
        assetCandidates=[],
    )

    prompt = build_advanced_scope_prompt(
        task_spec,
        extract_data_shape(task_spec),
        get_cardplan_registry(),
        available_capability_ids=(capability_id,),
    )
    candidates = json.loads(prompt[1]["content"])["advancedComponents"]
    selected = next(item for item in candidates if item["id"] == component_id)

    assert selected["variants"] == variants


def test_explicit_card_spec_provider_ids_override_query_and_schema_terms():
    task_spec = TaskSpec(
        userQuery="展示待办和设置，但只有应用时长能力可用",
        size="2x2",
        dataModelSchema={"data": {"task": "待办", "setting": "设置"}},
        assetCandidates=[],
    )

    prompt = build_advanced_scope_prompt(
        task_spec,
        extract_data_shape(task_spec),
        get_cardplan_registry(),
        available_capability_ids=("GetAppUsageDuration",),
    )
    ids = {item["id"] for item in json.loads(prompt[1]["content"])["advancedComponents"]}

    assert ids == {"AppUsageOverview"}


def test_2x2_scope_prompt_does_not_advertise_atomic_context_as_second_component():
    task_spec = _weather_scope_task()
    prompt = build_advanced_scope_prompt(
        task_spec,
        extract_data_shape(task_spec),
        get_cardplan_registry(),
        available_capability_ids=("ViewWeather",),
    )
    candidates = json.loads(prompt[1]["content"])["advancedComponents"]
    weather = next(item for item in candidates if item["id"] == "WeatherOverview")

    assert "LocationOverview" not in weather["compatibleWith"]


@pytest.mark.asyncio
async def test_2x2_scope_normalizes_weather_location_to_atomic_weather_owner():
    task_spec = _weather_scope_task()

    async def generate_json(_messages, _phase):
        return {
            "scopeVersion": "advanced-scope-brief/1",
            "themeId": "family-weather-care-blue",
            "advancedComponentIds": ["WeatherOverview", "LocationOverview"],
        }

    scope = await plan_advanced_scope_with_llm(
        task_spec,
        extract_data_shape(task_spec),
        generate_json,
        get_cardplan_registry(),
        available_capability_ids=("ViewWeather",),
    )

    assert scope.advanced_component_ids == ("WeatherOverview",)


def test_calendar_selector_derives_only_trusted_date_weekday_and_time_aliases():
    task_spec = TaskSpec(
        userQuery="下一场会议",
        size="2x2",
        dataModelSchema={
            "data": {
                "calendar": {
                    "events": [
                        {
                            "title": _sample_field("产品评审"),
                            "dtStart": _sample_field("09:30"),
                            "dtEnd": _sample_field("10:30"),
                            "eventLocation": _sample_field("A区会议室"),
                            "startDate": _sample_field("07-15"),
                        }
                    ],
                    "updatedAt": _sample_field("2026-07-15 09:00"),
                }
            }
        },
        assetCandidates=[],
    )

    selected = apply_content_selectors(task_spec, {"GetCalendarEvents"})
    derived = selected.dataModelSchema["data"]["_advancedSelectors"]

    assert derived["schedule"]["title"]["sampleValue"] == "产品评审"
    assert derived["schedule"]["timeText"]["sampleValue"] == "09:30 - 10:30"
    assert derived["schedule"]["location"]["sampleValue"] == "A区会议室"
    assert derived["date"]["date"]["sampleValue"] == "15日"
    assert derived["date"]["weekday"]["sampleValue"] == "星期三"
    assert "_advancedSelectors" not in task_spec.dataModelSchema["data"]


def test_weather_selector_requires_complete_current_weather_facts():
    complete = TaskSpec(
        userQuery="天气",
        size="2x2",
        dataModelSchema={
            "data": {
                "location": {
                    "districtName": _sample_field("龙岗区"),
                    "prefectureName": _sample_field("深圳市"),
                },
                "current": {
                    "temperatureText": _sample_field("38℃"),
                    "condition": _sample_field("晴"),
                    "airQuality": _sample_field("优"),
                },
                "daily": [{"temperatureRangeText": _sample_field("26℃ / 16℃")}],
                "updatedAt": _sample_field("2026-07-15 09:30"),
            }
        },
        assetCandidates=[],
    )

    selected = apply_content_selectors(complete, {"ViewWeather"})
    derived = selected.dataModelSchema["data"]["_advancedSelectors"]

    assert derived["weather"]["city"]["sampleValue"] == "龙岗区"
    assert derived["weather"]["temperature"]["sampleValue"] == "38℃"
    assert derived["weather"]["temperatureRange"]["sampleValue"] == "26℃ / 16℃"
    assert derived["location"]["label"]["sampleValue"] == "天气位置"

    incomplete = complete.model_copy(
        update={"dataModelSchema": {"data": {"current": {"condition": _sample_field("晴")}}}}
    )
    unchanged = apply_content_selectors(incomplete, {"ViewWeather"})
    assert unchanged is incomplete


def test_selectors_make_v2_weather_and_calendar_templates_satisfiable():
    weather = TaskSpec(
        userQuery="天气",
        size="2x2",
        dataModelSchema={
            "ViewWeather": {
                "districtName": _sample_field("龙岗区"),
                "temperatureText": _sample_field("38℃"),
                "condition": _sample_field("晴"),
                "airQuality": _sample_field("优"),
                "temperatureRangeText": _sample_field("26℃ / 16℃"),
                "updatedAt": _sample_field("2026-07-15 09:30"),
            }
        },
        assetCandidates=[
            {
                "src": "resources/base/media/weather.svg",
                "description": "天气状态图标",
                "sceneTags": ["condition", "weather"],
            }
        ],
    )
    selected = apply_content_selectors(weather, {"ViewWeather"})
    templates = scope_template_ids(
        AdvancedScopeBrief(
            themeId="family-weather-care-blue",
            advancedComponentIds=("WeatherOverview",),
        ),
        get_cardplan_registry(),
        selected,
    )

    assert "ux-weather-overview@2" in templates

    calendar = TaskSpec(
        userQuery="下一场会议",
        size="2x2",
        dataModelSchema={
            "GetCalendarEvents": {
                "title": _sample_field("产品评审"),
                "dtStart": _sample_field("09:30"),
                "dtEnd": _sample_field("10:30"),
                "eventLocation": _sample_field("A区会议室"),
                "startDate": _sample_field("07-15"),
                "updatedAt": _sample_field("2026-07-15 09:00"),
            }
        },
        assetCandidates=[],
    )
    selected_calendar = apply_content_selectors(calendar, {"GetCalendarEvents"})
    calendar_templates = scope_template_ids(
        AdvancedScopeBrief(
            themeId="meeting-paper-neutral",
            advancedComponentIds=("DateOverview", "ScheduleOverview"),
        ),
        get_cardplan_registry(),
        selected_calendar,
    )

    assert "ux-date-overview@2" in calendar_templates
    assert "ux-schedule-overview@2" in calendar_templates


def test_second_layer_projection_keeps_only_selected_component_display_facts():
    task_spec = TaskSpec(
        userQuery="深圳天气",
        size="2x2",
        dataModelSchema={
            "data": {
                "weather": {
                    "location": {"districtName": _sample_field("深圳")},
                    "current": {
                        "temperatureText": _sample_field("38°"),
                        "condition": _sample_field("晴"),
                        "airQuality": _sample_field("空气优"),
                    },
                    "daily": [{"temperatureRangeText": _sample_field("26° / 16°")}],
                    "updatedAt": _sample_field("2026-08-11 10:00"),
                    "transportMetadata": _sample_field("不得进入卡片"),
                },
                "calendar": {"events": [{"title": _sample_field("另一个领域")}]},
            }
        },
        assetCandidates=[],
    )

    selected = apply_content_selectors(task_spec, {"ViewWeather", "GetCalendarEvents"})
    projected = project_content_component_facts(
        selected,
        {"ViewWeather", "GetCalendarEvents"},
        ("WeatherOverview",),
    )

    assert set(projected.dataModelSchema["data"]) == {"WeatherOverview"}
    assert set(projected.dataModelSchema["data"]["WeatherOverview"]) == {
        "city",
        "temperature",
        "condition",
        "airQuality",
        "temperatureRange",
    }
    assert "updatedAt" not in json.dumps(projected.dataModelSchema, ensure_ascii=False)
    assert "不得进入卡片" not in json.dumps(projected.dataModelSchema, ensure_ascii=False)


def test_workout_projection_uses_only_provider_backed_countdown_variant_fields():
    task_spec = TaskSpec(
        userQuery="赛事倒计时",
        size="2x2",
        dataModelSchema={
            "data": {
                "countdown": {"countdownDays": {"type": "integer", "sampleValue": 32}},
                "health": {
                    "exerciseTypeName": _sample_field("户外跑步"),
                    "exerciseDurationText": _sample_field("40分"),
                    "exerciseCalorieText": _sample_field("298 千卡"),
                },
            }
        },
        assetCandidates=[],
    )

    projected = project_content_component_facts(
        task_spec,
        {"GetCountdownDays"},
        ("WorkoutOverview",),
    )

    assert projected.dataModelSchema == {
        "data": {"WorkoutOverview": {"countdownDays": {"type": "integer", "sampleValue": 32}}}
    }


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        ("3小时45分钟", ("3", "小时", "45", "分钟")),
        ("45分钟", ("45", "分钟", "", "")),
        ("3h 05m", ("3", "小时", "05", "分钟")),
    ],
)
def test_app_usage_projection_derives_trusted_dual_value_segments(
    duration: str,
    expected: tuple[str, str, str, str],
) -> None:
    task_spec = TaskSpec(
        userQuery="应用时长",
        size="2x2",
        dataModelSchema={
            "data": {
                "usage": {
                    "appName": _sample_field("抖音"),
                    "durationText": _sample_field(duration),
                    "updatedAt": _sample_field("今日"),
                }
            }
        },
        assetCandidates=[],
    )

    projected = project_content_component_facts(
        task_spec,
        {"GetAppUsageDuration"},
        ("AppUsageOverview",),
    )
    usage = projected.dataModelSchema["data"]["AppUsageOverview"]

    assert (
        tuple(
            usage[name]["sampleValue"]
            for name in (
                "durationPrimaryValueText",
                "durationPrimaryUnitText",
                "durationSecondaryValueText",
                "durationSecondaryUnitText",
            )
        )
        == expected
    )


def test_sleep_projection_reuses_trusted_dual_value_segments() -> None:
    task_spec = TaskSpec(
        userQuery="睡眠",
        size="2x2",
        dataModelSchema={
            "data": {
                "health": {
                    "sleepStatus": _sample_field("睡眠不足"),
                    "nightSleepDurationText": _sample_field("5小时45分钟"),
                    "fallAsleepTimeText": _sample_field("23:15"),
                    "wakeupTimeText": _sample_field("05:00"),
                }
            }
        },
        assetCandidates=[],
    )

    projected = project_content_component_facts(
        task_spec,
        {"GetHealthAndSportSummary"},
        ("SleepOverview",),
    )
    sleep = projected.dataModelSchema["data"]["SleepOverview"]

    assert tuple(
        sleep[name]["sampleValue"]
        for name in (
            "sleepDurationPrimaryValueText",
            "sleepDurationPrimaryUnitText",
            "sleepDurationSecondaryValueText",
            "sleepDurationSecondaryUnitText",
        )
    ) == ("5", "小时", "45", "分钟")


def _sample_field(value: str) -> dict[str, str]:
    return {
        "type": "string",
        "description": "可信能力字段",
        "sampleValue": value,
    }


@pytest.mark.asyncio
async def test_scope_planner_normalizes_empty_model_selection_without_retry():
    task_spec = _weather_scope_task()
    calls = 0

    async def generate_json(_messages, _phase):
        nonlocal calls
        calls += 1
        return {
            "scopeVersion": "advanced-scope-brief/1",
            "themeId": "family-weather-care-blue",
            "advancedComponentIds": [],
        }

    scope = await plan_advanced_scope_with_llm(
        task_spec,
        extract_data_shape(task_spec),
        generate_json,
        get_cardplan_registry(),
    )

    assert calls == 1
    assert scope.advanced_component_ids == ("WeatherOverview",)


def test_scope_templates_prune_variant_with_missing_required_business_fact():
    task_spec = TaskSpec(
        userQuery="加入当前会议",
        size="2x2",
        eventCandidates=[EventAction(id="event.enter.meeting", call="clickToApi", args={})],
        dataModelSchema={
            "data": {
                "event": {
                    "title": "UI需求评审会",
                    "time": "14:00 - 15:30",
                    "location": "深圳市龙岗区五和大道华为园区",
                }
            }
        },
        assetCandidates=[
            {
                "src": "resources/base/media/ux_golden_asset_time_beige.svg",
                "description": "时间图标",
            },
            {
                "src": "resources/base/media/ux_golden_asset_icon_id.svg",
                "description": "位置图标",
            },
        ],
    )
    registry = get_cardplan_registry()
    selected = scope_template_ids(
        AdvancedScopeBrief(
            themeId="meeting-paper-neutral",
            advancedComponentIds=("ScheduleOverview",),
        ),
        registry,
        task_spec,
    )

    assert "ux-meeting-metadata@1" in selected
    assert "calendar-event@1" not in selected


@pytest.mark.asyncio
async def test_scope_planner_trims_only_when_selected_components_have_no_common_layout():
    task_spec = TaskSpec(
        userQuery="展示会议日期和地点",
        size="2x2",
        eventCandidates=[EventAction(id="event.enter.meeting", call="clickToApi", args={})],
        dataModelSchema={
            "GetCalendarEvents": {
                "date": "27日",
                "calendarEvent": "UI需求评审会",
            },
            "ViewWeather": {"location": "深圳"},
        },
        assetCandidates=[],
    )

    async def generate_json(_messages, _phase):
        return {
            "scopeVersion": "advanced-scope-brief/1",
            "themeId": "meeting-paper-neutral",
            "advancedComponentIds": [
                "ScheduleOverview",
                "DateOverview",
                "LocationOverview",
            ],
        }

    scope = await plan_advanced_scope_with_llm(
        task_spec,
        extract_data_shape(task_spec),
        generate_json,
        get_cardplan_registry(),
    )

    assert scope.advanced_component_ids == ("ScheduleOverview", "DateOverview")


@pytest.mark.asyncio
async def test_new_mixed_entry_uses_new_phases_and_lowers_layout_to_standard_a2ui():
    model_client = UxMixedModelClient()

    output = await AdvancedComponentPipeline().generate_mixed(
        _weather_scope_task(),
        model_client,
        {"title": "天气", "description": "天气状态", "suggestSize": "2x2"},
    )

    assert model_client.phases == ["advanced-component-scope", "advanced-mixed-body"]
    mixed_user_prompt = model_client.prompts["advanced-mixed-body"][1]["content"]
    assert '"深圳"' in mixed_user_prompt
    assert 'trustedAssetSources=["resources/base/media/weather.svg"]' in mixed_user_prompt
    assert 'requiredLocalTemplateGroups=[["ux-weather-overview@2"]]' in mixed_user_prompt
    assert output.ui_brief == AdvancedScopeBrief(
        themeId="family-weather-care-blue",
        advancedComponentIds=("WeatherOverview",),
    )
    assert output.route == "hybrid-template"
    assert output.confidence_bypassed is True
    assert output.whole_card_candidates == []
    assert "SingleFocusLayout" in output.raw_output
    assert "Layout" not in output.effective_output
    assert "Template" not in output.compiled_a2ui
    assert "Layout" not in output.compiled_a2ui
    assert '"borderRadius":20' in output.effective_output
    assert '"padding":12' in output.effective_output


def test_ux_mixed_contract_rejects_standard_components_replacing_selected_business_component():
    task_spec = apply_content_selectors(_weather_scope_task(), {"ViewWeather"})
    scope = AdvancedScopeBrief(
        themeId="family-weather-care-blue",
        advancedComponentIds=("WeatherOverview",),
    )
    projected = project_content_component_facts(
        task_spec,
        {"ViewWeather"},
        scope.advanced_component_ids,
    )
    projection = build_ux_mixed_prompt(
        task_spec=projected,
        card_spec={"suggestSize": "2x2"},
        scope=scope,
        registry=get_cardplan_registry(),
    )

    with pytest.raises(
        TerseDslNested2ConversionError,
        match="requires one trusted Template",
    ):
        compile_ux_layout_card(
            'SingleFocusLayout(Text("晴", "body"));',
            task_spec=projected,
            contract=projection.contract,
            protocol_profile=A2UIProtocolRegistry.read_design_protocol_profile(
                TERSE_DSL_NESTED2_PROFILE_ID
            ),
            registry=get_cardplan_registry(),
        )


def test_ux_mixed_contract_converts_unknown_template_variant_to_repairable_error():
    task_spec = apply_content_selectors(_weather_scope_task(), {"ViewWeather"})
    scope = AdvancedScopeBrief(
        themeId="family-weather-care-blue",
        advancedComponentIds=("WeatherOverview",),
    )
    projected = project_content_component_facts(
        task_spec,
        {"ViewWeather"},
        scope.advanced_component_ids,
    )
    projection = build_ux_mixed_prompt(
        task_spec=projected,
        card_spec={"suggestSize": "2x2"},
        scope=scope,
        registry=get_cardplan_registry(),
    )
    source = _WEATHER_TEMPLATE_BODY.replace('"medium"', '"compact"')

    with pytest.raises(
        TerseDslNested2ConversionError,
        match="Template variant is not allowed: ux-weather-overview@2/compact",
    ):
        compile_ux_layout_card(
            source,
            task_spec=projected,
            contract=projection.contract,
            protocol_profile=A2UIProtocolRegistry.read_design_protocol_profile(
                TERSE_DSL_NESTED2_PROFILE_ID
            ),
            registry=get_cardplan_registry(),
        )


def test_ux_mixed_contract_normalizes_single_variant_and_trusted_relation_number():
    task_spec = TaskSpec(
        userQuery="展示低电状态并开启省电模式",
        size="2x2",
        eventCandidates=[
            EventAction(
                id="event.setPowerSavingMode",
                displayLabel="省电模式",
                call="clickToIntent",
                args={},
            )
        ],
        dataModelSchema={
            "data": {
                "battery": {
                    "batterySOC": {"type": "number", "sampleValue": 18},
                    "batterySOCText": {"type": "string", "sampleValue": "18%"},
                    "batteryCapacityLevelDesc": {
                        "type": "string",
                        "sampleValue": "手机电量低于20%，建议开启省电模式",
                    },
                    "chargingStatusDesc": {"type": "string", "sampleValue": "未充电"},
                }
            }
        },
        assetCandidates=[
            {
                "src": "resources/base/media/battery.svg",
                "description": "充电/闪电图标，适用场景：低电模式",
            },
            {
                "src": "resources/base/media/save-power.svg",
                "description": "省电模式图标",
            },
        ],
    )
    projected = project_content_component_facts(
        task_spec,
        {"GetPhoneBatteryInfo"},
        ("BatteryOverview",),
    )
    projection = build_ux_mixed_prompt(
        task_spec=projected,
        card_spec={"suggestSize": "2x2"},
        scope=AdvancedScopeBrief(
            themeId="system-low-power-blue",
            advancedComponentIds=("BatteryOverview",),
        ),
        registry=get_cardplan_registry(),
    )
    source = (
        'HeroActionLayout({"actionPlacement":"bottom"}, '
        'Template("ux-battery-overview@2", "hero", {'
        '"batteryCapacityLevelDesc":"手机电量低于20%，建议开启省电模式",'
        '"chargingStatusDesc":"未充电"}),'
        'PillAction({"actionId":"event.setPowerSavingMode",'
        '"icon":"resources/base/media/save-power.svg"}));'
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

    assert compiled.stats.template_variant_normalization_count == 1
    assert compiled.stats.template_provider_param_normalization_count == 3
    assert compiled.stats.template_relation_number_normalization_count == 0
    assert compiled.stats.template_used_ids == ("ux-battery-overview@2",)
    assert "Template" not in compiled.a2ui


def test_ux_mixed_prompt_counts_action_outside_business_children():
    task_spec = _metric_task_spec()
    projection = build_ux_mixed_prompt(
        task_spec=task_spec,
        card_spec={"suggestSize": "2x2"},
        scope=AdvancedScopeBrief(
            themeId="device-clean-blue-teal",
            advancedComponentIds=("BatteryOverview",),
        ),
        registry=get_cardplan_registry(),
    )
    system_prompt = projection.messages[0]["content"]

    assert "businessChildren=" in system_prompt
    assert "Action 必须是连续末尾直接 children" in system_prompt
    assert "configSchema=" in system_prompt
    assert "禁止放进 Column/Row/Stack/List/Template" in system_prompt


def test_action_matrix_layout_requires_two_approved_controls_in_scope_and_prompt():
    registry = get_cardplan_registry()
    scope = AdvancedScopeBrief(
        themeId="meeting-paper-neutral",
        advancedComponentIds=("SettingsOverview",),
    )
    one_action = _metric_task_spec()
    two_actions = one_action.model_copy(
        update={
            "eventCandidates": [
                EventAction(id="event.first", call="clickToApi", args={}),
                EventAction(id="event.second", call="clickToApi", args={}),
            ]
        }
    )

    assert "ActionMatrixLayout" not in resolve_scope_layout_ids(scope, one_action, registry)
    assert "ActionMatrixLayout" in resolve_scope_layout_ids(scope, two_actions, registry)

    projection = build_ux_mixed_prompt(
        task_spec=two_actions,
        card_spec={"suggestSize": "2x2"},
        scope=scope,
        registry=registry,
    )

    assert projection.contract.content_action_ids == ("event.first", "event.second")
    assert "actions=2..2" in projection.messages[0]["content"]


def test_ux_mixed_prompt_hides_variant_without_semantic_asset_source():
    task_spec = TaskSpec(
        userQuery="展示雨天天气并支持打车",
        size="2x2",
        eventCandidates=[EventAction(id="event.startNavigate", call="clickToApi", args={})],
        dataModelSchema={
            "data": {
                "weather": {
                    "symbol": {"type": "string", "sampleValue": "🌧️"},
                    "temperature": {"type": "string", "sampleValue": "12°"},
                    "condition": {"type": "string", "sampleValue": "雨"},
                    "city": {"type": "string", "sampleValue": "深圳"},
                }
            }
        },
        assetCandidates=[
            {
                "src": "resources/base/media/car.svg",
                "description": "汽车打车图标",
            }
        ],
    )
    projection = build_ux_mixed_prompt(
        task_spec=task_spec,
        card_spec={"suggestSize": "2x2"},
        scope=AdvancedScopeBrief(
            themeId="rainy-commute-gray-blue",
            advancedComponentIds=("WeatherOverview",),
        ),
        registry=get_cardplan_registry(),
    )
    system_prompt = projection.messages[0]["content"]

    assert "Template('ux-weather-hero@1', 'hero'" in system_prompt
    assert "Template('ux-weather-hero@1', 'medium'" not in system_prompt


@pytest.mark.asyncio
async def test_new_mixed_entry_rejects_standard_container_as_content_root():
    model_client = UxMixedModelClient('Column("section", Text("晴", "body"));')

    with pytest.raises(ValueError, match="root must be one Layout Component"):
        await AdvancedComponentPipeline().generate_mixed(
            _weather_scope_task(),
            model_client,
            {"title": "天气", "description": "天气状态", "suggestSize": "2x2"},
        )


@pytest.mark.asyncio
async def test_new_mixed_entry_retries_only_second_layer_after_contract_rejection():
    model_client = RetryingUxMixedModelClient(
        [
            'SingleFocusLayout(Text("模型新增标签", "body"));',
            _WEATHER_TEMPLATE_BODY,
        ]
    )

    output = await AdvancedComponentPipeline().generate_mixed(
        _weather_scope_task(),
        model_client,
        {"title": "天气", "description": "天气状态", "suggestSize": "2x2"},
    )

    assert model_client.phases == [
        "advanced-component-scope",
        "advanced-mixed-body",
        "advanced-mixed-body-repair",
    ]
    retry_prompt = model_client.prompts["advanced-mixed-body-repair"]
    assert retry_prompt[-2]["role"] == "assistant"
    assert "模型新增标签" in retry_prompt[-2]["content"]
    assert "trustedStringLiterals" in retry_prompt[-1]["content"]
    assert output.invocation["validationRepairCount"] == 1
    assert output.fallback_used is False
    assert 'Template("ux-weather-overview@2"' in output.raw_output


@pytest.mark.asyncio
async def test_new_mixed_entry_repairs_unknown_template_variant_without_scope_retry():
    model_client = RetryingUxMixedModelClient(
        [
            _WEATHER_TEMPLATE_BODY.replace('"medium"', '"compact"'),
            _WEATHER_TEMPLATE_BODY,
        ]
    )

    output = await AdvancedComponentPipeline().generate_mixed(
        _weather_scope_task(),
        model_client,
        {"title": "天气", "description": "天气状态", "suggestSize": "2x2"},
    )

    assert model_client.phases == [
        "advanced-component-scope",
        "advanced-mixed-body",
        "advanced-mixed-body-repair",
    ]
    assert (
        "Template variant is not allowed"
        in model_client.prompts["advanced-mixed-body-repair"][-1]["content"]
    )
    assert output.invocation["validationRepairCount"] == 1


@pytest.mark.asyncio
async def test_new_mixed_entry_uses_second_repair_without_repeating_scope(monkeypatch):
    monkeypatch.setattr(get_settings(), "ux_mixed_validation_max_retry_attempts", 2)
    model_client = RetryingUxMixedModelClient(
        [
            'SingleFocusLayout(Text("模型新增标签", "body"));',
            'SingleFocusLayout(Column("section"));',
            _WEATHER_TEMPLATE_BODY,
        ]
    )

    output = await AdvancedComponentPipeline().generate_mixed(
        _weather_scope_task(),
        model_client,
        {"title": "天气", "description": "天气状态", "suggestSize": "2x2"},
    )

    assert model_client.phases == [
        "advanced-component-scope",
        "advanced-mixed-body",
        "advanced-mixed-body-repair",
        "advanced-mixed-body-repair",
    ]
    assert output.invocation["validationRepairCount"] == 2
    assert output.fallback_used is False
    assert 'Template("ux-weather-overview@2"' in output.raw_output


@pytest.mark.asyncio
async def test_new_mixed_entry_groups_overflowing_layout_children():
    model_client = UxMixedModelClient(
        _WEATHER_TEMPLATE_BODY.replace(
            "));",
            '), Text("晴", "body"));',
        )
    )

    output = await AdvancedComponentPipeline().generate_mixed(
        _weather_scope_task(),
        model_client,
        {"title": "天气", "description": "天气状态", "suggestSize": "2x2"},
    )

    assert output.compiled_a2ui
    assert "SingleFocusLayout" in output.raw_output
    assert "SingleFocusLayout" not in output.compiled_a2ui


def test_ux_mixed_framer_repairs_only_trailing_typed_delimiters():
    source = 'Template("card@1", {"title":"天气"}, SingleFocusLayout(Text("晴", "body"));'

    framed, repaired = frame_ux_layout_children(
        source,
        size="2x2",
        registry=get_cardplan_registry(),
    )

    assert repaired is True
    assert framed.endswith(")));")
    assert parse_hybrid_card(framed).name == "card@1"


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


@pytest.mark.asyncio
async def test_terse_endpoint_runs_advanced_pipeline_end_to_end(
    monkeypatch,
):
    saved_genui: list[str] = []
    saved_design_tokens: list[str | None] = []

    async def new_mixed_entry(_pipeline, _task_spec, _model_client, *_args, **_kwargs):
        source = (
            'Template("card@1", {"title":"天气"}, SingleFocusLayout(Text("天气状态", "body")));'
        )
        compiled = convert_terse_dsl_nested2_to_a2ui(
            'Column("card", Text("天气状态", "body"));',
            size="2x2",
            protocol_profile=A2UIProtocolRegistry.read_design_protocol_profile(
                TERSE_DSL_NESTED2_PROFILE_ID
            ),
        )
        return AdvancedPipelineOutput(
            component_id="ux-advanced-component-mixed",
            style_id="family-weather-care-blue",
            source_dsl='Column("card", Text("天气状态", "body"));',
            source_format="a2ui",
            ui_brief=AdvancedScopeBrief(
                themeId="family-weather-care-blue",
                advancedComponentIds=("WeatherOverview",),
            ),
            invocation={},
            planner_mode="llm",
            mapper_mode="llm",
            route="hybrid-template",
            confidence_bypassed=True,
            raw_output=source,
            effective_output='Column("card", Text("天气状态", "body"));',
            compiled_a2ui=compiled,
        )

    async def old_entry_must_not_run(*_args, **_kwargs):
        raise AssertionError("fifth interface must bypass the legacy generate entry")

    def save_artifact(store, artifact):
        saved_genui.append(artifact.genui)
        saved_design_tokens.append(store.design_token)
        return ArtifactSaveResult(
            artifactUrl="https://artifact.test/advanced",
            artifactDigest="sha256:advanced",
        )

    monkeypatch.setattr(AdvancedComponentPipeline, "generate_mixed", new_mixed_entry)
    monkeypatch.setattr(AdvancedComponentPipeline, "generate", old_entry_must_not_run)
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
async def test_new_mixed_entry_failure_does_not_fall_back_to_old_entry_or_legacy_terse(
    monkeypatch,
):
    calls: list[str] = []

    async def invalid_mixed(_pipeline, _task_spec, _model_client, *_args, **_kwargs):
        raise ValueError("invalid mixed output")

    async def old_entry(_pipeline, _task_spec, _model_client, *_args, **_kwargs):
        calls.append("old-entry")
        raise AssertionError("legacy entry must stay bypassed")

    async def generate_terse(_client, _prompt, _profile):
        calls.append("terse")
        return 'Column("card", Text("回退成功", "title"));'

    monkeypatch.setattr(AdvancedComponentPipeline, "generate_mixed", invalid_mixed)
    monkeypatch.setattr(AdvancedComponentPipeline, "generate", old_entry)
    monkeypatch.setattr(A2UIModelClient, "generate", generate_terse)
    request = GenerateWidgetCardRequest(
        uid="fallback-e2e",
        prdVer="11.7.5.205",
        device={"romVersion": "CLS-AL30 6.0.0.328"},
        userQuery="生成静态摘要",
        title="摘要",
        description="回退测试",
    )

    response = await WidgetGenerationService().generate_widget_card_terse_dsl_nested2(request)

    assert response.status == GenerationStatus.FAILED
    assert response.generationFallbackUsed is False
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_whole_card_route_never_falls_back_to_legacy_terse(monkeypatch):
    calls: list[str] = []

    async def invalid_hybrid(_pipeline, _task_spec, _model_client, *_args, **_kwargs):
        raise ValueError("invalid hybrid output")

    async def generate_terse(_client, _prompt, _profile):
        calls.append("terse")
        return 'Column("card", Text("不应执行", "title"));'

    monkeypatch.setattr(get_settings(), "enable_advanced_whole_card_template", False)
    monkeypatch.setattr(AdvancedComponentPipeline, "generate_mixed", invalid_hybrid)
    monkeypatch.setattr(A2UIModelClient, "generate", generate_terse)
    request = GenerateWidgetCardRequest(
        uid="strict-hybrid-e2e",
        prdVer="11.7.5.205",
        device={"romVersion": "CLS-AL30 6.0.0.328"},
        userQuery="生成静态摘要",
        title="摘要",
        description="严格混合测试",
    )

    response = await WidgetGenerationService().generate_widget_card_terse_dsl_nested2(request)

    assert response.status == GenerationStatus.FAILED
    assert response.generationFallbackUsed is False
    assert calls == []


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


def test_mixed_generation_diagnostics_are_allowlisted_and_payload_free():
    try:
        raise TerseDslNested2ConversionError(
            "Template asset is not approved: private-business-asset"
        )
    except TerseDslNested2ConversionError as exc:
        error_code, error_origin = advanced_pipeline_module.safe_generation_error_metadata(exc)

    assert error_code == "TEMPLATE_ASSET_NOT_APPROVED"
    assert error_origin.startswith(
        "test_mixed_generation_diagnostics_are_allowlisted_and_payload_free:"
    )
    assert "private-business-asset" not in error_code
    assert "private-business-asset" not in error_origin

    unknown_code, _ = advanced_pipeline_module.safe_generation_error_metadata(
        ValueError("private-business-literal")
    )
    assert unknown_code == "PIPELINE_VALUE_ERROR"
    shape = advanced_pipeline_module._safe_raw_contract_shape(
        'SingleFocusLayout(Template("ux-private@1", "small", {"value":18}));',
        (("ux-private@1",),),
    )
    assert shape == (1, 1, 1)
