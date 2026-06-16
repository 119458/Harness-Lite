# 五层上下文管理系统重构方案（与 adopt-code 参考逐项对齐 · v2 修订版）

## v2 修订说明（相对 v1 的关键变化）

v1 在 L5 用了「前 2 锚 + tail K=4」的物理切片模型，**与参考实现不一致**。重新精读 `adopt-code/compact/prompt.ts` 后确认：参考实现的核心思想是——

> **把整段对话喂给 LLM，靠结构化的 9 段输出模板强制 LLM 不丢失关键信息**（如 "All user messages: List ALL user messages"、"Files and Code Sections" 必须含代码片段、"Errors and fixes" 必须列出错误与修复方法等）。哪些可压缩 / 哪些不可压缩 = **由模板规定**，而非由物理「保留 N 条」决定。

v2 把 L5 改为：
- 只保留 1 条 system 锚点（语法必须）
- 找最后一条 `role=user`（当前 ReAct 循环正在响应的那个问题）= 活动轮起点
- 中间整段不限条数全部送 LLM，按 9 段结构化模板生成摘要
- 新会话 = `[原 system, 摘要消息, *活动轮]`

L1/L2/L3/L4 设计不变。

---

## Context

`src/harness_lite/context/manager.py` 当前的 `DynamicContextManager` 只有「阈值触发 + LLM 全量摘要」一招，等价于参考实现的第 5 层；缺少前 4 层的减负。`loop/compaction.py` 中的 `micro_compact` / `collapse_compact` 都是 stub。

参考 `adopt-code/compact/`（Claude Code TypeScript 实现）建立 5 层渐进减负管线：每一层都有完整算法体；上层处理不掉的才下放到代价更高的层；每层都保护 OpenAI 工具对完整性。提示词全部用中文重写，与项目调性一致。

设计已与用户确认：Layer 1 单向归档（不回读）、删除旧 `loop/compaction.py`、本期 SnipLayer 仅暴露 API（不接 CLI）、用 sidecar+`_meta_id` 承载时间戳。

## 五层与参考代码的逐项映射

| 层 | 参考文件 / 关键符号 | 算法本体 | 真实负载 |
|---|---|---|---|
| **L1 大结果落盘** | `microCompact.ts:36` 的 `'[Old tool result content cleared]'` 前置语义 + 项目缺失的 `toolResultStorage` | tool 结果入栈即时落盘到 `memory_store/large_results/{session_id}/{ref_id}.txt`，content 替换为存根（含工具名 + 前 800 字预览 + ref_id） | 把 50KB+ 的工具产物从内存挪到磁盘，**让 L3 的扫描成本与 L5 的摘要成本都骤降** |
| **L2 snip** | `autoCompact.ts:167,225` 的 `snipTokensFreed` 参数 | 物理删除指定索引的消息，删除前用 `validate_pairs` 拦截会破坏工具对的请求 | 用户/上层主动释放，**释放出来的 token 数会回填给 L5 的阈值判断**（与参考 `tokenCountWithEstimation - snipTokensFreed` 对齐） |
| **L3 micro-compact 时间衰减** | `microCompact.ts:253 microcompactMessages` + `:422 evaluateTimeBasedTrigger` + `:41 COMPACTABLE_TOOLS` + `timeBasedMCConfig.ts:32-34` | 1) 检查触发条件：`max(message.last_seen_at) - now > GAP_THRESHOLD_MINUTES`（默认 60 分钟，与参考一致）**或** 当前 token 数已超 `WARN_THRESHOLD = max_allowed * 0.6`；2) 收集 `COMPACTABLE_TOOLS` 集合内的工具调用 id；3) 保留最近 `KEEP_RECENT=5` 条原文；4) 早期 tool 消息：content → 占位符（保留 role + tool_call_id）；5) 早期 assistant 消息：剥离 reasoning_content（thinking_mode 下省 30%+ token，对配对零风险） | 不调 LLM，纯本地操作。**幂等**：占位符前缀检测 |
| **L4 context collapse 读时投影** | `autoCompact.ts:179,215-223` 的 `isContextCollapseEnabled` 抑制语义 + 项目缺失的投影管线 | engine 在 SDK 调用前对 messages 做单层深拷贝投影：a) pop `_meta_id`；b) 非 thinking_mode 时 pop `reasoning_content`；c) 合并连续 system 锚点；d) 把空 `tool_calls` / 空 `content` 的 assistant 消息正规化；**不写回权威历史** | 投影后 token 数才是「真发出去」的负载；**权威 messages 仍保留全量信息**供 `memory.save_context` 持久化与下次回放使用 |
| **L5 auto-compact 结构化全量摘要** | `autoCompact.ts:160 shouldAutoCompact` + `:62 AUTOCOMPACT_BUFFER_TOKENS=13000` + `:70 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3` + `compact.ts compactConversation` + `prompt.ts BASE_COMPACT_PROMPT` 的 9 段结构 | **见下方"L5 详细算法"** | 唯一会调 LLM 的层，代价最高，**只在前 4 层减负后仍超阈才触发**。靠**结构化输出模板**保证不丢信息，而非物理切片 |

