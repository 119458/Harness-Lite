"""
PromptBuilder 主体：负责把若干 section 按"静态前缀 + 边界 + 动态后缀"组装。

设计取向：
- section 注册表：以列表形式声明渲染顺序，新增 section 只需追加一行
- 缓存外挂：实际命中/失效由 SectionCache 负责，本类只在两端发起 get/put
- 异常隔离：单个 section 抛错只跳过该段并 warn，绝不让 engine 整体崩溃
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional, Tuple

from harness_lite.prompt.context import PromptContext
from harness_lite.prompt.section_cache import SectionCache
from harness_lite.prompt.sections import (
    action_safety,
    doing_tasks,
    environment,
    intro,
    memory_recall,
    session_guidance,
    skills_catalog,
    system_rules,
    tone_style,
    tools_catalog,
    using_tools,
)

logger = logging.getLogger("harness_lite.prompt")

# 静态前缀与动态后缀之间的物理分隔标记，便于上游 prompt cache 切片
DYNAMIC_BOUNDARY = "<<<HARNESS_LITE_DYNAMIC_BOUNDARY>>>"


# section 渲染函数协议：compute(ctx) -> Optional[str]，并挂 dep_sig(ctx) -> str
SectionFn = Callable[[PromptContext], Optional[str]]


class PromptBuilder:
    """分层 system prompt 组装器。

    使用示例：
        builder = PromptBuilder(ctx, cache)
        system_text = builder.build()
    """

    # 静态前缀：依赖的字段在长会话中变化极少，命中率高
    STATIC_SECTIONS: List[Tuple[str, SectionFn]] = [
        ("intro", intro.compute),
        ("system_rules", system_rules.compute),
        ("doing_tasks", doing_tasks.compute),
        ("action_safety", action_safety.compute),
        ("using_tools", using_tools.compute),
        ("tone_style", tone_style.compute),
    ]

    # 动态后缀：依赖工作区路径、技能、工具、记忆等会变更的字段
    DYNAMIC_SECTIONS: List[Tuple[str, SectionFn]] = [
        ("session_guidance", session_guidance.compute),
        ("environment", environment.compute),
        ("tools_catalog", tools_catalog.compute),
        ("skills_catalog", skills_catalog.compute),
        ("memory_recall", memory_recall.compute),
    ]

    def __init__(self, ctx: PromptContext, cache: SectionCache) -> None:
        self.ctx = ctx
        self.cache = cache

    def build(self) -> str:
        """组装最终的 system content 字符串。"""
        static_parts = self._render_group(self.STATIC_SECTIONS)
        dynamic_parts = self._render_group(self.DYNAMIC_SECTIONS)

        pieces: List[str] = []
        pieces.extend(static_parts)
        pieces.append(DYNAMIC_BOUNDARY)
        pieces.extend(dynamic_parts)
        return "\n\n".join(pieces)

    def _render_group(self, group: List[Tuple[str, SectionFn]]) -> List[str]:
        """渲染一组 section，过滤掉返回 None 或为空字符串的段。"""
        rendered: List[str] = []
        for name, fn in group:
            text = self._render_one(name, fn)
            if text:
                rendered.append(text)
        return rendered

    def _render_one(self, name: str, fn: SectionFn) -> Optional[str]:
        """单段渲染。先查缓存，未命中则调用 compute 并回写。"""
        try:
            sig = self._dep_sig_of(fn)
        except Exception as exc:
            logger.warning("section %s 计算 dep_sig 失败：%s", name, exc)
            sig = f"__nosig__:{name}"

        cached = self.cache.get(name, sig)
        if cached is not None:
            return cached

        try:
            value = fn(self.ctx)
        except Exception as exc:
            logger.warning("section %s 渲染异常被跳过：%s", name, exc)
            return None

        # None 也写入缓存，避免下次重复触发同样的空计算
        self.cache.put(name, sig, value)
        return value

    def _dep_sig_of(self, fn: SectionFn) -> str:
        """从 section 函数对象上取 dep_sig 钩子。"""
        dep_sig = getattr(fn, "dep_sig", None)
        if dep_sig is None:
            # 没声明依赖等价于无缓存
            return f"__no_dep_sig__:{id(fn)}"
        return dep_sig(self.ctx)
