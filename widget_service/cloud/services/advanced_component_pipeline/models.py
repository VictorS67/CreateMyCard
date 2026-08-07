"""高级组件选择阶段的稳定数据模型。"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Availability = Literal[
    "available",
    "unavailable",
    "permissionDenied",
    "unsupported",
    "stale",
]
ComponentRole = Literal["hero", "support", "micro"]
Presentation = Literal["compact", "standard", "expanded"]
PrivacyMode = Literal["full", "masked", "hidden"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class DataEnvelope(StrictModel):
    """领域数据的统一可用性包装；零值与无数据保持不同语义。"""

    data: Any = None
    availability: Availability
    updated_at: str | None = Field(default=None, alias="updatedAt")
    source: str | None = None

    @model_validator(mode="after")
    def availability_matches_data(self) -> DataEnvelope:
        if self.availability == "available" and self.data is None:
            raise ValueError("available data must not be null")
        if self.availability in {"unavailable", "permissionDenied", "unsupported"}:
            if self.data is not None:
                raise ValueError("unavailable data must be null")
        return self


class AdvancedComponentCapability(StrictModel):
    name: str
    domain_id: str = Field(alias="domainId")
    description: str
    supported_roles: tuple[ComponentRole, ...] = Field(alias="supportedRoles")
    supported_card_sizes: tuple[Literal["2x2", "2x4"], ...] = Field(alias="supportedCardSizes")
    min_area: Presentation = Field(alias="minArea")
    variants: tuple[str, ...]
    default_variant: str = Field(alias="defaultVariant")
    field_priorities: dict[Literal["mustShow", "preferShow", "expandedOnly"], tuple[str, ...]] = (
        Field(alias="fieldPriorities")
    )
    max_items_by_presentation: dict[Presentation, int] = Field(
        default_factory=dict,
        alias="maxItemsByPresentation",
    )
    supports_primary_action: bool = Field(alias="supportsPrimaryAction")
    supports_primary_chart: bool = Field(alias="supportsPrimaryChart")
    sensitive_fields: tuple[str, ...] = Field(alias="sensitiveFields")
    detection_terms: tuple[str, ...] = Field(alias="detectionTerms")
    variant_terms: dict[str, tuple[str, ...]] = Field(alias="variantTerms")
    local_template_ids: tuple[str, ...] = Field(alias="localTemplateIds")

    @model_validator(mode="after")
    def valid_default_variant(self) -> AdvancedComponentCapability:
        if self.default_variant not in self.variants:
            raise ValueError("defaultVariant must be registered")
        return self


class AdaptiveTemplateSlot(StrictModel):
    name: str
    kind: Literal["advanced", "action"]
    role: ComponentRole | None = None
    required: bool


class AdaptiveTemplateFamily(StrictModel):
    template_id: str = Field(alias="templateId")
    description: str
    supported_card_sizes: tuple[Literal["2x2", "2x4"], ...] = Field(alias="supportedCardSizes")
    slots: tuple[AdaptiveTemplateSlot, ...]
    max_components_by_size: dict[Literal["2x2", "2x4"], int] = Field(alias="maxComponentsBySize")
    supports_primary_action: bool = Field(alias="supportsPrimaryAction")
    supports_primary_chart: bool = Field(alias="supportsPrimaryChart")
    required_data_signals: tuple[str, ...] = Field(alias="requiredDataSignals")


class CardSizeContentBudget(StrictModel):
    size: Literal["2x2", "2x4"]
    recommended_advanced_components: int = Field(
        gt=0,
        alias="recommendedAdvancedComponents",
    )
    max_advanced_components: int = Field(gt=0, alias="maxAdvancedComponents")
    max_primary_actions: int = Field(ge=0, alias="maxPrimaryActions")
    max_action_hit_zones: int = Field(ge=0, alias="maxActionHitZones")
    max_primary_charts: int = Field(ge=0, alias="maxPrimaryCharts")
    max_list_items: int = Field(ge=0, alias="maxListItems")
    max_information_levels: int = Field(gt=0, alias="maxInformationLevels")


class AdvancedComponentAssignment(StrictModel):
    component_id: str
    domain_id: str
    role: ComponentRole
    variant: str
    presentation: Presentation
    privacy_mode: PrivacyMode
    max_items: int | None = Field(default=None, ge=1)
    uses_primary_chart: bool = False
    score: float = Field(ge=0)
    local_template_ids: tuple[str, ...] = ()
    visible_field_keys: tuple[str, ...] = ()


class AdvancedCompositionPlan(StrictModel):
    registry_version: str
    size: Literal["2x2", "2x4"]
    primary_domain: str
    primary_goal: str
    adaptive_template_id: str | None = None
    assignments: tuple[AdvancedComponentAssignment, ...]
    action_count: int = Field(ge=0)
    primary_chart_count: int = Field(ge=0)
    max_list_items: int = Field(ge=0)
    information_levels: int = Field(gt=0)
    data_signals: tuple[str, ...] = ()
    local_template_ids: tuple[str, ...] = ()
    dropped_domain_ids: tuple[str, ...] = ()


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
    theme_id: str | None = Field(default=None, alias="themeId")
    theme_semantics: list[str] = Field(default_factory=list, alias="themeSemantics")
    layout_semantics: list[str] = Field(default_factory=list, alias="layoutSemantics")
    local_template_ids: list[str] = Field(default_factory=list, alias="localTemplateIds")
    action_placement: Literal["auto", "card", "content", "none"] = Field(
        default="auto",
        alias="actionPlacement",
    )
    advanced_component_ids: list[str] = Field(
        default_factory=list,
        alias="advancedComponentIds",
    )
    adaptive_template_id: str | None = Field(default=None, alias="adaptiveTemplateId")
    primary_domain: str | None = Field(default=None, alias="primaryDomain")
    content_priorities: list[str] = Field(alias="contentPriorities", min_length=1)
    reason: str

    @field_validator("purpose", "visual_tone", "reason")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("local_template_ids")
    @classmethod
    def versioned_template_ids(cls, values: list[str]) -> list[str]:
        pattern = re.compile(r"^[a-z][a-z0-9-]{0,63}@[1-9][0-9]*$")
        if len(values) != len(set(values)):
            raise ValueError("localTemplateIds must be unique")
        if any(pattern.fullmatch(value) is None for value in values):
            raise ValueError("localTemplateIds must contain versioned IDs")
        return values


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
    route: Literal["whole-card-template", "hybrid-template"] = "whole-card-template"
    whole_card_confidence: float = 0.0
    whole_card_candidates: list[CandidateScore] = Field(default_factory=list)
    confidence_bypassed: bool = False
    raw_output: str = ""
    effective_output: str = ""
    compiled_a2ui: str = ""
    fallback_used: bool = False
    template_call_count: int = 0
    template_used_ids: list[str] = Field(default_factory=list)
    expanded_component_count: int = 0
    advanced_composition: AdvancedCompositionPlan | None = None