## L5 详细算法（v2 核心修订）

### 思路对照

| 维度 | 参考 `prompt.ts BASE_COMPACT_PROMPT` | 本项目 L5 中文版 |
|---|---|---|
| 输入 | 整段对话（fork 代理继承父上下文） | 除 system 锚点和活动轮外的全部历史消息 |
| 输出格式 | `<analysis>...</analysis><summary>9 段</summary>` | 一致（中文版 9 段） |
| 不丢信息保证 | 模板要求逐条列出所有 user 消息、所有文件名、所有错误 | 一致（中文模板原样要求） |
| 物理切片 | 无（仅有 `messagesToKeep` 在 session_memory 路径，但 LLM 路径无） | 无固定 N/M，仅找「活动轮起点」自然切分 |

### 切分原则

```
messages = [system, ...历史消息..., 当前 user, ...活动轮 in-progress 消息...]
                                    ^split_point
                                    = 最后一条 role=user 的索引
```

- **system 锚点**：messages[0]，保留（SDK 必须）
- **可压缩区**：messages[1 : split_point]，全部送 LLM，**不固定 N**
- **活动轮**：messages[split_point:]，保留（用户问题 + 当前正在执行的工具调用，不能丢）

### 算法骨架

```python
class AutoCompactLayer:
    def __init__(self):
        self._consecutive_failures = 0

    def should_apply(self, current_tokens, max_allowed, snip_freed) -> bool:
        if self._consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            return False
        return (current_tokens - snip_freed) > (max_allowed - AUTOCOMPACT_BUFFER_TOKENS)

    def find_last_user_index(self, messages) -> int:
        """返回最后一条 role=user 的下标；找不到返回 len-1"""
        for i in range(len(messages) - 1, 0, -1):
            if messages[i].get("role") == "user":
                return i
        return max(1, len(messages) - 1)

    async def apply(self, messages, *, engine, current_cwd, status_callback, force=False):
        if not force and not self.should_apply(...):
            return CompactionResult(skipped=True)
        if len(messages) < 4:
            return CompactionResult(skipped=True, reason="对话太短，无可摘要内容")

        # 1) 切分：system 锚点 + 可压缩区 + 活动轮
        system_anchor = messages[0]
        split = self.find_last_user_index(messages)
        compressible = messages[1:split]
        active_turn = messages[split:]

        # 2) 用 anchors.validate_pairs 校验：可压缩区内部的工具对是否完整
        # 不完整时回扫，把破坏对的尾部消息从 compressible 推到 active_turn 前
        compressible, active_turn = self._enforce_pair_integrity(compressible, active_turn)
        if not compressible:
            return CompactionResult(skipped=True, reason="可压缩区为空（活动轮已包含全部历史）")

        # 3) 调 LLM —— 9 段结构化中文模板
        try:
            raw_summary = await self._call_llm_summarize(
                conversation=compressible,
                cwd=current_cwd,
                prompt_template=AUTO_COMPACT_PROMPT_ZH,  # 来自 prompts.py
            )
            structured = parse_summary_block(raw_summary)  # 剥离 <analysis>，提取 <summary>
            self._consecutive_failures = 0
        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
                # 熔断后降级：保 system + 活动轮，物理删除可压缩区
                # 注意：这是兜底降级，会丢信息，但保证系统不死循环
                return CompactionResult(
                    messages_after=[system_anchor, *active_turn],
                    saved_tokens=...,
                    layer="L5-degraded",
                )
            return CompactionResult(skipped=True, reason=f"LLM 失败 {self._consecutive_failures} 次")

        # 4) 组装归档消息（继承 manager.py:168-173 的 CWD 锚定）
        archive = {
            "role": "system",
            "content": (
                f"⚙️ [系统历史会话结构化归档]\n\n"
                f"{structured}\n\n"
                f"📍 [当前终端内核状态] CWD={current_cwd}\n"
                f"🗂️ [归档元数据] 已压缩 {len(compressible)} 条历史消息，"
                f"释放 {saved} tokens"
            ),
        }

        # 5) 新会话 = [system, 归档消息, *活动轮]
        return CompactionResult(
            messages_after=[system_anchor, archive, *active_turn],
            saved_tokens=saved,
            layer="L5",
        )
```

