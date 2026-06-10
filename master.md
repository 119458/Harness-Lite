# Master 实施规划 —— Adopt-Code Loop 流程移植

> 源代码：`adopt-code/query.ts`（核心 ReAct 循环）+ `adopt-code/QueryEngine.ts`（会话编排层）
> 目标：在 Harness-Lite 的 `loop/` 与 `cli/` 体系内复刻这套“会话引擎 + 循环引擎”的双层架构，并复用现有 Memory / Security / Registry / Context 模块。
> 本文件将作为 **harness-coder（编码）** 与 **harness-code-reviewer（审查）** 两位子 agent 的共同蓝图。

---

## 一、源代码整体定位

Adopt-Code 是 Claude Code 真实生产环境的 Loop 实现，包含两个分层：

| 层级 | 文件 | 职责 |
|------|------|------|
| **L1 会话编排层** | `QueryEngine.ts` | 一个 conversation 一个 QueryEngine 实例；每次 `submitMessage()` 启动一个新 turn；管理 mutableMessages、abortController、totalUsage、readFileState、权限拒绝列表 |
| **L2 循环引擎层** | `query.ts` | 真正的 ReAct While-True 循环；处理压缩 / 流式调用 / 工具执行 / 错误恢复 / 终止判定；以 AsyncGenerator 形式产出消息流 |

---

## 二、L2 循环引擎（query.ts）流程拆解

### 2.1 入口与状态机

```
query(params) → queryLoop(params, consumedCommandUuids)
    ↓
初始化 State { messages, toolUseContext, autoCompactTracking,
              maxOutputTokensRecoveryCount, hasAttemptedReactiveCompact,
              maxOutputTokensOverride, pendingToolUseSummary,
              stopHookActive, turnCount, transition }
    ↓
进入 while(true) 主循环
```

**关键设计**：State 是**跨迭代可变状态**，每次 `continue` 时整体替换；每轮迭代顶部解构成裸变量读取。

### 2.2 主循环单轮迭代步骤（按顺序）

#### Step 1：迭代前预处理
1. 解构 state，发出 `stream_request_start` 事件
2. 记录 `queryCheckpoint('query_fn_entry')`
3. 维护 `queryTracking { chainId, depth }`（用于分析跟踪链路深度）
4. 启动 **Skill 预取**（`pendingSkillPrefetch`）—— 在流式调用与工具执行的耗时窗口内并行做技能发现

#### Step 2：上下文压缩流水线（多级 fallback）
按顺序执行（每一级都是可独立开关的 feature flag）：

1. **applyToolResultBudget** —— 单条消息工具结果总字节预算控制
2. **snipCompactIfNeeded**（HISTORY_SNIP）—— 历史片段裁剪，释放 token 量返回给后续压缩判定
3. **microcompact** —— 微压缩（缓存级别）
4. **contextCollapse**（CONTEXT_COLLAPSE）—— 上下文折叠（视图投影方式，源 REPL 历史不动）
5. **autocompact** —— 完整自动压缩（产生 summary boundary 消息）
6. **阻塞限额检查** —— 没有 compact 时若超 blocking limit 直接 `return { reason: 'blocking_limit' }`

#### Step 3：流式 LLM 调用
1. 计算 `currentModel`（含 plan-mode 200k 升档逻辑）
2. 通过 `deps.callModel({ messages, systemPrompt, tools, signal, options })` 发起流式请求
3. 在 `for await (message of ...)` 中：
   - 处理 `streamingFallbackOccured` —— 模型回退时清空已收消息并发墓碑（tombstone）
   - 对 `tool_use` 块做 `backfillObservableInput` 增量字段补全
   - **withhold 机制**：对可恢复错误（prompt-too-long / max-output-tokens / media-size）**截留不 yield**，等迭代末判定能否恢复
   - 把 `assistant` 消息推入 `assistantMessages[]`，提取 `tool_use` 块到 `toolUseBlocks[]`，设置 `needsFollowUp = true`
   - 若启用 `streamingToolExecutor` 流式执行器，每收到一个 tool_use 立刻 `addTool` 启动执行并把已完成结果 yield

4. **try/catch fallbackModel**：捕获 `FallbackTriggeredError` 后切换 `currentModel = fallbackModel`，清空 assistantMessages 重试

#### Step 4：流式后判定 / 错误恢复
1. 执行 `executePostSamplingHooks`（后采样钩子）
2. 检查 `abortController.aborted` → 产 synthetic tool_results → `return { reason: 'aborted_streaming' }`
3. 若上一轮有 `pendingToolUseSummary`（用 haiku 异步生成的工具调用摘要），await 后 yield

#### Step 5：终止 vs 继续判定 —— `if (!needsFollowUp)`

