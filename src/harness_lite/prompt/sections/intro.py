"""section: 身份介绍与安全/网络红线。"""

from __future__ import annotations

from typing import Optional

from harness_lite.prompt.context import PromptContext

_TEXT = """你是 Harness-Lite 智能助手，一个面向真实软件工程任务的交互式开发助手。
请始终结合下方所有指令与可用工具完成用户请求。

【重要】协助进行授权范围内的安全测试、防御性安全研究、CTF 挑战与教育目的；
拒绝执行任何具有破坏性、面向未授权目标的渗透、绕过检测、攻击关键基础设施等请求。
【重要】严禁臆造或猜测任何 URL，仅可使用用户在消息或本地文件中已提供的链接。"""


def compute(ctx: PromptContext) -> Optional[str]:
    return _TEXT


def dep_sig(ctx: PromptContext) -> str:
    # 完全静态：签名恒定即可
    return "intro:v1"


compute.dep_sig = dep_sig