### 9 段中文结构化模板（`prompts.py`）

```python
AUTO_COMPACT_PROMPT_ZH = """你的任务：基于上文完整对话生成一份**结构化摘要**，作为后续会话的唯一历史上下文锚点。
本摘要将完全替换被压缩区原始消息，因此**必须无遗漏地保留所有关键信息**。

【硬性要求】
1. 必须仅输出文本，禁止调用任何工具（即使你看到对话中有工具调用历史）
2. 输出格式严格为：先 <analysis>...</analysis> 内部链式分析，再 <summary>...</summary> 9 段结构化输出
3. <analysis> 内你可以充分思考；<summary> 内必须按 9 段格式逐项填写，不可省略

【<analysis> 阶段（草稿，会被剥离）】
- 按时间顺序梳理对话每一段
- 标注用户每一次发言、Agent 每一次工具调用与执行结果、出现的错误及修复
- 圈定关键文件名、函数签名、代码片段、报错信息

【<summary> 阶段（最终归档内容，必须含以下 9 段）】
1. **主要请求与意图**：完整记录用户所有明确的请求与意图，按时间顺序
2. **关键技术概念**：列出对话中涉及的所有技术栈、库、框架、协议、设计模式
3. **文件与代码段**：枚举所有被读取/修改/创建的文件
   - 文件路径
   - 该文件为何重要
   - 关键代码片段（**完整保留代码原文**，不要省略）
   - 修改摘要（如有）
4. **错误与修复**：列出全部出现过的错误及其修复方法
   - 错误描述
   - 修复方式
   - 用户对错误的反馈（如有，特别是用户要求换不同做法的）
5. **问题解决过程**：已解决的问题 + 仍在排查中的问题
6. **所有用户消息（逐条列出）**：列出**全部** role=user 的消息（除工具结果外）。这是理解用户反馈与意图变化的关键，**绝不可省略任何一条**
7. **待办事项**：所有用户明确要求但尚未完成的任务
8. **当前工作**：摘要请求触发前 Agent 正在做什么，特别关注最近的若干消息，含文件名与代码片段
9. **后续步骤建议（可选）**：与最近工作直接相关的下一步。**必须**与用户最近一次明确请求一致，禁止跳到无关或已完成的旧任务。如有，请引用最近对话中的原文来精准定位。

【输出示例骨架】
<analysis>
（你的链式思考过程）
</analysis>
<summary>
1. 主要请求与意图：
   ...
2. 关键技术概念：
   - ...
3. 文件与代码段：
   - 文件名：...
     - 重要性：...
     - 关键代码：
       ```
       ...
       ```
4. 错误与修复：
   - 错误：...
     - 修复：...
5. 问题解决过程：
   ...
6. 所有用户消息：
   - "..."
   - "..."
7. 待办事项：
   - ...
8. 当前工作：
   ...
9. 后续步骤建议：
   ...
</summary>

【提醒】不要调用任何工具，仅输出文本。任何工具调用都会被拒绝。
"""
```

