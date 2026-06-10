# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Harness-Lite 是一个轻量级大模型多智能体编排框架，支持异步工具调用、对话记忆、安全沙箱和基于技能的 SOP 规范。

## 常用命令

```bash
# 安装包（开发模式）
pip install -e .

# 运行 CLI
harness-lite "任务"                     # 单轮对话
harness-lite -i                        # 交互模式（自动生成会话）
harness-lite -i --session <id>         # 继续指定会话

# 运行全部测试
pytest tests/ -v

# 运行单个测试模块
pytest tests/test_security.py -v
```

## 架构

### 核心模块依赖链

```
Config → Memory → Registry → Security → Loop (Engine + Strategy) → CLI
                              ↓
                          Tools/Skills
```

### 执行流程 (ReAct 循环)

```
用户输入 → AsyncLoopEngine → ReActStrategy.execute()
    → DynamicContextManager.compress_if_overflow()  # Token 自适应压缩
    → call_llm_async() → [tool_calls?] → process_tool_calls_async()
    → _execute_tool() → security.intercept() → tool.execute()
    → memory.save_context() → 返回响应
```

### 核心组件

| 模块 | 路径 | 职责 |
|------|------|------|
| **Config** | `config/loader.py` | 从 `.env` 加载 LLM 配置（全局单例） |
| **Memory** | `memory/manager.py`, `memory/store.py` | 三层记忆 + mem0 向量长记忆 |
| **Context** | `context/manager.py` | Token 级自适应上下文压缩引擎 |
| **Registry** | `registry/base.py` | `Tool` 和 `Skill` 抽象基类 |
| **ToolRegistry** | `registry/tool_registry.py` | 插件注册表 |
| **SkillRegistry** | `registry/skill_registry.py` | 技能注册表 |
| **Security** | `security/manager.py` | 三层安全防御 + Session 沙箱隔离 + 多沙箱挂载 |
| **AsyncLoopEngine** | `loop/engine.py` | 异步 LLM 编排器 |
| **ReActStrategy** | `loop/strategy.py` | ReAct 循环策略 + 熔断机制 |
| **CLI** | `cli/app.py` | Typer + prompt_toolkit 交互式终端 |

## .env 配置

必须配置的环境变量（项目根目录 `.env`）：

| 变量 | 必需 | 说明 |
|------|------|------|
| `LLM_API_KEY` | 是 | LLM API 密钥 |
| `LLM_BASE_URL` | 是 | API 端点（兼容 OpenAI 格式即可） |
| `LLM_MODEL_NAME` | 是 | 模型标识符 |
| `LLM_THINKING_MODE` | 否 | `true`/`false`，开启思维链输出 |
| `WORKSPACE_ROOT` | 否 | 沙箱根路径，多路径用逗号分隔 |

## CLI 斜杠命令

交互模式下可用（`/` 触发自动补全）：

| 命令 | 功能 |
|------|------|
| `/model` | 查看 LLM 配置与思维链模式 |
| `/tool` | 列出已注册的原子工具 |
| `/skill` | 列出已加载的 SOP 技能 |
| `/mem0` | 开关 mem0 向量长记忆系统 |
| `/clear` | 清空当前会话短期记忆 |
| `/session` | 查看会话 ID 与沙箱信息 |
| `/sandbox [路径...]` | 动态挂载沙箱；无参数则列出当前；`reset` 重置为 .env 默认 |
| `/exit` | 退出 CLI |

## 三层安全防御体系

### Layer 1 - 确定性静态防御
- **AST 语法审计** (`PythonASTAuditor`)：拦截 `eval/exec/compile/__import__`，禁止 `subprocess` 模块，封锁 `system/popen/spawn*/execl*/fork/ctypes` 等危险属性
- **路径沙箱** (`_check_path_jail`)：所有文件操作重写为授权沙箱目录内，支持多沙箱路径挂载
- **危险 Shell 正则拦截**：匹配 `sudo`、`rm -rf`、`chmod 0777`、`curl|wget |bash` 等高危指令

### Layer 2 - LLM 语义审查
- 对 `bash_terminal` 和 `python_interpreter` 调用 OpenAI SDK 进行意图评分
- 评分 90-100：自动放行；60-89：转 Layer 3 人工终审；0-59：直接拦截

### Layer 3 - 人工终审
- 灰色地带操作打印详细审计信息，等待用户 Y/N 确认
- 用户拒绝时触发**自动记忆蒸馏**（`distill_and_record_correction`），将错误固化为长期 Markdown 记忆

## 三层记忆管理系统

### Tier 1 - 短期 JSON 上下文
- 存储路径：`memory_store/{YYYY-MM-DD}/{session-id}.json`
- 接口：`save_context()`、`load_context()`、`trim_history()`、`clear_context()`

### Tier 2 - 长期 Markdown 记忆
- `memory_store/global_preferences.md`：全局用户开发偏好
- `memory_store/persistent_memory/CLAUDE.md`：项目显式规范手册
- `memory_store/persistent_memory/auto_memory/MEMORY.md`：行为纠错经验库

### Tier 3 - 自动记忆蒸馏管道
- 当 Layer 3 人工拒绝操作时，调用 LLM 将错误提炼为单条行为准则
- 格式：`- [纠错] 在处理XXXX时，严禁使用XXXX，必须通过XXXX来实现。`
- 持久化写入 `MEMORY.md`，跨会话共享

