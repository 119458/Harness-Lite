"""section: 长效行为备忘录（拼接 MemoryManager 输出 + mem0 开关提示）。"""

from __future__ import annotations

import hashlib
from typing import Optional

from harness_lite.prompt.context import PromptContext

_HEADER = (
    "# 长效行为备忘录\n"
    "以下内容是你过往学习沉淀或被人工纠错后固化的行为准则与项目偏好，请严格遵守：\n"
)


def compute(ctx: PromptContext) -> Optional[str]:
    body = ctx.memory_text.strip()
    mem0_hint = (
        "\n\n## 从历史会话动态检索的相关经验\n"
        "（mem0 已启用，相关条目已在上方按相似度注入。）"
        if ctx.mem0_enabled
        else "\n\n## 从历史会话动态检索的相关经验\n（mem0 当前关闭，不进行向量检索。）"
    )

    if not body:
        return _HEADER + "\n（当前未沉淀任何长效记忆。）" + mem0_hint
    return f"{_HEADER}\n{body}{mem0_hint}"


def dep_sig(ctx: PromptContext) -> str:
    body = f"{ctx.mem0_enabled}|{ctx.memory_text}"
    return "memory:" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


compute.dep_sig = dep_sig
