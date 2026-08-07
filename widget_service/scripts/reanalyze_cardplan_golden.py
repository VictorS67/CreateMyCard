#!/usr/bin/env python3
"""Recompile saved real-model CardPlan evidence without spending model budget."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import json_repair

SERVICE_ROOT = Path(__file__).resolve().parents[1]
CLOUD_ROOT = SERVICE_ROOT / "cloud"
if str(CLOUD_ROOT) not in sys.path:
    sys.path.insert(0, str(CLOUD_ROOT))

from evaluate_cardplan_golden import (  # noqa: E402
    FIXTURE_PATH,
    _a2ui_summary,
    _action_ids_from_a2ui,
    _aggregate_usage,
    _alignment,
    _scenario_inputs,
    _summary,
)

from config.config import get_settings  # noqa: E402
from custom.deepseek_call_budget import DeepSeekCallBudget  # noqa: E402
from services.advanced_component_pipeline.component_selector import (  # noqa: E402
    select_component,
)
from services.advanced_component_pipeline.data_shape import extract_data_shape  # noqa: E402
from services.advanced_component_pipeline.models import (  # noqa: E402
    SelectionConstraints,
    UIBrief,
)
from services.cardplan_template.compiler import compile_hybrid_card  # noqa: E402
from services.cardplan_template.prompt import build_hybrid_prompt  # noqa: E402
from services.cardplan_template.registry import get_cardplan_registry  # noqa: E402
from services.protocol_registry import (  # noqa: E402
    TERSE_DSL_NESTED2_PROFILE_ID,
    A2UIProtocolRegistry,
)


def _sources(paths: list[Path]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    selected: dict[str, dict[str, Any]] = {}
    reports: list[dict[str, Any]] = []
    for path in paths:
        report = json.loads(path.read_text(encoding="utf-8"))
        reports.append(report)
        for scene in report["scenarios"]:
            selected[scene["scenarioId"]] = scene
    return selected, reports


def _reanalyze_scene(
    fixture: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    calls = evidence.get("modelCalls", [])
    result = dict(evidence)
    if len(calls) != 2:
        result.update(
            modelRawProtocolSuccess=False,
            finalReady=False,
            fallback=False,
            goldenAlignment=_failed_alignment(
                evidence.get("goldenAlignment"),
                "Expected exactly two saved model calls.",
            ),
            failureReason="Expected exactly two saved model calls.",
        )
        return result
    try:
        brief_payload = json_repair.loads(calls[0]["raw_output"])
        brief = UIBrief.model_validate(brief_payload)
        task_spec, card_spec = _scenario_inputs(fixture)
        registry = get_cardplan_registry()
        projection = build_hybrid_prompt(
            task_spec=task_spec,
            card_spec=card_spec,
            ui_brief=brief,
            registry=registry,
        )
        profile = A2UIProtocolRegistry.read_design_protocol_profile(TERSE_DSL_NESTED2_PROFILE_ID)
        compilation = compile_hybrid_card(
            calls[1]["raw_output"],
            task_spec=task_spec,
            contract=projection.contract,
            protocol_profile=profile,
            registry=registry,
        )
        selection = select_component(
            extract_data_shape(task_spec),
            brief,
            SelectionConstraints(
                size=task_spec.size,
                action_count=len(task_spec.eventCandidates),
            ),
        )
        actual = _a2ui_summary(compilation.a2ui, ())
        actual["actionIds"] = _action_ids_from_a2ui(compilation.a2ui)
        calls[0]["protocol_success"] = True
        calls[1]["protocol_success"] = True
        result.update(
            uiBrief=brief.model_dump(mode="json", by_alias=True),
            candidateTemplates=list(projection.requested_template_ids),
            wholeCardConfidence=selection.confidence if selection else 0.0,
            wholeCardCandidates=(
                [item.model_dump(mode="json") for item in selection.candidates] if selection else []
            ),
            confidenceBypassed=True,
            route="hybrid-template",
            rawHybridOutput=compilation.raw_output,
            effectiveHybridOutput=compilation.effective_output,
            compiledA2UI=compilation.a2ui,
            modelCalls=calls,
            modelRawProtocolSuccess=True,
            uiBriefFallback=False,
            finalReady=True,
            fallback=False,
            template={
                "callCount": compilation.stats.template_call_count,
                "usedIds": compilation.stats.template_used_ids,
                "expandedComponentCount": compilation.stats.expanded_component_count,
            },
            tokens=_aggregate_usage(calls),
            latencyMs=round(
                float(evidence.get("latencyMs", 0)) + (time.perf_counter() - started) * 1000,
                2,
            ),
            goldenAlignment=_alignment(actual, fixture["goldenSummary"]),
            failureReason="",
        )
    except Exception as exc:
        failure_reason = f"{type(exc).__name__}: {exc}"
        if calls:
            calls[0]["protocol_success"] = bool(calls[0].get("raw_output", "").strip())
        if len(calls) > 1:
            calls[1]["protocol_success"] = False
        result.update(
            modelCalls=calls,
            modelRawProtocolSuccess=False,
            finalReady=False,
            fallback=False,
            goldenAlignment=_failed_alignment(evidence.get("goldenAlignment"), failure_reason),
            failureReason=failure_reason,
        )
    return result


def _failed_alignment(existing: Any, reason: str) -> dict[str, Any]:
    alignment = dict(existing) if isinstance(existing, dict) else {}
    alignment["passed"] = False
    alignment["failureReasons"] = [reason]
    return alignment


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence, reports = _sources(args.input)
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    results = [_reanalyze_scene(scene, evidence[scene["id"]]) for scene in fixture["scenarios"]]
    settings = get_settings()
    budget = DeepSeekCallBudget(
        settings.resolved_deepseek_call_budget_path,
        settings.deepseek_call_budget_limit,
    )
    summary = _summary(results)
    report = {
        "schemaVersion": "cardplan-template-python-evaluation/1",
        "mode": "live-reanalysis",
        "createdAt": datetime.now(UTC).isoformat(),
        "sourceReports": [str(path) for path in args.input],
        "fallbackRequired": False,
        "budgetBefore": reports[0].get("budgetBefore"),
        "budgetAfter": asdict(budget.status()),
        "summary": summary,
        "scenarios": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(args.output, 0o600)
    print(json.dumps({"output": str(args.output), "summary": summary}))
    passed = summary["finalReadyCount"] == len(results)
    no_fallback = summary["fallbackCount"] == 0
    return 0 if passed and no_fallback else 1


if __name__ == "__main__":
    raise SystemExit(main())
