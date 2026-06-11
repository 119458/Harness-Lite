"""section: 工具选择与并行调用策略。"""

from __future__ import annotations

from typing import Optional

from harness_lite.prompt.context import PromptContext

_TEXT = """# 使用工具
- 有专用工具时优先用专用工具，不要走 bash_terminal：
  - 读取文件用 `read_file`，不用 cat / head / tail
  - 编辑文件用 `edit_file` 或 `fuzzy_edit`，不用 sed / awk
  - 创建文件用 `create_file`，不用 echo > 或 heredoc
  - 列目录用 `list_directory`，不用 ls
  - 搜索内容用 `grep_search`
  - `bash_terminal` 仅用于必须走 shell 的系统级命令
- 计划复杂工作（≥3 步）时，先在脑海中分解步骤，按顺序逐步推进。
- 同一条响应中可发起多个工具调用：若调用之间无依赖，请并行发起以提升效率；
  若 B 依赖 A 的结果，必须串行。
- 长任务请频繁报告进度，不要长时间静默。"""


def compute(ctx: PromptContext) -> Optional[str]:
    return _TEXT


def dep_sig(ctx: PromptContext) -> str:
    return "using_tools:v1"


compute.dep_sig = dep_sig