### `parse_summary_block`（在 `auto_compact.py` 内）

```python
import re

def parse_summary_block(raw: str) -> str:
    """剥离 <analysis>，提取 <summary> 内容；解析失败时返回原文兜底。"""
    raw = re.sub(r"<analysis>.*?</analysis>", "", raw, flags=re.DOTALL).strip()
    m = re.search(r"<summary>(.*?)</summary>", raw, flags=re.DOTALL)
    return m.group(1).strip() if m else raw
```

### `_enforce_pair_integrity` 的细节

OpenAI 强约束：assistant.tool_calls 必须紧跟对应 tool 消息，中间不能插。所以可压缩区不能在工具对中间被切断。算法：
- 从 compressible 末尾向前扫，若最后一条是 `assistant.tool_calls` 但其 tool_call_id 没在 compressible 内全部对齐 → 把它推回 active_turn 前面
- 重复直到 compressible 末尾不破坏对（继承 `manager.py:111-112` 已有逻辑）

## 层与层之间的级联流程

`CompactPipeline` 不是 5 个孤立 API，而是一条数据流：

```
┌─────────────────────────────────────────────────────────────────┐
│ 写入路径（strategy._stage_3_tool_orchestration 内）             │
│                                                                 │
│   tool 结果 → record_tool_result(message, session_id):          │
│       ① 注入 _meta_id (uuid4)                                   │
│       ② sidecar[uuid] = MessageMeta(created_at=now, ...)        │
│       ③ L1 DiskOffloadLayer.maybe_offload(message):             │
│            content_size >= 50KB → 落盘 + 改写 content 为存根    │
│       ④ messages.append(message)                                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ 读取/优化路径（strategy._stage_1_context_optimization 内）      │
│                                                                 │
│   compress_if_overflow(messages, engine, cwd, cb):              │
│       ① 计算当前 tokens = T0                                    │
│       ② 若 T0 - snip_freed_tokens <= max * 0.6:                 │
│            → 直接返回                                           │
│       ③ 跑 L3 TimeDecayLayer.apply(messages, sidecar)：         │
│            释放 R3；T1 = T0 - R3                                │
│            cb(f"时间衰减释放 {R3} tokens")                       │
│       ④ 若 T1 <= max - BUFFER(13k):                             │
│            → 返回（L5 不触发，省一次 LLM 调用）                 │
│       ⑤ 否则跑 L5 AutoCompactLayer.apply(force=False):          │
│            split = find_last_user_index(messages)               │
│            compressible = messages[1 : split]                   │
│            active_turn = messages[split : ]                     │
│            校验 compressible 内工具对完整性,                    │
│              不完整则把尾部破坏对的消息推回 active_turn        │
│            把 compressible 整段送 LLM,                          │
│              用 9 段中文结构化模板生成 <summary>               │
│            new_messages = [system, archive, *active_turn]       │
│            释放 R5；cb(f"结构化全量摘要释放 {R5} tokens")        │
│            若 LLM 失败 3 次 → 熔断,                              │
│              降级为 [system, *active_turn]（信息丢失但保命）   │
│       ⑥ 返回 messages                                           │
│                                                                 │
│   force_compact(messages, ...):  ← reactive 路径                │
│       直接调 L5 apply(force=True),                               │
│       绕过阈值与熔断（替代旧 _force_compact 改阈值 hack）       │
│                                                                 │
│   snip(messages, indices):  ← L2 主动减负                       │
│       L2 SnipLayer.apply：                                      │
│         a) validate_pairs：删除后是否破坏工具对？              │
│         b) 否：物理删除 + sidecar 清理 + 累加 snip_freed        │
│         c) 是：拒绝并返回错误                                    │
│       释放的 tokens 累加到 snip_freed,                           │
│       下次 compress_if_overflow ② 步阈值判断会扣减它            │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ 出口路径（engine.call_llm_async 内 SDK 调用前）                 │
│                                                                 │
│   project_for_llm(safe_messages, thinking_mode):                │
│       L4 ContextCollapse.project：                              │
│         a) deepcopy 一层（不写回权威历史）                      │
│         b) 每条消息 pop("_meta_id")                             │
│         c) 非 thinking_mode：pop("reasoning_content")           │
│         d) 合并连续 system 锚点                                  │
│         e) assistant.tool_calls=[] 时 pop                        │
│       返回投影副本 → 真正发给 OpenAI SDK                        │
└─────────────────────────────────────────────────────────────────┘
```

