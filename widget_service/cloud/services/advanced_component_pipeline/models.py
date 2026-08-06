"""高级组件选择阶段的稳定数据模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FieldProfile(BaseModel):
    """TaskSpec 中一个叶子字段的语义摘要。"""

    path: str
    name: str
    data_type: str
    description: str = ""
    roles: list[str] = Field(default_factory=list)


class DataShape(BaseModel):
    """供确定性组件选择使用的数据形状，不包含实际业务数据。"""

    numeric_count: int = 0
    text_count: int = 0
    collection_count: int = 0
    metric_count: int = 0
    duration_count: int = 0
    time_range_count: int = 0
    percentage_count: int = 0
    action_count: int = 0
    repeated_metric_group_count: int = 0
    fields: list[FieldProfile] = Field(default_factory=list)


class UIBrief(BaseModel):
    """第一轮模型输出的抽象视觉意图，不能包含组件或布局实现细节。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    purpose: str
    primary_information: list[str] = Field(alias="primaryInformation", min_length=1)
    information_hierarchy: list[str] = Field(alias="informationHierarchy", min_length=1)
    density: Literal["sparse", "normal", "compact"] = "normal"
    temporality: Literal["now", "upcoming", "historical", "timeless"] = "now"
    interaction: Literal["none", "one-primary-action", "multiple-actions"] = "one-primary-action"
    attention: Literal["normal", "prominent", "warning-capable", "urgent"] = "normal"
    visual_tone: str = Field(alias="visualTone")
    content_priorities: list[str] = Field(alias="contentPriorities", min_length=1)
    reason: str

    @field_validator("purpose", "visual_tone", "reason")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()


class SelectionConstraints(BaseModel):
    size: Literal["2x2", "2x4"]
    action_count: int


class ComponentSpec(BaseModel):
    component_id: str
    description: str
    supported_sizes: list[str]
    required_signals: dict[str, float] = Field(default_factory=dict)
    preferred_signals: dict[str, float] = Field(default_factory=dict)
    min_actions: int = 0
    max_actions: int = 1


class CandidateScore(BaseModel):
    component_id: str
    score: float
    matched: list[str] = Field(default_factory=list)
    penalties: list[str] = Field(default_factory=list)


class ComponentSelection(BaseModel):
    component_id: str
    confidence: float
    candidates: list[CandidateScore]


class BindingRef(BaseModel):
    path: str
    fallback: Any = None

    @field_validator("path")
    @classmethod
    def valid_pointer(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("binding path must be a JSON Pointer")
        return value


class ActionRef(BaseModel):
    event_id: str
    label: str = Field(min_length=1)
    icon: str | None = None


class AdvancedPipelineOutput(BaseModel):
    component_id: str
    style_id: str
    source_dsl: str
    source_format: Literal["terse", "a2ui"]
    ui_brief: UIBrief
    invocation: dict[str, Any]
    planner_mode: Literal["llm", "offline"]
    mapper_mode: Literal["llm", "offline"]
