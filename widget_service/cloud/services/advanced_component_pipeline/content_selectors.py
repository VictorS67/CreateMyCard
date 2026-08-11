"""Deterministic provider-schema selectors for content business components."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime
from typing import Any

from models.generation import TaskSpec

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

_SELECTOR_COMPONENT_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "WeatherOverview": (
        "weather",
        ("city", "temperature", "condition", "airQuality", "temperatureRange"),
    ),
    "DateOverview": ("date", ("date", "weekday")),
    "ScheduleOverview": ("schedule", ("title", "timeText", "location")),
    "LocationOverview": ("location", ("label", "city", "updatedText")),
}

_SELECTOR_FALLBACK_FIELDS: dict[str, tuple[str, ...]] = {
    "WeatherOverview": (
        "condition",
        "temperatureText",
        "districtName",
        "prefectureName",
        "airQuality",
        "temperatureRangeText",
    ),
    "DateOverview": ("startDate", "weekday"),
    "ScheduleOverview": ("title", "dtStart", "dtEnd", "eventLocation"),
    "LocationOverview": ("districtName", "prefectureName", "updatedAt"),
}

_PROVIDER_COMPONENT_FIELDS: dict[str, tuple[str, ...]] = {
    "BatteryOverview": (
        "batterySOC",
        "batterySOCText",
        "batteryCapacityLevelDesc",
        "chargingStatusDesc",
    ),
    "ResourceUsageOverview": ("usagePercent", "availableMemText", "totalMemText"),
    "AppUsageOverview": (
        "appName",
        "durationText",
        "updatedAt",
        # Compatibility facts used by the exported v1 cross-language Golden.
        # They remain provider-derived and are only retained when present.
        "safeValue",
        "warningValue",
        "total",
        "firstLabel",
        "firstValue",
        "secondLabel",
        "secondValue",
    ),
    "ActivityOverview": ("dailySteps", "dailyTotalCaloriesText", "dailyDistanceText"),
    "HeartRateOverview": ("exerciseHeartRateAvg", "updatedAt"),
    "SleepOverview": (
        "sleepStatus",
        "nightSleepDurationText",
        "fallAsleepTimeText",
        "wakeupTimeText",
    ),
    "BluetoothDeviceOverview": (
        "earphoneName",
        "leftBatteryLevel",
        "rightBatteryLevel",
        "batteryLevel",
    ),
}


def apply_content_selectors(
    task_spec: TaskSpec,
    capability_ids: set[str],
) -> TaskSpec:
    """Add trusted display aliases without changing provider or CardSpec contracts."""
    schema = deepcopy(task_spec.dataModelSchema)
    selectors: dict[str, dict[str, dict[str, Any]]] = {}

    if "ViewWeather" in capability_ids:
        weather = _weather_selector(schema)
        if weather:
            selectors["weather"] = weather
            if "updatedAt" in weather:
                selectors["location"] = {
                    "label": _field("天气位置", "可信天气查询位置标签"),
                    "city": weather["city"],
                    "updatedText": weather["updatedAt"],
                }

    if "GetCalendarEvents" in capability_ids:
        schedule, date = _calendar_selectors(schema)
        if schedule:
            selectors["schedule"] = schedule
        if date:
            selectors["date"] = date

    if not selectors:
        return task_spec
    data = schema.setdefault("data", {})
    if not isinstance(data, dict):
        return task_spec
    data["_advancedSelectors"] = selectors
    return task_spec.model_copy(update={"dataModelSchema": schema})


def project_content_component_facts(
    task_spec: TaskSpec,
    capability_ids: set[str],
    component_ids: tuple[str, ...],
) -> TaskSpec:
    """Narrow the second-layer contract to selected component display facts.

    The first-layer scope planner still sees the complete provider schema. The
    mixed-body model receives only fields that the selected strict content
    component can render, so transport metadata does not become UI ``mustKeep``.
    """
    schema = task_spec.dataModelSchema
    selector_root = schema.get("data", {})
    selectors = (
        selector_root.get("_advancedSelectors", {}) if isinstance(selector_root, dict) else {}
    )
    projected: dict[str, dict[str, Any]] = {}
    for component_id in component_ids:
        selector_spec = _SELECTOR_COMPONENT_FIELDS.get(component_id)
        if selector_spec is not None:
            selector_name, field_names = selector_spec
            selector = selectors.get(selector_name) if isinstance(selectors, dict) else None
            selected = _select_direct_fields(selector, field_names)
            if not selected:
                fallback_fields = _SELECTOR_FALLBACK_FIELDS[component_id]
                source = _best_source_object(schema, fallback_fields)
                selected = {
                    field_name: deepcopy(field)
                    for field_name in fallback_fields
                    if (field := _first_field(source, field_name)) is not None
                }
        else:
            field_names = _provider_fields(component_id, capability_ids)
            source = _best_source_object(schema, field_names)
            selected = {
                field_name: deepcopy(field)
                for field_name in field_names
                if (field := _first_field(source, field_name)) is not None
            }
        if component_id == "AppUsageOverview":
            selected.update(
                _duration_segments(
                    selected.get("durationText"),
                    prefix="duration",
                    description_subject="使用时长",
                )
            )
        elif component_id == "SleepOverview":
            selected.update(
                _duration_segments(
                    selected.get("nightSleepDurationText"),
                    prefix="sleepDuration",
                    description_subject="睡眠时长",
                )
            )
        if selected:
            projected[component_id] = selected
    if not projected:
        raise ValueError("Selected advanced components have no renderable provider facts")
    return task_spec.model_copy(update={"dataModelSchema": {"data": projected}})


def _provider_fields(component_id: str, capability_ids: set[str]) -> tuple[str, ...]:
    if component_id != "WorkoutOverview":
        return _PROVIDER_COMPONENT_FIELDS.get(component_id, ())
    fields: list[str] = []
    if "GetHealthAndSportSummary" in capability_ids:
        fields.extend(("exerciseTypeName", "exerciseDurationText", "exerciseCalorieText"))
    if "GetCountdownDays" in capability_ids:
        fields.append("countdownDays")
    return tuple(fields)


def _select_direct_fields(value: Any, field_names: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        field_name: deepcopy(value[field_name]) for field_name in field_names if field_name in value
    }


def _best_source_object(value: Any, field_names: tuple[str, ...]) -> Any:
    if not field_names:
        return {}
    wanted = set(field_names)
    best: tuple[int, int, dict[str, Any]] | None = None

    def visit(current: Any, depth: int) -> None:
        nonlocal best
        if isinstance(current, dict):
            names = _descendant_field_names(current)
            candidate = (len(wanted & names), depth, current)
            if candidate[0] and (best is None or candidate[:2] > best[:2]):
                best = candidate
            for child in current.values():
                visit(child, depth + 1)
        elif isinstance(current, list):
            for child in current:
                visit(child, depth + 1)

    visit(value, 0)
    return best[2] if best is not None else {}


def _descendant_field_names(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            item for child in value.values() for item in _descendant_field_names(child)
        }
    if isinstance(value, list):
        return {item for child in value for item in _descendant_field_names(child)}
    return set()


def _first_field(value: Any, field_name: str) -> Any:
    if isinstance(value, dict):
        if field_name in value:
            return value[field_name]
        for child in value.values():
            selected = _first_field(child, field_name)
            if selected is not None:
                return selected
    elif isinstance(value, list):
        for child in value:
            selected = _first_field(child, field_name)
            if selected is not None:
                return selected
    return None


def _weather_selector(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    city = _first_sample(schema, "districtName") or _first_sample(schema, "prefectureName")
    temperature = _first_sample(schema, "temperatureText")
    condition = _first_sample(schema, "condition")
    air_quality = _first_sample(schema, "airQuality")
    temperature_range = _first_sample(schema, "temperatureRangeText")
    if not all(
        isinstance(value, str) and value
        for value in (city, temperature, condition, air_quality, temperature_range)
    ):
        return {}
    selected = {
        "city": _field(city, "可信天气查询城市或地区"),
        "temperature": _field(temperature, "可信当前温度文本"),
        "condition": _field(condition, "可信当前天气状态"),
        "airQuality": _field(air_quality, "可信当前空气质量"),
        "temperatureRange": _field(temperature_range, "可信当日温度范围"),
    }
    updated_at = _first_sample(schema, "updatedAt")
    if isinstance(updated_at, str) and updated_at:
        selected["updatedAt"] = _field(updated_at, "可信天气更新时间")
    return selected


def _calendar_selectors(
    schema: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    title = _first_sample(schema, "title")
    start = _first_sample(schema, "dtStart")
    end = _first_sample(schema, "dtEnd")
    location = _first_sample(schema, "eventLocation")
    schedule: dict[str, dict[str, Any]] = {}
    if isinstance(title, str) and title and isinstance(start, str) and start:
        schedule["title"] = _field(title, "可信日程标题")
        time_text = start
        if isinstance(end, str) and end:
            time_text = f"{start} - {end}"
        schedule["timeText"] = _field(time_text, "可信日程起止时间")
        if isinstance(location, str) and location:
            schedule["location"] = _field(location, "可信日程地点")

    start_date = _first_sample(schema, "startDate")
    updated_at = _first_sample(schema, "updatedAt")
    parsed = _parse_calendar_date(start_date, updated_at)
    if parsed is None:
        return schedule, {}
    date = {
        "date": _field(f"{parsed.day}日", "可信日程日期"),
        "weekday": _field(_WEEKDAYS[parsed.weekday()], "可信日程星期"),
    }
    return schedule, date


def _parse_calendar_date(start_date: Any, updated_at: Any) -> datetime | None:
    if not isinstance(start_date, str) or not start_date:
        return None
    for pattern in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(start_date, pattern)
        except ValueError:
            pass
    if not isinstance(updated_at, str) or len(updated_at) < 4:
        return None
    try:
        year = int(updated_at[:4])
        return datetime.strptime(f"{year}-{start_date}", "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _first_sample(value: Any, field_name: str) -> Any:
    if isinstance(value, dict):
        if field_name in value:
            return _sample_value(value[field_name])
        for child in value.values():
            selected = _first_sample(child, field_name)
            if selected is not None:
                return selected
    elif isinstance(value, list):
        for child in value:
            selected = _first_sample(child, field_name)
            if selected is not None:
                return selected
    return None


def _sample_value(value: Any) -> Any:
    if isinstance(value, dict) and "sampleValue" in value:
        return value["sampleValue"]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list) and value:
        return _sample_value(value[0])
    return None


def _duration_segments(
    duration_field: Any,
    *,
    prefix: str,
    description_subject: str,
) -> dict[str, dict[str, Any]]:
    """Split one trusted provider duration into the UX 30fp value/12fp unit pairs."""
    duration = _sample_value(duration_field)
    if not isinstance(duration, str) or not duration.strip():
        return {}
    normalized = re.sub(r"\s+", "", duration).casefold()
    match = re.fullmatch(
        r"(?:(?P<hours>\d+)(?P<hours_unit>小时|时|h))?"
        r"(?:(?P<minutes>\d+)(?P<minutes_unit>分钟|分|m))?",
        normalized,
    )
    if match is None or not (match.group("hours") or match.group("minutes")):
        return {}
    values = (
        (
            match.group("hours") or match.group("minutes") or "",
            "小时" if match.group("hours") else "分钟",
        ),
        (
            match.group("minutes") or "" if match.group("hours") and match.group("minutes") else "",
            "分钟" if match.group("hours") and match.group("minutes") else "",
        ),
    )
    return {
        f"{prefix}PrimaryValueText": _field(
            values[0][0], f"从可信{description_subject}解析的主数值"
        ),
        f"{prefix}PrimaryUnitText": _field(
            values[0][1], f"从可信{description_subject}解析的主单位"
        ),
        f"{prefix}SecondaryValueText": _field(
            values[1][0], f"从可信{description_subject}解析的次数值"
        ),
        f"{prefix}SecondaryUnitText": _field(
            values[1][1], f"从可信{description_subject}解析的次单位"
        ),
    }


def _field(value: str, description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "description": description,
        "sampleValue": value,
    }
