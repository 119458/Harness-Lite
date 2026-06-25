"""section: 长期记忆系统（仅装载行为指南 + MEMORY.md 索引）。

v2 装载内容：
- 行为指南：4 类记忆说明 + 何时通过 read_file 读取详情 + STALE 警告
- MEMORY.md 索引：按 type 分组渲染，project 类带日期 + 自动 STALE

不再在 turn 开始时注入「本轮可能相关」推荐清单——推荐改由 ReActStrategy 在
主模型返回 tool_calls 后并行筛选，并在工具结果之后追加为临时 `is_meta` system 消息。
主模型按需用 read_file 读取具体记忆全文。
"""

from __future__ import annotations

import hashlib
from typing import Optional

from harness_lite.prompt.context import PromptContext


def compute(ctx: PromptContext) -> Optional[str]:
    body = ctx.long_term_memory_text.strip()
    return body if body else None


def dep_sig(ctx: PromptContext) -> str:
    body = ctx.long_term_memory_text
    return "ltm:" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


compute.dep_sig = dep_sig
