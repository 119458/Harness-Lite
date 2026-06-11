"""
Harness-Lite 系统提示词组装包。

对外仅暴露三个符号：
- PromptBuilder：分层组装入口
- PromptContext：渲染所需的上下文数据载体
- DYNAMIC_BOUNDARY：静态/动态分隔标记
"""

from harness_lite.prompt.builder import DYNAMIC_BOUNDARY, PromptBuilder
from harness_lite.prompt.context import PromptContext

__all__ = ["PromptBuilder", "PromptContext", "DYNAMIC_BOUNDARY"]