**关键级联性质**：
- L1 让 L3 处理的早期 tool 消息已经是「存根」，L3 替换占位符的 token 释放更纯粹
- L1+L3 让 L5 真要摘要时，被压缩段已无大块原始日志，**LLM 摘要质量更高、提示更短、成本更低**
- L4 是出口净化，每次都跑，**不与前 3 层互斥**
- L2 释放的 tokens 通过 sidecar `snip_freed_tokens` 影响 L5 阈值
- L5 熔断后降级到「只保留 system + 活动轮」，会丢信息但保证不死循环

## 文件结构（7 个）

```
src/harness_lite/context/compact/
├── __init__.py             # 导出 CompactPipeline + 主要类型
├── types.py                # MessageMeta / ToolResultRef / LayerStats / TokenCounter
├── prompts.py              # AUTO_COMPACT_PROMPT_ZH（9 段结构化模板）+ parse_summary_block
├── storage.py              # LargeResultStore + L1 DiskOffloadLayer
├── local_layers.py         # L2 SnipLayer + L3 TimeDecayLayer + COMPACTABLE_TOOLS 集合
├── auto_compact.py         # L5 AutoCompactLayer + 熔断状态 + 活动轮切分 + 摘要解析
└── pipeline.py             # CompactPipeline 编排 + anchors.find_safe_cut_points/validate_pairs + L4 ContextCollapse
```

`src/harness_lite/context/manager.py` 重写为薄封装（保留类名 + 方法签名）。
`src/harness_lite/loop/compaction.py` **删除**。

## 关键常量（直接对齐参考）

```python
# storage.py
LARGE_RESULT_THRESHOLD_BYTES = 50_000     # 50KB 落盘门槛
LARGE_RESULT_PREVIEW_CHARS = 800          # 存根中保留的预览长度

# local_layers.py
COMPACTABLE_TOOLS = {                     # 与 microCompact.ts:41 对齐 + 项目工具
    "read_file", "list_directory", "grep_search", "create_file", "edit_file",
    "bash_terminal", "python_interpreter",
    "intelligence_search", "web_scraper",
    "read_skill",
}
KEEP_RECENT_TOOL_RESULTS = 5              # 与 timeBasedMCConfig.ts:33 对齐
GAP_THRESHOLD_MINUTES = 60                # 与 timeBasedMCConfig.ts:32 对齐
TIME_DECAY_PROACTIVE_RATIO = 0.6          # token 已超 60% 即提前衰减（项目特有）

# auto_compact.py
AUTOCOMPACT_BUFFER_TOKENS = 13_000        # 与 autoCompact.ts:62 对齐
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3  # 与 autoCompact.ts:70 对齐
# 不设固定 TAIL_KEEP，由「最后一条 role=user 起的活动轮」动态决定
# system 锚点固定保留 1 条（messages[0]）
```

## L1/L2/L3/L4 算法骨架（v1 内容保留，未修改）

### L1 `storage.py` 核心

```python
class LargeResultStore:
    def write(self, session_id, ref_id, content) -> Path: ...
    def read(self, session_id, ref_id) -> Optional[str]: ...
    def cleanup_session(self, session_id) -> None: ...

class DiskOffloadLayer:
    def maybe_offload(self, message: dict, session_id: str) -> dict:
        if message.get("role") != "tool": return message
        content = message.get("content", "")
        if len(content.encode("utf-8")) < LARGE_RESULT_THRESHOLD_BYTES:
            return message
        ref_id = sha256((message["tool_call_id"] + sha256(content)).encode())[:16]
        self.store.write(session_id, ref_id, content)
        preview = content[:LARGE_RESULT_PREVIEW_CHARS]
        message["content"] = (
            f"[⚠️ 大结果已自动归档] tool_call_id={message['tool_call_id']} "
            f"ref_id={ref_id} byte_size={len(content.encode('utf-8'))}\n"
            f"─── 原始输出前 {LARGE_RESULT_PREVIEW_CHARS} 字预览 ───\n"
            f"{preview}\n"
            f"─── 已截断，剩余字符落盘至 large_results/{session_id}/{ref_id}.txt ───"
        )
        return message
```

