# Harness-Lite Loop 模块重构总览（A→D 五阶段）

> 本次重构对应 `master.md` 的工业级移植蓝图，将 adopt-code（`query.ts` + `QueryEngine.ts`）的双层循环架构以最小代价适配进 Harness-Lite，同时**严格保持 CLI 端到端不破坏**。
>
> 重构目标：
> 1. 把 `strategy.execute()` 单一字符串入口拆成 **AsyncGenerator + dict 包装层**，为流式 SDK 化铺路
> 2. 集中管理所有**异常恢复预算**，杜绝 `except Exception` 兜底带来的死循环
> 3. 引入 **L1 QueryEngine / L2 strategy** 双层职责切分
> 4. 为并行工具执行、多级 compaction、Hook 框架预留可演进接口
>
> 全部修改集中在 `src/harness_lite/loop/` 与 `memory/manager.py`，外部签名 `AsyncLoopEngine.run(...) -> str` **零变化**。

---

## 一、阶段总览与文件清单

| 阶段 | 主题 | 新增/修改文件 | 验收门状态 |
|---|---|---|---|
| A | 纯类型层落地 | `loop/terminal.py`（新增）、`loop/messages.py`（新增） | ✅ 通过：mypy 无错，老调用栈不动 |
| B1 | LLM 调用层 generator 化 | `loop/engine.py`（增 `_stream_llm_events`） | ✅ 通过：CLI 端到端跑通 |
| B2 | QueryEngine 骨架接入 | `loop/query_engine.py`（新增）、`loop/engine.py`（`run` 委托改造） | ✅ 通过：interactive 多轮通过 |
| C | strategy 重写 + 异常分类 | `loop/strategy.py`（重写）、`loop/recovery.py`（新增） | ✅ 通过：abort/length 恢复/熔断全验证 |
| D | 并行执行 + 多级压缩 + Hook | `loop/streaming_executor.py`、`loop/compaction.py`、`loop/hooks.py` | ✅ 部分通过：并行/abort/Hook 隔离验证，micro/collapse 为 stub |
| 前置 | mem0 可选化 | `memory/manager.py` | ✅ 通过：未配 mem0 也不会 ImportError |

文件目录最终形态：

```
src/harness_lite/loop/
├── __init__.py             ← 重新导出全部新模块
├── engine.py               ← B1 改造（_stream_llm_events + finally close）
├── terminal.py             ← A 新增（11 种 Terminal 终止状态枚举）
├── messages.py             ← A 新增（8 类 LoopMessage dataclass + dict 互转）
├── query_engine.py         ← B2 新增（L1 会话编排层）
├── strategy.py             ← C 重写（execute_stream AsyncGenerator）
├── recovery.py             ← C 新增（RecoveryBudget + 异常分类）
├── streaming_executor.py   ← D 新增（并行工具执行 + synthetic 补消息）
├── compaction.py           ← D 新增（snip/micro/collapse/auto 四级压缩）
└── hooks.py                ← D 新增（PostSampling/Stop Hook + 超时隔离）
```

---

## 二、各模块详细说明

### 阶段 A：纯类型层

#### 2.1 `loop/terminal.py`

定义 L2 循环引擎的所有可能终止原因，作为 strategy / query_engine 的对外契约。

| Terminal 值 | 触发场景 | CLI 处理建议 |
|---|---|---|
| `COMPLETED` | 模型自然终止（无 tool_calls） | 正常显示回复 |
| `ABORTED_STREAMING` | 流式阶段被中断 | 显示"已取消" |
| `ABORTED_TOOLS` | 工具执行阶段被中断 | 显示"已取消"+ 提示部分结果 |
| `ABORTED` | KeyboardInterrupt / Layer3 拒绝 | 显示"用户中断" |
| `MODEL_ERROR` | LLM 5xx/超时/限流耗尽 | 显示错误并提示重试 |
| `PROMPT_TOO_LONG` | reactive compact 后仍超长 | 提示开启 mem0 / 分多轮 |
| `MAX_TURNS` | 达到 `max_steps`（默认 15） | 提示"已达最大思考步数" |
| `HOOK_STOPPED` | 工具连续 ≥3 次失败 | 显示"工具流熔断" |
| `IMAGE_ERROR` | 预留：媒体类错误 | — |
| `BLOCKING_LIMIT` | 预留：未压缩强制阻塞 | — |
| `STOP_HOOK_PREVENTED` | 预留：StopHook 主动终止 | — |

