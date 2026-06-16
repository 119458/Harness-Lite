"""
DynamicContextManager：兼容旧调用栈的薄封装，内部委派给 CompactPipeline。

保留类名与方法签名 100% 不变，避免上游 strategy.py / loop 模块大改。
新代码请直接使用 CompactPipeline；本类仅作为兼容层存在。
"""
import logging
from typing import Any, Dict, List

from harness_lite.context.compact.pipeline import CompactPipeline
from harness_lite.context.compact.types import TokenCounter

logger = logging.getLogger("harness_lite.context")


class DynamicContextManager:
    """5 层渐进式上下文管理（兼容旧接口）。"""

    def __init__(self, max_allowed_tokens: int = 128_000, model_name: str = "gpt-4-mini"):
        self.max_allowed_tokens = max_allowed_tokens
        self.model_name = model_name
        self._pipeline = CompactPipeline(
            max_allowed_tokens=max_allowed_tokens,
            token_counter=TokenCounter(model_name=model_name),
        )

    @property
    def pipeline(self) -> CompactPipeline:
        return self._pipeline

    def calculate_string_tokens(self, text: str) -> int:
        return self._pipeline.token_counter.count_string(text)

    def calculate_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        return self._pipeline.calculate_messages_tokens(messages)

    async def compress_if_overflow(
        self,
        messages: List[Dict[str, Any]],
        engine: Any,
        current_cwd: str,
        status_callback: Any = None,
    ) -> List[Dict[str, Any]]:
        return await self._pipeline.compress_if_overflow(
            messages,
            engine=engine,
            current_cwd=current_cwd,
            status_callback=status_callback,
        )
