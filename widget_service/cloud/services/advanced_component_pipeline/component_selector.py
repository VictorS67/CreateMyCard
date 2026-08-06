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
    purpose = brief.purpose.lower()
    return {
        "metrics": min(1.0, data_shape.metric_count / 3.0),
        "duration": min(1.0, data_shape.duration_count / 2.0),
        "time-range": float(data_shape.time_range_count > 0),
        "percentage": min(1.0, float(data_shape.percentage_count)),
        "repeated-metrics": float(data_shape.repeated_metric_group_count > 0),
        "action": float(constraints.action_count > 0),
        "monitoring-intent": float(
            any(word in purpose for word in ("monitor", "resource", "status"))
        ),
        "schedule-intent": float(
            any(word in purpose for word in ("schedule", "appointment", "event"))
        ),
    }


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
            score -= 8.0
            penalties.append("missing-required-action")
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