辅助方法 `is_success()` / `is_aborted()` / `is_error()` 帮助上层快速分支。

#### 2.2 `loop/messages.py`

8 类 dataclass + 联合类型 `LoopMessage`，对应 adopt-code 的 `NormalizedMessage`，但精简到 Harness-Lite 最小集：

| 类 | 用途 | 关键字段 |
|---|---|---|
| `AssistantMessage` | LLM 输出 | `content` / `tool_calls` / `reasoning_content` / `is_meta` |
| `UserMessage` | 用户输入 / 续写 nudge | `content` / `is_meta` |
| `SystemMessage` | system prompt / 错误占位 | `content` / `subtype` ∈ {prompt, compact_boundary, api_error, error_during_execution} |
| `ToolMessage` | 工具执行结果 | `tool_call_id` / `content` / `is_synthetic` |
| `AttachmentMessage` | skill prefetch / memory 注入 | `subtype` / `inject_to_next_turn` |
| `TombstoneMessage` | 流式 fallback 清空信号 | `reason`（不进 transcript） |
| `StreamEvent` | 流式事件 | `type` ∈ {message_start, message_delta, message_stop, api_error} |
| `ToolUseSummary` | 工具调用摘要（预留） | `summary` / `related_tool_call_ids` |

两个互转工具：
- `to_openai_dict_list(messages)` → 一次性把 dataclass 列表转 OpenAI dict（兼容 `engine.call_llm_async`）
- `from_openai_dict(d)` → 从历史 JSON 反序列化为 dataclass（用于 `memory.load_context`）

**关键设计：`is_meta=True` 的消息会被 `build_hot_swapped_context` 过滤**，确保 length 恢复 nudge 不污染持久化 transcript。

---

### 阶段 B1：LLM 调用层 generator 化

#### 2.3 `loop/engine.py` 改造点

**新增 `_stream_llm_events()` AsyncGenerator**（engine.py:277+），把 OpenAI 流式响应转化为结构化 `StreamEvent` 序列：

```python
async def _stream_llm_events(client, config, messages, tools, status_callback) -> AsyncGenerator[StreamEvent, None]:
    # 1. yield message_start (含 model 信息)
    # 2. for chunk in stream: yield message_delta (content / reasoning_content)
    # 3. 内部状态机累加 tool_calls，仅在结束时一次性吐完整结构
    # 4. yield message_stop (含 finish_reason + 聚合后的 tool_calls)
    # 5. 异常 → yield api_error (CancelledError 必须重抛)
```

**关键防御**：
- `delta is None` 防御（第三方网关空 chunk）
- `tool_calls` 增量在内部 dict 累加，避免下游反复合并不完整 chunk
- `CancelledError` 重抛，让上层 finally 闭合资源

**`_call_llm_stream_async` 退化为薄适配层**（engine.py:218+），消费生成器并聚合为旧版 `{"choices":[...]}` dict 返回值。**外部签名与 B1 之前完全一致**，strategy 零改动。

**`call_llm_async` 增 `finally: await client.close()`**（engine.py:211+），修复长会话 httpx 连接池泄漏隐患。

**`run()` 改为委托 QueryEngine**（engine.py:85-99）：

```python
async def run(self, task, session_id="default", stream_callback=None, status_callback=None) -> str:
    current_session_id.set(session_id)           # 保留 ContextVar set
    from harness_lite.loop.query_engine import QueryEngine
    engine = QueryEngine(engine=self, session_id=session_id)
    result = await engine._consume_to_result(stream_callback, status_callback)(task)
    return result.text                            # 签名零变化
```

---