##### 5a. 没有 follow-up（模型不要工具）时的恢复链：
- **isWithheld413 → context collapse drain** → `transition: 'collapse_drain_retry'`
- **isWithheld413/isWithheldMedia → tryReactiveCompact** → `transition: 'reactive_compact_retry'`
- **isWithheldMaxOutputTokens → 升档/重试**
  - 首次：尝试将 max_output_tokens 提升到 64k（ESCALATED_MAX_TOKENS）
  - 后续：注入 user meta 消息 “Resume directly — no apology...” 重试，最多 3 次
- API 错误：跳过 stop hooks，直接 `return { reason: 'completed' }`
- 否则：执行 `handleStopHooks` —— stop hooks 可强制阻止终止或注入 blocking errors
- 若 token budget 触发 continue：注入 nudge 消息继续
- 否则正常 `return { reason: 'completed' }`

##### 5b. 有 follow-up（要执行工具）时：
1. 选择执行器：`streamingToolExecutor.getRemainingResults()` 或 `runTools(...)` 顺序/并行执行
2. for-await 消费工具结果，每条 yield 出去 + push 到 `toolResults[]`
3. 检测 `hook_stopped_continuation` 附件 → 设 `shouldPreventContinuation`
4. 生成 `nextPendingToolUseSummary`（haiku 异步任务，不阻塞下一次调用）
5. 检查 abort / preventContinuation / maxTurns
6. **拉取 attachments**：`getAttachmentMessages(...)` —— 包含队列命令快照、用户额外注入
7. **消费 memoryPrefetch / skillPrefetch** 结果作为 attachments
8. 调用 `refreshTools()` 刷新（新连接 MCP server 即时可见）
9. 构造下一轮 `next: State` 并 `continue`

### 2.3 终止条件（Terminal 枚举）
- `completed` / `aborted_streaming` / `aborted_tools` / `model_error` / `image_error`
- `prompt_too_long` / `blocking_limit` / `max_turns` / `hook_stopped` / `stop_hook_prevented`

---

## 三、L1 会话编排层（QueryEngine.ts）流程拆解

### 3.1 构造期
- `new QueryEngine(config)` 接收 cwd / tools / commands / mcpClients / agents / canUseTool / state getter-setter / readFileCache / 模型与系统提示词覆盖等
- 维护：`mutableMessages[]`、`abortController`、`permissionDenials[]`、`totalUsage`、`discoveredSkillNames`、`loadedNestedMemoryPaths`

### 3.2 `submitMessage(prompt, options)` 单 turn 流程

1. **预备阶段**
   - `setCwd(cwd)` 切目录
   - 包装 `canUseTool` —— 记录权限拒绝供 SDK 上报
   - 获取 `initialMainLoopModel`、`thinkingConfig` 默认值

2. **系统提示组装**
   - `fetchSystemPromptParts({ tools, mcpClients, customSystemPrompt })`
   - 注入 coordinator userContext / memory mechanics prompt / appendSystemPrompt
   - 最终 `systemPrompt = asSystemPrompt([...])`

3. **结构化输出钩子注册**（如果有 jsonSchema）
4. **processUserInput**
   - 解析 prompt（含 slash 命令）→ 产 `messagesFromUserInput`、`shouldQuery`、`allowedTools`、`modelFromUserInput`
   - push 到 mutableMessages
5. **会话持久化**
   - `recordTranscript(messages)`（bare mode fire-and-forget）
   - 必要时 `flushSessionStorage()`
6. **加载 skills + plugins**（缓存模式）
7. **yield system init 消息**
8. **shouldQuery=false 分支**：仅 slash 命令本地输出，直接 yield 结果消息后返回

9. **进入 query() 循环**
   ```
   for await (message of query({ messages, systemPrompt, userContext, systemContext,
                                  canUseTool: wrappedCanUseTool, toolUseContext,
                                  fallbackModel, querySource: 'sdk', maxTurns, taskBudget }))
   ```
   按消息类型分发：
   - `assistant` / `user` / `system(compact_boundary)` → 写 transcript（assistant fire-and-forget，其他 await）
   - `tombstone` → 跳过（控制信号）
   - `progress` / `attachment` → push + 内联记录
   - `stream_event` → message_start/delta/stop 时更新 `currentMessageUsage` 和 `totalUsage`
   - `system(compact_boundary)` → 释放 pre-compact 消息给 GC
   - `system(api_error)` → 转成 `api_retry` SDK 消息
   - `tool_use_summary` → 转成 SDK 消息
   - `attachment(max_turns_reached)` → 直接产 `error_max_turns` 结果并 return
   - 每轮检查 maxBudgetUsd / 结构化输出重试上限

