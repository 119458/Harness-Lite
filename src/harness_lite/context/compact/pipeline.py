"""compact 编排器：5 层管线 + L4 投影 + 锚点不变量校验。

本文件包含三块内容：
- anchors 辅助：find_safe_cut_points / nearest_safe_cut（pair 完整性切点工具）
- ContextCollapse：L4 读时投影层
- CompactPipeline：编排器，统一对外暴露 record_tool_result / compress_if_overflow /
  force_compact / snip / project_for_llm 等方法
"""
from __future__ import annotations

import copy
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import uuid4

from harness_lite.context.compact.auto_compact import AutoCompactLayer
from harness_lite.context.compact.local_layers import (
    SnipLayer,
    TimeDecayLayer,
    _validate_pairs,
)
from harness_lite.context.compact.storage import (
    DiskOffloadLayer,
    LargeResultStore,
)
from harness_lite.context.compact.types import (
    CompactionResult,
    LayerStats,
    MessageMeta,
    TokenCounter,
)

logger = logging.getLogger("harness_lite.compact")

DEFAULT_PROACTIVE_RATIO = 0.6   # 全局 proactive 触发阈值（与 L3 相同）
META_ID_KEY = "_meta_id"        # 消息上承载 sidecar 索引的字段名


# ============================================================================
# anchors（pair 校验工具）
# ============================================================================

def find_safe_cut_points(messages: List[Dict[str, Any]]) -> List[int]:
    """返回可安全切割的索引集合：i 表示「在 messages[i] 之前可以切」。

    安全意味着 messages[:i] 不破坏工具对（assistant.tool_calls 必须紧跟同 id 的 tool）。
    遇到不闭合的 assistant.tool_calls 则跳出循环（之后都不安全）。
    """
    safe = [0]
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            expected_ids = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
            j = i + 1
            ok = True
            for eid in expected_ids:
                if (
                    j >= n
                    or messages[j].get("role") != "tool"
                    or messages[j].get("tool_call_id") != eid
                ):
                    ok = False
                    break
                j += 1
            if ok:
                safe.append(j)
                i = j
            else:
                # 工具对不闭合，从此处起的索引都不安全
                break
        else:
            i += 1
            safe.append(i)
    return safe


def nearest_safe_cut(messages: List[Dict[str, Any]], idx: int) -> int:
    """从 idx 向前回扫到最近的安全切割点。"""
    safes: Set[int] = set(find_safe_cut_points(messages))
    while idx > 0 and idx not in safes:
        idx -= 1
    return idx


# ============================================================================
# L4 ContextCollapse（读时投影）
# ============================================================================