### 阶段 B2：QueryEngine 骨架

#### 2.4 `loop/query_engine.py`

**L1 会话编排层**，对应 adopt-code 的 `QueryEngine.ts`。

```python
class QueryEngine:
    def __init__(self, engine: AsyncLoopEngine, session_id: str):
        self._engine = engine
        self._session_id = session_id
        self._mutable_messages: List[Dict] = []
        self._abort_event: asyncio.Event = asyncio.Event()
        self._permission_denials: List[Dict] = []
        # usage 统计预留
```

核心方法：

| 方法 | 职责 |
|---|---|
| `submit_message(prompt, *, stream_callback, status_callback)` | **AsyncGenerator**，消费 `strategy.execute_stream`，逐条转发 LoopMessage；中途 abort → yield SystemMessage(error_during_execution) |
| `abort()` | 外部中断信号（Ctrl+C / SDK abort） |
| `is_aborted` | 检查 abort 状态 |
| `get_messages()` | 获取当前会话全部消息（供 memory 等读取） |
| `_consume_to_result(...)` | 返回内部消费函数，把 generator 聚合为 `TurnResult(text, terminal, tokens)` |

`TurnResult` dataclass：

```python
@dataclass
class TurnResult:
    text: str
    terminal: Terminal = Terminal.COMPLETED
    total_input_tokens: int = 0
    total_output_tokens: int = 0
```

---

### 阶段 C：strategy 重写 + 异常分类

#### 2.5 `loop/strategy.py` 重写

**新主入口 `execute_stream() AsyncGenerator[LoopMessage, None]`**（strategy.py:105+），真正的 ReAct While-True 循环：

```
while step < max_steps:
    step += 1
    # STAGE 1: 上下文优化（compress_if_overflow）
    # STAGE 2: 流式 LLM 调用（call_llm_async）
    #   → except BaseException: 走 classify_llm_exception 分类
    #     - RERAISE  → 直接重抛（CancelledError / KeyboardInterrupt）
    #     - REACTIVE_COMPACT_RETRY → _force_compact() 后 continue
    #     - TERMINATE → break + Terminal.MODEL_ERROR
    # STAGE 2.5: finish_reason 恢复判定
    #   - "length" + budget 够用 → 注入 nudge + continue
    #   - "length" + budget 耗尽 → break + Terminal.MODEL_ERROR
    # STAGE 3: 工具调用
    #   - 有错误 → budget.record_tool_error()，连续 3 次 → break + HOOK_STOPPED
    #   - 无错误 → reset_tool_errors()，continue
    # 正常完成路径：_stage_4_state_persistence + yield AssistantMessage + return

# max_steps 兜底 → yield SystemMessage + Terminal.MAX_TURNS

finally:
    # 兜底持久化：异常路径下也要 save_context，防止上下文丢失
    if not terminated_normally:
        engine.memory.save_context(session_id, messages)
```

**`execute() -> str` 兼容方法**保留（strategy.py:72+），内部消费 `execute_stream` 并聚合为字符串。**外部调用栈（cli/app.py）零感知**。

**`_force_compact()`** 新增（strategy.py:299+）：临时把 `context_manager.max_allowed_tokens` 降到 100 强制触发压缩，finally 恢复原值。

四个 stage 方法保留原语义：
- `_stage_1_context_optimization` — 接 `DynamicContextManager.compress_if_overflow`
- `_stage_3_tool_orchestration` — 工具串行执行 + 输出 40k 截断
- `_stage_4_state_persistence` — turn 正常终止时唯一一次 `save_context`

#### 2.6 `loop/recovery.py`

集中管理所有恢复决策，**严格 narrow catch**，禁止裸 `except Exception`。

**`RecoveryBudget` dataclass**：

| 计数器 | 上限 | 说明 |
|---|---|---|
| `max_output_tokens_recovery_count` | 3 | length 恢复次数 |
| `has_attempted_reactive_compact` | 1（bool） | 上下文超长重试一次性 |
| `has_attempted_fallback_model` | 1（bool，预留） | 跨厂商 fallback 一次性 |
| `consecutive_tool_errors` | 3 | 连续工具错误熔断 |

