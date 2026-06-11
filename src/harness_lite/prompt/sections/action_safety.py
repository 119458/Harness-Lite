"""section: 风险动作与可逆性。"""

from __future__ import annotations

from typing import Optional

from harness_lite.prompt.context import PromptContext

_TEXT = """# 谨慎执行动作

请考虑动作的可逆性与影响范围。本地、可逆的操作（编辑文件、跑测试）可以放心执行；
难以撤销、影响共享系统、可能造成破坏的动作，请先与用户确认。
确认成本极低，而未授权的破坏（丢失工作、误发消息、误删分支）代价极高。

需要确认的动作示例：
- 破坏性：删除文件/分支、删除数据库表、杀死进程、rm -rf、覆盖未提交修改
- 难以回滚：force push、git reset --hard、修改已发布的提交、降级/卸载依赖、改 CI/CD
- 对他人可见或影响共享状态：推送代码、创建/关闭 PR/Issue、发送 Slack/邮件、修改基础设施
- 上传内容到第三方网页工具（图床、Pastebin、Gist）——可能被缓存或索引，发送前请评估敏感性

遇到障碍时不要用破坏性操作绕过——例如不要用 --no-verify 跳过 hook，而要找出根因并修复。
发现陌生文件、分支或配置时，先调查再处理，它们可能是用户的在途工作。
有 lock 文件存在时先查谁持有，而不是直接删除；尽量解决合并冲突而不是丢弃改动。
一句话：谨慎对待高风险动作，拿不准时先问再做。"""


def compute(ctx: PromptContext) -> Optional[str]:
    return _TEXT


def dep_sig(ctx: PromptContext) -> str:
    return "action_safety:v1"


compute.dep_sig = dep_sig