class ContextCollapse:
    """L4：每次发往 LLM 前对 messages 做单层深拷贝投影，权威历史不动。

    职责：
    - 剥离内部字段（_meta_id 等不应外泄给 LLM 的字段）
    - 非 thinking_mode 时移除 reasoning_content
    - 清理 assistant 的空 tool_calls
    - 合并相邻的 system 锚点为一条
    """

    def project(
        self,
        messages: List[Dict[str, Any]],
        *,
        thinking_mode: bool,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for m in messages:
            # 深拷贝单条消息，确保后续 SDK 序列化或调用方意外修改投影副本时
            # 不会回污染权威历史（tool_calls 等嵌套结构必须独立副本）。
            mm = copy.deepcopy({k: v for k, v in m.items() if k != META_ID_KEY})
            if not thinking_mode and "reasoning_content" in mm:
                del mm["reasoning_content"]
            if mm.get("role") == "assistant" and mm.get("tool_calls") in (None, []):
                mm.pop("tool_calls", None)

            # 合并连续 system 锚点
            if (
                out
                and out[-1].get("role") == "system"
                and mm.get("role") == "system"
            ):
                merged = dict(out[-1])
                merged["content"] = (
                    (merged.get("content", "") or "")
                    + "\n\n"
                    + (mm.get("content", "") or "")
                )
                out[-1] = merged
                continue
            out.append(mm)
        return out


# ============================================================================
# CompactPipeline 编排器
# ============================================================================

class CompactPipeline:
    """5 层渐进式上下文管理管线（线程安全）。

    持有：
    - sidecar：MessageMeta 字典，承载消息元数据（不污染消息 schema）
    - snip_freed_tokens：L2 累计释放的 token 数（用于 L5 阈值折算）
    - LayerStats：累计统计（CLI 可读）
    """

    def __init__(
        self,
        max_allowed_tokens: int = 128_000,
        memory_store_dir: str = "./memory_store",
        token_counter: Optional[TokenCounter] = None,
    ):
        self.max_allowed_tokens = max_allowed_tokens
        self.token_counter = token_counter or TokenCounter()
        self._sidecar: Dict[str, MessageMeta] = {}
        self._snip_freed_tokens = 0
        self._lock = threading.RLock()
        self.stats = LayerStats()

        self.l1 = DiskOffloadLayer(LargeResultStore(base_dir=Path(memory_store_dir)))
        self.l2 = SnipLayer()
        self.l3 = TimeDecayLayer()
        self.l4 = ContextCollapse()
        self.l5 = AutoCompactLayer(self.token_counter)

    # ------------------------------------------------------------------
    # 写入路径：tool 消息入栈钩子
    # ------------------------------------------------------------------

    def record_tool_result(
        self,
        message: Dict[str, Any],
        session_id: str,
    ) -> Dict[str, Any]:
        """strategy 在 messages.append(tool_msg) 前调用。

        返回处理后的消息（dict）。包含两件事：
        1. 注入 _meta_id + 写入 sidecar 时间戳
        2. 触发 L1.maybe_offload（>=阈值则落盘并改写 content）
        """
        msg = dict(message)
        meta_id = uuid4().hex
        msg[META_ID_KEY] = meta_id
        with self._lock:
            self._sidecar[meta_id] = MessageMeta(
                created_at=datetime.now(),
                last_seen_at=datetime.now(),
            )

        # ⚠️ 关键：maybe_offload 返回 (msg, ref_or_none) 元组，必须解包
        new_msg, ref = self.l1.maybe_offload(msg, session_id)
        if ref is not None:
            with self._lock:
                meta = self._sidecar.get(meta_id)
                if meta:
                    meta.large_ref_id = ref.ref_id
                    meta.source_layer = "L1"
                self.stats.l1_offload_count += 1
                self.stats.l1_bytes_offloaded += ref.byte_size
        # new_msg 可能是同一对象也可能是新 dict；确保 _meta_id 仍在
        if META_ID_KEY not in new_msg:
            new_msg[META_ID_KEY] = meta_id
        return new_msg

    # ------------------------------------------------------------------
    # 读取/优化路径
    # ------------------------------------------------------------------

    async def compress_if_overflow(
        self,
        messages: List[Dict[str, Any]],
        *,
        engine: Any,
        current_cwd: str,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> List[Dict[str, Any]]:
        """proactive 压缩入口：按 60% 阈值检查 → 触发 L3 → 必要时 L5。"""
        with self._lock:
            snip_freed = self._snip_freed_tokens
        T0 = self.token_counter.count_messages(messages)
        if (T0 - snip_freed) <= self.max_allowed_tokens * DEFAULT_PROACTIVE_RATIO:
            return messages

        # —— L3 时间衰减 ——
        if self.l3.should_trigger(messages, self._sidecar, T0, self.max_allowed_tokens):
            r3 = self.l3.apply(messages, self._sidecar, self.token_counter)
            if r3.success and not r3.skipped and r3.messages_after is not None:
                messages = r3.messages_after
                with self._lock:
                    self.stats.l3_apply_count += 1
                    self.stats.l3_total_tokens_freed += r3.saved_tokens
                if status_callback:
                    status_callback(
                        f"[🧹 L3 时间衰减] 释放 {r3.saved_tokens} tokens "
                        f"(清理工具={r3.details.get('cleared_tools', 0)}, "
                        f"剥思考={r3.details.get('stripped_reasonings', 0)})"
                    )
            elif not r3.success:
                logger.warning("L3 apply 失败已回滚: %s", r3.reason)

        # —— L5 全量摘要 ——
        T1 = self.token_counter.count_messages(messages)
        if self.l5.should_apply(T1, self.max_allowed_tokens, snip_freed):
            r5 = await self.l5.apply(
                messages,
                engine=engine,
                current_cwd=current_cwd,
                status_callback=status_callback,
                force=False,
            )
            if r5.success and not r5.skipped and r5.messages_after is not None:
                messages = r5.messages_after
                with self._lock:
                    self.stats.l5_apply_count += 1
                    self.stats.l5_total_tokens_freed += r5.saved_tokens
                    self.stats.l5_consecutive_failures = self.l5._consecutive_failures
                if status_callback:
                    status_callback(
                        f"[📦 L5 结构化全量摘要] 释放 {r5.saved_tokens} tokens "
                        f"(压缩 {r5.details.get('compressible_count', 0)} 条)"
                    )
            elif r5.skipped:
                with self._lock:
                    self.stats.l5_consecutive_failures = self.l5._consecutive_failures

        return messages

    async def force_compact(
        self,
        messages: List[Dict[str, Any]],
        *,
        engine: Any,
        current_cwd: str,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> List[Dict[str, Any]]:
        """reactive 压缩入口：超长异常恢复时跳过阈值检查，强制 L5。"""
        r5 = await self.l5.apply(
            messages,
            engine=engine,
            current_cwd=current_cwd,
            status_callback=status_callback,
            force=True,
        )
        if r5.success and r5.messages_after is not None:
            with self._lock:
                self.stats.l5_apply_count += 1
                self.stats.l5_total_tokens_freed += r5.saved_tokens
                self.stats.l5_consecutive_failures = self.l5._consecutive_failures
            return r5.messages_after
        # force 仍跳过/失败兜底
        return messages

    # ------------------------------------------------------------------
    # L2 主动减负
    # ------------------------------------------------------------------

    def snip(
        self,
        messages: List[Dict[str, Any]],
        indices: List[int],
    ) -> CompactionResult:
        """L2 手动截断：暴露给 CLI / 高级用户主动操作。"""
        with self._lock:
            r = self.l2.apply(messages, indices, self._sidecar, self.token_counter)
            if r.success and not r.skipped and r.messages_after is not None:
                self._snip_freed_tokens += r.saved_tokens
                self.stats.l2_snip_freed_tokens = self._snip_freed_tokens
            return r

    # ------------------------------------------------------------------
    # 出口投影
    # ------------------------------------------------------------------

    def project_for_llm(
        self,
        messages: List[Dict[str, Any]],
        *,
        thinking_mode: bool,
    ) -> List[Dict[str, Any]]:
        """L4：每次 LLM 调用前投影；不修改权威 messages。"""
        return self.l4.project(messages, thinking_mode=thinking_mode)

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------

    def calculate_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        return self.token_counter.count_messages(messages)

    def reset_session(self, reason: str = "") -> None:
        """配合 MemoryManager.clear_context 的失效回调使用。"""
        with self._lock:
            count = len(self._sidecar)
            self._sidecar.clear()
            self._snip_freed_tokens = 0
            self.l5._consecutive_failures = 0
            self.stats = LayerStats()
        logger.info(
            "CompactPipeline reset_session: cleared %d sidecar entries (reason=%s)",
            count, reason,
        )