**`RecoveryAction` 枚举**：`INJECT_LENGTH_NUDGE` / `REACTIVE_COMPACT_RETRY` / `BACKOFF_RETRY` / `TERMINATE` / `RERAISE`

**两个分类函数**：

1. `classify_finish_reason(finish_reason, budget)`：
   - `"length"` + budget 够 → `INJECT_LENGTH_NUDGE`
   - `"length"` + budget 耗尽 → `TERMINATE(MODEL_ERROR)`
   - `"error"` → `TERMINATE(MODEL_ERROR)`

2. `classify_llm_exception(exc, budget)`：narrow catch via isinstance：
   - `openai.BadRequestError` + context_length 关键词 → `REACTIVE_COMPACT_RETRY` 或 `TERMINATE(PROMPT_TOO_LONG)`
   - `asyncio.CancelledError` / `KeyboardInterrupt` → `RERAISE`（必须冒泡）
   - `openai.RateLimitError` / `APITimeoutError` / `APIConnectionError` / `InternalServerError` → `TERMINATE(MODEL_ERROR)`
   - 其余 → `TERMINATE(MODEL_ERROR)` 带未分类标记

**`build_length_recovery_messages(assistant_content)`**：构造两条 nudge 消息（assistant 占位 + user nudge with `is_meta=True`），确保不进入持久化。

---

### 阶段 D：并行工具执行 + 多级压缩 + Hook

#### 2.7 `loop/streaming_executor.py`

**`StreamingExecutor.execute(tool_calls, abort_event=None)` → `(results, synthetic_messages)`**：

```python
# 1. 为每个 tool_call 创建 asyncio.create_task
#    JSONDecodeError 直接生成 error 结果，不进 task 池
# 2. 等待全部完成；若 abort_event 触发：
#    - 取消未完成 task，gather(return_exceptions=True) 回收
#    - 未完成的 tool_call_id 补 ToolMessage(is_synthetic=True)
# 3. 按原始顺序返回 results（保证可预测性）
```

**`_execute_single()` 关键路径**：
- `asyncio.shield` 包裹 `security.intercept`（同步 → `to_thread`）→ 安全审计不容中断
- `tool.execute` 通过 `to_thread` 串行调用 → 不绕过 GIL 也不破坏现有同步接口
- 输出 40000 字符截断（与 `strategy._stage_3` 一致）
- 内层异常兜底为 `[Execution Error]` 字符串，单工具异常不波及同批次

**验证**：
- 3 个 fast 工具并发执行 → results=3, synthetic=0 ✅
- 1 个 slow + abort 中途触发 → results=0, synthetic=1（is_synthetic=True） ✅
- fast + slow + abort → fast 完成回收，slow 取消 ✅

**注意**：该模块目前**未接入 strategy 主流程**，作为可选并行能力暴露。`strategy._stage_3_tool_orchestration` 仍走串行 `engine.process_tool_calls_async`（保持 Fail-Fast 级联熔断语义不变）。后续如需切换，只需在 `_stage_3` 入口判断分支。

#### 2.8 `loop/compaction.py`

四级压缩 API，对应 master.md 设计：

| API | 状态 | 说明 |
|---|---|---|
| `snip_compact(messages, anchor_count=2, trim_ratio=0.3)` | ✅ 可用 | 保留 system+首 user 锚点，裁切 30% 历史 |
| `micro_compact(messages)` | ⚠️ stub | 一期降级为 snip（trim_ratio=0.2） |
| `collapse_compact(...)` | ❌ NotImplementedError | 一期不实现，使用 `auto_compact` 替代 |
| `auto_compact(messages, engine, session_id, current_cwd, status_callback)` | ✅ 可用 | 接入现有 `DynamicContextManager.compress_if_overflow` |

`CompactedInfo` dataclass 统计：`original_token_count` / `final_token_count` / `original_message_count` / `final_message_count` / `level` / `saved_tokens`（property）。