10. **terminal 阶段**
    - 找出 `result = findLast(assistant|user)`
    - flush 存储
    - `isResultSuccessful` 判定 → 产 `success` 或 `error_during_execution` 结果消息
    - 提取 `textResult` 作为最终 result

### 3.3 辅助方法
- `interrupt()` —— 调用 abortController.abort()
- `getMessages()` / `getReadFileState()` / `getSessionId()` / `setModel()`

### 3.4 顶层便捷函数 `ask()`
一次性包装：`new QueryEngine(...)` → `yield* engine.submitMessage(...)` → finally 把 readFileState 回写给调用方

---

## 3.5 现有代码兼容性约束（必读）

> 移植 adopt-code 时**不可破坏的现有契约**。以下每一项都对应实际代码事实，违反任意一条都会导致 CLI / 会话隔离 / 安全防御失效。harness-coder 实施前必须通读本节，harness-code-reviewer 审查时必须逐条核对。

### 3.5.1 API 与回调签名（外部契约，绝对不变）

| 契约项 | 当前实现位置 | 不变约束 |
|---|---|---|
| `AsyncLoopEngine.run(task, session_id, stream_callback, status_callback) -> str` | `loop/engine.py:25-86` | **签名与返回类型必须保留**；CLI 通过 `await engine.run(...)` 拿到最终字符串，新版 QueryEngine 必须作为内部委托，外层仍提供这个同名同签名方法 |
| `stream_callback(text: str)` | `cli/app.py:RichCLIOutputHandler` | 接收**纯文本片段**（增量字符），不接收带 `[]` 前缀的状态行 |
| `status_callback(text: str)` | 同上 | 接收**带前缀的状态行**，如 `[🧠 思考中]`、`[⚙️ 线程激活]`、`[💾 上下文压缩...]`；语义不可改 |
| `engine.run()` 必须返回最终文本 | `cli/app.py:run_loop_async` | CLI 用 `result = await engine.run(...)` 写入 history / 显示终态；不能改为只 yield generator |
| `global_engine = AsyncLoopEngine()` 跨 turn 复用 | `cli/app.py:run_interactive_async` | 交互模式下同一 engine 实例服务多 turn；QueryEngine 实例的生命周期必须与此对齐（一个 engine 内可有多个 QueryEngine turn 或一个 QueryEngine 复用多 turn —— 二选一，但必须有明确决策） |
| 工具 schema 嵌套 function-calling 格式 | `tools/*.py` schema 定义 | `{"type":"function","function":{"name":..., "parameters":...}}` 结构不能改；assistant `tool_calls` 字段也不能改 |
| Slash 命令通路 | `cli/app.py:handle_slash_command` | `/model /tool /skill /mem0 /clear /sandbox /session /exit` 必须仍然走 CLI 层而非 LLM 层 |

### 3.5.2 Session 与并发模型（内部不变量）

| 不变量 | 实现位置 | 约束 |
|---|---|---|
| `current_session_id.set(session_id)` 在 turn 入口执行 | `loop/engine.py:25-86` | 移植后必须保留；否则 `BashTerminalTool` / `PythonInterpreterTool` 无法定位 session 沙箱 |
| `ContextVar` 跨 `asyncio.to_thread` 自动传播 | Python 3.9+ 标准库行为 | **禁止改成裸 `ThreadPoolExecutor` 或 `loop.run_in_executor(executor, ...)` 配自定义 executor**，否则 ContextVar 不传播 |
| `SessionProcessManager` 单例 + 锁 | `tools/execution_ops.py` | 多 session 并发时仍然按 session_id 隔离 bash 进程，不能因 streaming_executor 改用全局共享 shell |
| `security.intercept(tool_name, input_data, user_id)` 是同步方法 | `security/manager.py:301+` | **禁止异步化**；并行工具执行时必须通过 `asyncio.Lock` 串行化 Layer 3 人工审计阶段（不能两个工具同时弹 input()） |
| Layer 3 使用 `input()` 阻塞 | `security/manager.py:_human_audit (line 274+)` | 必须在 `asyncio.to_thread` 中调用，且整个 `_human_audit` 区段需要在 SecurityManager 内加 `_human_lock: asyncio.Lock`；KeyboardInterrupt 必须捕获并转为 `aborted` terminal，不冒泡 |

### 3.5.3 Memory / Context / Schema 不变量

