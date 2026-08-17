"""模板路由独立模块的关键边界和天气 POC。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from api.schemas import GenerateWidgetCardRequest
from core.errors import GenerationStatus
from models.generation import CandidateDataBinding, TaskSpec
from models.service import ArtifactSaveResult
from services.artifact_store import ArtifactStore
from services.generation_pipeline import (
    DslProcessorKind,
    GenerationRoutePolicy,
)
from services.protocol_registry import (
    A2UI_FORM_PROTOCOL_PROFILE_ID,
    TERSE_DSL_NESTED2_PROFILE_ID,
    A2UIProtocolRegistry,
)
from services.template_generation import (
    facade,
    route_legacy_python_terse_generation,
)
from services.template_generation.engine.advanced.content_selectors import (
    app_usage_overview_is_eligible,
    app_usage_overview_query_is_supported,
    apply_content_selectors,
)
from services.template_generation.engine.advanced.models import AdvancedScopeBrief
from services.template_generation.engine.advanced.scope_planner import (
    TemplateRouteNotApplicable,
    validate_template_request_coverage,
)
from services.template_generation.engine.cardplan.registry import get_cardplan_registry
from services.template_generation.engine.pipeline import (
    TemplateGenerationError,
    generate_template_a2ui,
)
from services.widget_generation_service import WidgetGenerationService

_WEATHER_BODY = (
    'SingleFocusLayout(Template("WeatherOverview@1","heroIcon",'
    '{"conditionIcon":"resources/base/media/icon_weather1.svg"}));'
)
_WEATHER_TEMPLATE_FIELDS = (
    "/location/districtName",
    "/current/temperatureText",
    "/current/condition",
    "/current/airQuality",
    "/daily/0/temperatureRangeText",
)


def test_all_provider_templates_are_loaded_from_the_isolated_directory():
    registry = get_cardplan_registry()

    assert set(registry.provider_template_ids) == {
        "ActivityOverview@1",
        "AppUsageOverview@1",
        "BatteryOverview@1",
        "BluetoothDeviceOverview@1",
        "DateOverview@1",
        "HeartRateOverview@1",
        "ResourceUsageOverview@1",
        "ScheduleOverview@1",
        "SleepOverview@1",
        "WeatherOverview@1",
        "WorkoutCountdown@1",
        "WorkoutOverview@1",
    }


@pytest.mark.asyncio
async def test_derived_parameter_source_field_is_counted_as_template_coverage():
    def field(value: str) -> dict[str, Any]:
        return {
            "type": "string",
            "description": "trusted app usage field",
            "sampleValue": value,
        }

    registry = get_cardplan_registry()
    task_spec = TaskSpec(
        userQuery="看看抖音今天用了多久",
        size="2x2",
        eventCandidates=[],
        assetCandidates=[],
        dataModelSchema={
            "data": {
                "appUsageStats": {
                    "appUsage": {
                        "appName": field("示例应用"),
                        "durationText": field("1小时20分钟"),
                    },
                    "updatedAt": field("今天 12:00"),
                }
            }
        },
    )
    task_spec = apply_content_selectors(task_spec, {"GetAppUsageDuration"})
    assert app_usage_overview_is_eligible(task_spec, {"GetAppUsageDuration"})
    binding = CandidateDataBinding(
        capabilityId="GetAppUsageDuration",
        writeResultTo="/data/appUsageStats",
        candidateOutputFields=[
            "/appUsage/appName",
            "/appUsage/durationText",
            "/updatedAt",
        ],
    )
    scope = AdvancedScopeBrief(
        themeId="digital-wellbeing-neutral-dark",
        advancedComponentIds=["AppUsageOverview"],
    )
    card_spec = {
        "title": "应用时长",
        "description": "今日使用情况",
        "suggestSize": "2x2",
        "dataBindings": [
            {
                "capabilityId": "GetAppUsageDuration",
                "arguments": {},
                "writeResultTo": "/data/appUsageStats",
            }
        ],
    }

    validate_template_request_coverage(
        scope,
        task_spec,
        registry,
        (binding,),
        {"GetAppUsageDuration": ("/appUsage/durationText",)},
        card_spec,
    )

    class AppUsageTemplateModel:
        async def generate_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "routeVersion": "template-route-decision/2",
                "templateUsable": True,
                "themeId": "digital-wellbeing-neutral-dark",
                "advancedComponentIds": ["AppUsageOverview"],
                "requiredOutputFieldsByCapability": {
                    "GetAppUsageDuration": ["/appUsage/durationText"],
                },
            }

        async def generate(self, *_args: Any, **_kwargs: Any) -> str:
            return 'SingleFocusLayout(Template("AppUsageOverview@1","singleApp",{}));'

    output = await generate_template_a2ui(
        task_spec,
        card_spec,
        (binding,),
        AppUsageTemplateModel(),
    )
    projected_data = output.projected_task_spec.dataModelSchema["data"]
    assert "AppUsageOverview" not in projected_data
    assert (
        projected_data["appUsageStats"]["_templateProjection"]["AppUsageOverview"]
        ["durationPrimaryValueText"]["sampleValue"]
        == "1"
    )


def test_placeholder_app_name_still_rejects_an_obvious_multi_app_query():
    assert not app_usage_overview_query_is_supported(
        "看看抖音和微信今天用了多久",
        "示例应用",
    )


class WeatherTemplateModel:
    def __init__(
        self,
        required_fields: tuple[str, ...] = _WEATHER_TEMPLATE_FIELDS,
    ) -> None:
        self.body_called = False
        self.required_fields = required_fields

    async def generate_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "routeVersion": "template-route-decision/2",
            "templateUsable": True,
            "themeId": "family-weather-care-blue",
            "advancedComponentIds": ["WeatherOverview"],
            "requiredOutputFieldsByCapability": {
                "ViewWeather": list(self.required_fields),
            },
        }

    async def generate(self, *_args: Any, **_kwargs: Any) -> str:
        self.body_called = True
        return _WEATHER_BODY


def _policy() -> GenerationRoutePolicy:
    return GenerationRoutePolicy(
        operation="generateWidgetCardCompactDsl",
        protocol_profile_id=A2UI_FORM_PROTOCOL_PROFILE_ID,
        backend="openai",
        processor_kind=DslProcessorKind.DESIGN_COMPACT,
        source_format="design-compact-dsl",
        model_profile_id="design-compact-dsl",
        model_format="compact-dsl",
        design_profile_id="design-compact-dsl",
        validation_failure_blocking=True,
        stores_design_token=True,
    )


def _terse_policy() -> GenerationRoutePolicy:
    return GenerationRoutePolicy(
        operation="generateWidgetCardTerseDslNested2",
        protocol_profile_id=A2UI_FORM_PROTOCOL_PROFILE_ID,
        backend="openai",
        processor_kind=DslProcessorKind.TERSE_NESTED2,
        source_format=TERSE_DSL_NESTED2_PROFILE_ID,
        model_profile_id=TERSE_DSL_NESTED2_PROFILE_ID,
        model_format=TERSE_DSL_NESTED2_PROFILE_ID,
        design_profile_id=TERSE_DSL_NESTED2_PROFILE_ID,
        supports_dynamic_capabilities=True,
        validation_failure_blocking=True,
        stores_design_token=True,
    )


def _weather_request() -> GenerateWidgetCardRequest:
    return GenerateWidgetCardRequest(
        uid="template-test",
        prdVer="11.7.5.205",
        device={"romVersion": "6.0"},
        userQuery="做一个天气卡片，显示城市、温度、天气、空气质量和温度范围",
        size="2x2",
        title="今日天气",
        description="天气概览",
        candidateDataBindings=[
            {
                "capabilityId": "ViewWeather",
                "arguments": {"districtName": "青浦区", "forecastDays": 1},
                "writeResultTo": "/data/weather",
                "candidateOutputFields": [
                    "/location/districtName",
                    "/current/temperatureText",
                    "/current/condition",
                    "/current/airQuality",
                    "/daily/0/temperatureRangeText",
                ],
            }
        ],
        candidateAssetIds=["asset.icon_weather1"],
    )


def _weather_task_spec() -> TaskSpec:
    def field(value: str) -> dict[str, Any]:
        return {
            "type": "string",
            "description": "weather field",
            "sampleValue": value,
        }

    return TaskSpec(
        userQuery="天气",
        size="2x2",
        eventCandidates=[],
        assetCandidates=[
            {
                "src": "resources/base/media/icon_weather1.svg",
                "description": "天气状态图标",
                "sceneTags": ["condition", "weather"],
            }
        ],
        dataModelSchema={
            "data": {
                "weather": {
                    "location": {"districtName": field("青浦区")},
                    "current": {
                        "temperatureText": field("29°C"),
                        "condition": field("多云"),
                        "airQuality": field("良"),
                    },
                    "daily": [{"temperatureRangeText": field("25° / 32°")}],
                }
            }
        },
    )


def _weather_card_spec() -> dict[str, Any]:
    return {
        "title": "今日天气",
        "description": "天气概览",
        "suggestSize": "2x2",
        "dataBindings": [
            {
                "capabilityId": "ViewWeather",
                "arguments": {"districtName": "青浦区", "forecastDays": 1},
                "writeResultTo": "/data/weather",
            }
        ],
    }


@pytest.mark.asyncio
async def test_weather_template_generates_a2ui_and_compact_artifact(monkeypatch):
    model = WeatherTemplateModel()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        facade,
        "create_template_model_client",
        lambda _runtime, _context: model,
    )

    async def save(store: ArtifactStore, artifact: Any) -> ArtifactSaveResult:
        captured["artifact"] = artifact
        captured["compact"] = store.design_token
        return ArtifactSaveResult(
            artifactUrl="https://artifact.test/weather-template",
            artifactDigest="sha256:weather-template",
        )

    monkeypatch.setattr(ArtifactStore, "save", save)
    starts: list[str] = []

    async def before_model_call(size: str) -> None:
        starts.append(size)

    response = await WidgetGenerationService(
        model_runtime=object(),
    ).generate_widget_card_compact_dsl(
        _weather_request(),
        before_model_call=before_model_call,
    )

    assert response.status == GenerationStatus.SUCCESS
    assert response.artifactUrl == "https://artifact.test/weather-template"
    assert starts == ["2x2"]
    assert model.body_called is True
    assert captured["compact"]
    assert "{{ ${/data/weather/current/condition}" in captured["compact"]
    messages = [json.loads(line) for line in captured["artifact"].genui.splitlines()]
    protocol_profile = A2UIProtocolRegistry(A2UI_FORM_PROTOCOL_PROFILE_ID).get_profile()
    assert messages[0]["createSurface"]["catalogId"] == protocol_profile["catalogId"]
    root = next(
        item
        for item in messages[1]["updateComponents"]["components"]
        if item["id"] == "root"
    )
    assert root["styles"]["borderRadius"] == 18
    assert captured["artifact"].effectiveCapabilities["data"] == ["ViewWeather"]


@pytest.mark.asyncio
async def test_weather_template_generates_a2ui_and_terse_artifact(monkeypatch):
    model = WeatherTemplateModel()
    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        facade,
        "create_template_model_client",
        lambda _runtime, _context: model,
    )

    async def save(store: ArtifactStore, artifact: Any) -> ArtifactSaveResult:
        captured["artifact"] = artifact
        captured["terse"] = store.design_token
        return ArtifactSaveResult(
            artifactUrl="https://artifact.test/weather-template-terse",
            artifactDigest="sha256:weather-template-terse",
        )

    monkeypatch.setattr(ArtifactStore, "save", save)
    response = await WidgetGenerationService(
        model_runtime=object(),
    ).generate_widget_card_terse_dsl_nested2(_weather_request())

    assert response.status == GenerationStatus.SUCCESS
    assert response.artifactUrl == "https://artifact.test/weather-template-terse"
    assert captured["terse"]
    assert "Column(" in captured["terse"]
    assert "Template(" not in captured["terse"]
    messages = [json.loads(line) for line in captured["artifact"].genui.splitlines()]
    protocol_profile = A2UIProtocolRegistry(A2UI_FORM_PROTOCOL_PROFILE_ID).get_profile()
    assert messages[0]["createSurface"]["catalogId"] == protocol_profile["catalogId"]
    assert captured["artifact"].effectiveCapabilities["data"] == ["ViewWeather"]


@pytest.mark.asyncio
async def test_uncovered_requested_field_rejects_template_before_body_generation():
    model = WeatherTemplateModel(("/current/humidityPercent",))
    binding = CandidateDataBinding(
        capabilityId="ViewWeather",
        arguments={"districtName": "青浦区"},
        writeResultTo="/data/weather",
        candidateOutputFields=[
            "/current/condition",
            "/current/humidityPercent",
        ],
    )

    with pytest.raises(TemplateRouteNotApplicable, match="do not cover every"):
        await generate_template_a2ui(
            _weather_task_spec(),
            _weather_card_spec(),
            (binding,),
            model,
        )

    assert model.body_called is False


@pytest.mark.asyncio
async def test_unused_candidate_fields_do_not_block_query_required_weather_fields():
    model = WeatherTemplateModel(
        (
            "/current/temperatureText",
            "/current/condition",
        )
    )
    binding = CandidateDataBinding(
        capabilityId="ViewWeather",
        arguments={"districtName": "青浦区"},
        writeResultTo="/data/weather",
        candidateOutputFields=[
            *_WEATHER_TEMPLATE_FIELDS,
            "/current/humidityPercent",
            "/current/windDirection",
            "/current/uvIndex",
        ],
    )

    output = await generate_template_a2ui(
        _weather_task_spec(),
        _weather_card_spec(),
        (binding,),
        model,
    )

    assert output.template_ids == ("WeatherOverview@1",)
    assert model.body_called is True


@pytest.mark.asyncio
async def test_query_required_fields_must_come_from_candidates():
    model = WeatherTemplateModel(("/current/airQuality",))
    binding = CandidateDataBinding(
        capabilityId="ViewWeather",
        arguments={"districtName": "青浦区"},
        writeResultTo="/data/weather",
        candidateOutputFields=["/current/condition"],
    )

    with pytest.raises(TemplateRouteNotApplicable, match="selected from candidateOutputFields"):
        await generate_template_a2ui(
            _weather_task_spec(),
            _weather_card_spec(),
            (binding,),
            model,
        )

    assert model.body_called is False


@pytest.mark.asyncio
async def test_edit_skips_template_attempt_and_uses_original_flow(monkeypatch):
    request = _weather_request().model_copy(
        update={"sourceArtifactUrl": "https://artifact.test/source.md"}
    )
    request.model_fields_set.add("sourceArtifactUrl")
    original_response = object()

    class Host:
        async def _generate_widget_card_with_policy(self, *_args: Any, **_kwargs: Any) -> Any:
            return original_response

    async def unexpected_attempt(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("edit must not enter the template attempt")

    monkeypatch.setattr(facade, "_try_generate_template_artifact", unexpected_attempt)
    response = await facade.route_compact_generation(Host(), request, _policy())

    assert response is original_response


@pytest.mark.asyncio
async def test_selected_template_failure_does_not_fallback_to_original(monkeypatch):
    original_called = False

    class Host:
        async def _generate_widget_card_with_policy(self, *_args: Any, **_kwargs: Any) -> Any:
            nonlocal original_called
            original_called = True
            return object()

    async def selected_failure(*_args: Any, **_kwargs: Any) -> Any:
        raise TemplateGenerationError("selected route failed")

    monkeypatch.setattr(facade, "_try_generate_template_artifact", selected_failure)
    response = await facade.route_compact_generation(
        Host(),
        _weather_request(),
        _policy(),
    )

    assert response.status == GenerationStatus.FAILED
    assert original_called is False


@pytest.mark.asyncio
async def test_first_layer_rejection_falls_back_and_notifies_model_start_once(monkeypatch):
    notifications: list[str] = []

    class Host:
        async def _generate_widget_card_with_policy(
            self,
            _request: Any,
            _policy_value: Any,
            *,
            before_model_call: Any,
        ) -> str:
            await before_model_call("2x2")
            await before_model_call("2x2")
            return "original"

    async def rejected(
        _host: Any,
        _request: Any,
        _policy_value: Any,
        notify: Any,
    ) -> Any:
        await notify("2x2")
        raise TemplateRouteNotApplicable("LLM rejected template route")

    async def notify(size: str) -> None:
        notifications.append(size)

    monkeypatch.setattr(facade, "_try_generate_template_artifact", rejected)
    response = await facade.route_compact_generation(
        Host(),
        _weather_request(),
        _policy(),
        before_model_call=notify,
    )

    assert response == "original"
    assert notifications == ["2x2"]


@pytest.mark.asyncio
async def test_terse_template_mismatch_returns_failed_without_original_flow(monkeypatch):
    original_called = False

    class Host:
        async def _generate_widget_card_with_policy(self, *_args: Any, **_kwargs: Any) -> Any:
            nonlocal original_called
            original_called = True
            return object()

    async def rejected(*_args: Any, **_kwargs: Any) -> Any:
        raise TemplateRouteNotApplicable("LLM rejected template route")

    monkeypatch.setattr(facade, "_try_generate_template_artifact", rejected)
    response = await facade.route_terse_nested2_generation(
        Host(),
        _weather_request(),
        _terse_policy(),
    )

    assert response.status == GenerationStatus.FAILED
    assert original_called is False


@pytest.mark.asyncio
async def test_terse_edit_returns_failed_without_template_or_original_flow(monkeypatch):
    request = _weather_request().model_copy(
        update={"sourceArtifactUrl": "https://artifact.test/source.md"}
    )
    request.model_fields_set.add("sourceArtifactUrl")

    class Host:
        async def _generate_widget_card_with_policy(self, *_args: Any, **_kwargs: Any) -> Any:
            pytest.fail("Terse edit must not enter the original flow")

    async def unexpected_attempt(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Terse edit must not attempt template generation")

    monkeypatch.setattr(facade, "_try_generate_template_artifact", unexpected_attempt)
    response = await facade.route_terse_nested2_generation(
        Host(),
        request,
        _terse_policy(),
    )

    assert response.status == GenerationStatus.FAILED
    assert response.errorCode == "A2UI_GENERATION_FAILED"


@pytest.mark.asyncio
async def test_legacy_python_terse_entry_is_explicit_and_delegates_to_original():
    expected = object()
    observed_callback: Any = None

    class Host:
        async def _generate_widget_card_with_policy(
            self,
            _request: Any,
            _policy_value: Any,
            *,
            before_model_call: Any,
        ) -> Any:
            nonlocal observed_callback
            observed_callback = before_model_call
            return expected

    async def notify(_size: str) -> None:
        return None

    response = await route_legacy_python_terse_generation(
        Host(),
        _weather_request(),
        _terse_policy(),
        before_model_call=notify,
    )

    assert response is expected
    assert observed_callback is notify
