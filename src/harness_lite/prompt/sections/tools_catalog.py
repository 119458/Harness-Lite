"""section: 工具目录 + 完整 JSON Schema。"""

from __future__ import annotations

import hashlib
from typing import Optional

from harness_lite.prompt.context import PromptContext


def compute(ctx: PromptContext) -> Optional[str]:
    schema_block = ctx.tools_schema_json.strip() or "[]"
    return (
        "# 可用工具目录\n"
        "以下是当前已加载的原子工具及其 JSON Schema。请使用工具的 `name` 字段作为 tool_call 的\n"
        "function name；参数严格按 schema 提供。\n\n"
        f"{schema_block}\n\n"
        "【提示】当使用 edit_file 时，请先用 read_file 查阅目标文件并获取准确的 start_line / end_line。"
    )


def dep_sig(ctx: PromptContext) -> str:
    # 工具集合或 schema 任一变化都需要重算
    names_part = ",".join(sorted(ctx.enabled_tools))
    body = f"{names_part}|{ctx.tools_schema_json}"
    return "tools:" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


compute.dep_sig = dep_sig