### L2 `local_layers.py::SnipLayer`

```python
class SnipLayer:
    def apply(self, messages, indices, sidecar) -> CompactionResult:
        target = sorted(set(indices), reverse=True)
        sim = [m for i, m in enumerate(messages) if i not in target]
        if not validate_pairs(sim):
            return CompactionResult(success=False, reason="会破坏工具对完整性")
        freed = sum(token_count(messages[i]) for i in target)
        for i in target:
            mid = messages[i].get("_meta_id")
            if mid: sidecar.pop(mid, None)
        return CompactionResult(messages_after=sim, saved_tokens=freed, layer="L2")
```

### L3 `local_layers.py::TimeDecayLayer`

```python
class TimeDecayLayer:
    def should_trigger(self, messages, sidecar, current_tokens, max_allowed) -> bool:
        if current_tokens >= max_allowed * TIME_DECAY_PROACTIVE_RATIO:
            return True
        latest_ts = max(
            (sidecar[m["_meta_id"]].last_seen_at for m in messages
             if m.get("_meta_id") and m["_meta_id"] in sidecar),
            default=None,
        )
        if latest_ts and (now() - latest_ts).total_seconds() / 60 > GAP_THRESHOLD_MINUTES:
            return True
        return False

    def apply(self, messages, sidecar) -> CompactionResult:
        tool_ids = collect_compactable_tool_ids(messages)
        keep_set = set(tool_ids[-KEEP_RECENT_TOOL_RESULTS:])

        new_messages = []
        freed = 0
        for m in messages:
            if m.get("role") == "tool" and m.get("content", "").startswith("[⏳ 早期"):
                new_messages.append(m); continue
            if m.get("role") == "tool" and m.get("tool_call_id") not in keep_set:
                old_tokens = token_count(m)
                tool_name = lookup_tool_name(messages, m["tool_call_id"])
                m = dict(m, content=f"[⏳ 早期工具输出已自动清理 | tool={tool_name} | tool_call_id={m['tool_call_id']}]")
                freed += old_tokens - token_count(m)
            elif m.get("role") == "assistant" and "reasoning_content" in m:
                if m.get("tool_calls") and all(tc.get("id") not in keep_set for tc in m["tool_calls"]):
                    old_tokens = token_count(m)
                    m = {k: v for k, v in m.items() if k != "reasoning_content"}
                    freed += old_tokens - token_count(m)
            new_messages.append(m)

        if not validate_pairs(new_messages):
            return CompactionResult(success=False, reason="L3 后工具对失效，回滚")
        return CompactionResult(messages_after=new_messages, saved_tokens=freed, layer="L3")
```

### L4 `pipeline.py::ContextCollapse`

```python
class ContextCollapse:
    def project(self, messages, *, thinking_mode: bool) -> List[dict]:
        out = []
        for m in messages:
            mm = {k: v for k, v in m.items() if k != "_meta_id"}
            if not thinking_mode and "reasoning_content" in mm:
                del mm["reasoning_content"]
            if mm.get("role") == "assistant" and not mm.get("tool_calls"):
                mm.pop("tool_calls", None)
            if out and out[-1].get("role") == "system" and mm.get("role") == "system":
                out[-1]["content"] = out[-1].get("content","") + "\n\n" + mm.get("content","")
                continue
            out.append(mm)
        return out
```

### `pipeline.py::CompactPipeline`

