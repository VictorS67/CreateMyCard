from __future__ import annotations

import json
import random
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from api.schemas import GenerateWidgetCardRequest
from app.logger import json_for_log
from config.config import Settings, get_settings
from custom.deepseek_call_budget import DeepSeekCallBudget, DeepSeekCallBudgetExceeded
from custom.model_transport import ModelTransportError
from custom.unified_model_client import UnifiedModelClient
from models.generation import EventAction, TaskSpec
from services.advanced_component_pipeline.models import UIBrief
from services.cardplan_template.compiler import (
    _normalize_card_params,
    _normalize_component_values,
    compile_hybrid_card,
)
from services.cardplan_template.framer import HybridCardFramer
from services.cardplan_template.parser import parse_hybrid_card
from services.cardplan_template.prompt import build_hybrid_prompt
from services.cardplan_template.registry import CardPlanRegistry, get_cardplan_registry
from services.protocol_registry import TERSE_DSL_NESTED2_PROFILE_ID, A2UIProtocolRegistry
from services.terse_dsl_nested2_converter import TerseDslNested2ConversionError
from services.widget_generation_service import WidgetGenerationService

SERVICE_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_FIXTURE = SERVICE_ROOT / "tests/fixtures/cardplan_golden_scenarios.json"


def _sample_schema(value: object) -> object:
    if isinstance(value, dict):
        return {key: _sample_schema(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_sample_schema(value[0])] if value else []
    data_type = "boolean" if isinstance(value, bool) else "number"
    if not isinstance(value, (bool, int, float)):
        data_type = "string"
    return {
        "type": data_type,
        "description": "Golden sample",
        "sampleValue": value,
    }


def _scenario_inputs(scenario: dict) -> tuple[TaskSpec, dict, UIBrief]:
    data = {
        item["capabilityId"]: item["dataSlice"]
        for item in scenario["dataEntries"]
    }
    task_spec = TaskSpec(
        userQuery=scenario["userQuery"],
        size=scenario["cardSize"],
        eventCandidates=[
            EventAction(
                id=action_id,
                displayLabel=label,
                call="fixtureAction",
                args={},
            )
            for action_id, label in scenario["eventDisplayLabels"].items()
        ],
        dataModelSchema={"data": _sample_schema(data)},
        assetCandidates=scenario["assets"],
    )
    card_spec = {
        "title": scenario["title"],
        "description": scenario["description"],
        "suggestSize": scenario["cardSize"],
    }
    ui_brief = UIBrief(
        purpose="Golden cross-language regression",
        primaryInformation=[scenario["description"]],
        informationHierarchy=["main", "action"],
        visualTone="fixture-derived",
        themeId=scenario["cardTemplate"]["themeProfileId"],
        themeSemantics=[scenario["cardTemplate"]["themeProfileId"]],
        layoutSemantics=["compact 2x2"],
        localTemplateIds=[
            item
            for item in scenario["cardTemplate"]["requestTemplateIds"]
            if item != "card@1"
        ],
        contentPriorities=["preserve supplied facts"],
        reason="Exercise the Python port against the exported TypeScript baseline.",
    )
    return task_spec, card_spec, ui_brief


def _compile_scenario(scenario: dict):
    task_spec, card_spec, ui_brief = _scenario_inputs(scenario)
    registry = get_cardplan_registry()
    projection = build_hybrid_prompt(
        task_spec=task_spec,
        card_spec=card_spec,
        ui_brief=ui_brief,
        registry=registry,
    )
    profile = A2UIProtocolRegistry.read_design_protocol_profile(
        TERSE_DSL_NESTED2_PROFILE_ID
    )
    result = compile_hybrid_card(
        scenario["rawHybridSource"],
        task_spec=task_spec,
        contract=projection.contract,
        protocol_profile=profile,
        registry=registry,
    )
    return result, task_spec, projection


def test_generated_prompt_bundle_has_no_drift() -> None:
    subprocess.run(
        [sys.executable, "scripts/build_cardplan_bundle.py", "--check"],
        cwd=SERVICE_ROOT,
        check=True,
    )


def test_registry_fails_closed_on_source_sha_drift(tmp_path: Path) -> None:
    source = SERVICE_ROOT / "cloud/data/cardplan_template/source"
    copied = tmp_path / "source"
    shutil.copytree(source, copied)
    registry_path = copied / "template-registry.json"
    registry_path.write_text(registry_path.read_text() + " ", encoding="utf-8")

    with pytest.raises(ValueError, match="file drift"):
        CardPlanRegistry(copied)


def test_all_ten_cross_language_golden_programs_compile_without_template_leak() -> None:
    payload = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    assert len(payload["scenarios"]) == 10
    for scenario in payload["scenarios"]:
        result, _task_spec, projection = _compile_scenario(scenario)
        assert result.fallback_used is False
        assert result.stats.template_call_count >= 2
        assert set(result.stats.template_used_ids) == set(
            projection.requested_template_ids
        )
        assert "Template" not in result.effective_output
        assert "Template" not in result.a2ui
        rows = [json.loads(line) for line in result.a2ui.splitlines()]
        assert [next(key for key in row if key != "version") for row in rows] == [
            "createSurface",
            "updateComponents",
            "updateDataModel",
        ]
        assert all(row["version"] == "v0.9" for row in rows)


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ('__import__("os")', "Unsupported component"),
        ('Template("card@1", {}, Column("section", Text("伪造", "body")));', "trusted"),
        (
            'Template("card@1", {}, Column("section", '
            'Button("打开详情", "primary", {onClick: [{call: "evil", args: {}}]})));',
            "Direct events",
        ),
        (
            'Template("card@1", {}, Column("section", '
            'Template("missing@1", "small", {})));',
            "not allowed",
        ),
    ],
)
def test_illegal_hybrid_inputs_fail_closed(source: str, message: str) -> None:
    scenario = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))["scenarios"][0]
    _result, task_spec, projection = _compile_scenario(scenario)
    profile = A2UIProtocolRegistry.read_design_protocol_profile(
        TERSE_DSL_NESTED2_PROFILE_ID
    )
    with pytest.raises(TerseDslNested2ConversionError, match=message):
        compile_hybrid_card(
            source,
            task_spec=task_spec,
            contract=projection.contract,
            protocol_profile=profile,
            registry=get_cardplan_registry(),
        )


