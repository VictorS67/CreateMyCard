"""TerseDSL-Nested-2 的高级组件生成流水线。

该包只服务于 ``generateWidgetCardTerseDslNested2``。它与既有的通用 DSL
生成链路隔离，未选中高级组件时由调用方继续使用原有 Terse 生成流程。
"""

from .compiler import build_terse_nested2
from .pipeline import AdvancedComponentPipeline

__all__ = ["AdvancedComponentPipeline", "build_terse_nested2"]