```python
class CompactPipeline:
    def __init__(self, max_allowed_tokens=64000):
        self.max_allowed_tokens = max_allowed_tokens
        self.token_counter = TokenCounter()
        self._sidecar: dict[str, MessageMeta] = {}
        self._snip_freed_tokens = 0
        self.l1 = DiskOffloadLayer(LargeResultStore())
        self.l2 = SnipLayer()
        self.l3 = TimeDecayLayer()
        self.l4 = ContextCollapse()
        self.l5 = AutoCompactLayer()

    def record_tool_result(self, message, session_id):
        message = dict(message)
        meta_id = uuid4().hex
        message["_meta_id"] = meta_id
        self._sidecar[meta_id] = MessageMeta(created_at=now(), last_seen_at=now())
        return self.l1.maybe_offload(message, session_id)

    async def compress_if_overflow(self, messages, *, engine, current_cwd, status_callback=None):
        T0 = self.token_counter.count_messages(messages)
        if (T0 - self._snip_freed_tokens) <= self.max_allowed_tokens * 0.6:
            return messages

        if self.l3.should_trigger(messages, self._sidecar, T0, self.max_allowed_tokens):
            r3 = self.l3.apply(messages, self._sidecar)
            if r3.success:
                messages = r3.messages_after
                if status_callback: status_callback(f"[🧹 时间衰减] 释放 {r3.saved_tokens} tokens")
        T1 = self.token_counter.count_messages(messages)

        if self.l5.should_apply(T1, self.max_allowed_tokens, self._snip_freed_tokens):
            r5 = await self.l5.apply(messages, engine=engine, current_cwd=current_cwd, status_callback=status_callback)
            if r5.messages_after is not None:
                messages = r5.messages_after
                if status_callback: status_callback(f"[📦 结构化全量摘要] 释放 {r5.saved_tokens} tokens")
        return messages

    async def force_compact(self, messages, *, engine, current_cwd, status_callback=None):
        r5 = await self.l5.apply(messages, engine=engine, current_cwd=current_cwd, status_callback=status_callback, force=True)
        return r5.messages_after if r5.messages_after else messages

    def snip(self, messages, indices):
        r = self.l2.apply(messages, indices, self._sidecar)
        if r.success: self._snip_freed_tokens += r.saved_tokens
        return r

    def project_for_llm(self, messages, *, thinking_mode):
        return self.l4.project(messages, thinking_mode=thinking_mode)

    def calculate_messages_tokens(self, messages):
        return self.token_counter.count_messages(messages)

    def reset_session(self, reason: str):
        self._sidecar.clear()
        self._snip_freed_tokens = 0
        self.l5._consecutive_failures = 0
```

## 调用点改造

### `src/harness_lite/context/manager.py` 重写
保留 `DynamicContextManager` 类名，内部委派给 `CompactPipeline`。`compress_if_overflow` 与 `calculate_messages_tokens` 接口签名 100% 不变。

### `src/harness_lite/loop/strategy.py`
- `_force_compact`：用 `self.context_manager.pipeline.force_compact(...)` 替代「改阈值」hack
- `_stage_3_tool_orchestration` tool 消息 append 前：`message = self.context_manager.pipeline.record_tool_result(message, session_id)`

### `src/harness_lite/loop/engine.py`
- `call_llm_async` 在 `safe_messages = sanitize_surrogates(...)` 之后：
  ```python
  pipeline = self.strategy.context_manager.pipeline
  safe_messages = pipeline.project_for_llm(safe_messages, thinking_mode=config.get("thinking_mode", False))
  ```

### `src/harness_lite/memory/manager.py`
- `_fire_invalidation` 触发时让 pipeline 也清 sidecar：在 `engine.__init__` 末尾注册 `register_invalidation_callback(lambda r: pipeline.reset_session(r))`

### `src/harness_lite/loop/compaction.py` —— 删除

### `src/harness_lite/prompt/sections/system_rules.py:19`
描述更新为「分层上下文管理管线（5 层渐进减负：大结果落盘 / snip / 时间衰减 / 读时投影 / 结构化全量摘要）」。

## 关键不变量

