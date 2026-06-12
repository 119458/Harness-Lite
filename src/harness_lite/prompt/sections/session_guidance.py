"""section: 当前会话特有的指引（CLI 斜杠命令、技能调用、熔断规则）。"""

from __future__ import annotations

from typing import Optional

from harness_lite.prompt.context import PromptContext

_TEXT = """# 当前会话提示
- 当前由 Harness-Lite CLI 驱动，交互模式下可用斜杠命令：
  `/model` `/tool` `/skill` `/mem0` `/clear` `/session` `/sandbox` `/exit`
- 若你需要用户在 shell 里手动执行某条命令（如交互式登录），请提示用户在输入框前加 `!`，
  例如：`! gcloud auth login`，命令输出将直接进入对话。
- 当用户输入 `/技能名` 时，请通过 `read_skill` 工具读取对应 SKILL.md 后再执行；
  不要凭记忆假设技能内容。
- 复杂任务可用 ReAct 多步循环；当连续 3 次工具调用都失败时，框架会自动熔断，
  请就此向用户求助或更换思路。"""


def compute(ctx: PromptContext) -> Optional[str]:
    return _TEXT


def dep_sig(ctx: PromptContext) -> str:
    # 当前内容为纯静态文本，dep_sig 因此恒定。
    # 注意：若未来给 compute 增加任何 ctx 依赖字段（如 sandbox_roots、session_id），
    # 必须把对应字段拼入此处签名，否则缓存会读到脏数据。
    return "session_guidance:v1"


compute.dep_sig = dep_sig
