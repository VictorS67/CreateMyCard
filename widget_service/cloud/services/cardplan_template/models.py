"""Strict immutable models for the trusted Python CardPlan implementation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSpan(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class HybridLimits(StrictModel):
    max_raw_components: int = Field(gt=0)
    max_expanded_components: int = Field(gt=0)
    max_nesting_depth: int = Field(gt=0)
    vertical_budget_vp: int = Field(gt=0)


class ActionBinding(StrictModel):
    action_id: str
    display_label: str
    call: str
    args: dict[str, Any]
    importance: Literal["primary", "secondary"] = "primary"
    material_hint: Literal[
        "frosted", "brand-solid", "semantic-solid", "icon-control"
    ] = "frosted"


class HybridBodyContract(StrictModel):
    contract_version: Literal["hybrid-body-contract/0.4"] = "hybrid-body-contract/0.4"
    theme_profile_id: str
    allowed_components: tuple[str, ...]
    allowed_design_tokens: tuple[str, ...]
    allowed_layout_tokens: tuple[str, ...]
    allowed_template_ids: tuple[str, ...]
    allowed_asset_sources: tuple[str, ...]
    required_asset_sources: tuple[str, ...] = ()
    trusted_literals: tuple[str, ...]
    trusted_numbers: tuple[int | float, ...]
    required_literals: tuple[str, ...]
    protected_literals: tuple[str, ...]
    action_bindings: tuple[ActionBinding, ...] = ()
    limits: HybridLimits


class TemplateValue(StrictModel):
    kind: Literal["literal", "parameter", "array", "object"]
    value: str | int | float | bool | None = None
    name: str | None = None
    items: tuple[TemplateValue, ...] = ()
    properties: dict[str, TemplateValue] = Field(default_factory=dict)


class TemplateNode(StrictModel):
    component: str
    values: tuple[TemplateValue, ...] = ()
    children: tuple[TemplateNode, ...] = ()


class TemplateVariant(StrictModel):
    size: Literal["small", "medium", "hero", "hero-large"]
    parameters_schema: dict[str, Any] = Field(alias="parametersSchema")
    root: TemplateNode
    expanded_node_budget: int = Field(alias="expandedNodeBudget", gt=0)
    expanded_depth_budget: int = Field(alias="expandedDepthBudget", gt=0)


class RecommendedVariantLayout(StrictModel):
    inline_sizes: tuple[str, ...] = Field(alias="inlineSizes")
    full_width_sizes: tuple[str, ...] = Field(alias="fullWidthSizes")
    max_inline_items: int = Field(alias="maxInlineItems", gt=0)
    inline_layout_token: str = Field(alias="inlineLayoutToken")


class TemplateDefinition(StrictModel):
    template_id: str = Field(alias="templateId")
    version: int = Field(gt=0)
    description: str
    domain_tags: tuple[str, ...] = Field(alias="domainTags")
    compatible_theme_profile_ids: tuple[str, ...] = Field(
        alias="compatibleThemeProfileIds"
    )
    recommended_container_layout_token: str | None = Field(
        default=None,
        alias="recommendedContainerLayoutToken",
    )
    recommended_variant_order: tuple[str, ...] | None = Field(
        default=None,
        alias="recommendedVariantOrder",
    )
    recommended_variant_layout: RecommendedVariantLayout | None = Field(
        default=None,
        alias="recommendedVariantLayout",
    )
    allowed_parent_components: tuple[str, ...] = Field(alias="allowedParentComponents")
    action_policy: Literal["none", "optional", "required"] = Field(alias="actionPolicy")
    supported_sizes: tuple[str, ...] = Field(alias="supportedSizes")
    allowed_design_tokens: tuple[str, ...] = Field(alias="allowedDesignTokens")
    allowed_layout_tokens: tuple[str, ...] = Field(alias="allowedLayoutTokens")
    asset_parameter_semantic_tags: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        alias="assetParameterSemanticTags",
    )
    variants: tuple[TemplateVariant, ...]

    @property
    def wire_id(self) -> str:
        return f"{self.template_id}@{self.version}"


class ThemeDefinition(StrictModel):
    theme_profile_id: str = Field(alias="themeProfileId")
    description: str
    supported_capability_ids: tuple[str, ...] = Field(alias="supportedCapabilityIds")
    surface_role: str = Field(alias="surfaceRole")
    primary_color_role: str = Field(alias="primaryColorRole")
    text_role: str = Field(alias="textRole")
    spacing_scale: str = Field(alias="spacingScale")
    radius_scale: str = Field(alias="radiusScale")
    density: Literal["sparse", "normal"]
    root_component: Literal["Column", "Stack"] = Field(alias="rootComponent")
    root_styles: dict[str, Any] = Field(alias="rootStyles")


class TemplateCall(StrictModel):
    template_id: str
    size: str
    params: dict[str, Any]
    span: SourceSpan


class CardComposition(StrictModel):
    card_params: dict[str, Any]
    content: Any
    span: SourceSpan


class ExpansionStats(StrictModel):
    template_call_count: int = 0
    template_used_ids: tuple[str, ...] = ()
    expanded_component_count: int = 0
    raw_component_count: int = 0
    max_depth: int = 0
    estimated_height_vp: int = 0
    vertical_budget_vp: int = 0
    space_constrained: bool = False
    action_used_ids: tuple[str, ...] = ()


class Fact(StrictModel):
    source: str
    path: str
    value: str | int | float | bool | None

    @field_validator("path")
    @classmethod
    def pointer_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("fact path must be a JSON Pointer")
        return value
