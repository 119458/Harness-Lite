# 🚀 Harness-Lite 架构优化更新日志 (2026-5-20)

## 🛡️ 安全底座与沙箱隔离 (Security & Control)
**核心目标**：阻断大模型可能产生的幻觉越权，保护宿主机物理环境绝对安全。
**涉及文件**：`security/manager.py`, `tools/execution_ops.py`, `loop/engine.py`

* **集中化安全拦截 (Centralized Interception)**：
  * 废弃了 `BashTerminalTool` 中极其脆弱的内部数组黑名单机制，将所有涉及“文件读写”和“终端执行”的权限校验收拢到 `security_manager.intercept()` 中进行 AOP（切面）统一拦截。
  * 新增了基于正则的高危 Shell 指令特征库（如 `rm -rf`, `mkfs`, `sudo`, 覆盖磁盘等），防止恶意或失控指令破坏系统。
* **物理沙箱隔离 (Workspace Jail)**：
  * 在 `manager.py` 中实现了严格的目录防穿越逻辑。自动识别并定位到 `Harness-Lite/sandbox` 目录下（如果不存在则自动创建）。
  * 利用 `pathlib.Path.resolve()` 计算绝对路径，强制阻断大模型任何试图跳出 sandbox 目录的文件操作（如 `../../etc/passwd`）。
* **消除路径幻觉 (Context Injection)**：
  * 修改 `engine.py` 的 `SYSTEM_PROMPT`，在每次对话前将沙箱的绝对路径作为【环境与沙箱状态】动态注入给大模型，让模型明确知道自己的工作区在哪，并引导其使用相对路径。

## 🛠️ 核心工具链健壮性升级 (Tool Robustness)
**核心目标**：解决大模型工具调用失败率最高的两个痛点：“执行状态丢失”与“格式匹配严苛”。
**涉及文件**：`tools/execution_ops.py`, `tools/file_ops.py`

* **打造持久化记忆终端 (Persistent Stateful Shell)**：
  * 将 `BashTerminalTool` 底层完全重构，从单次无状态的 `subprocess.run` 升级为使用后台守护线程维护的持久化 Bash 进程。
  * 通过写入唯一标记 (`UUID Marker`) 来精准捕获每次命令的执行结果与退出码。
  * **效果**：`cd` 目录切换、`export` 环境变量激活、虚拟环境等操作现在可以跨步数永久生效，告别大模型“刚进目录下一步又回到根目录”的失忆问题。
* **精准行号切片替换 (Line-based Replacer)**：
  * 废弃了 `EditFileTool` 中对大模型极度不友好的 `str.replace`（旧字符串匹配）方案。
  * 改为强制大模型提供 `start_line` 和 `end_line`，通过对文件按行切片进行局部替换和插入。
  * **效果**：极大提高了代码修改的成功率，再也不会因为大模型少输出一个空格或缩进错乱而导致修改失败。

## 🛡️ 循环编排与通信容错 (Loop & Engine)
**核心目标**：对抗云端大模型 API 网络抖动，保护上下文（Context Window）不被日志撑爆。
**涉及文件**：`loop/engine.py`, `loop/strategy.py`

* **API 指数退避重试 (Exponential Backoff Retry)**：
  * 在 `engine.py` 的异步 LLM 调用中，加入了针对 `httpx.RequestError` 和 `502/429` 状态码的拦截与重试机制（默认重试 3 次，间隔 2/4/8 秒递增）。
  * 通过 `status_callback` 将网络抖动状态反馈给前台，防止偶发网络断连导致整个多步任务崩溃。
* **安全滑动窗口修剪 (Sliding Window Pruning)**：
  * 在 `strategy.py` 的 `ReAct` 循环中加入 `_prune_context` 内存修剪功能。
  * 当历史步数过长时（默认 12 步），系统会自动折叠早期的尝试与工具日志，永远只保留核心 System 提示词和最近几步的对话上下文，彻底解决 Token 消耗过大与 API 上限报错问题。
* **输出硬截断 (Hard Truncation)**：
  * 强制约束单个工具的返回字符上限（如 4000 字符）。若大模型失误打印了巨型日志，框架会进行“腰斩”截断，并留下提示信息。