def test_template_schema_and_asset_validation_fail_closed() -> None:
    scenario = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))["scenarios"][0]
    _result, task_spec, projection = _compile_scenario(scenario)
    profile = A2UIProtocolRegistry.read_design_protocol_profile(
        TERSE_DSL_NESTED2_PROFILE_ID
    )
    invalid = scenario["rawHybridSource"].replace(
        '"resources/base/media/ux_golden_asset_time_beige.svg"',
        '"resources/base/media/unapproved.svg"',
    )
    with pytest.raises(TerseDslNested2ConversionError, match="asset is not approved"):
        compile_hybrid_card(
            invalid,
            task_spec=task_spec,
            contract=projection.contract,
            protocol_profile=profile,
            registry=get_cardplan_registry(),
        )


def test_space_overflow_is_bounded_and_reported() -> None:
    payload = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    scenario = next(item for item in payload["scenarios"] if item["id"] == "headset-music")
    result, _task_spec, _projection = _compile_scenario(scenario)
    assert result.stats.space_constrained is True
    assert result.stats.estimated_height_vp > result.stats.vertical_budget_vp
    assert '"clip":true' in result.effective_output


def test_stream_framer_accepts_random_chunks_and_rejects_partial_or_crossed() -> None:
    source = 'Template("card@1", {}, Column("section", Text("A, [B]", "body")));'
    for seed in range(50):
        randomizer = random.Random(seed)
        framer = HybridCardFramer()
        units = []
        offset = 0
        while offset < len(source):
            width = randomizer.randint(1, 7)
            units.extend(framer.push(source[offset : offset + width]))
            offset += width
        assert framer.finish() == source
        assert [unit.source for unit in units if unit.kind == "program"] == [source]

    with pytest.raises(TerseDslNested2ConversionError, match="crossed"):
        HybridCardFramer().push('Template("card@1", {])')
    partial = HybridCardFramer()
    partial.push('Template("card@1", {}, Column(')
    with pytest.raises(TerseDslNested2ConversionError, match="before delimiters closed"):
        partial.finish()


def test_parser_accepts_safe_model_child_array_variant() -> None:
    source = (
        'Template("card@1", {}, Column({layout: "section"}, '
        '[Text("事实", "body")]));'
    )
    parsed = parse_hybrid_card(source)
    content = parsed.children[0]
    assert content.values == ({"layout": "section"},)
    assert [child.name for child in content.children] == ["Text"]


def test_hybrid_prompt_exposes_template_parameter_json_types() -> None:
    scenario = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))["scenarios"][0]
    _result, _task_spec, projection = _compile_scenario(scenario)
    system_prompt = projection.messages[0]["content"]
    assert '"type": "string"' in system_prompt
    assert "看起来像数字的 string 仍需加引号" in system_prompt


def test_ts_layout_design_and_card_icon_aliases_lower_to_existing_adapter() -> None:
    assert _normalize_component_values("Column", ("metric-stack",)) == ("section",)
    assert _normalize_component_values("Row", ("compact",)) == ("between",)
    assert _normalize_component_values("Text", ("42", "metric-hero")) == (
        "42",
        "title",
    )
    assert _normalize_card_params({"icon": "asset.svg"}) == {
        "titleIcon": "asset.svg"
    }
    with pytest.raises(TerseDslNested2ConversionError, match="icon and titleIcon"):
        _normalize_card_params({"icon": "a.svg", "titleIcon": "b.svg"})