| 不变量 | 实现位置 | 约束 |
|---|---|---|
| `memory.save_context(session_id, messages)` 全量覆盖写 | `memory/manager.py` | **一个 turn 末尾仅调用一次**；不能改成逐消息落盘（会破坏 mem0 后台线程触发节奏 + 增加 IO 抖动） |
| `is_meta=True` 消息被 `build_hot_swapped_context` 过滤 | `loop/engine.py:122-142` | length 恢复 nudge、其他临时注入消息必须打 `is_meta=True`，避免持久化进入 transcript |
| `sanitize_surrogates` 在所有 LLM 调用前生效 | `loop/engine.py:47-62` | 移植后 QueryEngine 也必须在 `callModel` 入口调用；否则会导致 `UnicodeEncodeError` 序列化崩溃 |
| `MAX_SINGLE_OUTPUT_LIMIT = 40000` 单条工具输出截断 | `loop/engine.py` | 截断逻辑必须保留 |
| `tiktoken cl100k_base` 精确计算 | `context/manager.py` | 压缩判定仍使用此口径，不引入 Anthropic 专有 token 计算 |
| AsyncOpenAI 客户端生命周期 | 当前 `engine.py` 每轮新建 client，**未 close 是隐患** | 改造时由 `QueryEngine` 持有 client + finally `await client.close()` 修复 |

### 3.5.4 现有调用栈与配套测试

实施时**必须验证**以下端到端通路仍工作：
1. `harness-lite "1+1=?"` 单轮无工具 → 正常返回字符串
2. `harness-lite -i` 多轮交互 → mem0 状态保持 + slash 命令工作
3. `bash_terminal` + `python_interpreter` 在 session 沙箱内执行 → CWD 隔离 + 资源限制生效
4. 双 session 并发 → ContextVar 不串台
5. Ctrl+C 在 Layer 3 人工审计时 → 转 aborted，不冒泡 KeyboardInterrupt 到 CLI

---

## 四、移植到 Harness-Lite 的实现规划

### 4.1 架构对齐

| Adopt-Code 概念 | Harness-Lite 现有对应 | 是否需要新增 |
|----------------|---------------------|------------|
| QueryEngine | （无） | **新增 `loop/query_engine.py`** |
| query (AsyncGenerator) | `loop/strategy.py:ReActStrategy` | **重构升级** |
| StreamingToolExecutor | `loop/engine.py:process_tool_calls_async` | **抽取独立类** |
| DynamicContextManager | `context/manager.py` | 已有，需扩展多级压缩 |
| Memory Prefetch | `memory/manager.py` + mem0 | 已有，需新增“并行预取”模式 |
| Skill Prefetch | `registry/skill_registry.py` | 已有，需新增“启发式发现”接口 |
| canUseTool / permission | `security/manager.py` 三层防御 | 已有 |
| Transcript | `memory/store.py` JSON 会话 | 已有 |
| stream_event / SDKMessage | 自定义 dict | **新增 `loop/messages.py` 类型** |
| autocompact / microcompact / snip / collapse | 单一 DynamicContextManager | **拆分成四级流水线** |
| stop hooks / post-sampling hooks | （无） | **新增 `loop/hooks.py`** |
| max_output_tokens 升档 | （无） | **新增** |
| token budget continuation | `ReActStrategy` 步数熔断 | 增强为 token-based |

### 4.2 文件级落地清单

每个新增文件附带**实现约束**（建立时必须遵守）：

```
loop/
├── engine.py             ← 【保留】AsyncLoopEngine 薄封装
│                            约束：run(...) -> str 签名不变；内部委托 QueryEngine；
│                                  保留 current_session_id.set() / sanitize_surrogates / MAX_SINGLE_OUTPUT_LIMIT
│
├── query_engine.py       ← 【新】L1 会话编排层
│                            约束：必须作为 AsyncLoopEngine.run 内部委托；
│                                  持有 AsyncOpenAI 客户端 + finally await client.close()；
│                                  submitMessage() 是 AsyncGenerator，但 engine.run() 外层消费它并 fan-out 到 callback
│
├── strategy.py           ← 【重写】L2 ReAct 循环
│                            约束：改为 AsyncGenerator yield messages；
│                                  保留 max_steps=15 与 consecutive_errors=3 熔断；
│                                  State 全量替换 + continue 模式
│
├── streaming_executor.py ← 【新】流式工具执行器
│                            约束：asyncio.create_task 并发；
│                                  finally 中 task.cancel() + asyncio.gather(return_exceptions=True) 回收；
│                                  通过 asyncio.shield 保护"安全审计 + 落盘"关键区；
│                                  每个未完成 tool_use 必须补 synthetic tool message（否则 OpenAI 400）
│
├── messages.py           ← 【新】消息类型定义
│                            约束：dataclass 或 TypedDict；
│                                  AssistantMessage / UserMessage / SystemMessage / AttachmentMessage /
│                                  TombstoneMessage / StreamEvent / ToolUseSummary
│
├── hooks.py              ← 【新】post-sampling / stop hooks 钩子框架
│                            约束：每个 hook asyncio.wait_for(timeout=N)；
│                                  单 hook 异常 → 记录 + 跳过；
│                                  stop_hook 异常 → 默认 fail-open（放行终止）
│
├── compaction.py         ← 【新】多级压缩流水线
│                            约束：snip / micro / collapse / auto 四级；
│                                  前三级可先 stub 抛 NotImplemented，重点跑通 autocompact 接现有 DynamicContextManager；
│                                  对外暴露 try_compact(state) -> CompactResult 单一入口
│
├── recovery.py           ← 【新】错误恢复策略
│                            约束：必须显式定义 RecoveryBudget dataclass，集中管理：
│                                  max_output_tokens_recovery_count (≤3)
│                                  reactive_compact_attempted (一次性 bool)
│                                  fallback_model_used (一次性 bool)
│                                  禁止裸 except Exception；narrow catch 见七节异常清单
│
└── terminal.py           ← 【新】Terminal 枚举与终止判定
                             约束：Enum：completed / aborted_streaming / aborted_tools /
                                   model_error / image_error / prompt_too_long /
                                   blocking_limit / max_turns / hook_stopped / stop_hook_prevented

context/
└── manager.py            ← 扩展：暴露各级 compact 接口给 compaction.py 编排
                             约束：DynamicContextManager 单例不变；新增方法不破坏既有 compress_if_overflow 签名
```

