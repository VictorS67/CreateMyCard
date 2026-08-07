# -*- coding: utf-8 -*-
# ruff: noqa: E402
"""高级组件注册、确定性组合、数据派生和安全预算测试。"""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLOUD_ROOT = PROJECT_ROOT / "cloud"
if str(CLOUD_ROOT) not in sys.path:
    sys.path.insert(0, str(CLOUD_ROOT))

from models.generation import EventAction, TaskSpec
from services.advanced_component_pipeline.composition import (
    build_advanced_composition_plan,
    fields_for_presentation,
    validate_advanced_composition_plan,
)
from services.advanced_component_pipeline.data_shape import extract_data_shape
from services.advanced_component_pipeline.domain_rules import (
    derive_app_usage_state,
    derive_battery_state,
    derive_bluetooth_state,
    derive_call_summary,
    derive_location_state,
    derive_schedule_state,
    derive_sleep_state,
    derive_system_mode_state,
    derive_task_summary,
    derive_workout_state,
    safe_ratio,
)
from services.advanced_component_pipeline.models import (
    AdvancedComponentAssignment,
    AdvancedCompositionPlan,
    DataEnvelope,
)
from services.advanced_component_pipeline.ui_planner import plan_ui_with_llm
from services.cardplan_template.registry import get_cardplan_registry


def _field(data_type: str, description: str, sample_value):
    return {
        "type": data_type,
        "description": description,
        "sampleValue": sample_value,
    }


def _task(
    query: str,
    data_model_schema: dict,
    *,
    size: str = "2x2",
    with_action: bool = False,
) -> TaskSpec:
    events = [EventAction(id="event.open", call="clickToApi", args={})] if with_action else []
    return TaskSpec(
        userQuery=query,
        size=size,
        eventCandidates=events,
        dataModelSchema=data_model_schema,
        assetCandidates=[],
    )


def test_registry_contains_fifteen_components_and_eight_adaptive_templates():
    registry = get_cardplan_registry()

    assert registry.advanced_registry_version == "advanced-component-registry/1"
    assert len(registry.advanced_components) == 15
    assert len(registry.adaptive_templates) == 8
    assert set(registry.size_budgets) == {"2x2", "2x4"}
    assert all(item.local_template_ids for item in registry.advanced_components.values())
    assert all(item.field_priorities["mustShow"] for item in registry.advanced_components.values())


def test_field_cropping_never_hides_must_show_values():
    capability = get_cardplan_registry().require_advanced_component("ScheduleOverview")

    compact = fields_for_presentation(capability, "compact")
    standard = fields_for_presentation(capability, "standard")
    expanded = fields_for_presentation(capability, "expanded")

    assert compact == capability.field_priorities["mustShow"]
    assert set(compact) < set(standard) < set(expanded)
    assert "events.content" not in standard
    assert "events.content" in expanded


@pytest.mark.parametrize(
    ("component_id", "query", "schema_key", "description"),
    [
        ("WeatherOverview", "天气温度", "weather", "当前天气温度"),
        ("ScheduleOverview", "今日会议日程", "calendar", "会议开始时间"),
        ("TaskOverview", "待办任务", "tasks", "待办截止状态"),
        ("MemoPreview", "备忘录便签", "memo", "备忘录正文"),
        ("CallOverview", "未接来电电话", "calls", "未接通话联系人"),
        ("BatteryOverview", "电量充电", "battery", "当前电量充电状态"),
        ("AppUsageOverview", "应用使用时长屏幕时间", "usage", "应用使用时长"),
        ("ActivityOverview", "今日步数活动热量", "activity", "步数与活动热量"),
        ("WorkoutOverview", "运动记录训练", "workout", "最近运动训练"),
        ("HeartRateOverview", "心率 bpm", "heartRate", "当前心率 bpm"),
        ("SleepOverview", "睡眠入睡", "sleep", "昨晚睡眠入睡时间"),
        ("LocationOverview", "当前位置经纬度", "location", "位置经度纬度"),
        ("SystemModeOverview", "专注免打扰静音", "systemMode", "专注和免打扰模式"),
        ("BluetoothDeviceOverview", "蓝牙耳机左右耳", "bluetooth", "蓝牙耳机连接状态"),
        ("SettingsOverview", "设置开关选项", "settings", "设置开关当前值"),
    ],
)
def test_each_registered_family_is_selectable_from_semantic_input(
    component_id,
    query,
    schema_key,
    description,
):
    task_spec = _task(
        query,
        {"data": {schema_key: {"value": _field("string", description, "示例")}}},
        size="2x4",
    )

    plan = build_advanced_composition_plan(
        task_spec,
        extract_data_shape(task_spec),
        get_cardplan_registry(),
    )

    assert plan is not None
    assert plan.assignments[0].component_id == component_id


