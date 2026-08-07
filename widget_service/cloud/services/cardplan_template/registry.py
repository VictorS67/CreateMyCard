"""Read-only registry and manifest validation for trusted CardPlan assets."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from config.config import get_settings

from .models import TemplateDefinition, TemplateVariant, ThemeDefinition

_WIRE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}@[1-9][0-9]*$")
_FORBIDDEN_KEYS = frozenset({"__proto__", "prototype", "constructor"})


class CardPlanRegistry:
    """Load the generated TypeScript baseline and fail closed on drift."""

    def __init__(self, source_root: Path | None = None) -> None:
        settings = get_settings()
        self.source_root = source_root or (
            settings.data_root / "cardplan_template" / "source"
        )
        generated_root = Path(__file__).with_name("generated")
        self.manifest_path = generated_root / "prompt-manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self._verify_manifest()
        template_payload = self._load_json("template-registry.json")
        theme_payload = self._load_json("theme-profiles.json")
        if template_payload.get("registryVersion") != "terse-template-registry/0.7":
            raise ValueError("unsupported CardPlan Template Registry version")
        templates = tuple(
            TemplateDefinition.model_validate(item)
            for item in template_payload.get("templates", [])
        )
        themes = tuple(
            ThemeDefinition.model_validate(item) for item in theme_payload.get("themes", [])
        )
        self.templates = self._unique_by_wire_id(templates)
        self.themes = self._unique_themes(themes)

    def _load_json(self, relative_path: str) -> dict[str, Any]:
        value = json.loads((self.source_root / relative_path).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"CardPlan source must be an object: {relative_path}")
        self._reject_forbidden_keys(value)
        return value

    def _verify_manifest(self) -> None:
        if self.manifest.get("catalogId") != "ohos.a2ui.extended.catalog":
            raise ValueError("CardPlan bundle Catalog mismatch")
        if self.manifest.get("a2uiWireVersion") != "v0.9":
            raise ValueError("CardPlan bundle wire version mismatch")
        files = self.manifest.get("files")
        if not isinstance(files, dict):
            raise ValueError("CardPlan bundle file manifest is missing")
        for relative_path, expected in files.items():
            path = self.source_root / relative_path
            if not path.is_file():
                raise ValueError(f"CardPlan bundle file is missing: {relative_path}")
            actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise ValueError(f"CardPlan bundle file drift: {relative_path}")

    @staticmethod
    def _reject_forbidden_keys(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in _FORBIDDEN_KEYS:
                    raise ValueError(f"forbidden CardPlan source key: {key}")
                CardPlanRegistry._reject_forbidden_keys(child)
        elif isinstance(value, list):
            for child in value:
                CardPlanRegistry._reject_forbidden_keys(child)

    @staticmethod
    def _unique_by_wire_id(
        templates: tuple[TemplateDefinition, ...],
    ) -> dict[str, TemplateDefinition]:
        result: dict[str, TemplateDefinition] = {}
        for definition in templates:
            if not _WIRE_ID_RE.fullmatch(definition.wire_id):
                raise ValueError(f"invalid Template wire ID: {definition.wire_id}")
            if definition.wire_id in result:
                raise ValueError(f"duplicate Template wire ID: {definition.wire_id}")
            variant_names = [item.size for item in definition.variants]
            if len(variant_names) != len(set(variant_names)):
                raise ValueError(f"duplicate Template variant: {definition.wire_id}")
            result[definition.wire_id] = definition
        return result

    @staticmethod
    def _unique_themes(themes: tuple[ThemeDefinition, ...]) -> dict[str, ThemeDefinition]:
        result: dict[str, ThemeDefinition] = {}
        for theme in themes:
            if theme.theme_profile_id in result:
                raise ValueError(f"duplicate CardPlan theme: {theme.theme_profile_id}")
            result[theme.theme_profile_id] = theme
        return result

    def require_template(self, wire_id: str) -> TemplateDefinition:
        if not _WIRE_ID_RE.fullmatch(wire_id):
            raise ValueError(f"invalid Template wire ID: {wire_id}")
        try:
            return self.templates[wire_id]
        except KeyError as exc:
            raise ValueError(f"unknown Template: {wire_id}") from exc

    def require_variant(self, wire_id: str, size: str) -> TemplateVariant:
        definition = self.require_template(wire_id)
        for variant in definition.variants:
            if variant.size == size:
                return variant
        raise ValueError(f"unknown Template variant: {wire_id}/{size}")

    def require_theme(self, theme_id: str) -> ThemeDefinition:
        try:
            return self.themes[theme_id]
        except KeyError as exc:
            raise ValueError(f"unknown CardPlan theme: {theme_id}") from exc


@lru_cache(maxsize=1)
def get_cardplan_registry() -> CardPlanRegistry:
    return CardPlanRegistry()
