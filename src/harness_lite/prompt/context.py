"""
PromptContext 数据载体。

独立放在 context.py 是为了打破 builder ↔ sections 之间的循环 import：
sections/*.py 只依赖该轻量模块，builder.py 在最顶层再聚合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class PromptContext:
    """渲染 system prompt 所需的全部上下文数据。

    所有字段在 builder 调用前由 engine 一次性收齐，
    section 内部只允许读取该结构，禁止访问外部全局状态。
    """

    task: str
    session_id: str
    model_name: str
    sandbox_roots: Tuple[str, ...]
    enabled_tools: Tuple[str, ...]
    tools_schema_json: str
    skills_list: Tuple[dict, ...] = field(default_factory=tuple)
    memory_text: str = ""
    mem0_enabled: bool = False
    cwd: str = ""
    is_git: bool = False
    platform: str = ""
    shell: str = ""
    os_version: str = ""
    current_date: str = ""
    thinking_mode: bool = False