### mem0 向量长记忆（可选）
- 通过 `/mem0` 命令启用，默认关闭
- 每次对话结束时对回答内容进行向量化和存储
- 每次用户提问时从数据库检索最相关的 5 条数据注入 system prompt
- 需要配置 embedding 模型 API，使用 Chroma 数据库（仅支持语义检索）

## 12 个内置工具

| 工具 | 文件 | 功能 |
|------|------|------|
| `calculator` | `tools/calculator.py` | 基于 AST 的安全数学计算 |
| `current_time` | `tools/current_time.py` | 格式化日期时间 |
| `list_directory` | `tools/file_ops.py` | 目录树形结构（带深度限制） |
| `read_file` | `tools/file_ops.py` | 按行号范围读取文件 |
| `create_file` | `tools/file_ops.py` | 安全创建文件（不覆盖已有文件） |
| `edit_file` | `tools/file_ops.py` | 基于行号区间的替换 |
| `grep_search` | `tools/file_ops.py` | 递归内容搜索 |
| `bash_terminal` | `tools/execution_ops.py` | 会话独占型持久化 Shell |
| `python_interpreter` | `tools/execution_ops.py` | Session 沙箱内 Python 执行 |
| `intelligence_search` | `tools/web_ops.py` | Tavily API 网络搜索 |
| `web_scraper` | `tools/web_ops.py` | BeautifulSoup HTML 抓取 |
| `read_skill` | `tools/skill_reader.py` | 读取 SKILL.md 内容 |

### 工具特殊机制

**IsolatedPersistentShell（持久化 Shell）**：
- 每个 Session 独占一个 bash 进程，cd 状态跨命令持久化
- 资源硬限制：RLIMIT_AS=1GB、RLIMIT_NPROC=50、RLIMIT_FSIZE=200MB
- 故障自愈：进程异常时自动强杀重建并重放 CWD 状态
- 沙箱逃逸检测：每次命令后通过 pwd 验证 CWD 是否越界

**SessionProcessManager**：
- 线程安全的多租户 Shell 进程池
- 根据 `session_id` 分配/复用专属 `IsolatedPersistentShell` 实例

**PythonInterpreterTool**：
- 临时文件在 Session 沙箱目录内执行
- 资源限制：RLIMIT_AS=512MB、RLIMIT_CPU=(timeout+1)秒
- 执行后自动清理临时文件

## 技能系统

### 自动注册机制
- `skills/__init__.py` 导入时自动调用 `_auto_register_skills()`
- 扫描 `skills/` 下所有包含 `SKILL.md` 的子目录
- 支持 YAML Frontmatter 解析（`name` + `description`），降级使用 Markdown 首标题和首段

### Skill 数据结构
```python
@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    content: str
    path: Optional[str] = None
```

## 上下文压缩引擎 (DynamicContextManager)

### Token 计算
- 使用 `tiktoken`（`cl100k_base`）精准计算消息 Token 消耗
- 对 tool_calls 包含函数名和 arguments 单独累加

### 压缩触发条件
- 默认阈值 7000 Token（可配置 `max_tokens_threshold`）
- 保留前 2 条锚点消息（system + 首个 user）

### 压缩算法
1. 收集最多 6 条连续的 tool 相关消息对（assistant tool_call + tool response）
2. 调用 LLM 将其提炼为 1-2 句话的历史摘要
3. 生成带 CWD 状态锚点的系统归档消息，替换原始日志
4. 释放的 Token 空间通过 status_callback 反馈

## ReAct 策略细节

- **最大步数**：默认 15 步
- **熔断机制**：连续 3 次工具执行异常（安全拦截/执行错误/工具不存在）则强制终止
- **单次输出截断**：超过 3500 字符的工具结果自动截断，防止上下文溢出
- **流式输出**：字符级打字机效果 + 状态滚动显示

## 重要实现说明

### 工具 Schema 格式
所有工具必须使用嵌套的 OpenAI function-calling 格式：
```python
{
    "type": "function",
    "function": {
        "name": "tool_name",
        "description": "...",
        "parameters": {"type": "object", "properties": {...}}
    }
}
```

### LLM 集成
- 使用兼容 OpenAI 格式的 API（通过 `.env` 配置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL_NAME`）
- 安全审计使用官方 `OpenAI` SDK（同步客户端，内置 3 次自动重试）
- API 调用通过 `AsOpenAI` 流式 SSE 响应
- 3 倍指数退避重试（网络错误）

### Session ID 传递
- 通过 `contextvars.ContextVar` 在工具执行时动态获取当前 Session ID
- `BashTerminalTool` 和 `PythonInterpreterTool` 使用此机制实现租户隔离

### 消息净化
- 流式响应中跳过第三方网关下发的空碎片（空对象）
- 递归清洗净化器在序列化前剔除非法 Surrogate 字符，防止编码崩溃
- `reasoning_content` 字段被忽略，只提取 `content` 作为回复

## 常见错误模式（见 .claude/ERRORS.md）

- Typer `Option()` 返回 `OptionInfo` - 需要手动解析 `sys.argv` 获取实际值
- 流式输出必须用 `sys.stdout.write()` + flush，禁止用 `print()`
- 工具调用中间响应不应展示给用户
- 非标准编码字符会污染历史导致序列化崩溃 - 需在入参前递归净化 Surrogate