def test_data_envelope_preserves_real_zero_and_rejects_available_null():
    envelope = DataEnvelope(data=0, availability="available")

    assert envelope.data == 0
    with pytest.raises(ValidationError, match="available data must not be null"):
        DataEnvelope(data=None, availability="available")
    with pytest.raises(ValidationError, match="unavailable data must be null"):
        DataEnvelope(data=0, availability="permissionDenied")


def test_commute_domains_select_coherent_components_and_action_template():
    task_spec = _task(
        "天气和下一场会议，点击查看详情",
        {
            "data": {
                "weather": {
                    "condition": _field("string", "当前天气状况", "多云"),
                    "temperature": _field("number", "当前温度", 26),
                },
                "calendar": {
                    "nextEvent": {
                        "title": _field("string", "会议标题", "产品评审"),
                        "startAt": _field("string", "会议开始时间", "09:30"),
                    }
                },
            }
        },
        with_action=True,
    )
    registry = get_cardplan_registry()

    plan = build_advanced_composition_plan(task_spec, extract_data_shape(task_spec), registry)

    assert plan is not None
    assert plan.adaptive_template_id == "hero-support-action"
    assert {item.domain_id for item in plan.assignments} == {"weather", "schedule"}
    assert plan.action_count == 1
    assert plan.primary_chart_count <= 1
    validate_advanced_composition_plan(plan, registry)


def test_unrelated_domains_are_not_combined_just_because_both_exist():
    task_spec = _task(
        "查看天气和未接来电",
        {
            "data": {
                "weather": {"condition": _field("string", "天气状况", "晴")},
                "calls": [
                    {
                        "phoneNumber": _field("string", "未接电话联系人号码", "138****0000"),
                        "status": _field("string", "通话状态", "missed"),
                    }
                ],
            }
        },
    )

    plan = build_advanced_composition_plan(
        task_spec,
        extract_data_shape(task_spec),
        get_cardplan_registry(),
    )

    assert plan is not None
    assert len({item.domain_id for item in plan.assignments}) == 1
    assert plan.dropped_domain_ids


def test_weather_forecast_requires_real_forecast_schema_fields():
    current = _task(
        "天气卡片",
        {"data": {"weather": {"condition": _field("string", "天气状况", "晴")}}},
    )
    forecast = _task(
        "天气预报卡片",
        {
            "data": {
                "weather": {
                    "condition": _field("string", "天气状况", "晴"),
                    "daily": [
                        {
                            "high": _field("number", "预报最高温", 30),
                            "low": _field("number", "预报最低温", 22),
                        }
                    ],
                }
            }
        },
    )
    registry = get_cardplan_registry()

    current_plan = build_advanced_composition_plan(current, extract_data_shape(current), registry)
    forecast_plan = build_advanced_composition_plan(
        forecast, extract_data_shape(forecast), registry
    )

    assert current_plan is not None
    assert forecast_plan is not None
    assert current_plan.adaptive_template_id != "weather-forecast"
    assert forecast_plan.adaptive_template_id == "weather-forecast"
    assert [item.variant for item in forecast_plan.assignments] == ["forecast", "forecast"]


def test_sensitive_components_default_to_masked_privacy():
    task_spec = _task(
        "显示当前位置",
        {
            "data": {
                "location": {
                    "label": _field("string", "当前位置名称", "公司"),
                    "latitude": _field("number", "位置纬度", 31.2),
                    "longitude": _field("number", "位置经度", 121.5),
                }
            }
        },
    )

    plan = build_advanced_composition_plan(
        task_spec,
        extract_data_shape(task_spec),
        get_cardplan_registry(),
    )

    assert plan is not None
    assert plan.assignments[0].component_id == "LocationOverview"
    assert plan.assignments[0].privacy_mode == "masked"


