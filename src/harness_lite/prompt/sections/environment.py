"""section: 运行环境（动态，依赖 cwd / 平台 / 模型 / 沙箱 / 日期等）。"""

from __future__ import annotations

import hashlib
from typing import Optional

from harness_lite.prompt.context import PromptContext


def _format_roots(roots: tuple) -> str:
    if not roots:
        return "  - （当前未挂载任何沙箱）"
    return "\n".join(f"  - `{r}`" for r in roots)


def compute(ctx: PromptContext) -> Optional[str]:
    roots_block = _format_roots(ctx.sandbox_roots)
    git_flag = "yes" if ctx.is_git else "no"
    thinking_flag = "on" if ctx.thinking_mode else "off"

    return (
        "# 环境\n"
        "你被调用在以下环境中：\n"
        f" - 主工作目录: {ctx.cwd or '<unknown>'}\n"
        f" - 是否 git 仓库: {git_flag}\n"
        " - 沙箱挂载根:\n"
        f"{roots_block}\n"
        f" - 平台: {ctx.platform or '<unknown>'}\n"
        f" - Shell: {ctx.shell or '<unknown>'}\n"
        f" - OS 版本: {ctx.os_version or '<unknown>'}\n"
        f" - 当前使用模型: {ctx.model_name or '<unknown>'}（思维链模式: {thinking_flag}）\n"
        f" - 当前会话 ID: {ctx.session_id}\n"
        f" - 当前日期: {ctx.current_date or '<unknown>'}\n\n"
        "【沙箱铁律】文件读写、bash、python 执行的所有路径必须严格限制在上述沙箱根之内，\n"
        "不得探出去访问 /etc、~/.ssh 等系统敏感目录。"
    )


def dep_sig(ctx: PromptContext) -> str:
    raw = "|".join([
        ctx.cwd,
        str(ctx.is_git),
        "/".join(ctx.sandbox_roots),
        ctx.platform,
        ctx.shell,
        ctx.os_version,
        ctx.model_name,
        ctx.session_id,
        ctx.current_date,
        str(ctx.thinking_mode),
    ])
    return "env:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


compute.dep_sig = dep_sig