### 4.3 实施阶段（5 阶段交付，每阶段必须 CLI 端到端通过）

| 阶段 | 范围 | 验收门 |
|---|---|---|
| **A** | `loop/messages.py` + `loop/terminal.py`（纯类型，零行为变更） | mypy 通过；CLI 端到端通过（老代码继续工作） |
| **B1** | `loop/engine.py:call_llm_async` 内部改 AsyncGenerator 协议层；strategy 仍消费聚合后的 dict（适配层） | CLI 端到端通过；流式输出 / 思考模式 / length 恢复全部正常 |
| **B2** | 新增 `loop/query_engine.py` 骨架 + `AsyncLoopEngine.run` 委托改造；保持 `run(...) -> str` 签名不变 | CLI 端到端通过 + 交互模式多轮 mem0 状态保持 + slash 命令工作 |
| **C** | 重写 `loop/strategy.py` 为 AsyncGenerator + 接入 `query_engine` + 新增 `loop/recovery.py`（含 413 / CancelledError / KeyboardInterrupt 显式分支） | 双 session 隔离测试 + Ctrl+C 测试 + tool error cascade 测试全部通过 |
| **D** | 新增 `loop/streaming_executor.py`（并行工具执行）+ `loop/compaction.py` 多级 + `loop/hooks.py` | 完整 4.5 测试集通过 |

**每阶段交付后必须验证**：
- `harness-lite "1+1=?"` 单轮通过
- `harness-lite -i` 交互模式 3 轮以上正常
- `pytest tests/ -v` 全绿
- 双 session 并发跑 bash_terminal 互不污染

任一项失败必须回滚阶段，不进入下阶段。

### 4.4 关键不变量与必须复刻的设计（14 条）

1. **withhold pattern**：可恢复错误必须截留不 yield，确保 SDK 消费方不会因中间错误终止会话
2. **AsyncGenerator 流式产出**：所有消息（含中间事件）必须以生成器 yield，不能聚合成最终列表返回（**例外**：`AsyncLoopEngine.run` 外层兼容方法可以消费生成器后返回 str，但内部 query_engine.submitMessage 必须是 generator）
3. **State 全量替换 + continue**：禁止零散写多个 state 字段，统一构造 `next: State` 后 `state = next`
4. **prefetch + lazy consume**：耗时操作（skill discovery / memory）在流式调用前启动，在工具执行完后消费
5. **熔断保护**：reactive compact 已执行过的标志位 `hasAttemptedReactiveCompact` 必须跨迭代保留，防止死循环
6. **abort 时必须 yield 合法 tool_results**：每个未完成的 tool_use 都要补一个 synthetic tool_result，否则消息历史不闭合（OpenAI 会 400）
7. **transcript 持久化时序**：`save_context` 一个 turn 末尾仅调用一次，全量覆盖写
8. **AsyncGenerator finally 闭合**：所有 generator 必须在 `try/finally` 中：
   - cancel 未完成的 streaming_executor task
   - cancel 异步 pending_tool_use_summary（haiku 摘要）任务
   - 强制兜底 `save_context`（防止异常路径丢消息）
   - 关闭 AsyncOpenAI httpx client
