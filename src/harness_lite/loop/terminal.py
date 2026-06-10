"""
Terminal 终止条件枚举。

对应 adopt-code/query.ts 中 queryLoop 的 return reason 枚举。
所有 query_engine.submitMessage 退出路径必须落到唯一一个 Terminal 值。

注意：这里只是类型定义，不引入任何运行时行为；阶段 A 仅供后续阶段引用。
"""
from __future__ import annotations
from enum import Enum


class Terminal(str, Enum):
    """L2 循环引擎的终止原因枚举（对应 adopt-code QueryTerminal）。"""

    # 正常完成：模型未请求工具，自然终止
    COMPLETED = "completed"

    # 用户中断 / abort 信号：流式阶段被中断
    ABORTED_STREAMING = "aborted_streaming"

    # 用户中断 / abort 信号：工具执行阶段被中断
    ABORTED_TOOLS = "aborted_tools"

    # 上层语义中断（含 KeyboardInterrupt 转化、Layer 3 用户拒绝）
    ABORTED = "aborted"

    # LLM 服务端错误（超时 / 限流 / 5xx / max_output_tokens 升档耗尽）
    MODEL_ERROR = "model_error"

    # 图像/媒体错误（一期不主动产生，预留位）
    IMAGE_ERROR = "image_error"

    # 上下文超长且 reactive_compact 已尝试过仍失败
    PROMPT_TOO_LONG = "prompt_too_long"

    # 未压缩时已超过强制阻塞限额（autocompact 未生效场景）
    BLOCKING_LIMIT = "blocking_limit"

    # 达到最大思考步数（max_steps 兜底）
    MAX_TURNS = "max_turns"

    # 工具连续异常熔断（consecutive_errors ≥ 3）
    HOOK_STOPPED = "hook_stopped"

    # stop hook 主动阻止终止（一期暂不实现 stop hooks，预留位）
    STOP_HOOK_PREVENTED = "stop_hook_prevented"

    def is_success(self) -> bool:
        """是否为成功终止（用于 QueryEngine 产 success vs error_during_execution）。"""
        return self == Terminal.COMPLETED

    def is_aborted(self) -> bool:
        """是否为中断类终止（不计入 error，但也非 success）。"""
        return self in (Terminal.ABORTED, Terminal.ABORTED_STREAMING, Terminal.ABORTED_TOOLS)

    def is_error(self) -> bool:
        """是否为错误类终止（CLI 应展示错误提示）。"""
        return self in (
            Terminal.MODEL_ERROR,
            Terminal.IMAGE_ERROR,
            Terminal.PROMPT_TOO_LONG,
            Terminal.BLOCKING_LIMIT,
            Terminal.HOOK_STOPPED,
        )


__all__ = ["Terminal"]