1. **OpenAI 工具对完整性**：每层 apply 后 `validate_pairs(messages)`，失败回滚；L5 切分时通过 `_enforce_pair_integrity` 把破坏对的消息推回活动轮
2. **L4 不写回**：投影返回新列表，权威 messages 永不污染
3. **`_meta_id` 单字段**：仅 1 个 uuid 字符串挂在消息 dict 上，L4 出口剥离
4. **熔断 + 降级双保险**：L5 连续 3 次 LLM 失败后降级为「保 system + 活动轮」，会丢信息但保证不死循环
5. **结构化模板保信息**：L5 的 9 段中文模板**强制要求**列出所有 user 消息、所有文件代码段、所有错误与修复。这是替代「物理保留 N 条」的核心机制
6. **幂等**：L3 检测占位符前缀；同层重复 apply 不破坏数据
7. **sidecar 生命周期**：随 `MemoryManager.clear_context` 失效，且 `LargeResultStore.cleanup_session` 同步清磁盘

## 实现与审查分工

- **写代码** —— `harness-coder` 子代理，分两批：
  - **批次 A（无 LLM 依赖）**：`types.py` / `prompts.py` / `storage.py` / `local_layers.py`
  - **批次 B（含 LLM + 集成）**：`auto_compact.py` / `pipeline.py`（含 anchors + L4） / `__init__.py` / 重写 `context/manager.py` / 改写 `loop/strategy.py` 与 `loop/engine.py` 调用点 / 删除 `loop/compaction.py` / 微调 `prompt/sections/system_rules.py` / 在 engine.__init__ 注册 sidecar 失效回调

- **审查** —— `harness-code-reviewer` 子代理，每批完成后审一次：
  - 批次 A 重点：L1 落盘路径安全（防 path traversal）、L3 工具对完整性、token 计数精度、幂等性
  - 批次 B 重点：L4 投影不写回、L5 切分点正确性、9 段中文 prompt 无歧义、`<summary>` 解析鲁棒（含解析失败的兜底）、熔断逻辑、调用点改造无破坏

## 验证

### 单元测试 `tests/test_compact_pipeline.py`

- L1：构造 60KB tool 结果 → maybe_offload 后 content 变存根 + 文件存在
- L2：snip 中间 tool 索引 → 拒绝；safe 索引 → 通过 + sidecar 清理 + snip_freed 累计
- L3：8 条 tool 消息 → 保留最近 5；早期 tool content 变占位符；早期 assistant reasoning_content 被剥离
- L4：含 `_meta_id` + `reasoning_content` 的 messages → project 后字段消失；连续 system 合并；权威 messages 未变
- **L5（v2 重点）**：
  - 构造 `[system, user1, asst1, tool1, asst2, user2, asst3]` → split=5（user2 索引），compressible=`[user1, asst1, tool1, asst2]`，active_turn=`[user2, asst3]`
  - mock LLM 返回标准 `<analysis>...</analysis><summary>1.主要请求...9.后续步骤</summary>` → 摘要解析正确，归档消息含 9 段
  - mock LLM 返回畸形输出（缺 `</summary>`）→ `parse_summary_block` 兜底返回原文，不崩
  - mock LLM 抛 3 次 → `_consecutive_failures=3`，第 4 次降级返回 `[system, *active_turn]`
  - `force=True` → 跳过阈值
  - 工具对完整性：构造 compressible 末尾是孤悬 `assistant.tool_calls`（对应 tool 在 active_turn 内）→ `_enforce_pair_integrity` 把它推回 active_turn 前
- anchors：构造孤儿 tool_call → `validate_pairs` 返回 False
- 级联：构造同时触发 L3 + L5 的 messages → 验证先 L3 后 L5、L3 释放后 L5 阈值检查使用新 token 数
- 幂等：连续 apply L3 两次 → 第二次 saved_tokens=0

### 集成测试

- `pytest tests/ -v` 全量通过
- `harness-lite "用 grep 在整个项目搜所有 def 定义并总结"` → `memory_store/large_results/{session_id}/` 有文件生成
- 长会话：人为构造 ~80k tokens 的对话 → 观察 L3 先减负、L5 触发后归档消息含完整 9 段
- thinking_mode 下 reasoning_content 在历史中被 L3 剥离，但当前轮仍正常返回
- `/clear` 触发 `pipeline._sidecar` 与 `large_results/{session_id}/` 同步清空
- 验证 L5 归档后下一轮 LLM 调用：归档 system 消息可读、模型能根据 9 段摘要继续工作而不询问已知信息