def test_budget_is_atomic_across_threads_and_enforces_exact_hard_limit(tmp_path: Path) -> None:
    budget = DeepSeekCallBudget(tmp_path / "concurrent.sqlite3")
    with ThreadPoolExecutor(max_workers=20) as executor:
        statuses = list(executor.map(lambda _: budget.reserve("deepseek_platform"), range(40)))
    assert sorted(item.used for item in statuses) == list(range(1, 41))
    assert budget.status().used == 40

    capped = DeepSeekCallBudget(tmp_path / "capped.sqlite3")
    capped.reserve("deepseek_platform")
    with sqlite3.connect(capped.path) as connection:
        connection.execute("UPDATE budget SET used = 399 WHERE id = 1")
    assert capped.reserve("deepseek_platform").used == 400
    with pytest.raises(DeepSeekCallBudgetExceeded, match="used=400"):
        capped.reserve("deepseek_platform")


@pytest.mark.asyncio
async def test_failed_physical_model_call_still_consumes_budget(tmp_path: Path) -> None:
    class FailingRuntime:
        async def generate_once(self, *_args, **_kwargs):
            raise ModelTransportError("physical failure")

    settings = Settings(
        _env_file=None,
        deepseek_call_budget_path=str(tmp_path / "failed.sqlite3"),
        enable_model_failure_retry=False,
    )
    client = UnifiedModelClient(settings, FailingRuntime(), operation_name="budget-test")
    with pytest.raises(ModelTransportError, match="physical failure"):
        await client.generate("openai", [], None, phase="initial")
    assert client.deepseek_budget.status().used == 1


def test_hybrid_bypass_requires_flag_environment_enablement_and_token(monkeypatch) -> None:
    settings = get_settings()
    request = GenerateWidgetCardRequest(
        uid="test-user",
        device={"romVersion": "6.0"},
        userQuery="测试 Hybrid",
        title="测试",
        description="安全边界",
    )
    assert WidgetGenerationService._authorize_hybrid_bypass(request) is False

    request.options.forceHybridTemplate = True
    request.options.testAuthorization = "expected"
    monkeypatch.setattr(settings, "enable_hybrid_test_bypass", False)
    monkeypatch.setattr(settings, "hybrid_test_bypass_token", "expected")
    monkeypatch.setattr(settings, "env", "test")
    with pytest.raises(ValueError, match="not authorized"):
        WidgetGenerationService._authorize_hybrid_bypass(request)

    monkeypatch.setattr(settings, "enable_hybrid_test_bypass", True)
    monkeypatch.setattr(settings, "env", "production")
    with pytest.raises(ValueError, match="not authorized"):
        WidgetGenerationService._authorize_hybrid_bypass(request)

    monkeypatch.setattr(settings, "env", "test")
    request.options.testAuthorization = "wrong"
    with pytest.raises(ValueError, match="not authorized"):
        WidgetGenerationService._authorize_hybrid_bypass(request)

    request.options.testAuthorization = "expected"
    assert WidgetGenerationService._authorize_hybrid_bypass(request) is True


def test_test_authorization_is_never_serialized_or_logged() -> None:
    request = GenerateWidgetCardRequest(
        uid="test-user",
        device={"romVersion": "6.0"},
        userQuery="测试",
        title="测试",
        description="测试",
        options={"forceHybridTemplate": True, "testAuthorization": "top-secret"},
    )
    dumped = request.model_dump(mode="json")
    logged = json_for_log({"request": request, "authorization": "bearer-secret"})
    assert "testAuthorization" not in json.dumps(dumped)
    assert "top-secret" not in logged
    assert "bearer-secret" not in logged


def test_production_structured_logs_remove_business_payloads(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "env", "production")
    logged = json_for_log(
        {
            "requestId": "request-safe-to-log",
            "request": {
                "userQuery": "private intent",
                "dataSlice": {"medical": "private value"},
            },
            "rawOutput": "private model result",
        }
    )
    assert "request-safe-to-log" in logged
    assert "private intent" not in logged
    assert "private value" not in logged
    assert "private model result" not in logged


def test_ui_brief_rejects_unversioned_or_duplicate_local_templates() -> None:
    base = {
        "purpose": "status",
        "primaryInformation": ["状态"],
        "informationHierarchy": ["状态"],
        "visualTone": "calm",
        "contentPriorities": ["状态"],
        "reason": "测试",
    }
    with pytest.raises(ValueError, match="versioned"):
        UIBrief(**base, localTemplateIds=["weather-summary"])
    with pytest.raises(ValueError, match="unique"):
        UIBrief(**base, localTemplateIds=["weather-summary@1", "weather-summary@1"])