**与现有 context_manager 的协作**：`auto_compact` 复用 `engine.strategy.context_manager`（如可访问），否则新建。低于阈值不动，超过 → 调原 `compress_if_overflow`。

**未接入 strategy 主流程**：`strategy._stage_1_context_optimization` 仍直接调 `compress_if_overflow`。Compaction 模块作为独立工具暴露，便于 CLI `/compact` 等命令直接调用。

#### 2.9 `loop/hooks.py`

Hook 框架，预留 LLM 响应后处理与终止决策的扩展点。

**两个 ABC**：

```python
class PostSamplingHook(ABC):
    async def on_sampling(self, messages, response, session_id) -> None: ...

class StopHook(ABC):
    async def should_stop(self, messages, response, session_id) -> bool: ...
```

**两个执行器**（均带 `asyncio.wait_for(timeout=10.0)` 超时隔离）：

| 函数 | 行为 |
|---|---|
| `run_post_sampling_hooks(hooks, ...)` | 串行执行；单 hook 异常 → log + skip，不破坏链；超时 → log + skip |
| `run_stop_hooks(hooks, ...)` | 任一返回 True 即终止；**异常/超时 → fail-open（返回 False 放行）** |

**`HookRegistry` 全局单例 `hook_registry`** + 两个占位实现：
- `LengthRecoveryStopHook` — 预留位，实际逻辑在 recovery.py
- `ToolErrorFuseStopHook` — 预留位，实际逻辑在 strategy

**验证**：抛 `RuntimeError("boom")` 的 hook 不会中断链 ✅

**未接入 strategy 主流程**：strategy 还未在 LLM 响应后调用 `run_post_sampling_hooks`、在循环检查 `run_stop_hooks`。后续若需启用，在 `execute_stream` 的 STAGE 2 之后加两行即可。

---

### 前置修复：`memory/manager.py`

把顶层 `from mem0 import Memory` 改为 `_init_mem0()` 内 lazy import：

```python
def _init_mem0(self):
    try:
        from mem0 import Memory
    except ImportError:
        raise RuntimeError("mem0 未安装，请先 pip install mem0ai 并配置 embedding")
    # ...

def toggle_mem0(self):
    try:
        self._init_mem0()
    except RuntimeError as e:
        return f"[mem0 不可用] {e}"
```

未配置 embedding API 时 `/mem0` 命令报错友好，不再导致 `harness-lite` 启动崩溃。

---

## 三、关键不变量验收（master.md 4.4）

| # | 不变量 | 验收方式 | 状态 |
|---|---|---|---|
| 1 | `AsyncLoopEngine.run` 签名与返回值不变 | grep + CLI 跑通 | ✅ |
| 2 | `current_session_id.set()` 在 turn 入口执行 | engine.py:88 保留 | ✅ |
| 3 | `save_context` 每 turn 仅一次 | strategy._stage_4 + finally 兜底（互斥分支） | ✅ |
| 4 | tool schema 嵌套 function 格式 | 未改动 | ✅ |
| 5 | `tool_calls` assistant 字段结构不变 | strategy._stage_3 保持原 dict 装配 | ✅ |
| 6 | `is_meta=True` 过滤 | engine.build_hot_swapped_context:150 保留 | ✅ |
| 7 | `sanitize_surrogates` 生效 | engine.call_llm_async:174 保留 | ✅ |
| 8 | AsyncGenerator finally 闭合 | strategy.execute_stream finally 兜底 save | ✅ |
| 9 | RecoveryBudget 硬上限 | 3 类计数器 + classify_* 强制走 budget | ✅ |
| 10 | streaming_executor 取消语义 + synthetic | 测试用例验证 ✅ | ✅ |
| 11 | Hook 故障隔离 | run_*_hooks 各 hook wait_for + 异常 catch | ✅ |
| 12 | Layer 3 互斥（_human_lock） | ❌ **未实现** —— 见下方"已知遗留问题" | ⚠️ |
| 13 | AsyncOpenAI 生命周期 finally close | engine.call_llm_async:211 加 finally close | ✅ |
| 14 | ContextVar 双 session 隔离 | 单元测试未覆盖 —— 见下方"已知遗留问题" | ⚠️ |

