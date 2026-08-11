"""高级组件的确定性选择器。"""

from __future__ import annotations

import math

from .component_registry import component_specs
from .models import CandidateScore, ComponentSelection, DataShape, SelectionConstraints, UIBrief


def _signals(
    data_shape: DataShape,
    brief: UIBrief,
    constraints: SelectionConstraints,
) -> dict[str, float]:
    return {
        "metrics": min(1.0, data_shape.metric_count / 3.0),
        "duration": min(1.0, data_shape.duration_count / 2.0),
        "time-range": float(data_shape.time_range_count > 0),
        "percentage": min(1.0, float(data_shape.percentage_count)),
        "repeated-metrics": float(data_shape.repeated_metric_group_count > 0),
        "action": float(constraints.action_count > 0),
        "monitoring-intent": float(brief.scenario == "resource-monitoring"),
        "schedule-intent": float(brief.domain == "schedule"),
    }


def _overlap_score(actual: list[str], expected: list[str], weight: float) -> float:
    if not expected:
        return 0.0
    return weight * len(set(actual) & set(expected)) / len(set(expected))


def _semantic_score(brief: UIBrief, spec) -> float:
    score = 0.0
    if brief.domain in spec.domains:
        score += 6.0
    if brief.scenario in spec.scenarios:
        score += 7.0
    score += _overlap_score(brief.status_semantics, spec.status_semantics, 4.0)
    score += _overlap_score(brief.content_semantics, spec.content_semantics, 5.0)
    score += _overlap_score(brief.action_semantics, spec.action_semantics, 5.0)
    if brief.temporality in spec.temporalities:
        score += 2.0
    return score


def select_component(
    data_shape: DataShape,
    brief: UIBrief,
    constraints: SelectionConstraints,
) -> ComponentSelection | None:
    """选择得分达到阈值的组件；无法可靠选择时返回 ``None`` 以回退原链路。"""
    signals = _signals(data_shape, brief, constraints)
    candidates: list[CandidateScore] = []
    for spec in component_specs():
        score = 0.0
        matched: list[str] = []
        penalties: list[str] = []
        if constraints.size not in spec.supported_sizes:
            score -= 100.0
            penalties.append("unsupported-size")
        if constraints.action_count < spec.min_actions:
            score -= 100.0
            penalties.append("missing-required-action")
        if constraints.asset_count < spec.min_assets:
            score -= 100.0
            penalties.append("missing-required-asset")
        if len(data_shape.fields) < spec.min_fields:
            score -= 100.0
            penalties.append("missing-required-fields")
        for role, required_count in spec.required_field_roles.items():
            actual_count = sum(role in field.roles for field in data_shape.fields)
            if actual_count < required_count:
                score -= 100.0
                penalties.append(f"missing-field-role:{role}")
        semantic_score = _semantic_score(brief, spec)
        if semantic_score < spec.min_semantic_score:
            score -= 100.0
            penalties.append("semantic-profile-mismatch")
        else:
            score += semantic_score
            if semantic_score:
                matched.append(f"semantic={semantic_score:.2f}")
        for signal, weight in spec.required_signals.items():
            value = signals.get(signal, 0.0)
            if value == 0.0:
                score -= abs(weight) * 1.5
                penalties.append(f"missing:{signal}")
            else:
                score += value * weight
                matched.append(f"{signal}={value:.2f}")
        for signal, weight in spec.preferred_signals.items():
            value = signals.get(signal, 0.0)
            if value:
                score += value * weight
                matched.append(f"{signal}={value:.2f}")
        candidates.append(
            CandidateScore(
                component_id=spec.component_id,
                score=round(score, 4),
                matched=matched,
                penalties=penalties,
            )
        )
    candidates.sort(key=lambda item: (-item.score, item.component_id))
    if not candidates or candidates[0].score < 1.0:
        return None
    margin = candidates[0].score - (candidates[1].score if len(candidates) > 1 else 0.0)
    confidence = 1.0 / (1.0 + math.exp(-margin / 2.5))
    return ComponentSelection(
        component_id=candidates[0].component_id,
        confidence=round(confidence, 4),
        candidates=candidates,
    )