9. **RecoveryBudget 硬上限**：
   - `max_output_tokens_recovery ≤ 3`
   - `reactive_compact_attempted` 一次性（true 后不再尝试）
   - `fallback_model_used` 一次性
   - 任一同类 error 连续 N 次未恢复 → 强制 `terminal = model_error`
10. **streaming_executor 取消语义**：
    - 通过 `asyncio.shield` 保护“安全审计 + memory 落盘”关键区，避免 cancel 中断落盘
    - 每个未完成的 tool_use 必须补 synthetic `tool` 消息
    - cancel 后必须 `await asyncio.gather(*tasks, return_exceptions=True)` 等待全部回收
11. **Hook 故障隔离**：
    - 每个 hook `asyncio.wait_for(timeout=N)`
    - 单 hook 异常 → 记录 + 跳过（不影响主循环）
    - stop_hook 异常 → 默认 fail-open（放行终止，避免误锁死会话）
12. **Layer 3 互斥**：streaming_executor 并行执行时，`SecurityManager` 必须加 `_human_lock: asyncio.Lock`，确保 `input()` 不被多任务抢占；KeyboardInterrupt 必须在 `intercept` 外层捕获并转为 `aborted` terminal
13. **AsyncOpenAI 生命周期**：改由 `query_engine` 持有 + finally close（修复现有 `engine.py` 每轮新建未 close 的隐患）
14. **ContextVar 隔离验收**：双 session 并发 + bash_terminal 必须互不污染；禁止改用自定义 ThreadPoolExecutor 破坏 ContextVar 自动传播

### 4.5 测试规划交付给 harness-code-reviewer

基础场景：
- 单轮无工具调用 → 直接 completed
- 单轮一次工具调用 → 工具执行 → 第二轮 completed
- 工具调用超出 max_turns → 产 max_turns 结果
- 安全拦截连续 3 次 → 熔断 hook_stopped
- 上下文超 7000 token → autocompact 触发 → boundary 消息正确插入

恢复场景：
- 模型抛 `openai.BadRequestError(code='context_length_exceeded')` → recovery.py 触发 reactive compact；仍失败 → terminal=prompt_too_long
- `finish_reason == "length"` → length recovery（≤3 次）；超限 → terminal=model_error
- `openai.APITimeoutError` / `APIConnectionError` 瞬态 → SDK 内 max_retries=3 已重试；耗尽 → terminal=model_error
- `openai.RateLimitError` → 指数退避 ≤2 次 → terminal=model_error

中断与隔离场景：
- 用户 Ctrl+C 在 Layer 3 人工审计时 → 转 `aborted` terminal，KeyboardInterrupt 不冒泡到 CLI
- 用户 Ctrl+C 在流式输出中 → `aborted_streaming`，所有未完工具补 synthetic result
- 并行工具执行中 abort → 所有未完 tool_use 必须补 synthetic `tool` 消息，messages 闭合（验证：下一轮 LLM 调用不报 400）
- 双 session 并发 + bash_terminal → ContextVar 不串台（session A 的 cd 不影响 session B）
- 双 session 并发 + Layer 3 → `_human_lock` 串行，不会两个 input() 抢占

集成场景：
- mem0 开启时 prefetch 应包成 `asyncio.to_thread` 后注入 system prompt（不阻塞主循环）
- AsyncOpenAI client 长会话连接数不递增（grep `httpx` 连接池状态或观察 `lsof` fd 数）
- SessionProcessManager 在 streaming_executor 并发场景下仍正确按 session_id 隔离 shell 进程
- slash 命令（`/clear` `/mem0` `/sandbox`）在新流程下仍直接由 CLI 处理，不进 LLM

---

## 五、给两个子 Agent 的工作分配

### harness-coder（实施 agent）
- **任务输入**：本 master.md + `adopt-code/` 源代码 + 现有 `src/harness_lite/` 模块
- **交付物**：按阶段 A→B1→B2→C→D 顺序产出代码，每阶段提交后等审查通过再进下一阶段
- **必须遵循**：
  - 严格遵守 3.5 节兼容性约束（13 条契约 + 14 条不变量）
  - 保持低耦合，新增的 loop 子模块之间通过显式接口而非全局单例通信
  - 严格 type hint，所有 message 类型必须有 dataclass / TypedDict 定义
  - 不破坏现有 `Tool` / `Skill` / `Security` 接口
  - 异常处理必须遵守第七节异常分类清单，禁止裸 `except Exception` 兜底（除最外层 `query_engine.submitMessage` 的最终 finally 之外）
  - 每阶段交付前自跑 `pytest tests/ -v` + `harness-lite -i` 手测 3 轮