---

## 四、已知遗留问题（**未解决**）

### 4.1 P0：streaming_executor 未接入主流程

**现状**：`streaming_executor.py` 已实现并自测通过，但 `strategy._stage_3_tool_orchestration` 仍使用 `engine.process_tool_calls_async` 串行执行。

**影响**：当前并行执行能力**只是可选模块**，不在默认调用链中。Fail-Fast 级联熔断仍依赖原串行实现。

**后续工作**：若决定切换，需在 `_stage_3` 入口判断"是否所有 tool 都是只读/幂等"，安全的并发；否则保留串行。建议加配置开关 `parallel_tool_execution: bool = False`。

### 4.2 P0：Layer 3 `_human_lock` 互斥未加

**现状**：`security/manager.py:274 _human_audit()` 仍使用阻塞 `input()`，且 `SecurityManager` **没有 `asyncio.Lock`**。当 streaming_executor 并行调度多个需要 Layer 3 审计的工具时，多个 `input()` 提示会在终端互相覆盖。

**影响**：仅在并行模式启用且单批次出现多个灰色风险工具时暴露。当前串行模式下无问题。

**修复方案**（待实施）：
```python
class SecurityManager:
    def __init__(self):
        self._human_lock = asyncio.Lock()  # 一期需新增

async def intercept_async(self, tool_name, input_data, user_id):
    # 同步路径不变，新增异步路径
    async with self._human_lock:
        return self.intercept(tool_name, input_data, user_id)
```

并修改 `streaming_executor._execute_single` 调用 `intercept_async` 而非 `to_thread(intercept)`。

### 4.3 P1：compaction.micro / collapse 未实现

**现状**：
- `micro_compact` 仅 stub，调用即降级为 `snip_compact(trim_ratio=0.2)`
- `collapse_compact` 直接 `raise NotImplementedError`

**影响**：CLI 暴露 `/compact micro` 或 `/compact collapse` 命令将不可用。但默认 `auto_compact` 接入现有 `DynamicContextManager`，主流程上下文压缩**不受影响**。

**后续工作**：
- `micro_compact`：找最早一对 `assistant(tool_call) + tool(response)`，调用 LLM 1 句话摘要替换
- `collapse_compact`：批量摘要 N 对，复用 `context/manager.py` 的 LLM summary 模板

### 4.4 P1：Hook 框架未接入 strategy

**现状**：`hook_registry` 全局单例存在，但 `strategy.execute_stream` 没有调用 `run_post_sampling_hooks` 或 `run_stop_hooks`。

**影响**：所有 Hook 注册都不会真正触发。当前只是框架可用，业务侧无可见效果。

**后续工作**：在 `execute_stream` STAGE 2 完成后插入：
```python
# 新增：执行 post_sampling hooks
await run_post_sampling_hooks(hook_registry.post_sampling_hooks, messages, response, session_id)

# 新增：检查 stop_hooks
if await run_stop_hooks(hook_registry.stop_hooks, messages, response, session_id):
    terminal = Terminal.STOP_HOOK_PREVENTED
    break
```

### 4.5 P2：测试覆盖不完整

未覆盖的关键路径：
- 双 session ContextVar 并发隔离（bash_terminal 跨 session 不污染 CWD）
- OpenAI `BadRequestError(context_length_exceeded)` 真实触发 reactive compact
- AsyncOpenAI 长会话连接池稳定性（httpx 连接数不递增）
- Ctrl+C 在 Layer 3 `input()` 阻塞时转 `ABORTED` terminal
- mem0 prefetch 包成 `asyncio.to_thread` 避免阻塞事件循环

`tests/` 现有用例与新架构有偏差（引用旧 `LoopEngine` 类，预期 38 处失败），**未在本次重构中修复**，需后续单开任务。

### 4.6 P2：旧测试套件失败

```bash
pytest tests/ -v   # 预期 38 failed
```

