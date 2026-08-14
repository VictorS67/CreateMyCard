# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import json
import os

from anyio import to_thread

from app.logger import logger
from config.config import get_settings
from models.artifact import WidgetArtifact
from models.service import ArtifactSaveResult
from services.source_artifact_repository import calculate_artifact_digest
from utils.file import delete_file, save_txt_file
from utils.upload_file_obs import UploadFileOSMS

_MODULE = "[Artifact Store]"

file_obs = UploadFileOSMS()


def _compute_10_percent_opacity(color: str) -> str | None:
    """Compute 10% opacity version of an ARGB color string.

    Args:
        color: ARGB color string like "#FF64BB5C" or "#AARRGGBB"

    Returns:
        Color string with 10% opacity (alpha = 0x19), or None if invalid format.
        10% of 255 ≈ 38 ≈ 0x26, but using 0x19 (25/255 ≈ 9.8%) for consistency.
    """
    if not color or not isinstance(color, str):
        return None
    color = color.strip()
    if not color.startswith("#") or len(color) != 9:
        return None
    # Extract RGB part (last 6 characters)
    rgb = color[3:]
    return f"#19{rgb}"


def _fix_bluetooth_button_background(genui: str, card_spec: dict) -> str:
    """Fix button background color for Bluetooth device cards.

    For cards with GetEarphoneInfo data binding, this function:
    1. Finds Stack components with onClick handlers (buttons)
    2. Gets the fontColor from Text components inside the button
    3. Computes 10% opacity of that fontColor
    4. Applies it as the button's backgroundColor

    This simulates runtime behavior where background color is computed from text color.

    Args:
        genui: The A2UI genui string with JSON objects
        card_spec: The cardSpec dict which may contain dataBindings
    """
    # Check if this is a bluetooth-related card
    has_bluetooth_binding = False
    data_bindings = card_spec.get("dataBindings", [])
    for binding in data_bindings:
        if binding.get("capabilityId") == "GetEarphoneInfo":
            has_bluetooth_binding = True
            break

    if not has_bluetooth_binding:
        logger.info(f"{_MODULE} bluetooth_button_background_skipped reason=no_GetEarphoneInfo_binding")
        return genui

    logger.info(f"{_MODULE} bluetooth_button_background_processing reason=GetEarphoneInfo_binding_found")

    # The genui contains multiple JSON objects, each on its own line
    # Format: {"version":"v0.9","createSurface":{...}}
    #         {"version":"v0.9","updateComponents":{...}}
    #         {"version":"v0.9","updateDataModel":{...}}
    lines = genui.strip().split('\n')
    modified_lines = []
    modified = False

    for line in lines:
        try:
            obj = json.loads(line)
            if obj.get("version") == "v0.9" and "updateComponents" in obj:
                # Parse and modify the components
                update_data = obj["updateComponents"]
                if "components" in update_data:
                    new_components = _fix_components_button_background(update_data["components"])
                    if new_components != update_data["components"]:
                        modified = True
                        update_data["components"] = new_components
            modified_lines.append(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        except json.JSONDecodeError:
            # Keep lines that aren't valid JSON as-is
            modified_lines.append(line)

    if modified:
        logger.info(f"{_MODULE} bluetooth_button_background_fixed applied=true")
        return '\n'.join(modified_lines)
    else:
        logger.info(f"{_MODULE} bluetooth_button_background_fixed applied=false (no Stack+onClick buttons found)")
        return '\n'.join(modified_lines)


def _build_component_lookup(components: list) -> dict:
    """Build a lookup map of component ID → component object."""
    lookup = {}
    for comp in components:
        if isinstance(comp, dict) and "id" in comp:
            lookup[comp["id"]] = comp
    return lookup


def _resolve_and_find_font_color(children_ids: list, lookup: dict) -> str | None:
    """Resolve children IDs to components and find the first Text's fontColor."""
    for child_id in children_ids:
        if not isinstance(child_id, str):
            continue
        comp = lookup.get(child_id)
        if not isinstance(comp, dict):
            continue
        # If this is a Text component, return its fontColor
        if comp.get("component") == "Text":
            styles = comp.get("styles", {})
            if isinstance(styles, dict):
                font_color = styles.get("fontColor")
                if font_color:
                    return font_color
        # If this component has children, recurse
        children = comp.get("children")
        if isinstance(children, list) and children:
            result = _resolve_and_find_font_color(children, lookup)
            if result:
                return result
    return None


def _fix_components_button_background(components: list) -> list:
    """Fix button (Stack with onClick) background color using ID-based resolution.

    In A2UI genui, children are referenced by ID strings, not direct objects.
    This function:
    1. Builds a lookup map of ID → component
    2. Finds Stack components with onClick (buttons)
    3. Resolves children IDs to find Text components and their fontColor
    4. Computes 10% opacity of fontColor and applies as backgroundColor
    """
    # Build lookup map for ID-based resolution
    lookup = _build_component_lookup(components)
    result = []
    modified_any = False

    for comp in components:
        if isinstance(comp, dict):
            comp_copy = dict(comp)
            # Check if this is a Stack with onClick (button)
            if comp_copy.get("component") == "Stack" and "onClick" in comp_copy:
                # Get children IDs (strings like ["root_0_1_0"])
                children_ids = comp_copy.get("children", [])
                if isinstance(children_ids, list) and children_ids:
                    # Find fontColor by resolving IDs
                    font_color = _resolve_and_find_font_color(children_ids, lookup)
                    if font_color:
                        bg_color = _compute_10_percent_opacity(font_color)
                        if bg_color:
                            styles = comp_copy.get("styles", {})
                            if isinstance(styles, dict):
                                styles_copy = dict(styles)
                                old_bg = styles_copy.get("backgroundColor", "none")
                                styles_copy["backgroundColor"] = bg_color
                                comp_copy["styles"] = styles_copy
                                modified_any = True
                                logger.info(
                                    f"{_MODULE} button_background_updated "
                                    f"fontColor={font_color} oldBackground={old_bg} newBackground={bg_color}"
                                )
            result.append(comp_copy)
        else:
            result.append(comp)
    return result


class ArtifactStore:
    def __init__(self, design_token: str | None = None) -> None:
        """接收第四、第五接口最终模型源输出，两个接口沿用同一 artifact 块名。"""
        self.design_token = design_token

    async def save(self, artifact: WidgetArtifact) -> ArtifactSaveResult:
        """保存 artifact 并返回访问地址和摘要。

        入参：
        - artifact：完整卡片产物。
        出参：artifact 保存结果，包含访问 URL 和 sha256 摘要。
        """
        artifact_data = artifact.model_dump(mode="json", exclude_none=True)
        payload_bytes = len(
            json.dumps(
                artifact_data,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest = calculate_artifact_digest(artifact)
        logger.info(
            f"{_MODULE} artifact_payload_built "
            f"payload_bytes={payload_bytes} digest={digest}"
        )

        # Artifact 以具名 Markdown 代码块上传。每个块名与对应契约字段一致，
        # 既保留端侧现有的 genui/cardspec 解析方式，也完整携带排障和回放信息。
        json_blocks = {
            "cardspec": artifact_data["cardSpec"],
            "schema": {"schemaVersion": artifact_data["schemaVersion"]},
            "taskspec": artifact_data["taskSpec"],
            "effectivecapabilities": artifact_data["effectiveCapabilities"],
            "removedcapabilities": artifact_data["removedCapabilities"],
            "generationplan": artifact_data["generationPlan"],
            "meta": artifact_data["meta"],
        }

        # Post-process: Fix button background color for bluetooth cards
        # dataBindings is in cardSpec, not taskSpec
        processed_genui = _fix_bluetooth_button_background(
            artifact.genui,
            artifact_data.get("cardSpec", {})
        )

        blocks = [
            "```cardspec\n"
            + json.dumps(json_blocks["cardspec"], ensure_ascii=False, indent=2)
            + "\n```",
            f"```genui\n{processed_genui}\n```",
        ]
        blocks.extend(
            "```" + name + "\n"
            + json.dumps(value, ensure_ascii=False, indent=2)
            + "\n```"
            for name, value in json_blocks.items()
            if name != "cardspec"
        )
        if self.design_token is not None:
            blocks.append(f"```designcompactdsl\n{self.design_token}\n```")
        file_content = "\n".join(blocks) + "\n"

        # UUID 同时进入 meta 和对象名，避免毫秒时间戳在并发生成时发生覆盖。
        file_name = f"artifact_{artifact.meta.artifactId}.md"
        file_path = os.path.join(str(get_settings().WORKSPACE_ROOT), file_name)
        await to_thread.run_sync(save_txt_file, file_path, file_content)
        logger.info(f"{_MODULE} artifact_file_saved path={file_path}")

        try:
            # 上传到 OBS，获取访问链接
            artifact_url = await file_obs.upload_file(file_path)
            if not artifact_url:
                raise RuntimeError("artifact upload to OBS failed")
            logger.info(f"{_MODULE} artifact_uploaded artifact_url={artifact_url}")
            return ArtifactSaveResult(artifactUrl=artifact_url, artifactDigest=digest)
        finally:
            # 清理本地临时文件
            await to_thread.run_sync(delete_file, file_path)
