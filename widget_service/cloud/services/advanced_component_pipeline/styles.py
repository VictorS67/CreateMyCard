"""高级组件的服务端主题 Catalog。"""

from __future__ import annotations

from .models import UIBrief

STYLE_TOKENS: dict[str, dict[str, object]] = {
    "night-violet": {
        "gradient": {
            "direction": "RightBottom",
            "colors": [
                ["#FF2E124D", 0.0],
                ["#FF67299F", 0.34],
                ["#FF6B2BCA", 0.56],
                ["#FF542AC2", 0.76],
                ["#FF7355EA", 1.0],
            ],
        },
        "background": "#FF542AC2",
        "surface": "#3DFFFFFF",
        "surfaceBorder": "#5CFFFFFF",
        "primary": "#FFFFFFFF",
        "secondary": "#D6FFFFFF",
        "accent": "#FFF7CE00",
        "track": "#4D4F2E83",
        "button": "#33FFFFFF",
        "buttonBorder": "#72FFFFFF",
        "danger": "#FFFF5376",
    },
    "warm-copper": {
        "gradient": {
            "direction": "RightBottom",
            "colors": [["#FF8B513F", 0.0], ["#FFC18470", 1.0]],
        },
        "background": "#FFC18470",
        "surface": "#22FFFFFF",
        "surfaceBorder": "#78FFE5D6",
        "primary": "#FFFFFFFF",
        "secondary": "#C8F7E9DF",
        "accent": "#FFFFE300",
        "track": "#38FFFFFF",
        "button": "#18FFFFFF",
        "buttonBorder": "#89FFE9D9",
        "danger": "#FFFF4E64",
    },
    "system-teal": {
        "gradient": {
            "direction": "RightBottom",
            "colors": [["#FF062A42", 0.0], ["#FF08779B", 1.0]],
        },
        "background": "#FF08779B",
        "surface": "#6B143B5B",
        "surfaceBorder": "#35FFFFFF",
        "primary": "#FFFFFFFF",
        "secondary": "#C9E6F3FF",
        "accent": "#FF42D67A",
        "track": "#3CFFFFFF",
        "button": "#80126791",
        "buttonBorder": "#668FD2ED",
        "danger": "#FFFF4770",
        "metricPalette": ["#FF56D880", "#FF67D86F", "#FF39C9A0"],
    },
}


def select_style(brief: UIBrief) -> tuple[str, dict[str, object]]:
    """根据抽象意图选择受控主题，模板不接受模型直接下发的颜色。"""
    text = f"{brief.purpose} {brief.visual_tone}".lower()
    if any(item in text for item in ("schedule", "warm", "focus", "日程")):
        style_id = "warm-copper"
    elif any(item in text for item in ("resource", "technical", "memory", "内存")):
        style_id = "system-teal"
    else:
        style_id = "night-violet"
    return style_id, STYLE_TOKENS[style_id]