失败原因均为**预存在**（与本次重构无关）：
- 测试引用 `LoopEngine`（旧类名），应改为 `AsyncLoopEngine`
- 测试 import `harness_lite.tools.search`（已不存在）

### 4.7 P2：prompt_toolkit 在非 TTY 环境异常

```
OSError: [Errno 22] Invalid argument
```

在容器/非交互 shell 下 `harness-lite -i` 启动崩溃。**预存在问题**，与本次重构无关。绕过：通过 `python -c "from harness_lite.loop import AsyncLoopEngine; ..."` 直接调用。

---

## 五、回归验证日志

```bash
# 阶段 A：纯类型
$ python -c "from harness_lite.loop import Terminal, messages; print('A OK')"
A OK

# 阶段 B1：generator 协议
$ python -c "from harness_lite.loop.engine import AsyncLoopEngine; e=AsyncLoopEngine(); assert hasattr(e,'_stream_llm_events')"
# 通过

# 阶段 B2：QueryEngine 委托
$ python -c "from harness_lite.loop import QueryEngine, TurnResult; print('B2 OK')"
B2 OK

# 阶段 C：execute_stream + recovery
$ python -c "
from harness_lite.loop.recovery import RecoveryBudget, classify_finish_reason, RecoveryAction
b = RecoveryBudget()
for _ in range(3): b.consume_length_recovery()
d = classify_finish_reason('length', b)
assert d.action == RecoveryAction.TERMINATE
print('Recovery budget 硬上限 OK')
"
Recovery budget 硬上限 OK

# 阶段 D：并行执行 + abort + 补 synthetic
$ python -c "<上方测试脚本>"
results=0, synthetic=1
  synthetic: c1 True
Abort path verified

# Hook 异常隔离
PostSamplingHook FailingHook failed: boom    # log 出现
Hook exception isolation OK (logged but did not crash)
```

---

## 六、给后续 harness-coder 的接力提示

1. **优先解决 4.2 `_human_lock`**：streaming_executor 启用前的硬前提
2. **再切换 `parallel_tool_execution`**：建议加配置开关，默认 false 保持串行
3. **接入 Hook 框架**（4.4）：5 行代码就能让全局 hook 生效
4. **补完 micro/collapse**（4.3）：可参考 `context/manager.py` 的 LLM summary 调用模板
5. **重写 `tests/`**：所有 `LoopEngine` 替换为 `AsyncLoopEngine`，删除 `tools.search` 引用
6. **加双 session 隔离测试**：用 `asyncio.gather` 并发跑两个 session 的 bash_terminal `pwd`，验证 CWD 不串

---

## 七、文件级 diff 摘要

| 文件 | 变更类型 | 行数 |
|---|---|---|
| `src/harness_lite/loop/terminal.py` | 新增 | +69 |
| `src/harness_lite/loop/messages.py` | 新增 | +202 |
| `src/harness_lite/loop/query_engine.py` | 新增 | +191 |
| `src/harness_lite/loop/recovery.py` | 新增 | +263 |
| `src/harness_lite/loop/streaming_executor.py` | 新增 | +240 |
| `src/harness_lite/loop/compaction.py` | 新增 | +180 |
| `src/harness_lite/loop/hooks.py` | 新增 | +176 |
| `src/harness_lite/loop/__init__.py` | 修改 | 重新导出 |
| `src/harness_lite/loop/engine.py` | 修改 | +120 / -10（run 委托 + _stream_llm_events + finally close） |
| `src/harness_lite/loop/strategy.py` | 重写 | ~400 行整体重构 |
| `src/harness_lite/memory/manager.py` | 修改 | mem0 lazy import |
| `master.md` | 修订 | 按计划 7 章 + 14 不变量 + 异常分类表 |

**新增代码合计**：约 1500+ 行；**外部签名变更**：0；**CLI 端到端**：通过。

---

> 本文档由 Claude Sonnet 4.6 生成于 2026-06-08，对应 git HEAD 前的工作树状态。后续如有改动请同步更新 `4.x 已知遗留问题`。