### harness-code-reviewer（审查 agent）
- **任务输入**：harness-coder 的每阶段产出
- **重点关注**：
  1. AsyncGenerator 是否在所有错误路径都正确闭合资源
  2. withhold/recovery 路径是否会产生死循环（验证 RecoveryBudget 计数器递增 + 上限）
  3. 工具执行的 session 隔离是否被破坏
  4. 安全防御三层是否在新流程中仍被强制经过
  5. 消息序列化是否处理 Surrogate 字符（参见 `.claude/ERRORS.md`）
  6. Schema 仍是嵌套 function-calling 格式
  7. 异常分类是否符合第七节清单，无裸 `except Exception`

#### P0 必须红线检查表（违反任意一条直接打回）

| # | 检查项 | 验证方法 |
|---|---|---|
| 1 | `AsyncLoopEngine.run(task, session_id, stream_callback, status_callback) -> str` 签名与返回值不变 | grep `def run` + 类型检查 |
| 2 | `current_session_id.set(session_id)` 在 turn 入口（QueryEngine.submitMessage 早期）执行 | grep `current_session_id.set` |
| 3 | `memory.save_context` 一个 turn 仅调用一次（含异常 finally 兜底也算一次） | 代码评审 + 日志注入 count 验证 |
| 4 | tool schema 嵌套 function 格式 `{"type":"function","function":{...}}` 未改 | grep tool schema 定义 |
| 5 | assistant `tool_calls` 字段结构不变 | 抓取流式响应日志比对 |
| 6 | `is_meta=True` 过滤逻辑保留（length 恢复 nudge 不进 transcript） | grep `is_meta` + 验证 transcript 不含 meta 消息 |
| 7 | `sanitize_surrogates` 在所有 LLM 调用前生效 | grep + 注入含 surrogate 字符的测试 |
| 8 | `security.intercept` 仍同步调用，未被并行化绕过；Layer 3 有 `_human_lock` 保护 | grep `intercept` + 并发测试 |
| 9 | max_steps=15 兜底防死循环；consecutive_errors=3 熔断保留 | grep 常量 + 注入持续报错工具测试 |
| 10 | 双 session 并发 ContextVar 隔离（bash_terminal 不串台） | 写并发测试用例 |

---

## 六、暂不移植的特性（明确范围）

### 6.1 Anthropic / Claude Code 专有，Harness-Lite 不适用
- `task_budget` / `cache_deleted_input_tokens` / `maxBudgetUsd` 精确计费 → 用现有 tiktoken 估算
- `chicagoMCP` / `BG_SESSIONS` / `CONTEXT_COLLAPSE` 等实验 feature → 在 compaction.py 留 stub 接口但不实现
- `streamingFallbackOccured` 跨厂商模型 fallback → 一期只支持单模型 + 简单重试
- `coworkPlugin` / `IDE 集成` / `fileHistory snapshot` → 不属于 loop 范畴

### 6.2 Adopt-Code 专有，Harness-Lite 用替代方案
- **MCP / `refreshTools()`** → Harness-Lite 用 `tool_registry` 单例，工具集合在启动期固化；不需要 turn 内动态刷新
- **`structuredOutput` / `jsonSchema` 重试** → 一期不实现结构化输出，CLI 只输出文本
- **`pendingCommandUuids` / `consumedCommandUuids`** → turn 内 slash 命令注入；Harness-Lite 的 slash 命令由 CLI 层直接处理，一期不实现 LLM 层注入
- **`discoveredSkillNames` 跨 turn 跟踪** → `skill_registry` 已全局；不需要 QueryEngine 内单独跟踪
- **Multi-agent / subagents** → Harness-Lite 是单 agent loop，不引入子 agent 编排
- **`coordinator userContext`** → 没有 coordinator 概念，userContext 直接来自 memory + cwd

### 6.3 留接口但不实现（compaction.py stub）
- `snip_compact_if_needed` → 抛 `NotImplementedError`，留 hook
- `microcompact` → 抛 `NotImplementedError`，留 hook
- `context_collapse` → 抛 `NotImplementedError`，留 hook
- `autocompact` → **唯一实现**，接到现有 `DynamicContextManager.compress_if_overflow`

---

## 七、异常分类清单（强制）

> **`recovery.py` 必须 narrow catch**，不允许裸 `except Exception` 兜底（除最外层 `query_engine.submitMessage` 的最终 finally 之外）。
> harness-coder 写代码时严格按本表分类；harness-code-reviewer 审查时逐条核对。