@pytest.mark.asyncio
async def test_ui_brief_cannot_override_server_owned_advanced_composition():
    task_spec = _task(
        "显示当前电量",
        {
            "data": {
                "battery": {
                    "levelPercent": _field("number", "当前电量百分比", 0),
                    "chargeState": _field("string", "充电状态", "discharging"),
                }
            }
        },
    )
    data_shape = extract_data_shape(task_spec)
    registry = get_cardplan_registry()
    plan = build_advanced_composition_plan(task_spec, data_shape, registry)
    assert plan is not None

    async def generate_json(_prompt, _phase):
        return {
            "purpose": "battery-status",
            "primaryInformation": ["电量"],
            "informationHierarchy": ["状态"],
            "visualTone": "calm",
            "contentPriorities": ["电量优先"],
            "advancedComponentIds": ["SettingsOverview"],
            "adaptiveTemplateId": "list-action",
            "primaryDomain": "settings",
            "reason": "展示电量。",
        }

    brief = await plan_ui_with_llm(
        task_spec,
        data_shape,
        generate_json,
        plan,
    )

    assert brief.advanced_component_ids == ["BatteryOverview"]
    assert brief.primary_domain == "battery"
    assert brief.adaptive_template_id == plan.adaptive_template_id
    assert set(plan.local_template_ids).issubset(brief.local_template_ids)


def test_validator_rejects_two_heroes_and_sensitive_full_mode():
    registry = get_cardplan_registry()
    assignments = (
        AdvancedComponentAssignment(
            component_id="LocationOverview",
            domain_id="location",
            role="hero",
            variant="current",
            presentation="standard",
            privacy_mode="full",
            score=10,
            local_template_ids=("ux-context-summary@1",),
        ),
        AdvancedComponentAssignment(
            component_id="WeatherOverview",
            domain_id="weather",
            role="hero",
            variant="current",
            presentation="compact",
            privacy_mode="masked",
            score=9,
            local_template_ids=("ux-weather-hero@1",),
        ),
    )
    plan = AdvancedCompositionPlan(
        registry_version=registry.advanced_registry_version,
        size="2x2",
        primary_domain="location",
        primary_goal="通勤",
        adaptive_template_id="hero-support",
        assignments=assignments,
        action_count=0,
        primary_chart_count=0,
        max_list_items=2,
        information_levels=2,
        data_signals=("location", "weather"),
    )

    with pytest.raises(ValueError, match="two heroes"):
        validate_advanced_composition_plan(plan, registry)


def test_deterministic_domain_rules_preserve_zero_and_reject_inference():
    now = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)

    assert safe_ratio(0, 100) == 0
    assert safe_ratio(None, 100) is None
    assert derive_battery_state(0, "discharging")["state"] == "alert"
    assert derive_battery_state(50, "discharging", -1)["trustedEstimatedMinutes"] is None
    assert derive_app_usage_state(0, 3600)["usageRatio"] == 0
    assert derive_schedule_state(
        "2026-08-08T08:10:00Z",
        "2026-08-08T09:10:00Z",
        join_uri="meeting://join",
        now=now,
    ) == {
        "minutesUntilStart": 10,
        "isOngoing": False,
        "isJoinable": True,
        "durationMinutes": 60,
    }
    assert derive_workout_state(0, 0, 3600)["goalRatio"] == 0
    assert (
        derive_sleep_state(
            "2026-08-07T23:00:00Z",
            "2026-08-08T05:00:00Z",
        )["isInsufficient"]
        is True
    )
    assert derive_location_state("2026-08-08T07:30:00Z", now=now)["isStale"] is True


def test_list_call_system_and_bluetooth_derivations_are_bounded():
    now = datetime(2026, 8, 8, 8, 0, tzinfo=UTC)
    task_summary = derive_task_summary(
        [
            {"status": "completed", "dueAt": "2026-08-07T08:00:00Z"},
            {"status": "pending", "dueAt": "2026-08-08T09:00:00Z"},
        ],
        now=now,
    )
    call_summary = derive_call_summary(
        [
            {
                "status": "missed",
                "startedAt": "2026-08-08T07:00:00Z",
                "durationSeconds": 0,
            }
        ]
    )
    bluetooth = derive_bluetooth_state(
        connection_state="connected",
        updated_at="2026-08-08T07:58:00Z",
        battery_percent=None,
        battery_parts={"left": 0, "right": 20, "case": None},
        now=now,
    )

    assert task_summary["completionRatio"] == 0.5
    assert call_summary["latestDurationText"] == "0分钟"
    assert bluetooth["summaryBatteryPercent"] == 0
    assert bluetooth["missingBatteryParts"] == ["case"]
    assert (
        derive_system_mode_state(
            focus_enabled=True,
            do_not_disturb=True,
            audio_mode="silent",
            focus_end_at="2026-08-08T08:30:00Z",
            now=now,
        )["focusRemainingMinutes"]
        == 30
    )
    with pytest.raises(ValueError, match="audioMode"):
        derive_system_mode_state(
            focus_enabled=False,
            do_not_disturb=False,
            audio_mode="ring-and-vibrate",
        )
