"""L5 全量结构化摘要层。

- 切分：[system 锚点 | 可压缩区 | 活动轮（最后一条 role=user 起的尾段）]
- 9 段中文模板强制 LLM 不丢失关键信息
- 熔断：连续 3 次失败后降级为「保 system + 活动轮」
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from openai import AsyncOpenAI

from harness_lite.config.loader import get_main_config
from harness_lite.context.compact.local_layers import _validate_pairs
from harness_lite.context.compact.prompts import (
    AUTO_COMPACT_PROMPT_ZH,
    parse_summary_block,
)
from harness_lite.context.compact.types import CompactionResult, TokenCounter

logger = logging.getLogger("harness_lite.compact")

AUTOCOMPACT_BUFFER_TOKENS = 13_000          # 与 autoCompact.ts:62 对齐
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3    # 与 autoCompact.ts:70 对齐
LLM_SUMMARIZE_TIMEOUT_SECONDS = 60.0
ARCHIVE_PREFIX = "⚙️ [系统历史会话结构化归档]"
DEGRADED_PREFIX = "⚠️ [系统历史会话已紧急截断]"


class AutoCompactLayer:
    """L5：调 LLM 做结构化全量摘要的最后一层。

    阈值/熔断逻辑参考上游 autoCompact.ts 实现，保持像素级语义对齐。
    """

    def __init__(self, token_counter: TokenCounter):
        self.token_counter = token_counter
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # 触发条件
    # ------------------------------------------------------------------

    def should_apply(
        self,
        current_tokens: int,
        max_allowed: int,
        snip_freed_tokens: int,
    ) -> bool:
        """阈值判断 + 熔断短路。

        - 已熔断（连续失败 >= 3）→ 永远 False
        - 折算后剩余 token 超过 (max_allowed - BUFFER) → True
        """
        if self._consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            return False
        effective = current_tokens - snip_freed_tokens
        return effective > (max_allowed - AUTOCOMPACT_BUFFER_TOKENS)

    # ------------------------------------------------------------------
    # 切分辅助
    # ------------------------------------------------------------------

    @staticmethod
    def find_last_user_index(messages: List[Dict[str, Any]]) -> int:
        """从尾向前找 role==user 的索引；找不到返回 max(1, len-1)。"""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                return i
        return max(1, len(messages) - 1)

    @staticmethod
    def _enforce_pair_integrity(
        compressible: List[Dict[str, Any]],
        active_turn: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """确保 compressible 末尾不留下「孤立 assistant.tool_calls」。

        若 compressible 末尾的 assistant 工具请求与其某个 tool 响应不在同段内，
        把这条 assistant 弹出并 prepend 到 active_turn 头部，循环直到通过校验。
        """
        comp = list(compressible)
        active = list(active_turn)

        while comp and not _validate_pairs(comp):
            tail = comp.pop()
            active.insert(0, tail)

        return comp, active

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def apply(
        self,
        messages: List[Dict[str, Any]],
        *,
        engine: Any,
        current_cwd: str,
        status_callback: Optional[Callable[[str], None]] = None,
        force: bool = False,
    ) -> CompactionResult:
        """对 messages 执行结构化摘要；force=True 时绕过阈值检查。"""
        # 阈值/熔断检查（force 跳过）
        if not force:
            current_tokens = self.token_counter.count_messages(messages)
            # snip_freed 由 pipeline 维护，这里默认 0；pipeline 调用前会自行判断
            if not self.should_apply(current_tokens, max_allowed=10**9, snip_freed_tokens=0):
                # 上游 pipeline 已正确判断；本兜底仅用于直接调用场景
                pass

        if len(messages) < 4:
            return CompactionResult(
                success=True, skipped=True, layer="L5", reason="对话太短",
            )

        system_anchor = messages[0]
        split = self.find_last_user_index(messages)
        if split <= 1:
            return CompactionResult(
                success=True, skipped=True, layer="L5", reason="无可压缩区间",
            )

        compressible = messages[1:split]
        active_turn = messages[split:]
        compressible, active_turn = self._enforce_pair_integrity(
            compressible, active_turn,
        )

        if not compressible:
            return CompactionResult(
                success=True, skipped=True, layer="L5", reason="可压缩区为空",
            )

        # 调 LLM 摘要
        try:
            if status_callback:
                status_callback("[📦 L5 摘要中] 正在调用 LLM 结构化压缩历史...")
            raw_summary = await self._call_llm_summarize(compressible, current_cwd)
        except Exception as exc:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
            logger.warning(
                "AutoCompactLayer 调 LLM 失败 (第 %d 次): %s", failures, exc,
            )
            if failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES and force:
                return self._build_degraded_result(
                    system_anchor, compressible, active_turn, messages,
                )
            reason = (
                f"LLM 失败 {failures} 次, 已熔断"
                if failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
                else f"LLM 失败 {failures} 次"
            )
            return CompactionResult(
                success=True, skipped=True, layer="L5", reason=reason,
            )

        # 解析 + 重置失败计数
        structured = parse_summary_block(raw_summary)
        self._consecutive_failures = 0

        archive = {
            "role": "system",
            "content": (
                f"{ARCHIVE_PREFIX}\n\n"
                f"{structured}\n\n"
                f"📍 [当前终端内核状态] CWD={current_cwd}\n"
                f"🗂️ [归档元数据] 已压缩 {len(compressible)} 条历史消息"
            ),
        }
        messages_after = [system_anchor, archive, *active_turn]

        before_tokens = self.token_counter.count_messages(messages)
        after_tokens = self.token_counter.count_messages(messages_after)
        saved = max(0, before_tokens - after_tokens)

        if status_callback:
            status_callback(f"[✅ L5 摘要完成] 释放 {saved} tokens")

        return CompactionResult(
            success=True,
            layer="L5",
            saved_tokens=saved,
            messages_after=messages_after,
            details={"compressible_count": len(compressible)},
        )

    # ------------------------------------------------------------------
    # 降级路径
    # ------------------------------------------------------------------

    def _build_degraded_result(
        self,
        system_anchor: Dict[str, Any],
        compressible: List[Dict[str, Any]],
        active_turn: List[Dict[str, Any]],
        original_messages: List[Dict[str, Any]],
    ) -> CompactionResult:
        """force=True 且已熔断时，强制丢弃可压缩区，仅保 system + 活动轮。"""
        degraded_archive = {
            "role": "system",
            "content": (
                f"{DEGRADED_PREFIX}\n"
                f"由于 LLM 摘要连续失败已触发熔断，{len(compressible)} 条历史消息已被强制丢弃。"
            ),
        }
        messages_after = [system_anchor, degraded_archive, *active_turn]
        before = self.token_counter.count_messages(original_messages)
        after = self.token_counter.count_messages(messages_after)
        saved = max(0, before - after)
        logger.warning(
            "AutoCompactLayer 已降级（熔断+force）：丢弃 %d 条消息，释放 %d tokens",
            len(compressible), saved,
        )
        return CompactionResult(
            success=True,
            layer="L5-degraded",
            saved_tokens=saved,
            messages_after=messages_after,
            details={"reason": "熔断后强制降级"},
        )

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    async def _call_llm_summarize(
        self,
        compressible: List[Dict[str, Any]],
        current_cwd: str,
    ) -> str:
        """调 LLM 把可压缩区结构化为 9 段摘要文本。

        关键：不传 tools schema，避免 LLM 又回头调工具污染输出。
        """
        # TODO(三模型差异化): 后续可切换为 get_medium_config()
        cfg = get_main_config()

        raw_history_text = self._serialize_for_summary(compressible)

        messages = [
            {"role": "system", "content": AUTO_COMPACT_PROMPT_ZH},
            {
                "role": "user",
                "content": (
                    f"以下是需要被结构化摘要的对话历史（CWD={current_cwd}）：\n\n"
                    f"{raw_history_text}"
                ),
            },
        ]

        extra_body = {
            "thinking": {"type": "disabled"},
            "enable_thinking": False,
            "chat_template_kwargs": {"thinking": False},
        }

        client = AsyncOpenAI(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            max_retries=2,
            http_client=httpx.AsyncClient(trust_env=False),
        )
        try:
            response = await client.chat.completions.create(
                model=cfg["model_name"],
                messages=messages,
                temperature=0.1,
                extra_body=extra_body,
                timeout=LLM_SUMMARIZE_TIMEOUT_SECONDS,
            )
            if not response.choices or not response.choices[0].message.content:
                raise ValueError("LLM 返回空内容")
            return response.choices[0].message.content
        finally:
            try:
                await client.close()
            except Exception:
                pass

    @staticmethod
    def _serialize_for_summary(compressible: List[Dict[str, Any]]) -> str:
        """把可压缩区消息序列化为可读文本，便于 LLM 摘要。"""
        lines: List[str] = []
        for m in compressible:
            role = (m.get("role") or "").upper()
            content = m.get("content") or ""
            if m.get("reasoning_content"):
                lines.append(f"[模型内心思考]: {m['reasoning_content']}")
            lines.append(f"[{role}]: {content}")
            tool_calls = m.get("tool_calls")
            if tool_calls:
                try:
                    payload = json.dumps(tool_calls, ensure_ascii=False)
                except (TypeError, ValueError):
                    payload = str(tool_calls)
                lines.append(f"(请求调用: {payload})")
        return "\n".join(lines)