| # | 异常类型 | 触发源 | 恢复策略 | Terminal 落点 | 严重度 |
|---|---|---|---|---|---|
| 1 | `openai.BadRequestError`（含 `code == 'context_length_exceeded'` 或 message 含 `prompt is too long`） | LLM 调用 | reactive_compact 一次；`hasAttemptedReactiveCompact=True` 后再次触发 | 仍失败 → `prompt_too_long` | **P0** |
| 2 | `asyncio.CancelledError` | 用户 Ctrl+C / `abort_controller.abort()` 信号 | 取消所有 streaming_executor task → 补 synthetic tool_result → 兜底 save_context | 流式阶段 → `aborted_streaming`；工具阶段 → `aborted_tools` | **P0** |
| 3 | `KeyboardInterrupt`（Layer 3 input 期间） | 人工审计被中断 | 在 `intercept` 外层 `try/except` 捕获 → 转 `CancelledError` 路径 | `aborted` | **P0** |
| 4 | `finish_reason == "length"`（非异常，是 LLM 元信息） | LLM 输出截断 | length recovery（≤3 次注入 nudge）；max_output_tokens 升档到 64k 一次 | 超限 → `model_error` | OK（已有，迁出到 recovery.py） |
| 5 | `openai.APITimeoutError` / `openai.APIConnectionError` | 网络瞬态 | SDK 内 `max_retries=3` 已自动重试 | 耗尽 → `model_error` | P1 |
| 6 | `openai.RateLimitError` | 限流 | 指数退避 ≤2 次 | 耗尽 → `model_error` | P1 |
| 7 | `openai.InternalServerError` / `APIStatusError` (5xx) | LLM 服务端错误 | 指数退避 ≤2 次 | 耗尽 → `model_error` | P1 |
| 8 | `json.JSONDecodeError`（tool args 解析失败） | 模型生成错误 JSON | 已有 cascade 熔断（consecutive_errors++）；连续 3 次 → `hook_stopped` | `hook_stopped` | OK |
| 9 | 工具 `execute()` 抛异常 | 工具实现 bug 或执行环境异常 | 已有 traceback 捕获 → 作为 tool_result 内容返回；consecutive_errors++ | 熔断后 `hook_stopped` | OK |
| 10 | 流 chunk `delta is None` | 第三方网关行为差异 | 已有 `continue` 防御（`engine.py:237-238`） | — | OK |
| 11 | `UnicodeEncodeError` / surrogate 字符 | 历史污染或工具输出 | 已有 `sanitize_surrogates` 递归净化 | — | OK |
| 12 | `security.intercept` 返回 `(False, reason)` | Layer 1/2/3 拦截 | 作为 tool_result 内容返回拒绝原因；consecutive_errors++ | 熔断后 `hook_stopped` | OK |
| 13 | `asyncio.TimeoutError`（hook 超时） | Hook 执行超时 | 记录 + 跳过该 hook | — | P2 |
| 14 | 其他未分类异常 | 未知 | **仅在 `query_engine.submitMessage` 最外层 finally** 捕获 → log + 兜底 save_context | `model_error` | P2 |

### 7.1 实现要求

```python
# recovery.py 推荐结构
@dataclass
class RecoveryBudget:
    max_output_tokens_recovery_count: int = 0      # ≤3
    has_attempted_reactive_compact: bool = False   # 一次性
    has_attempted_fallback_model: bool = False     # 一次性
    consecutive_tool_errors: int = 0               # ≥3 → 熔断

# 严格分类示例（禁止裸 except）
try:
    async for chunk in client.chat.completions.create(...):
        ...
except openai.BadRequestError as e:
    if 'context_length_exceeded' in str(e).lower() or 'prompt is too long' in str(e).lower():
        return await _try_reactive_compact(state, budget)
    raise  # 其他 BadRequestError 不归 recovery 管，向上抛
except openai.RateLimitError:
    return await _backoff_retry(state, budget, max_attempts=2)
except (openai.APITimeoutError, openai.APIConnectionError):
    # SDK 已重试，到这里就放弃
    return Terminal.MODEL_ERROR
except asyncio.CancelledError:
    await _emit_synthetic_tool_results(state)
    raise  # 必须重新抛出，让上层 finally 闭合资源
```

### 7.2 最外层兜底（唯一允许的 `except Exception`）

```python
# query_engine.py:submitMessage
async def submit_message(self, prompt, ...):
    try:
        async for msg in self._run_query_loop(...):
            yield msg
    except asyncio.CancelledError:
        raise  # CancelledError 必须重新抛出，不能吞掉
    except Exception as e:
        logger.exception("Unhandled exception in submitMessage")
        yield SystemMessage(subtype='error_during_execution', content=str(e))
        # terminal = model_error
    finally:
        await self._cleanup()  # close client + cancel tasks + save_context 兜底
```

---

> 完成本 master.md 的审阅后，请告知是否需要调整阶段拆分或落地文件结构；确认后将启动 harness-coder 开始阶段 A 实施。
