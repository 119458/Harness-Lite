"""section: 业务技能 / SOP 索引。"""

from __future__ import annotations

import hashlib
from typing import Optional

from harness_lite.prompt.context import PromptContext

_HEADER = (
    "# 可用业务技能 / SOP 手册\n"
    "以下是当前已加载的领域规范目录。涉及相关任务时，请先通过 `read_skill` 工具读取对应\n"
    "SKILL.md 全文再开始执行：\n"
)


def _normalize_skill(item: dict) -> tuple:
    name = item.get("name", "") or ""
    desc = item.get("description", "") or ""
    return name, desc


def compute(ctx: PromptContext) -> Optional[str]:
    if not ctx.skills_list:
        return _HEADER + "\n（若为空：当前未加载任何业务技能。）"

    lines = [_HEADER, ""]
    for item in ctx.skills_list:
        name, desc = _normalize_skill(item)
        if not name:
            continue
        lines.append(f"- 技能名称: `{name}` | 简介: {desc}")
    return "\n".join(lines)


def dep_sig(ctx: PromptContext) -> str:
    parts = []
    for item in ctx.skills_list:
        name, desc = _normalize_skill(item)
        parts.append(f"{name}::{desc}")
    body = "||".join(parts)
    return "skills:" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


compute.dep_sig = dep_sig
